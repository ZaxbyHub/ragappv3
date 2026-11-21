"""Acceptance checks for collated #798 + #1179. Each member is RED at base
1aba3307 and GREEN after the fix.

Members (issue AC in parentheses):

- TestActiveUserCacheInvalidationRace (#798 AC2) — a cache read whose key is
  invalidated mid-read must not write its stale snapshot back into
  ``_ACTIVE_USER_CACHE``.
- TestMissingColumnLoudFailure (#798 AC3) — a ``users`` table missing the
  ``password_changed_at`` column must fail loudly instead of silently
  substituting 0.0.
- TestUsersPoolDoubleHold (#798 AC4) — users.py handlers must not open
  standalone pool connections alongside the request-scoped pooled one.
- TestChatProbeBudget (#1179 AC1) — ``check_chat`` must bound every probe
  call with an explicit ``max_tokens``.
- TestHotRebindHealthChecker (#1179 AC2) — late activation through settings
  must rewire ``app.state.llm_health_checker`` so thinking reports healthy
  without a restart.
- TestTrustedHostHealthExemption (#1179 AC3) — health paths must be exempt
  from host-header validation.
- TestAccessLogOnException (#1179 AC4) — uncaught exceptions must still get
  exactly one ``http_request`` access record.
"""

import ast
import logging
import os
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Settings validates on import; provide the secrets BEFORE any app.* import.
# (tests/conftest.py also sets these at configure time; the setdefaults keep
# this module's import order self-contained.)
os.environ.setdefault("ADMIN_SECRET_TOKEN", "test-admin-secret")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret")

from app.api import deps as app_deps
from app.api.deps import (
    _ACTIVE_USER_CACHE,
    _build_active_user_cache_key,
    get_current_active_user,
    invalidate_active_user_cache,
)
from app.api.routes import settings as settings_routes
from app.main import _TrustedHostWithHealthExempt
from app.middleware.logging import LoggingMiddleware
from app.services.llm_health import LLMHealthChecker


@pytest.fixture(autouse=True)
def _clear_active_user_cache_around_test():
    """Clear the module-level active-user cache before AND after each test.

    Mirrors the conftest reset for the cache the #798 AC2 check inspects, so
    no neighboring test's entries can pollute the assertion. A plain dict
    clear (never the threading.Lock across the yield boundary) — see the
    conftest fixture of the same purpose.
    """
    _ACTIVE_USER_CACHE.clear()
    yield
    _ACTIVE_USER_CACHE.clear()


# =============================================================================
# #798 AC2 — active-user cache mid-read invalidation race
# =============================================================================


class TestActiveUserCacheInvalidationRace:
    """Mid-read invalidation must prevent the stale snapshot write-back."""

    async def test_mid_read_invalidation_prevents_writeback(self):
        """A read invalidated while its DB fetch is in flight must not re-cache.

        The mock DB's execute side effect calls invalidate_active_user_cache
        BEFORE returning the row, so the invalidation lands mid-read; the
        write-back that follows must detect it and skip caching.
        """
        user_id = 123
        cache_key = _build_active_user_cache_key(user_id)
        assert cache_key not in _ACTIVE_USER_CACHE

        row = (123, "testuser", "Test User", "member", 1, 0, 100.0)
        cursor = MagicMock()
        cursor.fetchone.return_value = row

        def execute_side_effect(query, params=None):
            # The invalidation lands WHILE the read is in flight (#798 AC2):
            # anything the reader writes back afterwards is a stale snapshot.
            invalidate_active_user_cache(user_id)
            return cursor

        mock_db = MagicMock()
        mock_db.execute.side_effect = execute_side_effect

        with (
            patch("app.api.deps.settings") as mock_settings,
            patch("app.api.deps.decode_access_token") as mock_decode,
        ):
            mock_settings.users_enabled = True
            mock_settings.active_user_cache_ttl_seconds = 30
            mock_decode.return_value = {"sub": "123", "type": "access", "iat": 200}
            await get_current_active_user(
                request=MagicMock(),
                authorization="Bearer x.y.z",
                db=mock_db,
            )

        assert cache_key not in _ACTIVE_USER_CACHE, (
            "stale snapshot re-cached after mid-read invalidation"
        )


# =============================================================================
# #798 AC3 — missing password_changed_at column must fail loudly
# =============================================================================


class TestMissingColumnLoudFailure:
    """A schema missing password_changed_at must raise, not substitute 0.0."""

    def test_missing_password_changed_at_raises(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                "CREATE TABLE users ("
                "id INTEGER PRIMARY KEY, "
                "username TEXT, "
                "full_name TEXT, "
                "role TEXT, "
                "is_active INTEGER, "
                "must_change_password INTEGER)"
            )
            conn.execute("INSERT INTO users VALUES (1, 'u', 'U', 'member', 1, 0)")
            conn.commit()

            with pytest.raises(sqlite3.OperationalError):
                app_deps._fetch_user_row_with_pwc(conn, 1)
        finally:
            conn.close()


# =============================================================================
# #798 AC4 — users.py must not hold a second standalone pool connection
# =============================================================================


class TestUsersPoolDoubleHold:
    """users.py handlers must use the request-scoped pooled connection only."""

    def test_no_standalone_pool_connection_calls(self):
        users_py = (
            Path(__file__).resolve().parents[1] / "app" / "api" / "routes" / "users.py"
        )
        tree = ast.parse(users_py.read_text(encoding="utf-8"), filename=str(users_py))

        standalone_pool_calls = sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get_connection_async"
        )
        assert standalone_pool_calls == 0, (
            f"get_connection_async() calls remain in users.py: "
            f"{standalone_pool_calls} standalone pool checkout(s) found "
            f"(each handler already holds the Depends(get_db) pooled "
            f"connection for the whole request)"
        )


# =============================================================================
# #1179 AC1 — check_chat must bound its probe with max_tokens
# =============================================================================


class _RecordingChatClient:
    """Fake LLMClient whose chat_completion records the kwargs it receives."""

    def __init__(self):
        self.calls: list[dict] = []

    async def chat_completion(self, messages, **kwargs):
        self.calls.append(dict(kwargs))
        return {"ok": True}


class TestChatProbeBudget:
    """check_chat must send zero probe calls lacking an explicit budget."""

    async def test_check_chat_bounds_probe_max_tokens(self):
        fake = _RecordingChatClient()

        with patch("app.services.llm_health.settings") as mock_settings:
            mock_settings.ollama_chat_url = "http://test"
            mock_settings.chat_model = "test-model"
            checker = LLMHealthChecker(llm_client=fake)
            await checker.check_chat()

        unbounded = [c for c in fake.calls if "max_tokens" not in c]
        assert len(unbounded) == 0, (
            f"unbounded probe call(s) without max_tokens: {unbounded}"
        )


# =============================================================================
# #1179 AC2 — hot rebind must rewire the health checker on late activation
# =============================================================================


class _OkChatClient:
    """Fake LLMClient that accepts the probe call and reports success."""

    async def chat_completion(self, messages, **kw):
        return {"ok": True}


class TestHotRebindHealthChecker:
    """Late activation must make check_chat_modes report thinking healthy."""

    async def test_late_activation_reports_thinking_healthy(self):
        app = MagicMock()
        app.state.thinking_llm_client = None
        app.state.instant_llm_client = None
        app.state.llm_client = None
        app.state.rag_engine = None
        app.state.background_processor = None
        app.state.multimodal_client = None
        app.state.llm_health_checker = LLMHealthChecker()

        update = MagicMock()
        update.ollama_chat_url = "http://test"
        update.chat_model = "test-model"
        update.thinking_max_tokens = None
        update.chat_api_key = None
        update.instant_chat_url = None
        update.instant_chat_model = None
        update.instant_max_tokens = None
        update.instant_enable_thinking = None
        update.instant_api_key = None

        with (
            patch("app.api.routes.settings.settings") as mock_settings,
            patch(
                "app.services.llm_client.create_thinking_client",
                return_value=_OkChatClient(),
            ) as mock_create,
        ):
            mock_settings.ollama_chat_url = "http://test"
            mock_settings.chat_model = "test-model"
            mock_settings.thinking_max_tokens = 4096
            mock_settings.chat_api_key = ""
            settings_routes._hot_rebind_llm_clients(app, update)

        # Harness sanity: the late-activation branch ran and built a client.
        assert mock_create.called, (
            "harness sanity failed: late-activation branch never constructed "
            "a thinking client"
        )

        modes = await app.state.llm_health_checker.check_chat_modes()
        assert modes["thinking"] is True, (
            f"thinking mode still reports unhealthy after late activation: {modes}"
        )


# =============================================================================
# #1179 AC3 — health paths exempt from host validation
# =============================================================================


class TestTrustedHostHealthExemption:
    """Health paths must be served even with an unlisted Host header."""

    def test_health_paths_exempt_from_host_validation(self):
        app = FastAPI()

        @app.get("/health")
        def health():
            return {"ok": True}

        @app.get("/api/healthz")
        def api_healthz():
            return {"ok": True}

        wrapped = _TrustedHostWithHealthExempt(app, allowed_hosts=["example.com"])
        client = TestClient(wrapped, raise_server_exceptions=False)

        r1 = client.get("/health", headers={"Host": "localhost"})
        r2 = client.get("/api/healthz", headers={"Host": "localhost"})

        assert [r1.status_code, r2.status_code] == [200, 200]


# =============================================================================
# #1179 AC4 — access record for uncaught exceptions
# =============================================================================


class _HttpRecordCollector(logging.Handler):
    """Capture log records whose message is exactly ``http_request``."""

    def __init__(self):
        super().__init__(level=logging.INFO)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage() == "http_request":
            self.records.append(record)


class TestAccessLogOnException:
    """Uncaught exceptions must still emit exactly one http_request record."""

    def test_uncaught_exception_logs_http_request(self):
        # backend/app/middleware/logging.py uses logging.getLogger("http")
        # (a fixed name, NOT __name__) — attach to that exact logger.
        http_logger = logging.getLogger("http")
        saved_level = http_logger.level
        collector = _HttpRecordCollector()
        http_logger.addHandler(collector)
        http_logger.setLevel(logging.INFO)
        try:
            app = FastAPI()
            app.add_middleware(LoggingMiddleware)

            @app.get("/boom")
            def boom():
                raise RuntimeError("probe")

            client = TestClient(app, raise_server_exceptions=False)
            response = client.get("/boom")
        finally:
            http_logger.removeHandler(collector)
            http_logger.setLevel(saved_level)

        assert response.status_code == 500
        assert len(collector.records) == 1, (
            f"http_request records for uncaught exceptions: expected exactly "
            f"1, got {len(collector.records)}"
        )
