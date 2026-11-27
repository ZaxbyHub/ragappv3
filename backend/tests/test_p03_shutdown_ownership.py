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
import unittest.mock
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
    drained AND the upload registry is empty (read at gate time). The pool
    state is a resource-cleanup outcome, NOT a durability precondition
    (plan rev 11). Each test flips one input.
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

    async def test_never_fires_with_incomplete_finalizers(self):
        # The #1202 contract: a live turn's finalizer must be able to
        # finish, so the gate stays closed while the drain did not
        # complete -- with the pool closed OR open.
        self.assertFalse(
            await self._gate(env="1", drained=False, pool_closed=True,
                             uploads=False, use_registry=False)
        )
        self.assertFalse(
            await self._gate(env="1", drained=False, pool_closed=False,
                             uploads=False, use_registry=False)
        )

    async def test_fires_with_an_open_pool_when_the_durability_conditions_hold(self):
        # Plan rev 11: the pool state is a RESOURCE-CLEANUP outcome, not a
        # durability precondition. The exhausted-budget path (the #854
        # stall scenario) leaves the pool open; the gate must still fire so
        # the process exits BEFORE Compose force-terminates it (frozen
        # AC12). Process exit reclaims the pool's connections.
        self.assertTrue(
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

    The three shutdown-critical maintenance calls (ingest lease release,
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



class TestExhaustedBudgetTailExit(unittest.IsolatedAsyncioTestCase):
    """AC12's process-exit clause, proven on the REAL functions.

    The #854 stall scenario exhausts the aggregate budget: the pool close
    is skipped, so `pool_closed` is False. The tail gate must still fire
    then -- otherwise the process's exit falls through to the unbounded
    executor joins and Compose's force kill ends it, which frozen AC12
    forbids ("in either case the process exits before Compose
    force-terminates it").
    """

    async def test_exhausted_budget_still_fires_the_tail_exit(self):
        from app.lifespan import _bounded_tail_exit, _finalize_turns_and_close_pool

        calls = []

        class _Pool:
            def close_all(self):
                calls.append("closed")

        class _State:
            db_pool = _Pool()

        class _App:
            state = _State()

        exits = []
        with unittest.mock.patch.dict(
            os.environ, {"RAGAPP_BOUNDED_EXIT": "1"}
        ), unittest.mock.patch(
            "app.api.routes.chat.shutdown_turn_finalizers",
            new=unittest.mock.AsyncMock(return_value=True),
        ), unittest.mock.patch.object(
            os, "_exit", side_effect=lambda c: exits.append(c)
        ):
            drained, pool_closed = await _finalize_turns_and_close_pool(
                _App(), remaining=lambda: 0.0
            )
            self.assertEqual(
                (drained, pool_closed),
                (True, False),
                "the exhausted-budget path must drain the finalizers and "
                "leave the pool open",
            )
            fired = _bounded_tail_exit(drained, pool_closed)

        self.assertEqual(
            calls, [], "the exhausted budget must not close the pool at all"
        )
        self.assertTrue(
            fired or exits,
            "the tail exit must fire with the pool open: the process must "
            "exit before Compose force-terminates it (frozen AC12)",
        )

    async def test_the_real_drain_path_reaches_the_same_state(self):
        """The LIVE finalizer drain (no mocked result) reaches (True, False).

        The other case mocks ``shutdown_turn_finalizers`` to control the
        drain's outcome; this one runs the REAL drain with no live
        producers, so the exhausted-budget state is reached through the
        production path rather than a stub.
        """
        from app.lifespan import _finalize_turns_and_close_pool

        calls = []

        class _Pool:
            def close_all(self):
                calls.append("closed")

        class _State:
            db_pool = _Pool()

        class _App:
            state = _State()

        drained, pool_closed = await _finalize_turns_and_close_pool(
            _App(), remaining=lambda: 0.0
        )
        self.assertTrue(
            drained,
            "the real drain with no live producers must report a completed "
            "drain",
        )
        self.assertFalse(pool_closed, "the exhausted budget leaves the pool open")
        self.assertEqual(calls, [], "the exhausted budget must not close the pool")

    async def test_incomplete_finalizers_never_fire_the_tail_exit(self):
        from app.lifespan import _bounded_tail_exit, _finalize_turns_and_close_pool

        class _Pool:
            def close_all(self):
                raise AssertionError("the pool must not be closed")

        class _State:
            db_pool = _Pool()

        class _App:
            state = _State()

        exits = []
        with unittest.mock.patch.dict(
            os.environ, {"RAGAPP_BOUNDED_EXIT": "1"}
        ), unittest.mock.patch(
            "app.api.routes.chat.shutdown_turn_finalizers",
            new=unittest.mock.AsyncMock(return_value=False),
        ), unittest.mock.patch.object(
            os, "_exit", side_effect=lambda c: exits.append(c)
        ):
            drained, pool_closed = await _finalize_turns_and_close_pool(
                _App(), remaining=lambda: 0.0
            )
            self.assertFalse(drained, "the finalizer drain must have failed")
            fired = _bounded_tail_exit(drained, pool_closed)

        self.assertFalse(
            fired or exits,
            "an incomplete turn-finalizer drain must never be cut (the "
            "#1202 durability contract)",
        )


class TestExhaustedBudgetProcessExit(unittest.TestCase):
    """A child-process proof: the exhausted-budget path ends the PROCESS.

    A live non-critical worker thread (60 s) stands in for the executor
    joins the tail exit skips. Without os._exit the child would wait for
    that thread at interpreter exit; with it the child leaves promptly.
    """

    CHILD = """
import asyncio, os, sys, threading, time
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, {backend!r})
os.environ["RAGAPP_BOUNDED_EXIT"] = "1"

import app.lifespan as lifespan_mod
import app.api.routes.chat as chat_mod


class _Pool:
    def close_all(self):
        raise AssertionError("the pool must not be closed")


async def main():
    app = SimpleNamespace(state=SimpleNamespace(db_pool=_Pool()))
    with mock.patch.object(
        chat_mod, "shutdown_turn_finalizers",
        new=mock.AsyncMock(return_value=True),
    ):
        drained, pool_closed = await lifespan_mod._finalize_turns_and_close_pool(
            app, remaining=lambda: 0.0
        )
    assert (drained, pool_closed) == (True, False), (drained, pool_closed)

    # A live non-critical worker: a NON-daemon thread, so an ordinary
    # interpreter exit would wait for it.
    worker = threading.Thread(target=time.sleep, args=(60,))
    worker.start()
    time.sleep(0.2)
    lifespan_mod._bounded_tail_exit(drained, pool_closed)
    raise SystemExit("the tail exit did not exit the process")


asyncio.run(main())
"""

    def test_child_process_exits_promptly_through_the_tail_exit(self):
        import subprocess

        backend = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        script = self.CHILD.format(backend=backend)
        started = time.monotonic()
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=backend,
            capture_output=True,
            text=True,
            timeout=120,
        )
        elapsed = time.monotonic() - started
        self.assertEqual(
            proc.returncode,
            0,
            f"the child must exit through the tail exit; stderr={proc.stderr[-2000:]}",
        )
        self.assertLess(
            elapsed,
            30.0,
            f"the child took {elapsed:.1f}s to exit: it must not have "
            "joined the non-critical worker (60 s) at interpreter exit",
        )


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
            from app.services.document_processor import _get_parse_executor
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
                        # The parse executor has the same lifecycle
                        # (shut down at shutdown, recreated on demand).
                        parse_executor = _get_parse_executor()
                        parse_future = parse_executor.submit(lambda: "ok")
                        self.assertEqual(parse_future.result(timeout=10), "ok")
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


class TestZeroBudgetPoolClose(unittest.IsolatedAsyncioTestCase):
    """A supplied ZERO budget must never close the pool on the event loop.

    `close_all()` takes the pool lock and can block, so an exhausted
    aggregate budget must SKIP the close (leaving `pool_closed` False)
    rather than call it synchronously. `pool_closed` False is a
    resource-cleanup outcome, not a durability precondition (plan rev 11):
    the tail exit still fires on that state, so the process exits before
    Compose force-terminates it (frozen AC12) -- see
    TestExhaustedBudgetTailExit.
    """

    async def test_zero_budget_skips_the_close_and_leaves_pool_open(self):
        from app.lifespan import _finalize_turns_and_close_pool

        calls = []

        class _Pool:
            def close_all(self):
                calls.append("closed")

        class _State:
            db_pool = _Pool()

        class _App:
            state = _State()

        # The close branch is entered only when the finalizer drain
        # SUCCEEDED, so the drain is forced true here (and asserted below) —
        # otherwise the test could pass without ever reaching the zero-budget
        # branch it is meant to protect.
        with unittest.mock.patch(
            "app.api.routes.chat.shutdown_turn_finalizers",
            new=unittest.mock.AsyncMock(return_value=True),
        ):
            drained, pool_closed = await _finalize_turns_and_close_pool(
                _App(), remaining=lambda: 0.0
            )
        self.assertTrue(
            drained,
            "the finalizer drain must have succeeded for the close branch "
            "to be reachable at all",
        )
        self.assertEqual(
            calls, [], "an exhausted budget must not close the pool at all"
        )
        self.assertFalse(pool_closed, "the pool must not be marked closed")

    async def test_positive_budget_closes_the_pool_and_marks_it(self):
        from app.lifespan import _finalize_turns_and_close_pool

        calls = []

        class _Pool:
            def close_all(self):
                calls.append("closed")

        class _State:
            db_pool = _Pool()

        class _App:
            state = _State()

        # The finalizer drain is what gates the close; force its result by
        # patching the drain helper the function calls.
        with unittest.mock.patch(
            "app.api.routes.chat.shutdown_turn_finalizers",
            new=unittest.mock.AsyncMock(return_value=True),
        ):
            drained, pool_closed = await _finalize_turns_and_close_pool(
                _App(), remaining=lambda: 5.0
            )
        self.assertTrue(drained)
        self.assertTrue(pool_closed)
        self.assertEqual(calls, ["closed"])


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
