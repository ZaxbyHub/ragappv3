"""Arm's-length acceptance check C8 for the draft-capability-liveness trace
(issues #902/#905/#911/#913), route gating side.

Frozen specification. DISCRIMINATING: RED at base, GREEN after the fix.

* C8 -- POST /compile and POST retry must each answer HTTP 503 with error
  code ``worker_unavailable`` and create NO new job row when no live worker
  can claim jobs. Covered shapes: ``draft_job_processor is None`` (compile,
  compile-retry and PARSE-job retry) and a REAL ``DraftJobProcessor``
  mounted on ``app.state`` whose poll-loop task terminated after a
  successful start (compile AND retry). At base both branches enqueue
  anyway (202 + a job row nothing will ever claim).
  Two guardrails pin the gate's placement (GREEN at base, must stay green):
  an idempotent REPLAY of a compile submitted while a worker was present
  still returns the ORIGINAL job while the worker is down (the gate must
  sit at enqueue, not in front of the idempotency replay); and a fresh 503
  leaves a Ready draft Ready (no ``ready_invalidated`` event, job rows
  unchanged). A third guardrail pins precedence: a non-owner still gets 404
  and a disabled Draft Room still gets its existing ``draft_room_disabled``
  answer — never ``worker_unavailable`` — when the worker is also down.

Setup note: the parent compile of the retry legs runs with a stub processor
mounted so the enqueue can succeed at head; the worker state is then set to
the shape under test before the gated request.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

from unittest.mock import patch

from app.services.draft_job_processor import DraftJobProcessor
from tests.draft_room.test_draft_compile_routes import CompileRouteTestBase
from tests.draft_room.test_n05c_capability_liveness import (
    PollLoopDeath,
    build_real_processor,
    set_app_state,
    start_processor_on_worker_loop,
    stop_worker_loop,
    wait_until_task_done,
)


class TestN05CWorkerUnavailableGate(CompileRouteTestBase):
    """C8 (DISCRIMINATING)."""

    def setUp(self):
        super().setUp()
        self._loop = None
        self._thread = None
        self._processor = None
        self._restore_state = None

    def tearDown(self):
        if self._loop is not None:
            stop_worker_loop(self._processor, self._loop, self._thread)
        if self._restore_state is not None:
            self._restore_state()
        super().tearDown()

    def _compile_job_rows(self, draft_id):
        conn = self._connection_pool.get_connection()
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM draft_jobs WHERE draft_id = ? "
                "AND job_type = 'compile'",
                (draft_id,),
            ).fetchone()[0]
        finally:
            self._connection_pool.release_connection(conn)

    def _parse_job_rows(self, draft_id):
        conn = self._connection_pool.get_connection()
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM draft_jobs WHERE draft_id = ? "
                "AND job_type = 'parse_input'",
                (draft_id,),
            ).fetchone()[0]
        finally:
            self._connection_pool.release_connection(conn)

    def _draft_status(self, draft_id):
        conn = self._connection_pool.get_connection()
        try:
            return conn.execute(
                "SELECT status FROM drafts WHERE id = ?", (draft_id,)
            ).fetchone()[0]
        finally:
            self._connection_pool.release_connection(conn)

    def _draft_events(self, draft_id, event_type):
        conn = self._connection_pool.get_connection()
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM draft_events WHERE draft_id = ? "
                "AND event_type = ?",
                (draft_id, event_type),
            ).fetchone()[0]
        finally:
            self._connection_pool.release_connection(conn)

    def _fail_parent_job(self, draft_id, job_id):
        """Drive a compile job to the terminal state retry requires."""
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "UPDATE draft_jobs SET status = 'failed', "
                "error_code = 'model_unavailable' WHERE id = ?",
                (job_id,),
            )
            conn.execute(
                "UPDATE drafts SET status = 'failed' WHERE id = ?", (draft_id,)
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def _retry(self, draft_id, job_id):
        return self.client.post(
            f"/api/draft-room/drafts/{draft_id}/jobs/{job_id}/retry",
            headers=self._owner_headers(),
        )

    def _assert_worker_unavailable(self, resp, draft_id, rows_before):
        self.assertEqual(
            resp.status_code,
            503,
            f"with no live worker the route must answer 503, got "
            f"{resp.status_code}: {resp.text}",
        )
        self.assertEqual(
            resp.json().get("code"),
            "worker_unavailable",
            f"the 503 must carry error code worker_unavailable, got {resp.text}",
        )
        self.assertEqual(
            self._compile_job_rows(draft_id),
            rows_before,
            "a 503 worker_unavailable response must leave no new job row behind",
        )

    # -- processor is None -------------------------------------------------

    def test_compile_without_a_worker_answers_503_and_enqueues_nothing(self):
        draft_id = self._draft_with_ready_input()
        restore = set_app_state("draft_job_processor", None)
        try:
            resp = self._compile(draft_id)
        finally:
            restore()
        self._assert_worker_unavailable(resp, draft_id, 0)

    def test_retry_without_a_worker_answers_503_and_enqueues_nothing(self):
        draft_id = self._draft_with_ready_input()
        stub_restore = set_app_state("draft_job_processor", object())
        try:
            parent = self._compile(draft_id)
        finally:
            stub_restore()
        self.assertEqual(parent.status_code, 202, parent.text)
        parent_job_id = parent.json()["id"]
        self._fail_parent_job(draft_id, parent_job_id)

        restore = set_app_state("draft_job_processor", None)
        try:
            resp = self._retry(draft_id, parent_job_id)
        finally:
            restore()
        self._assert_worker_unavailable(resp, draft_id, 1)

    # -- real processor whose poll loop terminated --------------------------

    def _kill_real_processor_loop(self):
        """Start a REAL DraftJobProcessor on a private loop and kill its
        poll loop with PollLoopDeath; returns the dead processor mounted on
        app.state (restore happens in tearDown)."""
        processor = build_real_processor(self)
        self._loop, self._thread = start_processor_on_worker_loop(processor)

        def _claim_then_die(inner_self):
            raise PollLoopDeath("simulated poll-loop crash inside the claim step")

        with patch.object(DraftJobProcessor, "_claim_next_job", _claim_then_die):

            async def _drive():
                await wait_until_task_done(processor._task)

            asyncio.run_coroutine_threadsafe(_drive(), self._loop).result(timeout=30)
        self.assertTrue(processor._task.done())
        self._processor = processor
        self._restore_state = set_app_state("draft_job_processor", processor)
        return processor

    def test_compile_with_a_dead_real_processor_answers_503(self):
        draft_id = self._draft_with_ready_input()
        self._kill_real_processor_loop()
        resp = self._compile(draft_id)
        self._assert_worker_unavailable(resp, draft_id, 0)

    def test_retry_with_a_dead_real_processor_answers_503(self):
        draft_id = self._draft_with_ready_input()
        stub_restore = set_app_state("draft_job_processor", object())
        try:
            parent = self._compile(draft_id)
        finally:
            stub_restore()
        self.assertEqual(parent.status_code, 202, parent.text)
        parent_job_id = parent.json()["id"]
        self._fail_parent_job(draft_id, parent_job_id)

        self._kill_real_processor_loop()
        resp = self._retry(draft_id, parent_job_id)
        self._assert_worker_unavailable(resp, draft_id, 1)

    # -- parse-job retry branch ---------------------------------------------

    def test_parse_job_retry_without_a_worker_answers_503_and_enqueues_nothing(self):
        """The retry route's non-compile branch (``retry_parse_job``) must be
        gated by the same worker check: a parse retry when no worker can
        claim it answers 503 ``worker_unavailable`` and creates no new parse
        job row. RED at base (the branch delegates to the store and answers
        202 with a child row)."""
        draft_id = self._draft_with_ready_input()
        # _draft_with_ready_input uploaded one input; its parse job is the
        # parent to retry. Drive it to the terminal state retry requires.
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "UPDATE draft_jobs SET status = 'failed', "
                "error_code = 'extraction_failed' WHERE draft_id = ? "
                "AND job_type = 'parse_input'",
                (draft_id,),
            )
            # retry_parse_job moves the input back to pending; from 'ready'
            # that transition is invalid, so park it in 'failed' first.
            conn.execute(
                "UPDATE draft_inputs SET parse_status = 'failed' WHERE draft_id = ?",
                (draft_id,),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)
        rows_before = self._parse_job_rows(draft_id)
        self.assertGreaterEqual(rows_before, 1)
        parse_job_id = self._first_parse_job_id(draft_id)

        restore = set_app_state("draft_job_processor", None)
        try:
            resp = self._retry(draft_id, parse_job_id)
        finally:
            restore()
        self.assertEqual(
            resp.status_code,
            503,
            f"with no live worker the route must answer 503, got "
            f"{resp.status_code}: {resp.text}",
        )
        self.assertEqual(
            resp.json().get("code"),
            "worker_unavailable",
            f"the 503 must carry error code worker_unavailable, got {resp.text}",
        )
        self.assertEqual(
            self._parse_job_rows(draft_id),
            rows_before,
            "a 503 worker_unavailable response must leave no new parse job row behind",
        )

    def _first_parse_job_id(self, draft_id):
        conn = self._connection_pool.get_connection()
        try:
            return conn.execute(
                "SELECT id FROM draft_jobs WHERE draft_id = ? "
                "AND job_type = 'parse_input' ORDER BY id LIMIT 1",
                (draft_id,),
            ).fetchone()[0]
        finally:
            self._connection_pool.release_connection(conn)

    # -- idempotent replay while the worker is down ---------------------------

    def test_idempotent_replay_while_worker_down_returns_the_original_job(self):
        """Guardrail (GREEN at base, must stay green after the fix): a
        compile submitted with an ``Idempotency-Key`` while a worker was
        present, then REPLAYED with the same key and identical body while
        the worker is gone, must return the ORIGINAL job (2xx, same id) and
        create no new row (SPEC 8.2) — the worker gate must sit at enqueue,
        not in front of the idempotency replay."""
        draft_id = self._draft_with_ready_input()
        stub_restore = set_app_state("draft_job_processor", object())
        try:
            first = self._compile(
                draft_id, idempotency_key="replay-key", start_stage="fact"
            )
        finally:
            stub_restore()
        self.assertEqual(first.status_code, 202, first.text)
        original_id = first.json()["id"]

        restore = set_app_state("draft_job_processor", None)
        try:
            replay = self._compile(
                draft_id, idempotency_key="replay-key", start_stage="fact"
            )
        finally:
            restore()
        self.assertEqual(
            replay.status_code,
            202,
            f"an idempotent replay must still return the original job while "
            f"the worker is down, got {replay.status_code}: {replay.text}",
        )
        self.assertEqual(
            replay.json()["id"],
            original_id,
            "an idempotent replay must return the ORIGINAL job id",
        )
        self.assertEqual(
            self._compile_job_rows(draft_id),
            1,
            "an idempotent replay must create no new compile job row",
        )

    # -- a fresh (non-replay) 503 must not disturb Ready state ----------------

    def test_fresh_503_preserves_ready_state_events_and_job_rows(self):
        """RED at base (the compile is accepted), GREEN after the fix: a
        fresh compile refused with 503 for a dead worker must leave the
        draft exactly as it was — a Ready draft stays Ready (no
        ``ready_invalidated`` event), and the job rows are unchanged."""
        draft_id, job_id, revision_id = self._seed_ready_eligible_draft(
            content_md="# Ready body\n\nReady to compile."
        )
        self._mark_ready(draft_id, revision_id)
        self.assertEqual(self._draft_status(draft_id), "ready")
        events_before = self._draft_events(draft_id, "ready_invalidated")
        compile_rows_before = self._compile_job_rows(draft_id)

        restore = set_app_state("draft_job_processor", None)
        try:
            # base_revision_id must match the Ready draft's current revision,
            # or compile answers 409 stale_base_revision before any gate.
            resp = self._compile(
                draft_id,
                idempotency_key="fresh-key-after-ready",
                base_revision_id=revision_id,
            )
        finally:
            restore()
        self.assertEqual(
            resp.status_code,
            503,
            f"with no live worker the route must answer 503, got "
            f"{resp.status_code}: {resp.text}",
        )
        self.assertEqual(resp.json().get("code"), "worker_unavailable", resp.text)
        self.assertEqual(
            self._draft_status(draft_id),
            "ready",
            "a Ready draft must stay Ready when compile is refused for no worker",
        )
        self.assertEqual(
            self._draft_events(draft_id, "ready_invalidated"),
            events_before,
            "refusing a compile for no worker must not emit ready_invalidated",
        )
        self.assertEqual(
            self._compile_job_rows(draft_id),
            compile_rows_before,
            "refusing a compile for no worker must leave the job rows unchanged",
        )

    # -- gate precedence -----------------------------------------------------

    def test_authorization_and_disabled_room_take_precedence_over_the_worker_gate(self):
        """Guardrail (GREEN at base, must stay green after the fix): the
        worker gate must not mask the route's own earlier gates — a non-owner
        still gets 404, and a disabled Draft Room still gets its existing
        disabled answer (503 ``draft_room_disabled``), not
        ``worker_unavailable``."""
        draft_id = self._draft_with_ready_input()

        restore = set_app_state("draft_job_processor", None)
        try:
            other = self.client.post(
                f"/api/draft-room/drafts/{draft_id}/compile",
                json={"base_revision_id": None, "lock_version": 1},
                headers=self._other_headers(),
            )
        finally:
            restore()
        self.assertEqual(
            other.status_code,
            404,
            f"a non-owner must still get 404 even with no worker, got "
            f"{other.status_code}: {other.text}",
        )

        from app.config import settings

        # setUp set draft_room_enabled=True and tearDown restores the
        # original value, so a plain assignment here is cleanup-safe.
        settings.draft_room_enabled = False
        restore = set_app_state("draft_job_processor", None)
        try:
            disabled = self._compile(draft_id)
        finally:
            restore()
        self.assertEqual(
            disabled.status_code,
            503,
            f"the disabled room must keep its existing disabled answer, got "
            f"{disabled.status_code}: {disabled.text}",
        )
        self.assertEqual(
            disabled.json().get("code"),
            "draft_room_disabled",
            f"the disabled room must answer draft_room_disabled (not "
            f"worker_unavailable) even with no worker, got {disabled.text}",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
