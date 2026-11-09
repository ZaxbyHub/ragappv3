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
import contextlib
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
    def _run_cancel_at_finalize(self, *, second_cancel_at_discard):
        """Park finalize, cancel the COMMIT TASK (loop-shutdown shape, not
        the request task); optionally park the cleanup's staged discard and
        deliver a SECOND native cancel there. Returns (draft_id, outcome,
        log records)."""
        from unittest.mock import patch

        from app.services.draft_input_storage import DraftInputStorage

        draft_id = self._create_draft(title="N05J commit-task cancel").json()["id"]
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        discard_entered = threading.Event()
        discard_release = threading.Event()
        real_discard = DraftInputStorage.discard

        def _parked_discard(storage_self, staged):
            discard_entered.set()
            if not discard_release.wait(timeout=60):
                raise AssertionError("the parked discard was never released")
            return real_discard(storage_self, staged)

        discard_patch = (
            patch.object(DraftInputStorage, "discard", _parked_discard)
            if second_cancel_at_discard
            else contextlib.nullcontext()
        )
        request = _request(draft_id, dict(self._owner_headers()))
        outcome: dict = {}

        async def _drive():
            with _parking_finalize_patch(entered, release, finished), discard_patch:
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
                        for commit_task in inflight:
                            commit_task.cancel()
                        if second_cancel_at_discard:
                            if not await loop.run_in_executor(
                                None, discard_entered.wait, 30
                            ):
                                raise AssertionError("the discard was never entered")
                            for commit_task in inflight:
                                commit_task.cancel()
                            discard_release.set()
                        outcome.update(
                            await asyncio.wait_for(_settle_request_task(task), 30)
                        )
                    finally:
                        # Parked threads are released only after the cleanup
                        # ran; the staged file is already gone by then.
                        discard_release.set()
                        release.set()
                    await _drain_pending_tasks()

        with self.assertLogs("app.api.routes.draft_room", level="WARNING") as logs:
            asyncio.run(_drive())
        return draft_id, outcome, logs.records

    def _assert_nothing_left(self, draft_id, outcome):
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

    def test_cancelling_the_commit_task_at_finalize_leaves_nothing_behind(self):
        draft_id, outcome, records = self._run_cancel_at_finalize(
            second_cancel_at_discard=False
        )
        self._assert_nothing_left(draft_id, outcome)
        cancelled_records = [
            r
            for r in records
            if r.levelno == logging.WARNING
            and "upload commit task cancelled" in r.getMessage()
        ]
        self.assertEqual(
            len(cancelled_records),
            1,
            "BOT-02: exactly one operator record for the cancelled commit, got "
            f"{[r.getMessage() for r in records]}",
        )
        message = cancelled_records[0].getMessage()
        self.assertIn(f"draft_id={draft_id}", message)
        self.assertIn("input_id=", message)

    def test_a_second_cancel_at_the_cleanup_discard_still_rolls_the_row_back(self):
        """Review r2: the anyio shield does not absorb a native cancel, so a
        second ``task.cancel()`` landing on the staged discard must still let
        the enrollment compensation run before propagating."""
        draft_id, outcome, _records = self._run_cancel_at_finalize(
            second_cancel_at_discard=True
        )
        self._assert_nothing_left(draft_id, outcome)


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
