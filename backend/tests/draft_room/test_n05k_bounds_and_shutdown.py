"""Review follow-up pins for PR #1103 bounds and shutdown (unfrozen).

* PRR-004 — the upload compensation is bounded by
  ``_UPLOAD_COMPENSATE_TIMEOUT_SECONDS``: a wedged rollback step returns
  at the deadline and logs the timeout WARNING (an inner shield would make
  the bound inert and this wait unbounded).
* PRR-021 — the startup orphan sweep honours ``_ORPHAN_SWEEP_BUDGET_SECONDS``:
  with the budget exhausted it stops before reaping and logs that the rest
  is deferred to the next restart.
* PRR-020 — ``drain_inflight_upload_commits`` waits for in-flight commit
  tasks, is bounded by its timeout (logging the abandonment), and lifespan
  shutdown calls it before the pool closes.
"""

import asyncio
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

import app.api.routes.draft_room as draft_room_module
import app.services.draft_input_storage as storage_module
from tests.draft_room.test_draft_input_storage import DraftInputStorageTestBase

LIVE_PAIR = (7, 3)


class TestUploadCompensationIsBounded(unittest.TestCase):
    def test_a_wedged_rollback_returns_at_the_deadline_and_logs(self):
        unblock = threading.Event()

        class _WedgedPath:
            def unlink(self, missing_ok=False):
                # Blocks the worker thread well past the patched bound.
                unblock.wait(timeout=10)

        storage = MagicMock()
        storage.resolve.return_value = _WedgedPath()

        async def _drive():
            started = time.monotonic()
            await draft_room_module._compensate_upload_enrollment_shielded(
                MagicMock(),
                storage,
                draft_id=1,
                owner_id=1,
                input_id=1,
                relpath="1/1/inputs/1/x.txt",
                conn=MagicMock(),
            )
            return time.monotonic() - started

        try:
            with (
                patch.object(
                    draft_room_module, "_UPLOAD_COMPENSATE_TIMEOUT_SECONDS", 0.3
                ),
                self.assertLogs("app.api.routes.draft_room", level="WARNING") as logs,
            ):
                elapsed = asyncio.run(_drive())
        finally:
            unblock.set()

        self.assertLess(
            elapsed,
            3.0,
            f"the compensation must return at its 0.3s bound, took {elapsed:.2f}s",
        )
        self.assertTrue(
            any("rollback timed out" in r.getMessage() for r in logs.records),
            [r.getMessage() for r in logs.records],
        )


class TestOrphanSweepHonoursItsBudget(DraftInputStorageTestBase):
    def _write(self, relpath: str) -> str:
        path = self.root.joinpath(*relpath.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"row-less")
        return relpath

    def test_the_budget_fits_inside_the_startup_recovery_window(self):
        # lifespan bounds draft startup recovery at 10s; the sweep budget
        # must leave room for the rest of reconcile (review PRR-021).
        self.assertGreater(storage_module._ORPHAN_SWEEP_BUDGET_SECONDS, 0)
        self.assertLess(storage_module._ORPHAN_SWEEP_BUDGET_SECONDS, 10)

    def test_an_exhausted_budget_defers_reaping_and_logs(self):
        orphan = self._write("7/3/inputs/11/orphan.txt")
        keep = self._write("7/3/inputs/10/keep.txt")
        with (
            patch.object(storage_module, "_ORPHAN_SWEEP_BUDGET_SECONDS", -1.0),
            self.assertLogs(storage_module.logger.name, level="WARNING") as logs,
        ):
            counts = self.storage.reconcile(
                {LIVE_PAIR}, max_age_seconds=3600, valid_input_relpaths={keep}
            )
        self.assertEqual(counts.get("orphan_files_removed", 0), 0, counts)
        self.assertTrue(self.storage.exists(orphan))
        self.assertTrue(
            any("budget" in r.getMessage() for r in logs.records),
            [r.getMessage() for r in logs.records],
        )

    def test_within_budget_the_orphan_is_still_reaped(self):
        orphan = self._write("7/3/inputs/11/orphan.txt")
        keep = self._write("7/3/inputs/10/keep.txt")
        counts = self.storage.reconcile(
            {LIVE_PAIR}, max_age_seconds=3600, valid_input_relpaths={keep}
        )
        self.assertEqual(counts.get("orphan_files_removed", 0), 1, counts)
        self.assertFalse(self.storage.exists(orphan))


class TestShutdownDrainsInflightCommits(unittest.TestCase):
    def tearDown(self):
        draft_room_module._INFLIGHT_UPLOAD_COMMITS.clear()

    def test_drain_waits_for_a_settling_commit(self):
        async def _drive():
            task = asyncio.create_task(asyncio.sleep(0.2))
            draft_room_module._INFLIGHT_UPLOAD_COMMITS.add(task)
            await draft_room_module.drain_inflight_upload_commits(timeout=5.0)
            return task.done()

        self.assertTrue(asyncio.run(_drive()), "drain must wait for the commit")

    def test_drain_is_bounded_and_logs_abandoned_commits(self):
        async def _drive():
            stuck = asyncio.create_task(asyncio.sleep(30))
            draft_room_module._INFLIGHT_UPLOAD_COMMITS.add(stuck)
            started = time.monotonic()
            await draft_room_module.drain_inflight_upload_commits(timeout=0.2)
            elapsed = time.monotonic() - started
            stuck.cancel()
            return elapsed

        with self.assertLogs("app.api.routes.draft_room", level="ERROR") as logs:
            elapsed = asyncio.run(_drive())
        self.assertLess(elapsed, 3.0)
        self.assertTrue(
            any("still in flight at shutdown" in r.getMessage() for r in logs.records)
        )

    def test_lifespan_drains_before_closing_the_pool(self):
        source = (
            Path(__file__).resolve().parents[2] / "app" / "lifespan.py"
        ).read_text(encoding="utf-8")
        drain_at = source.find("await drain_inflight_upload_commits(")
        close_at = source.find("app.state.db_pool.close_all()")
        self.assertGreater(drain_at, 0, "lifespan must call the shutdown drain")
        self.assertGreater(close_at, 0)
        self.assertLess(
            drain_at,
            close_at,
            "the drain must run before the pool closes (review PRR-020)",
        )
