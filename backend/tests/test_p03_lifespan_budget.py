"""Lifespan-level aggregate shutdown budget tests (#799 AC12 / #854).

The BackgroundProcessor.stop() budget is pinned by the frozen C12 family;
this file pins the LIFESPAN half — the sequence of shutdown stages that
runs AFTER stop() — under ONE aggregate deadline:

- Test A: stalling stages share one budget (the first staller consumes it;
  the later ones are SKIPPED, not each granted a fresh full budget).
- Test B: the residency-task settle is bounded even when a residency task
  swallows its cancellation.
"""

import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

BUDGET_SECONDS = 1.0
STALL_SECONDS = 3.0


class _Stall:
    """A shutdown stage double that blocks for STALL_SECONDS."""

    def __init__(self, name, log):
        self.name = name
        self.log = log

    async def _stall(self):
        self.log.append(self.name)
        await asyncio.sleep(STALL_SECONDS)

    async def stop(self):
        await self._stall()

    async def stop_polling(self):
        await self._stall()


class _SwallowsCancellation:
    """A residency client whose prime ignores cancellation (M7's shape).

    ``started`` is an Event the test waits on: a residency task cancelled
    before its first step never exercises the settle's bound, so the test
    must not assert until the prime is actually running.
    """

    def __init__(self):
        self.started = asyncio.Event()

    async def prime_residency(self):
        self.started.set()
        # Swallow ONE cancellation and keep running: the settle must be
        # bounded by its timeout wrapper, not by rt.cancel() alone.
        # (Measured: the shipped settle completes at ~6.2 s against a 1 s
        # budget + 5 s slack; a mutant that drops the wrapper takes ~8.7 s.
        # The bound below sits between the two.)
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await asyncio.sleep(3600)

    async def close(self):
        return None


class TestLifespanAggregateBudget(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_stages_share_one_budget(self):
        """Two or more stalling stages must cost ONE budget, not one each.

        Without this test a per-stage fresh budget (the #854 defect one
        level up) passes every other check in the tree.
        """
        from fastapi import FastAPI
        from test_p03_lifecycle_behavioral import _pin_lifespan_settings

        import app.lifespan as lifespan_mod

        entered: list[str] = []
        timings: dict[str, float] = {}

        async def scenario():
            app = FastAPI()
            async with lifespan_mod.lifespan(app):
                app.state.email_service = _Stall("email", entered)
                app.state.file_watcher = _Stall("watcher", entered)
                app.state.wiki_compile_processor = _Stall("wiki", entered)
                timings["start"] = time.monotonic()

        tmp = Path(tempfile.mkdtemp())
        restore = _pin_lifespan_settings(tmp, editorial=False)
        try:
            with patch.object(
                lifespan_mod, "_SHUTDOWN_BUDGET_SECONDS", BUDGET_SECONDS
            ):
                await scenario()
        finally:
            restore()

        elapsed = time.monotonic() - timings["start"]
        self.assertLessEqual(
            elapsed,
            BUDGET_SECONDS + 1.0,
            f"the shutdown stages took {elapsed:.2f}s against a "
            f"{BUDGET_SECONDS}s aggregate budget — each stage is being "
            f"granted a fresh full budget instead of sharing one",
        )
        self.assertEqual(
            entered,
            ["email"],
            "only the first stage may run: the later ones must be SKIPPED "
            "once the aggregate budget is exhausted",
        )

    async def test_residency_settle_is_bounded(self):
        """A residency task that swallows cancellation must not hang the
        shutdown: the settle runs under the remaining budget plus its
        documented slack.
        """
        from fastapi import FastAPI
        from test_p03_lifecycle_behavioral import _pin_lifespan_settings

        import app.lifespan as lifespan_mod

        timings: dict[str, float] = {}

        fake = _SwallowsCancellation()

        async def scenario():
            app = FastAPI()
            async with lifespan_mod.lifespan(app):
                # Wait until the residency prime is actually RUNNING (a task
                # cancelled before its first step never exercises the
                # settle's bound).
                await asyncio.wait_for(fake.started.wait(), timeout=10)
                timings["start"] = time.monotonic()

        tmp = Path(tempfile.mkdtemp())
        restore = _pin_lifespan_settings(tmp, editorial=True)
        try:
            with patch.object(
                lifespan_mod, "_SHUTDOWN_BUDGET_SECONDS", BUDGET_SECONDS
            ), patch(
                "app.services.llm_client.create_editorial_client",
                return_value=fake,
            ):
                await asyncio.wait_for(scenario(), timeout=BUDGET_SECONDS + 12)
        finally:
            restore()

        elapsed = time.monotonic() - timings["start"]
        self.assertLessEqual(
            elapsed,
            BUDGET_SECONDS + 7.0,
            f"the residency settle ran {elapsed:.2f}s — it must be bounded "
            f"by the remaining budget plus its documented 5 s slack",
        )
