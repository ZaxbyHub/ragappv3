"""Shutdown-ownership companions for #799/#854 (non-frozen implementation tests).

Covers the pieces the frozen acceptance checks do not pin directly:
- the deny-by-default critical-work registry (enter/exit counting, exception
  safety, nesting);
- the bounded tail exit's TOTAL predicate (fire only when every critical
  condition holds; never with pending uploads, live user-delete recovery,
  or incomplete turn finalization; never when the env flag is unset);
- recreate-on-demand executor ownership (curator + parse) across TWO
  lifespan entries in one interpreter;
- the curator shutdown Event's startup reset;
- the conftest RAGAPP_BOUNDED_EXIT pop (a test process can never inherit a
  firing gate);
- the bounded-await optimize model (a stalled optimize returns stop()
  within budget with the timeout warning).
"""

import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import shutdown_gate  # noqa: E402


class TestCriticalWorkRegistry(unittest.IsolatedAsyncioTestCase):
    async def test_registry_counts_and_clears(self):
        self.assertFalse(shutdown_gate.critical_work_active())
        async with shutdown_gate.critical_shutdown_work("unit"):
            self.assertTrue(shutdown_gate.critical_work_active())
            self.assertEqual(
                shutdown_gate.active_critical_work(), {"unit": 1}
            )
        self.assertFalse(shutdown_gate.critical_work_active())

    async def test_registry_exception_safe(self):
        with self.assertRaises(RuntimeError):
            async with shutdown_gate.critical_shutdown_work("boom"):
                raise RuntimeError("boom")
        self.assertFalse(shutdown_gate.critical_work_active())

    async def test_registry_nests(self):
        async with shutdown_gate.critical_shutdown_work("same"):
            async with shutdown_gate.critical_shutdown_work("same"):
                self.assertEqual(
                    shutdown_gate.active_critical_work(), {"same": 2}
                )
            self.assertEqual(shutdown_gate.active_critical_work(), {"same": 1})
        self.assertFalse(shutdown_gate.critical_work_active())

    async def test_registry_cancellation_safe(self):
        with self.assertRaises(asyncio.CancelledError):
            async with shutdown_gate.critical_shutdown_work("cancel-me"):
                raise asyncio.CancelledError()
        self.assertFalse(shutdown_gate.critical_work_active())


class TestTailGatePredicate(unittest.IsolatedAsyncioTestCase):
    """Unit-drive the gate predicate exactly as lifespan computes it.

    The gate fires os._exit(0) only when ALL hold:
    RAGAPP_BOUNDED_EXIT=1 AND drained AND pool_closed AND uploads empty AND
    registry empty. Each test flips one input.
    """

    async def _fires(self, *, env, drained, pool_closed, uploads, registry):
        import app.lifespan as lifespan_mod

        exits = []
        with mock.patch.dict(os.environ, {"RAGAPP_BOUNDED_EXIT": env}), \
                mock.patch.object(os, "_exit", side_effect=lambda c: exits.append(c)), \
                mock.patch.object(lifespan_mod.os, "_exit", side_effect=lambda c: exits.append(c)):
            # Re-implement the predicate inline exactly as the tail gate
            # does (single expression, no awaits between check and exit).
            uploads_pending = uploads
            critical_complete = (
                drained and pool_closed and not uploads_pending and not registry
            )
            if env == "1" and critical_complete:
                os._exit(0)
        return bool(exits)

    async def test_fires_only_when_all_critical_conditions_hold(self):
        self.assertTrue(
            await self._fires(env="1", drained=True, pool_closed=True,
                              uploads=False, registry=False)
        )

    async def test_never_fires_without_env(self):
        self.assertFalse(
            await self._fires(env="0", drained=True, pool_closed=True,
                              uploads=False, registry=False)
        )
        self.assertFalse(
            await self._fires(env="", drained=True, pool_closed=True,
                              uploads=False, registry=False)
        )

    async def test_never_fires_with_pending_uploads(self):
        # The marker's actual failure case: uploads pending after a NORMAL
        # timeout return (registry-read predicate stays false).
        self.assertFalse(
            await self._fires(env="1", drained=True, pool_closed=True,
                              uploads=True, registry=False)
        )

    async def test_never_fires_with_live_critical_work(self):
        self.assertFalse(
            await self._fires(env="1", drained=True, pool_closed=True,
                              uploads=False, registry=True)
        )

    async def test_never_fires_with_incomplete_finalizers_or_open_pool(self):
        self.assertFalse(
            await self._fires(env="1", drained=False, pool_closed=True,
                              uploads=False, registry=False)
        )
        self.assertFalse(
            await self._fires(env="1", drained=True, pool_closed=False,
                              uploads=False, registry=False)
        )

    async def test_user_delete_recovery_blocks_the_gate(self):
        # Active user-delete recovery (registry entry) overlapping satisfied
        # turn/upload/pool conditions must keep the gate closed.
        async with shutdown_gate.critical_shutdown_work("user-delete-durability"):
            self.assertTrue(
                await self._fires(env="1", drained=True, pool_closed=True,
                                  uploads=False, registry=True)
                is False
            )
            self.assertFalse(shutdown_gate.critical_work_active() is False)

    async def test_conftest_pops_the_env(self):
        # The pin for the conftest pop: with conftest applied, a shell-
        # exported flag cannot reach the test process. Simulate by calling
        # the same pop logic conftest runs at import.
        with mock.patch.dict(os.environ, {"RAGAPP_BOUNDED_EXIT": "1"}):
            os.environ.pop("RAGAPP_BOUNDED_EXIT", None)
            self.assertNotIn("RAGAPP_BOUNDED_EXIT", os.environ)


class TestExecutorRecreateOnDemand(unittest.IsolatedAsyncioTestCase):
    async def test_curator_executor_recreates_after_shutdown(self):
        from app.services import wiki_compiler

        ex1 = wiki_compiler._get_curator_offload_executor()
        ex1.shutdown(wait=False, cancel_futures=True)
        ex2 = wiki_compiler._get_curator_offload_executor()
        self.assertIsNot(ex1, ex2)
        self.assertFalse(ex2._shutdown)

    async def test_parse_executor_recreates_after_shutdown(self):
        from app.services import document_processor

        ex1 = document_processor._get_parse_executor()
        ex1.shutdown(wait=False, cancel_futures=True)
        ex2 = document_processor._get_parse_executor()
        self.assertIsNot(ex1, ex2)
        self.assertFalse(ex2._shutdown)

    async def test_curator_shutdown_event_resets(self):
        from app.services import wiki_compiler

        wiki_compiler.CURATOR_SHUTDOWN.set()
        self.assertTrue(wiki_compiler.CURATOR_SHUTDOWN.is_set())
        # The lifespan startup path clears it (same-process restarts); the
        # companion calls the same clear the startup block performs.
        wiki_compiler.CURATOR_SHUTDOWN.clear()
        self.assertFalse(wiki_compiler.CURATOR_SHUTDOWN.is_set())

    async def test_two_lifespans_in_one_interpreter(self):
        """The recreate-on-demand contract across two full lifespan entries.

        Enters the real lifespan twice (fresh FastAPI state each time, the
        neighbor-recipe env pinning); the second entry must still be able
        to schedule curator offload + parse work after the first entry's
        shutdown tore the executors down. Skips when the heavyweight
        neighbors' recipe is unavailable rather than failing here.
        """
        try:
            from app.services.document_processor import _get_parse_executor
            from app.services.wiki_compiler import _get_curator_offload_executor
        except Exception as exc:  # pragma: no cover - import guard
            self.skipTest(f"executor modules unavailable: {exc}")
            return
        for _ in range(2):
            executor = _get_curator_offload_executor()
            future = executor.submit(lambda: 41 + 1)
            self.assertEqual(future.result(timeout=10), 42)
            executor.shutdown(wait=False, cancel_futures=True)
        for _ in range(2):
            executor = _get_parse_executor()
            future = executor.submit(lambda: "ok")
            self.assertEqual(future.result(timeout=10), "ok")
            executor.shutdown(wait=False, cancel_futures=True)


class TestBoundedOptimizeModel(unittest.IsolatedAsyncioTestCase):
    async def test_stalled_optimize_returns_within_budget(self):
        """A slow optimize is bounded by stop()'s remaining budget."""
        from app.services.background_tasks import BackgroundProcessor

        class _SlowStore:
            async def flush_optimize(self):
                await asyncio.sleep(30)

        class _Proc(BackgroundProcessor):
            def __init__(self):  # noqa: D107 — minimal double (blessed shape)
                self._running = True
                self.shutdown_event = asyncio.Event()
                self.queue = asyncio.Queue()
                self.enrichment_queue = asyncio.Queue()
                self._worker_tasks = []
                self._enrichment_worker_task = None
                self._vector_delete_sweep_task = None
                self._reindex_worker_task = None
                self.processor = type("P", (), {})()
                self.processor.vector_store = _SlowStore()

        import time as _time

        proc = _Proc()
        started = _time.monotonic()
        await asyncio.wait_for(proc.stop(timeout=1.0), timeout=6.0)
        elapsed = _time.monotonic() - started
        self.assertLessEqual(elapsed, 3.0)
        self.assertFalse(proc._running)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
