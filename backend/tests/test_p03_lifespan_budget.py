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
# Guard on the shutdown phase for the residency test. A mutant that drops
# the settle's timeout wrapper hangs the shutdown; the guard ends it. The
# bound assertion below is what the shipped code satisfies. The guard is
# measured from the START of the shutdown phase, so a slow startup cannot
# shorten it.
SHUTDOWN_GUARD_SECONDS = 45.0
# A separate, generous bound on reaching the shutdown phase at all (a
# startup that never completes would otherwise hang the test).
STARTUP_GUARD_SECONDS = 120.0


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

    The prime swallows REPEATEDLY-cancelled requests (up to
    ``MAX_SWALLOWS``) and only then dies, so the test process can always
    tear the event loop down. Swallowing more than one is the point: a
    settle that awaits the task DIRECTLY forwards the enclosing scope's
    cancellation into it, so a task that ignores the FIRST cancellation
    but dies on the second would hide that hang.
    """

    MAX_SWALLOWS = 2

    def __init__(self):
        self.started = asyncio.Event()
        self.task: asyncio.Task | None = None

    async def prime_residency(self):
        self.task = asyncio.current_task()
        self.started.set()
        swallowed = 0
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                swallowed += 1
                if swallowed > self.MAX_SWALLOWS:
                    raise

    async def close(self):
        return None


async def _force_kill(task: asyncio.Task | None) -> None:
    """Make a cancellation-ignoring task die so the loop can tear down.

    The prime deliberately ignores cancellation, so the test has to end it
    explicitly; otherwise the event loop's own shutdown would wait for it.
    """
    if task is None or task.done():
        return
    for _ in range(_SwallowsCancellation.MAX_SWALLOWS + 3):
        if task.done():
            return
        task.cancel()
        # Give the task a step to observe the cancellation.
        await asyncio.sleep(0.02)



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
        task_still_running = None
        startup_deadline = time.monotonic() + STARTUP_GUARD_SECONDS
        try:
            with patch.object(
                lifespan_mod, "_SHUTDOWN_BUDGET_SECONDS", BUDGET_SECONDS
            ), patch(
                "app.services.llm_client.create_editorial_client",
                return_value=fake,
            ):
                # The guard covers the SHUTDOWN phase only: the startup
                # delay is measured separately, so a slow host cannot
                # shorten the window in which the mutant hangs.
                #
                # asyncio.wait (not wait_for) is the guard: wait_for
                # CANCELS the task it waits on and then waits for that
                # cancellation to land — and a settle that awaits the
                # residency task directly forwards the cancellation INTO
                # it, so wait_for would hang on the very defect it is
                # supposed to catch. asyncio.wait returns when its timeout
                # expires, whatever the task is doing.
                scenario_task = asyncio.create_task(scenario())
                # The guard covers the SHUTDOWN phase only: the scenario
                # records the moment the shutdown starts (the end of the
                # lifespan body), and the guard's deadline is measured
                # from THAT moment — so a slow startup cannot shorten the
                # window in which the mutant hangs.
                shutdown_deadline = None
                while not scenario_task.done():
                    now = time.monotonic()
                    if shutdown_deadline is None:
                        if "start" not in timings:
                            if now > startup_deadline:
                                self.fail(
                                    "the lifespan never reached the shutdown "
                                    f"phase within {STARTUP_GUARD_SECONDS:g}s"
                                )
                            await asyncio.wait({scenario_task}, timeout=0.2)
                            continue
                        shutdown_deadline = (
                            timings["start"]
                            + BUDGET_SECONDS
                            + SHUTDOWN_GUARD_SECONDS
                        )
                    remaining = shutdown_deadline - time.monotonic()
                    if remaining <= 0:
                        self.fail(
                            "the residency settle did not finish within "
                            f"{BUDGET_SECONDS + SHUTDOWN_GUARD_SECONDS:g}s of "
                            "the shutdown phase — it is waiting on the "
                            "residency task's cooperation instead of ending "
                            "on its own bound"
                        )
                    await asyncio.wait(
                        {scenario_task}, timeout=min(remaining, 0.2)
                    )
                scenario_task.result()
                # The prime ignores cancellation, so it is still running
                # when the shutdown returned: the settle ended on its
                # BOUND, not on the task's death.
                task_still_running = (
                    fake.task is not None and not fake.task.done()
                )
        finally:
            restore()
            # The prime ignores cancellation on purpose: end it so the
            # event loop can shut down (the settle's bound is what the
            # assertions below measure, not the prime's death).
            await _force_kill(fake.task)

        elapsed = time.monotonic() - timings["start"]
        self.assertLessEqual(
            elapsed,
            BUDGET_SECONDS + 7.0,
            f"the residency settle ran {elapsed:.2f}s — it must be bounded "
            f"by the remaining budget plus its documented 5 s slack",
        )
        self.assertTrue(
            task_still_running,
            "the residency task had already died when the shutdown returned "
            "— the settle must end on its own bound, not on the task's "
            "cooperation",
        )
