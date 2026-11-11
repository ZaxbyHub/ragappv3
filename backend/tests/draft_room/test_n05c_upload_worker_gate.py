"""Arm's-length acceptance check C17 for the draft-capability-liveness trace
(issues #902/#905/#911/#913), upload route gating side.

Frozen specification. DISCRIMINATING: RED at base, GREEN after the fix.

* C17 -- POST /api/draft-room/drafts/{id}/inputs must answer HTTP 503 with
  error code ``worker_unavailable`` when no live worker can claim jobs, and
  the request must leave NOTHING behind: no reserved ``draft_inputs`` row
  and no bytes anywhere under the draft input storage root (neither
  finalized project files nor a stranded staged ``.incoming`` upload).
  Covered for the ``draft_job_processor is None`` shape and for the
  dead-real-processor shape. At base the route accepts the upload (202),
  reserves the row and finalizes the bytes — enqueueing a parse job nothing
  will ever claim.

Lives in its own file (not a new class in test_n05c_worker_unavailable_gate)
so C8's frozen file-level command keeps selecting only C8's compile/retry
gate tests.
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

from pathlib import Path
from unittest.mock import patch

from app.config import settings
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

UPLOAD_BYTES = b"bytes that must never be stored without a live worker"


class TestN05CUploadWorkerGate(CompileRouteTestBase):
    """C17 (DISCRIMINATING)."""

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

    def _upload(self, draft_id):
        return self.client.post(
            f"/api/draft-room/drafts/{draft_id}/inputs",
            data={"role": "manuscript", "authority": "primary"},
            files={"file": ("notes.txt", UPLOAD_BYTES, "text/plain")},
            headers=self._owner_headers(),
        )

    def _input_row_count(self, draft_id):
        conn = self._connection_pool.get_connection()
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM draft_inputs WHERE draft_id = ?", (draft_id,)
            ).fetchone()[0]
        finally:
            self._connection_pool.release_connection(conn)

    def _storage_files(self):
        root = Path(settings.data_dir) / "draft-room"
        if not root.is_dir():
            return []
        return sorted(str(p) for p in root.rglob("*") if p.is_file())

    def _assert_gated(self, resp, draft_id):
        self.assertEqual(
            resp.status_code,
            503,
            f"with no live worker the upload route must answer 503, got "
            f"{resp.status_code}: {resp.text}",
        )
        self.assertEqual(
            resp.json().get("code"),
            "worker_unavailable",
            f"the 503 must carry error code worker_unavailable, got {resp.text}",
        )
        self.assertEqual(
            self._input_row_count(draft_id),
            0,
            "a gated upload must reserve no draft_inputs row",
        )
        self.assertEqual(
            self._storage_files(),
            [],
            "a gated upload must leave no bytes under the draft input "
            "storage root (no finalized files, no stranded staged upload)",
        )

    def test_upload_without_a_worker_answers_503_and_stores_nothing(self):
        draft_id = self._create_draft(title="Upload gate None").json()["id"]
        restore = set_app_state("draft_job_processor", None)
        try:
            resp = self._upload(draft_id)
        finally:
            restore()
        self._assert_gated(resp, draft_id)

    def test_upload_with_a_dead_real_processor_answers_503(self):
        draft_id = self._create_draft(title="Upload gate dead").json()["id"]
        processor = build_real_processor(self)
        self._loop, self._thread = start_processor_on_worker_loop(processor)

        def _claim_then_die(inner_self):
            raise PollLoopDeath("simulated poll-loop crash inside the claim step")

        with patch.object(DraftJobProcessor, "_claim_next_job", _claim_then_die):

            async def _drive():
                await wait_until_task_done(processor._task)

            asyncio.run_coroutine_threadsafe(_drive(), self._loop).result(timeout=30)
        self.assertTrue(processor._task.done())

        self._restore_state = set_app_state("draft_job_processor", processor)
        resp = self._upload(draft_id)
        self._assert_gated(resp, draft_id)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
