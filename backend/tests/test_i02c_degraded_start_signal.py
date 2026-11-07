"""Degraded-start behavior for the #749 operator-path fixes (RED at base,
GREEN after the fix). Drives the REAL lifespan with the pinning harness from
test_health_readiness_lifespan.py (settings paths → temp dir, sacrificial
auth executor, singleton resets, root-level restore).

Legs (plan-critic round 3, finding 1; feedback round hardened per the
pr1081-20261106 review):
  A — unwritable data dir: startup FAILS by raising RuntimeError whose own
      message names the directory, the current-uid token, the owner-uid
      token, and the chown remediation. The access fake ASSERTS the preflight
      requested W_OK|X_OK (the mode-blind fake the review caught could not
      distinguish W_OK from R_OK); a second leg exercises a REAL POSIX
      write-but-not-searchable directory mode (skipped on Windows, where
      directory modes do not carry the semantics).
  B — fresh start (absent data dir): the preflight passes and startup
      proceeds normally (migrations_ok stays True) — guards the
      no-fresh-start regression the critic flagged (init_db creates parents).
  C — settings-load-only failure, ISOLATED: real migrations run against a
      good temp DB, then `_load_persisted_settings` alone fails — startup
      COMPLETES degraded with migrations_ok False. The healthz half seeds
      HEALTHY app state and asserts the flag-caused differential (503 with
      the flag False, 200 with the flag True), so removing the readiness
      gate fails the leg instead of passing for the wrong reason.

Every leg that enters the real lifespan snapshots and restores the ROOT
handler list AND each handler's filter list (the lifespan's existing-handler
branch addFilter()s handler objects in place — the copied harness the review
caught omitted exactly this restore), and leg C snapshots/restores every
shared app.state attribute it touches.
"""

import asyncio
import atexit
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _pin_lifespan_settings(tmp_path: Path):
    """Same pinning contract as test_health_readiness_lifespan.py (credited
    copy so this module stays standalone): close/reset process-global pools,
    buses and the processor singleton, hand the lifespan a SACRIFICIAL auth
    executor, snapshot the root logger level, and pin every settings-derived
    path/flag to the temp dir. Returns a restore callable."""
    from app.config import settings
    from app.models.database import _pool_cache, _pool_cache_lock

    with _pool_cache_lock:
        for pool in list(_pool_cache.values()):
            try:
                pool.close_all()
            except Exception:  # noqa: BLE001 - already-closed pool is fine
                pass
        _pool_cache.clear()
    import app.services.background_tasks as _background_tasks
    import app.services.draft_events as _draft_events
    import app.services.wiki_events as _wiki_events

    _wiki_events._bus = None  # noqa: SLF001
    _draft_events._bus = None  # noqa: SLF001
    _background_tasks._processor_instance = None  # noqa: SLF001

    def _reset_lifecycle_globals():
        # Pools, buses and the processor singleton bind to the loop the real
        # lifespan ran on; reset them on BOTH sides so nothing bound to a
        # dead loop leaks into the next test on this xdist worker
        # (batch-5 review).
        with _pool_cache_lock:
            for pool in list(_pool_cache.values()):
                try:
                    pool.close_all()
                except Exception:  # noqa: BLE001 - already-closed pool is fine
                    pass
            _pool_cache.clear()
        _wiki_events._bus = None  # noqa: SLF001
        _draft_events._bus = None  # noqa: SLF001
        _background_tasks._processor_instance = None  # noqa: SLF001

    from app.services import auth_service as _auth_service

    saved_auth_executor = _auth_service._auth_executor
    _auth_service._auth_executor = ThreadPoolExecutor(
        max_workers=4, thread_name_prefix="auth-cpu-i02c"
    )

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
    settings.admin_secret_token = "i02c-test-token-0123456789abcdef0123456789"
    settings.jwt_secret_key = "i02c-test-jwt-0123456789abcdef0123456789"
    settings.ollama_embedding_url = "http://127.0.0.1:9/v1/embeddings"
    settings.allow_local_services = True  # type: ignore[assignment]

    def restore():
        import contextlib

        with contextlib.suppress(Exception):
            root_logger.setLevel(saved_root_level)
        with contextlib.suppress(Exception):
            _reset_lifecycle_globals()
        for key, value in saved.items():
            with contextlib.suppress(Exception):
                setattr(settings, key, value)
        from app.services import auth_service as _auth_service

        try:
            if _auth_service._auth_executor is not saved_auth_executor:
                _auth_service._auth_executor.shutdown(wait=False)
        finally:
            _auth_service._auth_executor = saved_auth_executor

    return restore


def _fresh_tmp_dir(prefix: str) -> Path:
    """mkdtemp + atexit rmtree (Windows-open-handles safe; PRR-012 pattern)."""
    tmp = Path(tempfile.mkdtemp(prefix=prefix))
    atexit.register(shutil.rmtree, tmp, True)
    return tmp


def _snapshot_root_handlers():
    """Snapshot the root handler list AND each handler's filter list (the
    lifespan's existing-handler branch addFilter()s handler OBJECTS in place,
    including pytest's session-reused capture handlers — restoring the list
    alone would reinstate the mutated objects)."""
    root = logging.getLogger()
    return root, [(h, list(h.filters)) for h in root.handlers]


def _restore_root_handlers(root, saved):
    root.handlers = [h for h, _ in saved]
    for handler, filters in saved:
        handler.filters = list(filters)


class _HealthyVectorStore:
    """Minimal healthy vector-store stand-in for the healthz differential
    (truthy .table + explicit _ready=True; mirrors test_health_readiness)."""

    def __init__(self):
        self.table = object()
        self._ready = True


class TestUnwritableDataDirFailsWithNamedMessage(unittest.TestCase):
    """Leg A: the preflight raises with directory/uid/owner/remediation."""

    def test_unwritable_dir_raises_named_runtime_error(self):
        from fastapi import FastAPI

        from app.lifespan import lifespan

        tmp = _fresh_tmp_dir("i02c-legA-")
        restore = _pin_lifespan_settings(tmp)
        tmp.mkdir(exist_ok=True)  # exists; access will be monkeypatched False
        root, saved_handlers = _snapshot_root_handlers()

        real_access = os.access
        modes_seen: list[int] = []

        def fake_access(path, mode):
            try:
                if Path(path) == tmp:
                    modes_seen.append(mode)
                    return False
            except (TypeError, ValueError):
                pass
            return real_access(path, mode)

        app = FastAPI()
        raised = None
        try:
            os.access = fake_access  # noqa: SLF001 - selective patch, restored below

            async def scenario():
                async with lifespan(app):
                    raise AssertionError("startup must not complete")

            try:
                asyncio.run(scenario())
            except RuntimeError as exc:
                raised = exc
        finally:
            os.access = real_access
            _restore_root_handlers(root, saved_handlers)
            restore()

        self.assertIsNotNone(raised, "unwritable data dir must fail startup")
        message = str(raised)
        self.assertIn(str(tmp), message, "must name the data directory")
        self.assertIn("uid", message, "must name the current uid")
        self.assertIn("owner", message, "must name the owner uid")
        self.assertIn("chown", message, "must carry the ownership remediation")
        # The preflight must require WRITE *and* SEARCH permission: requiring
        # the W_OK|X_OK combination among the probed modes kills the
        # W_OK-only and R_OK-only mutants the mode-blind fake could not
        # distinguish (pr1081-20261106 PRR-003). Other processes may probe
        # the same path with other modes, so this is a membership assertion.
        self.assertIn(
            os.W_OK | os.X_OK,
            modes_seen,
            "preflight must test os.access(dir, W_OK | X_OK)",
        )

    @unittest.skipIf(os.name == "nt", "POSIX directory mode semantics")
    def test_real_write_only_dir_mode_raises(self):
        from fastapi import FastAPI

        from app.lifespan import lifespan

        parent = _fresh_tmp_dir("i02c-legA2-")
        tmp = parent / "data"
        tmp.mkdir()
        tmp.chmod(0o200)  # write-only: no search bit for child creation
        restore = _pin_lifespan_settings(tmp)
        if os.access(tmp, os.W_OK | os.X_OK):
            tmp.chmod(0o700)
            restore()
            self.skipTest("permission checks not effective for this uid (root?)")
        root, saved_handlers = _snapshot_root_handlers()
        app = FastAPI()
        raised = None
        try:

            async def scenario():
                async with lifespan(app):
                    raise AssertionError("startup must not complete")

            try:
                asyncio.run(scenario())
            except RuntimeError as exc:
                raised = exc
        finally:
            tmp.chmod(0o700)  # allow the atexit rmtree to succeed
            _restore_root_handlers(root, saved_handlers)
            restore()

        self.assertIsNotNone(
            raised, "write-but-not-searchable dir must fail the preflight"
        )
        self.assertIn(str(tmp), str(raised))

    def test_stat_permission_error_raises_named_message(self):
        """Leg A3 (batch-5 review): an existing directory whose stat() fails
        with PermissionError (unsearchable PARENT) must hit the named
        message. Path.exists() raises a bare PermissionError on 3.11 and
        returns False on 3.12+ — either skipped the preflight before the
        explicit-stat gate."""
        from fastapi import FastAPI

        from app.lifespan import lifespan

        tmp = _fresh_tmp_dir("i02c-legA3-")
        restore = _pin_lifespan_settings(tmp)
        root, saved_handlers = _snapshot_root_handlers()
        real_stat = Path.stat

        def fake_stat(self, *args, **kwargs):
            if self == tmp:
                raise PermissionError(13, "Permission denied", str(self))
            return real_stat(self, *args, **kwargs)

        app = FastAPI()
        raised = None
        try:
            Path.stat = fake_stat  # type: ignore[method-assign]

            async def scenario():
                async with lifespan(app):
                    raise AssertionError("startup must not complete")

            try:
                asyncio.run(scenario())
            except RuntimeError as exc:
                raised = exc
        finally:
            Path.stat = real_stat  # type: ignore[method-assign]
            _restore_root_handlers(root, saved_handlers)
            restore()

        self.assertIsNotNone(raised, "stat-denied data dir must fail startup")
        self.assertIn("Data directory is not writable/searchable", str(raised))
        self.assertIn(str(tmp), str(raised))

    def test_regular_file_data_dir_raises_named_message(self):
        """Leg A4 (batch-7 review): a regular FILE at the data-dir path
        passes os.access(W_OK|X_OK) when its mode bits allow it, then fails
        later with a bare NotADirectoryError — the preflight must refuse it
        with the named message."""
        from fastapi import FastAPI

        from app.lifespan import lifespan

        parent = _fresh_tmp_dir("i02c-legA4-")
        not_a_dir = parent / "data"
        not_a_dir.write_text("not a directory", encoding="utf-8")
        not_a_dir.chmod(0o755)
        restore = _pin_lifespan_settings(not_a_dir)
        root, saved_handlers = _snapshot_root_handlers()
        app = FastAPI()
        raised = None
        try:

            async def scenario():
                async with lifespan(app):
                    raise AssertionError("startup must not complete")

            try:
                asyncio.run(scenario())
            except RuntimeError as exc:
                raised = exc
        finally:
            _restore_root_handlers(root, saved_handlers)
            restore()

        self.assertIsNotNone(raised, "a regular file at the data-dir path must fail")
        self.assertIn("Data directory is not writable/searchable", str(raised))
        self.assertIn(str(not_a_dir), str(raised))


class TestFreshStartPassesPreflight(unittest.TestCase):
    """Leg B: an absent data dir is NOT treated as unwritable."""

    def test_absent_dir_startup_completes_healthy(self):
        from fastapi import FastAPI

        from app.lifespan import lifespan

        tmp = _fresh_tmp_dir("i02c-legB-") / "data"  # does NOT exist yet
        restore = _pin_lifespan_settings(tmp)
        root, saved_handlers = _snapshot_root_handlers()
        app = FastAPI()
        migrations_ok = None
        try:

            async def scenario():
                nonlocal migrations_ok
                async with lifespan(app):
                    migrations_ok = getattr(app.state, "migrations_ok", None)

            asyncio.run(scenario())
        finally:
            _restore_root_handlers(root, saved_handlers)
            restore()

        self.assertTrue(
            migrations_ok, "fresh start must complete with migrations_ok True"
        )


class TestSettingsLoadFailureDegradesTo503(unittest.TestCase):
    """Leg C: ONLY the settings call fails → degraded start, flag-caused 503."""

    def test_isolated_settings_failure_sets_flag_and_serves_503(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        import app.lifespan as lifespan_module
        from app.lifespan import lifespan

        tmp = _fresh_tmp_dir("i02c-legC-")
        restore = _pin_lifespan_settings(tmp)
        root, saved_handlers = _snapshot_root_handlers()
        app = FastAPI()
        saved_loader = lifespan_module._load_persisted_settings

        def failing_loader(_path: str) -> None:
            raise sqlite3.OperationalError("unable to open database file")

        lifespan_module._load_persisted_settings = failing_loader  # noqa: SLF001
        try:

            async def scenario():
                async with lifespan(app):
                    return getattr(app.state, "migrations_ok", None)

            migrations_ok = asyncio.run(scenario())
        finally:
            lifespan_module._load_persisted_settings = saved_loader  # noqa: SLF001
            _restore_root_handlers(root, saved_handlers)
            restore()

        # ISOLATED failure: migrations ran fine on the good temp DB, so the
        # False here can only come from the settings-load guard.
        self.assertFalse(
            migrations_ok,
            "settings-load failure must set migrations_ok False (degraded)",
        )

        # The healthz differential is FLAG-CAUSED: seed every other readiness
        # input healthy, then assert 503 with the flag False and 200 with the
        # flag True. Under a readiness-gate-removal mutant the first call
        # would answer 200 and fail the leg (the shared app.main app carries
        # no real startup state, so the seed is what makes the flag the only
        # 503 source — the pr1081-20261106 review's gate-removal probe).
        from app.main import app as real_app

        attrs = (
            "db_pool",
            "vector_store",
            "embedding_service",
            "maintenance_service",
            "migrations_ok",
        )
        snapshot = {
            name: (hasattr(real_app.state, name), getattr(real_app.state, name, None))
            for name in attrs
        }
        client = TestClient(real_app)
        try:
            real_app.state.db_pool = object()  # presence = healthy (no saturation)
            real_app.state.vector_store = _HealthyVectorStore()
            real_app.state.embedding_service = object()
            real_app.state.maintenance_service = None

            real_app.state.migrations_ok = False
            degraded = client.get("/api/healthz")
            self.assertEqual(
                degraded.status_code,
                503,
                "healthz must answer 503 while migrations_ok is False",
            )
            self.assertIn(
                "migration",
                degraded.text.lower(),
                "the 503 issues must name the startup migration failure",
            )

            real_app.state.migrations_ok = True
            healthy = client.get("/api/healthz")
            self.assertEqual(
                healthy.status_code,
                200,
                "the seeded state must be healthy with the flag True "
                "(proves the 503 above is flag-caused, not state-caused)",
            )
        finally:
            for name, (present, value) in snapshot.items():
                if present:
                    setattr(real_app.state, name, value)
                elif hasattr(real_app.state, name):
                    delattr(real_app.state, name)


class TestDriftGuardRuntimeErrorPropagates(unittest.TestCase):
    """Leg D: the persisted-schema drift guard's RuntimeError must ABORT
    startup, not boot degraded (batch-2 review F-06 — the settings-load
    catch-all would otherwise swallow the drift contract's abort)."""

    def test_runtime_error_from_loader_propagates(self):
        from fastapi import FastAPI

        import app.lifespan as lifespan_module
        from app.lifespan import lifespan

        tmp = _fresh_tmp_dir("i02c-legD-")
        restore = _pin_lifespan_settings(tmp)
        root, saved_handlers = _snapshot_root_handlers()
        app = FastAPI()
        saved_loader = lifespan_module._load_persisted_settings

        def drift_guard_failure(_path: str) -> None:
            raise RuntimeError(
                "Persisted settings schema drift detected — refusing to load"
            )

        lifespan_module._load_persisted_settings = drift_guard_failure  # noqa: SLF001
        aborted = False
        try:

            async def scenario():
                async with lifespan(app):
                    raise AssertionError("startup must not complete past drift")

            try:
                asyncio.run(scenario())
            except RuntimeError as exc:
                aborted = "drift" in str(exc)
        finally:
            lifespan_module._load_persisted_settings = saved_loader  # noqa: SLF001
            _restore_root_handlers(root, saved_handlers)
            restore()

        self.assertTrue(
            aborted,
            "drift-guard RuntimeError must propagate and abort startup "
            "(not be swallowed into degraded start)",
        )


if __name__ == "__main__":
    unittest.main()
