"""Review follow-up pins for PR #1103 (unfrozen; not in the trace manifest).

* BOT-01 / BOT-02 — cancelling the detached upload COMMIT TASK while it is
  parked at the finalize await must run the shielded cleanup (staged discard
  + enrollment compensation) before the cancellation propagates: no input
  row, no parse job, no finalized bytes, nothing left in
  ``_INFLIGHT_UPLOAD_COMMITS``, and exactly one WARNING operator record
  naming the draft and input ids.
* PRR-006 — on the retry route the invalid-state 409 takes precedence over
  the worker-gate 503 for BOTH branches: a non-terminal parse parent answers
  409 ``conflict`` (the code this branch answered at base) and a non-terminal
  compile parent answers 409 ``invalid_state``, even with no worker mounted.
"""

import asyncio
import logging
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

import httpx

import app.api.routes.draft_room as draft_room_module
from app.main import app
from tests.draft_room.test_draft_compile_routes import CompileRouteTestBase
from tests.draft_room.test_n05c_capability_liveness import set_app_state
from tests.draft_room.test_n05f_detached_upload_commit import (
    _drain_pending_tasks,
    _parking_finalize_patch,
    _request,
    _settle_request_task,
)


class _ReviewFollowupBase(CompileRouteTestBase):
    def _count(self, sql, *params):
        conn = self._connection_pool.get_connection()
        try:
            return conn.execute(sql, params).fetchone()[0]
        finally:
            self._connection_pool.release_connection(conn)

    def _final_files(self, draft_id):
        from pathlib import Path

        from app.config import settings

        inputs_dir = (
            Path(settings.data_dir)
            / "draft-room"
            / str(self.OWNER_ID)
            / str(draft_id)
            / "inputs"
        )
        if not inputs_dir.is_dir():
            return []
        return sorted(str(p) for p in inputs_dir.rglob("*") if p.is_file())


class TestCommitTaskCancellationAtFinalize(_ReviewFollowupBase):
    def test_cancelling_the_commit_task_at_finalize_leaves_nothing_behind(self):
        draft_id = self._create_draft(title="N05J commit-task cancel").json()["id"]
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        request = _request(draft_id, dict(self._owner_headers()))
        outcome: dict = {}

        async def _drive():
            with _parking_finalize_patch(entered, release, finished):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://testserver"
                ) as client:
                    task = asyncio.create_task(client.post(**request))
                    loop = asyncio.get_running_loop()
                    try:
                        if not await loop.run_in_executor(None, entered.wait, 30):
                            raise AssertionError("finalize was never entered")
                        inflight = list(draft_room_module._INFLIGHT_UPLOAD_COMMITS)
                        outcome["inflight_at_park"] = len(inflight)
                        # Cancel the COMMIT TASK itself (loop-shutdown shape),
                        # not the request task.
                        for commit_task in inflight:
                            commit_task.cancel()
                        outcome.update(await _settle_request_task(task))
                    finally:
                        # The parked finalize thread is released only after
                        # the cleanup ran; its staged file is already gone.
                        release.set()
                    await _drain_pending_tasks()

        with self.assertLogs("app.api.routes.draft_room", level="WARNING") as logs:
            asyncio.run(_drive())

        self.assertEqual(outcome["inflight_at_park"], 1, outcome)
        self.assertNotEqual(
            outcome.get("status"),
            202,
            f"a cancelled commit must never report success: {outcome}",
        )
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM draft_inputs WHERE draft_id = ?", draft_id),
            0,
            "the reserved input row must be compensated away",
        )
        self.assertEqual(
            self._count(
                "SELECT COUNT(*) FROM draft_jobs WHERE draft_id = ? "
                "AND job_type = 'parse_input'",
                draft_id,
            ),
            0,
            "no parse job may exist for a cancelled commit",
        )
        self.assertEqual(
            self._final_files(draft_id),
            [],
            "no staged or finalized bytes may remain under the inputs dir",
        )
        self.assertFalse(draft_room_module._INFLIGHT_UPLOAD_COMMITS)
        cancelled_records = [
            r
            for r in logs.records
            if r.levelno == logging.WARNING
            and "upload commit task cancelled" in r.getMessage()
        ]
        self.assertEqual(
            len(cancelled_records),
            1,
            "BOT-02: exactly one operator record for the cancelled commit, got "
            f"{[r.getMessage() for r in logs.records]}",
        )
        message = cancelled_records[0].getMessage()
        self.assertIn(f"draft_id={draft_id}", message)
        self.assertIn("input_id=", message)


class TestRetryInvalidStatePrecedesWorkerGate(_ReviewFollowupBase):
    def _retry(self, draft_id, job_id):
        return self.client.post(
            f"/api/draft-room/drafts/{draft_id}/jobs/{job_id}/retry",
            headers=self._owner_headers(),
        )

    def _job_rows(self, draft_id, job_type):
        return self._count(
            "SELECT COUNT(*) FROM draft_jobs WHERE draft_id = ? AND job_type = ?",
            draft_id,
            job_type,
        )

    def test_non_terminal_parse_parent_answers_409_conflict_without_a_worker(self):
        draft_id = self._draft_with_ready_input()
        parse_job_id = self._count(
            "SELECT id FROM draft_jobs WHERE draft_id = ? "
            "AND job_type = 'parse_input' ORDER BY id LIMIT 1",
            draft_id,
        )
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "UPDATE draft_jobs SET status = 'running' WHERE id = ?",
                (parse_job_id,),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)
        rows_before = self._job_rows(draft_id, "parse_input")

        restore = set_app_state("draft_job_processor", None)
        try:
            resp = self._retry(draft_id, parse_job_id)
        finally:
            restore()

        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json().get("code"), "conflict", resp.text)
        self.assertEqual(self._job_rows(draft_id, "parse_input"), rows_before)

    def test_non_terminal_compile_parent_answers_409_invalid_state_without_a_worker(
        self,
    ):
        draft_id = self._draft_with_ready_input()
        stub_restore = set_app_state("draft_job_processor", object())
        try:
            parent = self._compile(draft_id)
        finally:
            stub_restore()
        self.assertEqual(parent.status_code, 202, parent.text)
        parent_job_id = parent.json()["id"]
        rows_before = self._job_rows(draft_id, "compile")

        restore = set_app_state("draft_job_processor", None)
        try:
            resp = self._retry(draft_id, parent_job_id)
        finally:
            restore()

        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json().get("code"), "invalid_state", resp.text)
        self.assertEqual(self._job_rows(draft_id, "compile"), rows_before)
