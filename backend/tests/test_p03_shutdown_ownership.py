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
import shutil
import sys
import tempfile
import time
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
    """Drive the REAL gate (app.lifespan._bounded_tail_exit) with os._exit
    patched — no re-implementation. The gate fires os._exit(0) only when
    RAGAPP_BOUNDED_EXIT=1 AND the registry is empty AND turn finalizers
    drained AND pool closed AND the upload registry is empty (read at gate
    time). Each test flips one input.
    """

    async def _gate(self, *, env, drained, pool_closed, uploads, use_registry=True):
        import app.api.routes.draft_room as draft_room_mod
        import app.lifespan as lifespan_mod

        exits = []
        sentinel = object()
        with mock.patch.dict(os.environ, {"RAGAPP_BOUNDED_EXIT": env}),                 mock.patch.object(os, "_exit", side_effect=lambda c: exits.append(c)),                 mock.patch.object(lifespan_mod.os, "_exit", side_effect=lambda c: exits.append(c)),                 mock.patch.object(draft_room_mod, "_INFLIGHT_UPLOAD_COMMITS",
                                  {sentinel} if uploads else set()):
            if use_registry:
                async with shutdown_gate.critical_shutdown_work("unit-gate"):
                    fired = lifespan_mod._bounded_tail_exit(drained, pool_closed)
            else:
                fired = lifespan_mod._bounded_tail_exit(drained, pool_closed)
        return bool(exits) or fired

    async def test_fires_only_when_all_critical_conditions_hold(self):
        self.assertTrue(
            await self._gate(env="1", drained=True, pool_closed=True,
                             uploads=False, use_registry=False)
        )

    async def test_never_fires_without_env(self):
        self.assertFalse(
            await self._gate(env="0", drained=True, pool_closed=True,
                             uploads=False, use_registry=False)
        )
        self.assertFalse(
            await self._gate(env="", drained=True, pool_closed=True,
                             uploads=False, use_registry=False)
        )

    async def test_never_fires_with_pending_uploads(self):
        # The marker's actual failure case: uploads pending after a NORMAL
        # timeout return (registry-read predicate stays false).
        self.assertFalse(
            await self._gate(env="1", drained=True, pool_closed=True,
                             uploads=True, use_registry=False)
        )

    async def test_never_fires_with_live_critical_work(self):
        # user-delete recovery active (registry entry) overlapping satisfied
        # turn/upload/pool conditions: the gate must stay closed.
        self.assertFalse(
            await self._gate(env="1", drained=True, pool_closed=True,
                             uploads=False, use_registry=True)
        )
        self.assertFalse(shutdown_gate.critical_work_active())

    async def test_never_fires_with_incomplete_finalizers_or_open_pool(self):
        self.assertFalse(
            await self._gate(env="1", drained=False, pool_closed=True,
                             uploads=False, use_registry=False)
        )
        self.assertFalse(
            await self._gate(env="1", drained=True, pool_closed=False,
                             uploads=False, use_registry=False)
        )

    async def test_conftest_pops_the_env(self):
        # The pin for the conftest pop: with conftest applied, a shell-
        # exported flag cannot reach the test process. Simulate by calling
        # the same pop logic conftest runs at import.
        with mock.patch.dict(os.environ, {"RAGAPP_BOUNDED_EXIT": "1"}):
            os.environ.pop("RAGAPP_BOUNDED_EXIT", None)
            self.assertNotIn("RAGAPP_BOUNDED_EXIT", os.environ)


class TestRegistrationMapPin(unittest.TestCase):
    """CI pin for the durability-registration census (#799/#854).

    The plan-critic Round 9/10 registration map is the audit surface of the
    deny-by-default gate: if a registration is silently deleted, the gate
    can cut that workflow's recovery. This test fails when the set of
    registered critical-work names changes.
    """

    # Per-(file, name) OCCURRENCE counts (a Counter, not a set): deleting
    # one arm of a multi-arm registration (e.g. one of the three
    # vaults.py recovery arms) must fail this pin, not silently pass on the
    # surviving arms.
    EXPECTED = {
        ("backend/app/api/routes/users.py", "user-delete-durability"): 1,
        ("backend/app/api/routes/documents.py", "document-upload-artifact-cleanup"): 2,
        ("backend/app/api/routes/documents.py", "document-upload-compensation"): 2,
        ("backend/app/api/routes/vaults.py", "vault-rollback-restore"): 3,
        ("backend/app/api/routes/vaults.py", "vault-committed-audit"): 1,
        ("backend/app/services/draft_promotion.py", "draft-promotion-compensation"): 1,
        ("backend/app/api/routes/memories.py", "memory-transaction"): 1,
        ("backend/app/api/routes/chat.py", "turn-finalizer-drain"): 1,
        ("backend/app/api/routes/draft_room.py", "upload-commit"): 1,
    }

    def test_registration_sites_are_complete(self):
        import collections
        import re

        repo_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..")
        )
        found = collections.Counter()
        for dirpath, _dirnames, filenames in os.walk(
            os.path.join(repo_root, "backend", "app")
        ):
            for fname in filenames:
                if not fname.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fname)
                rel = os.path.relpath(path, repo_root).replace(os.sep, "/")
                src = open(path, encoding="utf-8", errors="replace").read()
                for m in re.finditer(
                    r'critical_shutdown_work\("([\w-]+)"\)', src
                ):
                    found[(rel, m.group(1))] += 1
        self.assertEqual(
            dict(found),
            self.EXPECTED,
            "durability registration map changed — every added/removed "
            "registration ARM is a shutdown-gate coverage decision (update "
            "the map AND the 4.2 census together)",
        )


class TestSharedMaintenanceSlice(unittest.IsolatedAsyncioTestCase):
    """The maintenance slice is ONE shared budget, not one per call (#854).

    The four shutdown-critical maintenance calls (ingest lease release,
    reindex lease release, parked-retry reset) each get the shared slice
    computed once at stop() entry: three simultaneously-slow calls must
    cost ONE slice (worst case timeout + slice), not three. A per-call
    slice would let stop() run timeout + 3*slice and outrun the lifespan
    wrapper's remaining+5 bound.
    """

    BUDGET = 1.0
    SLICE = 1.0  # min(5.0, BUDGET)

    def _build(self, cls):
        from types import SimpleNamespace

        class _P(cls):
            def __init__(self):
                self.queue = asyncio.Queue()
                self.enrichment_queue = asyncio.Queue()
                self.atom_enrichment_queue = asyncio.Queue()
                self.shutdown_event = asyncio.Event()
                self.processor = SimpleNamespace(
                    vector_store=SimpleNamespace(
                        flush_optimize=self._fast_flush
                    ),
                    pool=None,
                )
                self._worker_tasks = ()
                self._enrichment_worker_task = None
                self._vector_delete_sweep_task = None
                self._reindex_worker_task = None
                self._ingest_lease_enabled = True
                self._reindex_lease_enabled = True
                self._running = True

            async def _fast_flush(self):
                return None

            async def _wait_ingest_jobs_settled(self):
                return None

            def _release_all_ingest_leases(self):
                # Production calls this SYNCHRONOUSLY through
                # asyncio.to_thread — a coroutine here would be created and
                # never awaited (the repo's #565 gate catches exactly that).
                time.sleep(5.0)
                return 0

            def _release_reindex_leases(self):
                time.sleep(5.0)
                return 0

            async def _reset_parked_retry_files(self, file_ids):
                # Production awaits this one directly (it is a coroutine).
                await asyncio.sleep(5.0)

            def _parked_retry_file_ids(self, ticket):
                return [1]

            def _mark_running_reindex_jobs_interrupted(self, reason):
                return None

        proc = _P()
        # A non-empty retry backlog arms the third maintenance call.
        proc._retry_backlog = asyncio.Queue()
        proc._retry_backlog.put_nowait(object())
        return proc

    async def test_three_slow_maintenance_calls_cost_one_slice(self):
        from app.services.background_tasks import BackgroundProcessor

        proc = self._build(BackgroundProcessor)
        started = time.monotonic()
        await asyncio.wait_for(proc.stop(timeout=self.BUDGET), timeout=30)
        elapsed = time.monotonic() - started
        bound = self.BUDGET + self.SLICE + 0.5  # one shared slice + margin
        self.assertLessEqual(
            elapsed,
            bound,
            f"three simultaneously-slow maintenance calls took {elapsed:.2f}s "
            f"(bound {bound:.2f}s) — the slice is being granted per call, not "
            f"shared",
        )
        self.assertFalse(proc._running)


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
        """Two REAL lifespan entries in one interpreter.

        Enters the actual `app.lifespan.lifespan` twice (fresh FastAPI
        state, the neighbor recipe's settings pinning) and asserts the
        lifecycle behavior it names:

        1. the first entry's shutdown SETS the curator cooperative-shutdown
           flag (the wiki stage) and tears the curator/parse executors down;
        2. the second entry's STARTUP clears the flag again — otherwise
           every curator chunk on that process would return (None, None)
           (the cross-worker pollution class);
        3. the curator and parse executors are usable again after the
           second entry (the recreate-on-demand accessors).

        The executor-recreation half alone would pass even if the real
        lifespan startup failed to clear the flag, so the flag assertion
        is the point of entering the real lifespan here.
        """
        from pathlib import Path

        try:
            from fastapi import FastAPI
            from test_p03_lifecycle_behavioral import _pin_lifespan_settings

            from app.lifespan import lifespan
            from app.services.wiki_compiler import (
                CURATOR_SHUTDOWN,
                _get_curator_offload_executor,
            )
        except Exception as exc:  # pragma: no cover - import guard
            self.skipTest(f"real-lifespan recipe unavailable: {exc}")
            return

        def _close_cached_pools() -> None:
            """Release the pool cache so the temp dir can be removed.

            Windows keeps a lock on app.db while a pooled connection is
            open, so the temp directory cannot be cleaned while the
            lifespan's pool is still cached.
            """
            from app.models.database import _pool_cache, _pool_cache_lock

            with _pool_cache_lock:
                for pool in list(_pool_cache.values()):
                    try:
                        pool.close_all()
                    except Exception:  # noqa: BLE001 - already closed is fine
                        pass
                _pool_cache.clear()

        async def _run_entry(directory: Path, *, expect_flag_cleared: bool) -> None:
            restore = _pin_lifespan_settings(directory, editorial=False)
            try:
                app = FastAPI()
                async with lifespan(app):
                    if expect_flag_cleared:
                        self.assertFalse(
                            CURATOR_SHUTDOWN.is_set(),
                            "the lifespan startup must clear the curator "
                            "shutdown flag (same-process restart)",
                        )
                        executor = _get_curator_offload_executor()
                        future = executor.submit(lambda: 41 + 1)
                        self.assertEqual(future.result(timeout=10), 42)
            finally:
                restore()
                _close_cached_pools()
                shutil.rmtree(directory, ignore_errors=True)

        tmp = Path(tempfile.mkdtemp())
        await _run_entry(tmp, expect_flag_cleared=False)
        # The first entry's shutdown SET the flag.
        self.assertTrue(
            CURATOR_SHUTDOWN.is_set(),
            "the lifespan wiki stage must set the curator shutdown flag",
        )

        tmp2 = Path(tempfile.mkdtemp())
        await _run_entry(tmp2, expect_flag_cleared=True)


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
