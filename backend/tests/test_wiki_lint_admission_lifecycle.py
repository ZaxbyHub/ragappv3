"""Admission-lifecycle companion pins for the wiki lint run route
(issue #1117; complements the frozen test_wiki_lint_admission.py).

The frozen file pins the admission CONTRACT (bound read at request time,
excess rejected 503-family at admission time, within-bound bursts
succeed). These companions pin the RELEASE LIFECYCLE the plan critic's
round-2/round-3 review required:

- cancelling the await mid-scan holds the slot until the worker finishes
  (then a subsequent request admits);
- a scan that RAISES still releases its slot;
- a construction/submission failure before the done-callback registration
  rolls the admission back;
- a worker completing AFTER its loop closed releases via the worker-thread
  fallback (counter returns to 0; a fresh loop admits again);
- the default bound is 2 (the dual-vault frontend flow,
  wiki-jobs-ui-checks.test.tsx, must keep succeeding).
"""

import asyncio
import sqlite3
import time
import unittest
from unittest.mock import patch

import app.api.routes.wiki as wiki_route
from app.services.wiki_linter import WikiLinter

SCAN_SECONDS = 0.6


def _slow_run_lint(self, vault_id):
    time.sleep(SCAN_SECONDS)
    return []


def _failing_run_lint(self, vault_id):
    time.sleep(0.05)
    raise RuntimeError("scan exploded")


class R:
    vault_id = 1


async def _allow(user, res, rid, action):
    return True


def _drive(coro):
    return asyncio.run(coro)


class TestAdmissionLifecycle(unittest.TestCase):
    def setUp(self):
        # Reset the module counter between tests; each test drives its own
        # event loop, and prior tests' releases are marshalled onto loops
        # that no longer exist only when they complete late (the fallback
        # path covers that), so an explicit reset keeps cases independent.
        wiki_route._wiki_lint_in_flight = 0

    def _invoke(self):
        return wiki_route.run_wiki_lint(
            R(), db=sqlite3.connect(":memory:"), user={"id": 1},
            evaluate=_allow, _csrf_token="x",
        )

    def test_cancel_mid_scan_holds_slot_until_worker_finishes(self):
        WikiLinter.run_lint = _slow_run_lint

        async def scenario():
            first = asyncio.create_task(self._invoke())
            await asyncio.sleep(0.1)  # first scan now running on the worker
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            # The handler is gone but the worker still occupies the scan:
            # with the default bound 2 one more request admits, a third is
            # rejected while both slots are held.
            second = asyncio.create_task(self._invoke())
            with patch.object(
                wiki_route, "_WIKI_LINT_MAX_CONCURRENT_RUNS", 2
            ):
                third = asyncio.create_task(self._invoke())
                await asyncio.sleep(0.05)
                with self.assertRaises(Exception) as ctx:
                    await third
                self.assertEqual(
                    ctx.exception.status_code if hasattr(ctx.exception, "status_code") else -1, 503
                )
                await second
            # After the cancelled scan's worker ACTUALLY finishes, the slot
            # it held is released: a fresh request admits under bound 1.
            await asyncio.sleep(SCAN_SECONDS)
            with patch.object(
                wiki_route, "_WIKI_LINT_MAX_CONCURRENT_RUNS", 1
            ):
                await self._invoke()
                self.assertEqual(wiki_route._wiki_lint_in_flight, 0)

        _drive(scenario())

    def test_failing_scan_releases_slot(self):
        WikiLinter.run_lint = _failing_run_lint

        async def scenario():
            with self.assertRaises(RuntimeError):
                await self._invoke()
            await asyncio.sleep(0.1)  # done-callback marshals the release
            self.assertEqual(wiki_route._wiki_lint_in_flight, 0)
            # The slot is free again: a follow-up request admits and the
            # release settles back to zero.
            WikiLinter.run_lint = _slow_run_lint
            await self._invoke()
            await asyncio.sleep(0.1)
            self.assertEqual(wiki_route._wiki_lint_in_flight, 0)

        _drive(scenario())

    def test_setup_failure_rolls_admission_back(self):
        WikiLinter.run_lint = _slow_run_lint
        # WikiStore construction blows up BEFORE the executor submission:
        # no future owns the release, so the route must roll back itself.
        real_store = wiki_route.WikiStore

        def boom(_db):
            raise ValueError("no store for you")

        wiki_route.WikiStore = boom
        try:

            async def scenario():
                with self.assertRaises(ValueError):
                    await self._invoke()

            _drive(scenario())
        finally:
            wiki_route.WikiStore = real_store
        self.assertEqual(wiki_route._wiki_lint_in_flight, 0)

    def test_worker_completing_after_loop_close_releases(self):
        WikiLinter.run_lint = _slow_run_lint
        loop = asyncio.new_event_loop()

        async def scenario():
            return await self._invoke()

        result = loop.run_until_complete(scenario())
        self.assertIn("findings", result)
        # Handler finished; the slot released on completion.
        self.assertEqual(wiki_route._wiki_lint_in_flight, 0)

        # Now the raw fallback leg: dispatch a future on a loop, close the
        # loop BEFORE the worker finishes, and confirm the counter still
        # returns to 0 through the worker-thread fallback path.
        loop2 = asyncio.new_event_loop()

        async def dispatch_and_close():
            coro = self._invoke()
            task2 = asyncio.ensure_future(coro)
            await asyncio.sleep(0.05)  # scan started
            return task2

        pending = loop2.run_until_complete(dispatch_and_close())
        # Cancel nothing: just close the loop while the scan runs.
        loop2.close()
        time.sleep(SCAN_SECONDS + 0.3)  # worker finishes after the close
        self.assertEqual(
            wiki_route._wiki_lint_in_flight, 0,
            "closed-loop completion must release via the fallback",
        )
        # A fresh loop admits again (embedded/multi-cycle server).
        loop3 = asyncio.new_event_loop()
        loop3.run_until_complete(scenario())
        loop3.close()
        self.assertEqual(wiki_route._wiki_lint_in_flight, 0)

    def test_default_bound_is_two(self):
        # The dual-vault frontend flow (start lint A, switch vaults, start
        # lint B) must keep succeeding without a 503: the default admits
        # exactly two concurrent runs.
        self.assertEqual(wiki_route._WIKI_LINT_MAX_CONCURRENT_RUNS, 2)


if __name__ == "__main__":
    unittest.main()
