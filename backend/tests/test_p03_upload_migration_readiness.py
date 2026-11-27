"""P03 upload-migration readiness checks (issue #799 AC6).

A failed upload migration is deliberately tolerated at startup ("continuing
anywhere"), but the failure must be OBSERVABLE: the except handler around the
``migrate_uploads`` call in lifespan.py must record the outcome on
``app.state``, and /api/healthz (backend/app/api/routes/health.py) must read
that same attribute — the same wiring migrations_ok already uses.

Four checks — two RED at base / GREEN after the fix, two passing at base
and after:

- AC6-AST (structural, RED at base): parse lifespan.py, find the try block
  whose subtree references ``migrate_uploads`` (the import and/or the
  ``asyncio.to_thread`` call), and require at least one ``app.state.<name>``
  attribute assigned in that try's except-handler body. At base the handler
  only logs, so the failure line carries
  ``AC6-AST recorded-attributes=none``.
- AC6-BEHAV (behavioral, RED at base): run the REAL lifespan with
  ``migrate_uploads`` patched to raise, then GET the REAL /api/healthz route
  and require 503. At base healthz stays 200, so the failure line carries
  ``AC6-BEHAV healthz-status=200 expected-503``.
- AC6-SUCCESS (passes at base and after): the real unpatched no-op
  migration on a fresh temp dir keeps healthz 200 — the fix must not
  degrade the success path.
- AC6-INERT (passes at base and after): with ``upload_migration_ok`` ABSENT
  from app.state, healthz stays 200 and surfaces no upload-migration issue —
  the route's getattr default must stay inert.

The behavioral leg mirrors the proven real-lifespan recipe from
tests/test_health_readiness_lifespan.py (pinned settings on a temp data dir,
singleton resets, sacrificial auth executor, root-logging snapshot), driving
the lifespan through FastAPI's TestClient so an actual healthz request runs.
"""

import ast
import atexit
import logging
import os
import shutil
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

BACKEND = Path(__file__).resolve().parents[1]
LIFESPAN_PATH = BACKEND / "app" / "lifespan.py"
HEALTH_PATH = BACKEND / "app" / "api" / "routes" / "health.py"


def _subtree_mentions(scope, symbol: str) -> bool:
    """True when any Name / Attribute / import alias in scope is ``symbol``."""
    for node in ast.walk(scope):
        if isinstance(node, ast.Name) and node.id == symbol:
            return True
        if isinstance(node, ast.Attribute) and node.attr == symbol:
            return True
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == symbol or alias.asname == symbol:
                    return True
    return False


def _find_migrate_uploads_try(tree: ast.Module):
    """The first try block whose subtree references ``migrate_uploads``."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and _subtree_mentions(node, "migrate_uploads"):
            return node
    return None


def _app_state_attrs_assigned(handler: ast.ExceptHandler) -> list[str]:
    """``app.state.<name>`` attribute targets assigned inside the handler.

    Targets of plain and annotated assignments whose attribute chain is
    exactly ``app.state.X`` (Attribute(attr=X, value=Attribute(attr='state',
    value=Name('app')))) are collected, via a full walk of each target so
    tuple-unpacked and nested targets are not missed.
    """
    attrs: list[str] = []
    for node in ast.walk(handler):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign) and node.target is not None:
            targets = [node.target]
        for target in targets:
            for part in ast.walk(target):
                if (
                    isinstance(part, ast.Attribute)
                    and isinstance(part.value, ast.Attribute)
                    and part.value.attr == "state"
                    and isinstance(part.value.value, ast.Name)
                    and part.value.value.id == "app"
                ):
                    attrs.append(part.attr)
    return attrs


def _pin_lifespan_settings(tmp_path: Path):
    """Pin every settings path/flag the real lifespan touches to a temp dir.

    Minimal local mirror of the proven recipe in
    tests/test_health_readiness_lifespan.py::_pin_lifespan_settings (same
    steps, trimmed comments): stale pools closed, wiki/draft event buses and
    the background-processor singleton reset, a SACRIFICIAL auth executor
    handed to the lifespan shutdown, and the settings singleton pinned (the
    chat endpoint pairs are additionally EMPTIED here so no LLM client is
    constructed — the check is about migrate_uploads/healthz, and client
    starts against dead endpoints only add latency and noise).
    """
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
    _background_tasks._processor_instance = None  # noqa: SLF001

    from app.services import auth_service as _auth_service

    saved_auth_executor = _auth_service._auth_executor
    _auth_service._auth_executor = ThreadPoolExecutor(
        max_workers=4, thread_name_prefix="auth-cpu"
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
        "ollama_chat_url": settings.ollama_chat_url,
        "chat_model": settings.chat_model,
        "instant_chat_url": settings.instant_chat_url,
        "instant_chat_model": settings.instant_chat_model,
    }
    settings.data_dir = tmp_path  # type: ignore[assignment]
    settings.redis_url = ""
    settings.users_enabled = False
    settings.admin_secret_token = "ac6-behav-token-0123456789abcdef0123"
    settings.jwt_secret_key = "ac6-behav-jwt-0123456789abcdef0123456789"
    settings.ollama_embedding_url = "http://127.0.0.1:9/v1/embeddings"
    settings.allow_local_services = True  # type: ignore[assignment]
    settings.ollama_chat_url = ""
    settings.chat_model = ""
    settings.instant_chat_url = ""
    settings.instant_chat_model = ""

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


class TestUploadMigrationFailureIsReadableByHealthz(unittest.TestCase):
    """AC6: recorded on app.state in the handler, read by /api/healthz."""

    def test_migration_handler_records_state_that_healthz_reads(self):
        tree = ast.parse(
            LIFESPAN_PATH.read_text(encoding="utf-8"),
            filename=str(LIFESPAN_PATH),
        )
        migrate_try = _find_migrate_uploads_try(tree)
        self.assertIsNotNone(
            migrate_try,
            "AC6-AST migrate-uploads-try=missing could not find any try "
            "block containing a migrate_uploads reference (import or "
            "to_thread call) in lifespan.py — the AC6 anchor is missing "
            "entirely, which cannot satisfy the criterion",
        )

        attrs: list[str] = []
        for handler in migrate_try.handlers:
            attrs.extend(_app_state_attrs_assigned(handler))

        if not attrs:
            self.fail(
                "AC6-AST recorded-attributes=none — the except handler "
                "around the lifespan migrate_uploads await records nothing "
                "on app.state (it only logs); a failed upload migration "
                "stays invisible to /api/healthz"
            )

        health_source = HEALTH_PATH.read_text(encoding="utf-8")
        read = all(attr in health_source for attr in attrs)
        self.assertTrue(
            read,
            f"AC6-AST healthz-read=False attrs={attrs} — the handler records "
            "app.state attributes that /api/healthz "
            "(backend/app/api/routes/health.py) never reads",
        )

    def test_healthz_503_when_upload_migration_fails(self):
        """Real lifespan + real healthz route; a failed upload migration
        must degrade readiness to 503 (it currently stays 200)."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.api.routes.health import router as health_router
        from app.lifespan import lifespan

        app = FastAPI(lifespan=lifespan)
        app.include_router(health_router, prefix="/api")

        root = logging.getLogger()
        saved_handlers = [(h, list(h.filters)) for h in root.handlers]
        tmp_dir = Path(tempfile.mkdtemp(prefix="ac6-behav-"))
        atexit.register(shutil.rmtree, tmp_dir, True)
        restore = _pin_lifespan_settings(tmp_dir)
        try:
            with patch(
                "app.services.upload_path.migrate_uploads",
                side_effect=RuntimeError("boom"),
            ):
                with TestClient(app) as client:
                    response = client.get("/api/healthz")
        finally:
            restore()
            root.handlers = [h for h, _ in saved_handlers]
            for handler, filters in saved_handlers:
                handler.filters = list(filters)

        status = response.status_code
        try:
            issues = response.json().get("issues")
        except ValueError:
            issues = "<unparseable body>"
        self.assertEqual(
            status,
            503,
            f"AC6-BEHAV healthz-status={status} expected-503 — a failed "
            f"upload migration (migrate_uploads patched to raise "
            f"RuntimeError) left /api/healthz reporting ready; issues={issues}",
        )

    def test_healthz_200_when_upload_migration_succeeds(self):
        """Success path (passes at base and after the fix): with the REAL
        migrate_uploads running unpatched on a fresh temp data dir (a
        no-op migration), readiness must stay 200."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.api.routes.health import router as health_router
        from app.lifespan import lifespan

        app = FastAPI(lifespan=lifespan)
        app.include_router(health_router, prefix="/api")

        root = logging.getLogger()
        saved_handlers = [(h, list(h.filters)) for h in root.handlers]
        tmp_dir = Path(tempfile.mkdtemp(prefix="ac6-success-"))
        atexit.register(shutil.rmtree, tmp_dir, True)
        restore = _pin_lifespan_settings(tmp_dir)
        try:
            with TestClient(app) as client:
                response = client.get("/api/healthz")
        finally:
            restore()
            root.handlers = [h for h, _ in saved_handlers]
            for handler, filters in saved_handlers:
                handler.filters = list(filters)

        status = response.status_code
        try:
            issues = response.json().get("issues")
        except ValueError:
            issues = "<unparseable body>"
        self.assertEqual(
            status,
            200,
            f"AC6-SUCCESS healthz-status={status} expected-200 — a "
            f"successful (no-op) upload migration must leave /api/healthz "
            f"reporting ready; issues={issues}",
        )

    def test_healthz_inert_when_upload_migration_state_absent(self):
        """Missing-state inertness (passes at base and after the fix): with
        ``upload_migration_ok`` ABSENT from app.state, healthz must not
        surface any upload-migration issue — the route's getattr default
        must be inert, so the fix cannot turn absence into degradation."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.api.routes.health import router as health_router
        from app.lifespan import lifespan

        app = FastAPI(lifespan=lifespan)
        app.include_router(health_router, prefix="/api")

        root = logging.getLogger()
        saved_handlers = [(h, list(h.filters)) for h in root.handlers]
        tmp_dir = Path(tempfile.mkdtemp(prefix="ac6-inert-"))
        atexit.register(shutil.rmtree, tmp_dir, True)
        restore = _pin_lifespan_settings(tmp_dir)
        try:
            with TestClient(app) as client:
                # Force the state the criterion names: the attribute ABSENT
                # (at base lifespan never sets it; delete defensively so the
                # leg keeps testing absence even after a fix that records a
                # success value).
                state_dict = getattr(app.state, "_state", None)
                if isinstance(state_dict, dict):
                    state_dict.pop("upload_migration_ok", None)
                elif hasattr(app.state, "upload_migration_ok"):
                    delattr(app.state, "upload_migration_ok")
                self.assertFalse(
                    hasattr(app.state, "upload_migration_ok"),
                    "AC6-INERT state-present — could not force the "
                    "upload_migration_ok-absent state",
                )
                response = client.get("/api/healthz")
        finally:
            restore()
            root.handlers = [h for h, _ in saved_handlers]
            for handler, filters in saved_handlers:
                handler.filters = list(filters)

        status = response.status_code
        try:
            body = response.json()
        except ValueError:
            body = {}
        surfaced = [
            str(item)
            for item in (body.get("issues") or []) + (body.get("warnings") or [])
            if "upload" in str(item).lower()
        ]
        self.assertEqual(
            status,
            200,
            f"AC6-INERT healthz-status={status} expected-200 — with "
            f"upload_migration_ok absent from app.state, healthz must stay "
            f"ready; issues={body.get('issues')}",
        )
        self.assertEqual(
            surfaced,
            [],
            f"AC6-INERT upload-issues-surfaced={surfaced} — with "
            "upload_migration_ok absent from app.state, healthz must not "
            "mention any upload-migration issue (inert getattr default)",
        )


if __name__ == "__main__":
    unittest.main()
