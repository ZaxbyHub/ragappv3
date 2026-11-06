"""Degraded-start behavior for the #749 operator-path fixes (RED at base,
GREEN after the fix). Drives the REAL lifespan with the pinning harness from
test_health_readiness_lifespan.py (settings paths → temp dir, sacrificial
auth executor, singleton resets, root-level restore).

Legs (plan-critic round 3, finding 1):
  A — unwritable data dir: startup FAILS by raising RuntimeError whose own
      message names the directory, the current-uid token, the owner-uid
      token, and the chown remediation (the criterion's explicitly permitted
      second arm). Kills the flag-only and preflight-deleted mutants.
  B — fresh start (absent data dir): the preflight passes and startup
      proceeds normally (migrations_ok stays True) — guards the
      no-fresh-start regression the critic flagged (init_db creates parents).
  C — settings-load-only failure, ISOLATED: real migrations run against a
      good temp DB, then `_load_persisted_settings` alone fails — startup
      COMPLETES degraded with migrations_ok False and /api/healthz answers
      503. The isolation is what kills the degraded-flag-elision mutant
      (whole-DB corruption would fail migrations first and mask it).
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


class TestUnwritableDataDirFailsWithNamedMessage(unittest.TestCase):
    """Leg A: the preflight raises with directory/uid/owner/remediation."""

    def test_unwritable_dir_raises_named_runtime_error(self):
        from fastapi import FastAPI

        from app.lifespan import lifespan

        tmp = _fresh_tmp_dir("i02c-legA-")
        restore = _pin_lifespan_settings(tmp)
        tmp.mkdir(exist_ok=True)  # exists; access will be monkeypatched False

        real_access = os.access

        def fake_access(path, mode):
            try:
                if Path(path) == tmp:
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
            restore()

        self.assertIsNotNone(raised, "unwritable data dir must fail startup")
        message = str(raised)
        self.assertIn(str(tmp), message, "must name the data directory")
        self.assertIn("uid", message, "must name the current uid")
        self.assertIn("owner", message, "must name the owner uid")
        self.assertIn("chown", message, "must carry the ownership remediation")


class TestFreshStartPassesPreflight(unittest.TestCase):
    """Leg B: an absent data dir is NOT treated as unwritable."""

    def test_absent_dir_startup_completes_healthy(self):
        from fastapi import FastAPI

        from app.lifespan import lifespan

        tmp = _fresh_tmp_dir("i02c-legB-") / "data"  # does NOT exist yet
        restore = _pin_lifespan_settings(tmp)
        app = FastAPI()
        migrations_ok = None
        try:

            async def scenario():
                nonlocal migrations_ok
                async with lifespan(app):
                    migrations_ok = getattr(app.state, "migrations_ok", None)

            asyncio.run(scenario())
        finally:
            restore()

        self.assertTrue(
            migrations_ok, "fresh start must complete with migrations_ok True"
        )


class TestSettingsLoadFailureDegradesTo503(unittest.TestCase):
    """Leg C: ONLY the settings call fails → degraded start, healthz 503."""

    def test_isolated_settings_failure_sets_flag_and_serves_503(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        import app.lifespan as lifespan_module
        from app.lifespan import lifespan

        tmp = _fresh_tmp_dir("i02c-legC-")
        restore = _pin_lifespan_settings(tmp)
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
            restore()

        # ISOLATED failure: migrations ran fine on the good temp DB, so the
        # False here can only come from the settings-load guard.
        self.assertFalse(
            migrations_ok,
            "settings-load failure must set migrations_ok False (degraded)",
        )
        # The real HTTP route reads the flag the lifespan recorded (no
        # lifespan on this client — same no-startup pattern as
        # test_health_readiness.py; app.state carries the scenario's values).
        from app.main import app as real_app

        real_app.state.migrations_ok = False
        client = TestClient(real_app)
        response = client.get("/api/healthz")
        self.assertEqual(
            response.status_code,
            503,
            "healthz must answer 503 while migrations_ok is False",
        )


if __name__ == "__main__":
    unittest.main()
