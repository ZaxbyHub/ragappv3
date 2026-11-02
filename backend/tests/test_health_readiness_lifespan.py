"""Lifespan wiring guard for the readiness migration flag (issue #550).

The AC1 behavior (healthz 503 when migrations_ok is False) is pinned by the
frozen check in test_health_readiness.py, which sets the flag directly. This
guard locks the OTHER half of the wiring: lifespan must actually record the
outcome of its run_migrations call on app.state, or the readiness signal is
dead code. Source-text inspection follows the repo convention for lifespan
wiring claims that are invisible to behavior tests (see
test_lifespan_pool_seeding_order.py); the guards match literal code
identifiers, so reverting the fix fails them. Runtime entry into the real
lifespan is covered by the tests appended below — the earlier
"impractical in unit CI" justification was refuted (see the appended
section for the working recipe).
"""

import os
import sys
import unittest
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

BACKEND = Path(__file__).resolve().parents[1]
LIFESPAN = BACKEND / "app" / "lifespan.py"
HEALTH = BACKEND / "app" / "api" / "routes" / "health.py"


class TestLifespanMigrationFlagWiring(unittest.TestCase):
    """Issue #550: the startup migration outcome must reach the probe."""

    def test_lifespan_records_migration_outcome(self):
        source = LIFESPAN.read_text(encoding="utf-8")

        call_at = source.find("run_migrations(str(settings.sqlite_path))")
        self.assertGreater(call_at, -1, "lifespan must call run_migrations")

        set_true_at = source.find("app.state.migrations_ok = True")
        set_false_at = source.find("app.state.migrations_ok = False")
        self.assertGreater(
            set_true_at, -1, "lifespan must pre-set migrations_ok = True"
        )
        self.assertGreater(
            set_false_at,
            -1,
            "lifespan must record a failed migration as migrations_ok = False",
        )
        self.assertLess(
            set_true_at,
            call_at,
            "migrations_ok = True must be set before the migration call",
        )
        self.assertGreater(
            set_false_at,
            call_at,
            "migrations_ok = False must be recorded in the failure path after the call",
        )

    def test_healthz_reads_the_recorded_flag(self):
        source = HEALTH.read_text(encoding="utf-8")
        self.assertIn(
            'getattr(state, "migrations_ok", None) is False',
            source,
            "healthz must gate on the lifespan-recorded migrations_ok flag",
        )



# ---------------------------------------------------------------------------
# Issue #743 (P03-SK2-07): tests that enter and exit the REAL lifespan().
# The source-text guards above stay (they pin ordering details cheaply); these
# runtime tests pin the behavior they could only describe — startup wires
# app.state.background_processor, shutdown awaits stop(), and the fresh-handler
# logging branch attaches the request-id filter. The mutation check C2
# (tests/test_h06_mutation_kills) freezes the shutdown half: replacing the
# stop() call with `pass` fails test_real_lifespan_stop_is_awaited.
# Feasibility follows the 2026-09-24 frontier-audit probe (P03): the real
# lifespan runs in the standard CI dependency set once the settings-derived
# paths are pinned — the old "impractical in unit CI" claim above is wrong.
# ---------------------------------------------------------------------------

import asyncio
import atexit
import logging
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI


def _pin_lifespan_settings(tmp_path: Path):
    """Pin every settings path/flag the real lifespan touches to a temp dir.

    Returns a restore callable. settings.vaults_dir and settings.sqlite_path
    are read-only properties derived from data_dir, so pinning data_dir
    redirects all of them. No env vars are touched (the settings singleton is
    already constructed; attribute pinning is the only effective lever).

    Entering the real lifespan wires process-global state (the module pool
    cache and the wiki/draft event-bus singletons), so each entry first
    closes stale pools and resets the buses — otherwise a second entry in
    the same process trips loop-bound queues and closed pools."""
    from app.config import settings
    from app.models.database import _pool_cache, _pool_cache_lock

    with _pool_cache_lock:
        for pool in list(_pool_cache.values()):
            try:
                pool.close_all()
            except Exception:  # noqa: BLE001 - an already-closed pool is fine
                pass
        _pool_cache.clear()
    import app.services.background_tasks as _background_tasks
    import app.services.draft_events as _draft_events
    import app.services.wiki_events as _wiki_events

    _wiki_events._bus = None  # noqa: SLF001 - reset singleton for a fresh loop
    _draft_events._bus = None  # noqa: SLF001
    _background_tasks._processor_instance = None  # noqa: SLF001 - its queues
    # bind to the loop that first runs its workers; a second lifespan entry in
    # the same process must get a fresh processor bound to the new loop.

    # The REAL lifespan shutdown kills the module-level auth executor
    # (lifespan.py: `_auth_executor.shutdown(wait=False)`). Hand it a
    # SACRIFICIAL executor for the duration of the entry so the original --
    # which other test modules imported BY VALUE (`from auth_service import
    # _auth_executor`) -- is never shut down. CI round 1 without this: 24
    # "RuntimeError: cannot schedule new futures after shutdown" failures on
    # one xdist worker (test_auth_service_async_hash, test_d07b).
    from app.services import auth_service as _auth_service

    saved_auth_executor = _auth_service._auth_executor
    _auth_service._auth_executor = ThreadPoolExecutor(
        max_workers=4, thread_name_prefix="auth-cpu"
    )

    # The lifespan startup raises the ROOT LOGGER LEVEL to settings.log_level
    # (default INFO; lifespan.py). Under pytest the root normally sits at
    # WARNING, which silently DROPS malformed log records before any handler
    # formats them -- e.g. document_retrieval.py logs max_distance_threshold
    # with %.3f, and a None threshold then raises TypeError inside handler
    # formatting once the level admits the record (CI round 2: 4
    # test_rag_engine_filtering_adversarial failures after a lifespan entry
    # on the same worker). Save and restore the root level so an entry cannot
    # change which records other tests' handlers see.
    root_logger = logging.getLogger()
    saved_root_level = root_logger.level

    saved = {
        "data_dir": settings.data_dir,
        "redis_url": settings.redis_url,
        "users_enabled": settings.users_enabled,
        "admin_secret_token": settings.admin_secret_token,
        "jwt_secret_key": settings.jwt_secret_key,
        "ollama_embedding_url": settings.ollama_embedding_url,
        "allow_local_services": settings.allow_local_services,
    }
    settings.data_dir = tmp_path  # type: ignore[assignment]
    settings.redis_url = ""
    settings.users_enabled = False
    settings.admin_secret_token = "lifespan-test-token-0123456789abcdef0123456789"
    settings.jwt_secret_key = "lifespan-test-jwt-0123456789abcdef0123456789"
    settings.ollama_embedding_url = "http://127.0.0.1:9/v1/embeddings"
    settings.allow_local_services = True  # type: ignore[assignment]

    def restore():
        # PRR-018: isolate each step so one failure cannot strand the
        # executor swap or leave settings half-restored.
        import contextlib

        with contextlib.suppress(Exception):
            root_logger.setLevel(saved_root_level)
        for key, value in saved.items():
            with contextlib.suppress(Exception):
                setattr(settings, key, value)
        # Give back the ORIGINAL auth executor (see the sacrificial swap in
        # the pin above). Shut the sacrificial down if the lifespan's own
        # shutdown somehow did not reach it.
        from app.services import auth_service as _auth_service

        try:
            if _auth_service._auth_executor is not saved_auth_executor:
                _auth_service._auth_executor.shutdown(wait=False)
        finally:
            _auth_service._auth_executor = saved_auth_executor

    return restore


def test_real_lifespan_stop_is_awaited_on_shutdown():
    """Startup wires the background processor; shutdown awaits its stop().

    Root HANDLERS are snapshotted and restored (PRR-014): with handlers
    present, the lifespan's existing-handler branch attaches
    RequestIdFilter/SensitiveFieldFilter to them — including pytest's
    session-reused capture handlers — and without the snapshot those
    filters would redact captured log extras for every later test on this
    worker (the same pollution class as the executor/level fixes)."""
    import logging as _logging

    from app.lifespan import lifespan

    root = _logging.getLogger()
    # Snapshot each handler's FILTER list too: the existing-handler branch
    # addFilter()s the handler OBJECTS in place, so restoring the handler
    # list alone would reinstate the same mutated objects (the leak the
    # reviewer's probe demonstrated).
    saved_handlers = [(h, list(h.filters)) for h in root.handlers]
    # PRR-012: mkdtemp + atexit rmtree — the TemporaryDirectory finalizer
    # silently fails on Windows while the lifespan's background handles are
    # open (delta-review after-exit probe); atexit runs at interpreter exit
    # when every handle is closed (user_route_helpers proves the pattern).
    _tmp_dir = Path(tempfile.mkdtemp(prefix="lifespan-rt-"))
    atexit.register(shutil.rmtree, _tmp_dir, True)
    restore = _pin_lifespan_settings(_tmp_dir)
    app = FastAPI()
    stop_calls: list[int] = []
    try:

        async def scenario():
            async with lifespan(app):
                processor = app.state.background_processor
                assert processor is not None, (
                    "startup must wire app.state.background_processor"
                )
                original_stop = processor.stop

                async def recording_stop():
                    stop_calls.append(1)
                    await original_stop()

                processor.stop = recording_stop

        asyncio.run(scenario())
    finally:
        restore()
        root.handlers = [h for h, _ in saved_handlers]
        for handler, filters in saved_handlers:
            handler.filters = list(filters)

    assert stop_calls == [1], "shutdown must await background_processor.stop()"


def test_real_lifespan_fresh_branch_attaches_request_id_filter():
    """Fresh-handler branch: with no pre-existing root handlers, startup's
    logging setup attaches RequestIdFilter (and SensitiveFieldFilter) to the
    handler it installs. The C3/C4 mutation checks freeze both halves."""
    from app.lifespan import RequestIdFilter, SensitiveFieldFilter, lifespan

    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    root.handlers.clear()
    _tmp_dir = Path(tempfile.mkdtemp(prefix="lifespan-fb-"))
    atexit.register(shutil.rmtree, _tmp_dir, True)
    restore = _pin_lifespan_settings(_tmp_dir)
    app = FastAPI()
    observed: list[tuple] = []
    try:

        async def scenario():
            async with lifespan(app):
                observed.extend(
                    tuple(type(f) for f in handler.filters)
                    for handler in list(root.handlers)
                )

        asyncio.run(scenario())
    finally:
        restore()
        root.handlers = saved_handlers

    assert observed, "startup must install a root logging handler"
    for filter_types in observed:
        assert RequestIdFilter in filter_types, (
            "RequestIdFilter must sit on every root handler (fresh-handler branch)"
        )
        assert SensitiveFieldFilter in filter_types, (
            "SensitiveFieldFilter must sit on every root handler (fresh-handler branch)"
        )

if __name__ == "__main__":
    unittest.main()
