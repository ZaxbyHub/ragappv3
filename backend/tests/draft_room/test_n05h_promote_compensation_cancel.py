"""Reviewer-required pin (issue-trace 902-905-911-913 wave 7, review Rev 4):
``draft_promotion._compensate`` must survive a native cancellation delivered
while its FIRST step (``cancel_pending_jobs_async``) is pending.

Post-implementation addition (ships as a plain test file; no repro-check or
checkpoint rows): it pins behavior that landed in the review-fix commit and
asserts it at HEAD.

Contract: with a REAL promotion attempt committed (promotion row, files row
and destination bytes seeded by driving the real promote route), call
``_compensate`` directly with a MagicMock background processor whose async
cancel twin parks on a threading event; cancel the driving task while the
twin is pending; release; settle. EVERY remaining compensation step must
still have run — promotion row deleted, files row deleted, destination
bytes discarded — and the ``CancelledError`` must propagate out of
``_compensate`` (issue #913's class: cancellation must never skip cleanup,
and must never be swallowed either).
"""

import asyncio
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

from pathlib import Path
from unittest.mock import MagicMock

from app.services.draft_promotion import _compensate
from tests.draft_room.test_draft_promote import DraftPromoteTestBase


async def _drain_pending_tasks():
    for _ in range(100):
        pending = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        ]
        if not pending:
            return
        await asyncio.wait(pending, timeout=30)
    raise AssertionError("loop tasks never drained after the cancellation")


def _blocking_cancel_processor(entered, release):
    """A MagicMock background processor whose async cancel twin (the FIRST
    compensation step) parks on a threading event until released."""

    async def cancel_pending_jobs_async(**kwargs):
        entered.set()
        await asyncio.get_running_loop().run_in_executor(None, release.wait, 60)

    processor = MagicMock()
    processor.cancel_pending_jobs_async = cancel_pending_jobs_async
    return processor


class TestN05HPromoteCompensationCancel(DraftPromoteTestBase):
    def _promoted_state(self):
        """Drive the REAL promote route (the base's mock processor accepts
        the enqueue) so a genuine promotion row, files row and destination
        bytes exist to compensate."""
        draft_id = self._create_draft().json()["id"]
        upload = self._upload_input(
            draft_id, content=b"promote then cancel compensation"
        )
        input_id = upload.json()["input"]["id"]
        self._mark_input_ready(input_id)
        resp = self._promote(draft_id, source_type="input", source_id=input_id)
        self.assertEqual(resp.status_code, 202, resp.text)
        body = resp.json()
        files = self._files_rows()
        self.assertEqual(len(files), 1)
        return (
            draft_id,
            body["promotion_id"],
            body["file_id"],
            Path(files[0]["file_path"]),
        )

    def test_cancellation_during_first_step_runs_every_step_and_reraises(self):
        draft_id, promotion_id, file_id, dest_path = self._promoted_state()
        self.assertTrue(dest_path.is_file(), "the promoted bytes must exist first")

        entered = threading.Event()
        release = threading.Event()
        processor = _blocking_cancel_processor(entered, release)
        outcome = {}

        async def _drive():
            task = asyncio.create_task(
                _compensate(
                    self._connection_pool,
                    background_processor=processor,
                    promotion_id=promotion_id,
                    file_id=file_id,
                    dest_path=dest_path,
                    enqueued_file_id=file_id,
                )
            )
            try:
                loop = asyncio.get_running_loop()
                if not await loop.run_in_executor(None, entered.wait, 30):
                    release.set()
                    raise AssertionError(
                        "the compensation's first step (cancel twin) was never entered"
                    )
                # Native cancellation while the FIRST step is pending;
                # release the parked twin immediately after delivery.
                task.cancel()
                release.set()
                try:
                    await task
                    outcome["propagated"] = False
                except asyncio.CancelledError:
                    outcome["propagated"] = True
                await _drain_pending_tasks()
            finally:
                release.set()

        asyncio.run(_drive())

        self.assertTrue(
            outcome["propagated"],
            "asyncio.CancelledError must propagate out of _compensate after "
            "the mid-first-step cancellation",
        )
        self.assertEqual(
            len(self._promotion_rows(draft_id)),
            0,
            "every remaining compensation step must still run — the "
            "draft_promotions row must be deleted despite the cancellation",
        )
        self.assertEqual(
            len(self._files_rows()),
            0,
            "every remaining compensation step must still run — the files "
            "row must be deleted despite the cancellation",
        )
        self.assertFalse(
            dest_path.exists(),
            "every remaining compensation step must still run — the copied "
            "destination bytes must be discarded despite the cancellation",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
