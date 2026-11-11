"""Upload ``commit_pending`` 503 contract (review F-02, PR #1103).

When the detached upload commit (finalize -> enqueue) does not settle within
``_UPLOAD_COMMIT_DRAIN_SECONDS`` on the UNCANCELLED path, the upload route
answers 503 with error code ``commit_pending`` (documented in
``docs/draft-room.md`` and ``specs/draft-room/SPEC.md``) and the detached task
keeps sole ownership of the outcome: once the wedged step returns, the upload
still settles COMMITTED (row + parse job) with no client retry.

Drives the REAL upload route through ``httpx.ASGITransport`` on a private
loop, parking ``DraftInputStorage.finalize`` on threading events (event-driven,
no polling sleeps), with the drain bound patched small.
"""

import asyncio
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

from unittest.mock import patch

import httpx

import app.api.routes.draft_room as draft_room_module
from app.main import app
from tests.draft_room.test_draft_compile_routes import CompileRouteTestBase
from tests.draft_room.test_n05f_detached_upload_commit import (
    _drain_pending_tasks,
    _parking_finalize_patch,
    _request,
)


class TestN05ICommitPending(CompileRouteTestBase):
    def _count(self, sql, draft_id):
        conn = self._connection_pool.get_connection()
        try:
            return conn.execute(sql, (draft_id,)).fetchone()[0]
        finally:
            self._connection_pool.release_connection(conn)

    def test_wedged_commit_answers_503_commit_pending_and_still_commits(self):
        draft_id = self._create_draft(title="N05I commit pending").json()["id"]
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        request = _request(draft_id, dict(self._owner_headers()))
        outcome: dict = {}

        async def _drive():
            with (
                patch.object(draft_room_module, "_UPLOAD_COMMIT_DRAIN_SECONDS", 0.5),
                _parking_finalize_patch(entered, release, finished),
            ):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://testserver"
                ) as client:
                    task = asyncio.create_task(client.post(**request))
                    loop = asyncio.get_running_loop()
                    try:
                        if not await loop.run_in_executor(None, entered.wait, 30):
                            raise AssertionError("finalize was never entered")
                        # No cancellation: the request must time out on its
                        # own while finalize is still parked. Bounded so a
                        # regression fails fast instead of hanging.
                        response = await asyncio.wait_for(task, timeout=15)
                        outcome["status"] = response.status_code
                        outcome["body"] = response.json()
                        outcome["finalize_still_parked"] = not finished.is_set()
                    finally:
                        release.set()
                    await loop.run_in_executor(None, finished.wait, 60)
                    await _drain_pending_tasks()

        asyncio.run(_drive())

        self.assertTrue(
            outcome["finalize_still_parked"],
            "the 503 must be returned while the commit is still wedged",
        )
        self.assertEqual(outcome["status"], 503, outcome["body"])
        self.assertEqual(outcome["body"].get("code"), "commit_pending", outcome["body"])
        # The detached task owned the outcome and committed it.
        self.assertEqual(
            self._count(
                "SELECT COUNT(*) FROM draft_inputs WHERE draft_id = ?", draft_id
            ),
            1,
            "the detached commit must settle the reserved input row",
        )
        self.assertEqual(
            self._count(
                "SELECT COUNT(*) FROM draft_jobs WHERE draft_id = ? "
                "AND job_type = 'parse_input'",
                draft_id,
            ),
            1,
            "the detached commit must enqueue the parse job",
        )
        self.assertFalse(
            draft_room_module._INFLIGHT_UPLOAD_COMMITS,
            "the settled commit task must leave the in-flight set",
        )
