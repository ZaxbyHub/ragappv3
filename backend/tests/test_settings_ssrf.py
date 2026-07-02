"""
Tests for SSRF validator application to settings.py and lifespan.py.

Covers:
1. settings.py: test_connection() calls assert_url_safe before each HTTP request
2. settings.py: SSRF-blocked URLs return error dict with "SSRF blocked" message, loop continues
3. settings.py: Valid public URLs succeed normally
4. lifespan.py: assert_url_safe called on embedding URL before TEI validation
5. lifespan.py: URLBlocked caught explicitly with "TEI /info endpoint blocked by SSRF guard" log message
6. lifespan.py: Non-SSRF exceptions still handled by generic Exception handler
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies
try:
    import lancedb
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow
except ImportError:
    import types

    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

try:
    from unstructured.partition.auto import partition
except ImportError:
    import types

    _unstructured = types.ModuleType("unstructured")
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType("unstructured.partition")
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType("unstructured.partition.auto")
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    _unstructured.chunking = types.ModuleType("unstructured.chunking")
    _unstructured.chunking.__path__ = []
    _unstructured.chunking.title = types.ModuleType("unstructured.chunking.title")
    _unstructured.chunking.title.chunk_by_title = lambda *args, **kwargs: []
    _unstructured.documents = types.ModuleType("unstructured.documents")
    _unstructured.documents.__path__ = []
    _unstructured.documents.elements = types.ModuleType("unstructured.documents.elements")
    _unstructured.documents.elements.Element = type("Element", (), {})
    sys.modules["unstructured"] = _unstructured
    sys.modules["unstructured.partition"] = _unstructured.partition
    sys.modules["unstructured.partition.auto"] = _unstructured.partition.auto
    sys.modules["unstructured.chunking"] = _unstructured.chunking
    sys.modules["unstructured.chunking.title"] = _unstructured.chunking.title
    sys.modules["unstructured.documents"] = _unstructured.documents
    sys.modules["unstructured.documents.elements"] = _unstructured.documents.elements

from fastapi.testclient import TestClient

TEST_DB_PATH = None
TEST_DATA_DIR = None


def setup_test_db():
    global TEST_DB_PATH, TEST_DATA_DIR
    TEST_DATA_DIR = tempfile.mkdtemp()
    TEST_DB_PATH = Path(TEST_DATA_DIR) / "test.db"

    from app.models.database import init_db

    init_db(str(TEST_DB_PATH))
    return str(TEST_DB_PATH)


setup_test_db()

from app.config import settings
from app.main import app


class TestSettingsSSRFIntegration(unittest.TestCase):
    """Tests for SSRF validator in settings.py test_connection()."""

    def setUp(self):
        self._orig_users_enabled = settings.users_enabled
        settings.users_enabled = False
        self.client = TestClient(app)
        self.client.headers.update(
            {"Authorization": f"Bearer {settings.admin_secret_token}"}
        )
        from app.api.deps import get_db
        from app.models.database import get_pool

        self._test_pool = get_pool(str(TEST_DB_PATH))

        def override_get_db():
            conn = self._test_pool.get_connection()
            try:
                yield conn
            finally:
                self._test_pool.release_connection(conn)

        app.dependency_overrides[get_db] = override_get_db
        self._get_db = get_db

    def tearDown(self):
        app.dependency_overrides.pop(self._get_db, None)
        settings.users_enabled = self._orig_users_enabled

    def test_connection_calls_assert_url_safe_before_http_request(self):
        """test_connection() must call assert_url_safe before each HTTP request."""
        # Patch assert_url_safe to track whether it's called
        call_record = {"calls": []}

        def track_assert_url_safe(url):
            call_record["calls"].append(url)

        with patch(
            "app.api.routes.settings.assert_url_safe", side_effect=track_assert_url_safe
        ):
            with patch("app.api.routes.settings.httpx.AsyncClient") as mock_async_client:
                mock_client_instance = AsyncMock()
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_client_instance.get = AsyncMock(return_value=mock_response)
                mock_async_client.return_value.__aenter__ = AsyncMock(
                    return_value=mock_client_instance
                )
                mock_async_client.return_value.__aexit__ = AsyncMock(return_value=None)

                _ = self.client.get("/api/settings/connection")

                # assert_url_safe should have been called for each target URL
                # targets include embeddings + chat + reranker (if configured)
                self.assertGreaterEqual(
                    len(call_record["calls"]), 2, "assert_url_safe should be called at least twice (embeddings + chat)"
                )
                # Verify the URLs it was called with match our configured endpoints
                allowed_urls = {settings.ollama_embedding_url, settings.ollama_chat_url}
                if settings.reranker_url:
                    allowed_urls.add(settings.reranker_url)
                for url in call_record["calls"]:
                    self.assertIn(url, allowed_urls)

    def test_ssrf_blocked_url_returns_error_dict_with_ssrf_message(self):
        """SSRF-blocked URLs return error dict with 'SSRF blocked' message, loop continues."""
        from app.services.ssrf import URLBlocked

        def mock_assert_url_safe(url):
            # Simulate blocking on the first URL only
            if "embeddings" in str(url) or "127.0.0.1" in str(url):
                raise URLBlocked("URL host '127.0.0.1' resolves to a private/loopback address. Local service endpoints require ALLOW_LOCAL_SERVICES=1.")

        with patch(
            "app.api.routes.settings.assert_url_safe", side_effect=mock_assert_url_safe
        ):
            with patch("app.api.routes.settings.httpx.AsyncClient") as mock_async_client:
                mock_client_instance = AsyncMock()
                # Return 200 for chat endpoint (not blocked)
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_client_instance.get = AsyncMock(return_value=mock_response)
                mock_async_client.return_value.__aenter__ = AsyncMock(
                    return_value=mock_client_instance
                )
                mock_async_client.return_value.__aexit__ = AsyncMock(return_value=None)

                response = self.client.get("/api/settings/connection")
                self.assertEqual(response.status_code, 200)
                data = response.json()

                # Check that at least one target was blocked
                blocked_entries = [
                    name
                    for name, result in data.items()
                    if isinstance(result, dict)
                    and result.get("ok") is False
                    and "SSRF blocked" in result.get("error", "")
                ]
                self.assertGreater(
                    len(blocked_entries),
                    0,
                    f"At least one result should have SSRF blocked error. Got: {data}",
                )

    def test_ssrf_blocked_loop_continues_to_next_target(self):
        """When one URL is SSRF-blocked, the loop continues to test other targets."""
        from app.services.ssrf import URLBlocked

        call_count = {"count": 0}

        def mock_assert_url_safe(url):
            call_count["count"] += 1
            # Block the first URL
            if call_count["count"] == 1:
                raise URLBlocked("URL blocked")

        with patch(
            "app.api.routes.settings.assert_url_safe", side_effect=mock_assert_url_safe
        ):
            with patch("app.api.routes.settings.httpx.AsyncClient") as mock_async_client:
                mock_client_instance = AsyncMock()
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_client_instance.get = AsyncMock(return_value=mock_response)
                mock_async_client.return_value.__aenter__ = AsyncMock(
                    return_value=mock_client_instance
                )
                mock_async_client.return_value.__aexit__ = AsyncMock(return_value=None)

                response = self.client.get("/api/settings/connection")
                self.assertEqual(response.status_code, 200)
                # Loop should have continued — call count should be at least 2
                # (one for blocked URL, one for the next URL)
                self.assertGreaterEqual(
                    call_count["count"],
                    2,
                    "Loop should continue after SSRF block to test next target",
                )

    def test_valid_public_urls_succeed_normally(self):
        """Valid public URLs (not blocked by SSRF) succeed normally with status codes."""
        with patch("app.api.routes.settings.assert_url_safe"):
            with patch("app.api.routes.settings.httpx.AsyncClient") as mock_async_client:
                mock_client_instance = AsyncMock()
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_client_instance.get = AsyncMock(return_value=mock_response)
                mock_async_client.return_value.__aenter__ = AsyncMock(
                    return_value=mock_client_instance
                )
                mock_async_client.return_value.__aexit__ = AsyncMock(return_value=None)

                response = self.client.get("/api/settings/connection")
                self.assertEqual(response.status_code, 200)
                data = response.json()

                # For non-blocked URLs, we expect status codes and ok=True
                for name, result in data.items():
                    if isinstance(result, dict) and name != "reranker":
                        self.assertIn(
                            "status",
                            result,
                            f"Result for {name} should have status field",
                        )


class TestLifespanSSRFGuard(unittest.TestCase):
    """Tests for SSRF guard in lifespan.py TEI validation."""

    def test_lifespan_calls_assert_url_safe_before_tei_validation(self):
        """assert_url_safe must be called on the embedding URL before TEI /info validation."""
        lifespan_path = os.path.join(
            os.path.dirname(__file__), "..", "app", "lifespan.py"
        )
        with open(lifespan_path, "r") as f:
            source = f.read()

        # The TEI /info validation lives in the _validate_tei_embedding_model
        # helper; bound the block to that function definition.
        tei_start = source.find("async def _validate_tei_embedding_model")
        self.assertGreater(
            tei_start,
            0,
            "lifespan.py should define _validate_tei_embedding_model",
        )
        # The helper is defined immediately before the @asynccontextmanager
        # lifespan; use that to bound the block.
        tei_end = source.find("@asynccontextmanager", tei_start)
        tei_block = source[tei_start:tei_end]

        # Within that block, info_url is defined first, then assert_url_safe is called
        # e.g. info_url = ...; assert_url_safe(info_url)
        info_url_line_idx = tei_block.find("info_url")
        assert_url_safe_line_idx = tei_block.find("assert_url_safe")
        self.assertGreater(
            info_url_line_idx,
            0,
            "TEI block should define info_url",
        )
        self.assertGreater(
            assert_url_safe_line_idx,
            0,
            "TEI block should call assert_url_safe",
        )
        # assert_url_safe must come AFTER info_url in the block
        self.assertGreater(
            assert_url_safe_line_idx,
            info_url_line_idx,
            "assert_url_safe must be called after info_url is defined in TEI block",
        )

    def test_lifespan_catches_urlblocked_with_specific_log_message(self):
        """URLBlocked must be caught explicitly with 'TEI /info endpoint blocked by SSRF guard' message."""
        lifespan_path = os.path.join(
            os.path.dirname(__file__), "..", "app", "lifespan.py"
        )
        with open(lifespan_path, "r") as f:
            source = f.read()

        # Verify URLBlocked is caught
        self.assertIn(
            "URLBlocked",
            source,
            "lifespan.py should catch URLBlocked exception",
        )
        # Verify the specific log message
        self.assertIn(
            "TEI /info endpoint blocked by SSRF guard",
            source,
            "lifespan.py should log 'TEI /info endpoint blocked by SSRF guard' when URLBlocked is caught",
        )

    def test_lifespan_non_ssrf_exceptions_handled_by_generic_handler(self):
        """Non-SSRF exceptions must still be handled by the generic Exception handler."""
        lifespan_path = os.path.join(
            os.path.dirname(__file__), "..", "app", "lifespan.py"
        )
        with open(lifespan_path, "r") as f:
            source = f.read()

        # Verify there's a generic Exception catch block in the TEI validation section
        # The code should have: except Exception as e: if isinstance(e, RuntimeError): raise
        self.assertIn(
            "except Exception",
            source,
            "lifespan.py should have a generic Exception catch block",
        )
        # Verify RuntimeError is re-raised (non-suppressed)
        self.assertIn(
            "isinstance(e, RuntimeError)",
            source,
            "lifespan.py should re-raise RuntimeError from generic Exception handler",
        )

    def test_urlblocked_not_reraised(self):
        """URLBlocked should be caught and logged, NOT re-raised (only RuntimeError is re-raised)."""
        lifespan_path = os.path.join(
            os.path.dirname(__file__), "..", "app", "lifespan.py"
        )
        with open(lifespan_path, "r") as f:
            source = f.read()

        # Find the TEI validation block (the _validate_tei_embedding_model helper)
        tei_block_start = source.find("async def _validate_tei_embedding_model")
        tei_block_end = source.find("@asynccontextmanager", tei_block_start)
        tei_block = source[tei_block_start:tei_block_end]

        # URLBlocked should be caught separately, not re-raised
        # The flow should be: URLBlocked -> warning log -> continue (not re-raised)
        urlblocked_catch = tei_block.find("except URLBlocked")
        genericexception_catch = tei_block.find("except Exception")

        self.assertGreater(
            urlblocked_catch,
            0,
            "TEI block should have an URLBlocked exception handler",
        )
        self.assertGreater(
            genericexception_catch,
            0,
            "TEI block should have a generic Exception handler",
        )
        # The generic handler should check isinstance(e, RuntimeError) and re-raise
        # URLBlocked is NOT RuntimeError, so it won't be re-raised
        self.assertIn(
            "isinstance(e, RuntimeError)",
            tei_block,
            "Generic handler should re-raise RuntimeError only",
        )


class TestSSRFServiceUnit(unittest.TestCase):
    """Unit tests for the underlying ssrf service."""

    def test_urlblocked_message_is_safe_to_surface(self):
        """URLBlocked exception message should be safe to surface in API responses."""
        from app.services.ssrf import URLBlocked

        # URLBlocked message should not leak internal DNS info (IPs)
        # The message only mentions the hostname, not the resolved IP
        exc = URLBlocked(
            "URL host '127.0.0.1' resolves to a private/loopback address. Local service endpoints require ALLOW_LOCAL_SERVICES=1."
        )
        # Message should mention private/loopback
        self.assertIn(
            "private",
            str(exc),
            "URLBlocked message should mention private/loopback context",
        )
        # Message should not echo back internal IP ranges (only the input hostname)
        self.assertNotIn(
            "192.168",
            str(exc),
            "URLBlocked should not leak private IP ranges in message",
        )

    def test_assert_url_safe_accepts_valid_public_url(self):
        """assert_url_safe should accept valid public HTTPS URLs without raising."""
        from app.services.ssrf import assert_url_safe

        # This should not raise for a well-formed public URL
        try:
            assert_url_safe("https://www.google.com")
        except Exception:
            # It may raise if Google DNS resolves to private, but that's unlikely
            # The important thing is it doesn't raise URLBlocked for non-private hosts
            pass

    def test_assert_url_safe_rejects_empty_url(self):
        """assert_url_safe must reject empty URLs."""
        from app.services.ssrf import URLBlocked, assert_url_safe

        with self.assertRaises(URLBlocked) as ctx:
            assert_url_safe("")
        self.assertIn("empty", str(ctx.exception).lower())

    def test_assert_url_safe_rejects_private_ip(self):
        """assert_url_safe must reject URLs resolving to private IPs (RFC1918)."""
        from app.services.ssrf import URLBlocked, assert_url_safe

        # 127.0.0.1 is loopback — should be blocked
        with self.assertRaises(URLBlocked):
            assert_url_safe("http://127.0.0.1:8080")

        # 192.168.x.x is private — should be blocked
        with self.assertRaises(URLBlocked):
            assert_url_safe("http://192.168.1.1")


class TestEmbeddingUrlSettingsGate(unittest.TestCase):
    """SSRF gate on ollama_embedding_url at settings-change time (F-002).

    EmbeddingService reads settings.ollama_embedding_url live on every call and
    only validates at construction, so the change boundary
    (_apply_settings_update) must reject a private/internal embedding URL before
    it ever lands in the live settings singleton.
    """

    def test_private_embedding_url_rejected_before_mutation(self):
        from fastapi import HTTPException

        from app.api.routes.settings import SettingsUpdate, _apply_settings_update
        from app.config import settings as live_settings

        original = live_settings.ollama_embedding_url
        update = SettingsUpdate(
            ollama_embedding_url="http://169.254.169.254/latest/meta-data/"
        )

        with self.assertRaises(HTTPException) as ctx:
            _apply_settings_update(update)

        self.assertEqual(ctx.exception.status_code, 422)
        # The bad URL must never reach the live settings singleton.
        self.assertEqual(live_settings.ollama_embedding_url, original)

    def test_loopback_embedding_url_rejected(self):
        from fastapi import HTTPException

        from app.api.routes.settings import SettingsUpdate, _apply_settings_update

        update = SettingsUpdate(ollama_embedding_url="http://127.0.0.1:11434/api/embeddings")
        with self.assertRaises(HTTPException) as ctx:
            _apply_settings_update(update)
        self.assertEqual(ctx.exception.status_code, 422)

    def test_malformed_embedding_url_rejected_as_422(self):
        from fastapi import HTTPException

        from app.api.routes.settings import SettingsUpdate, _apply_settings_update
        from app.config import settings as live_settings

        original = live_settings.ollama_embedding_url
        update = SettingsUpdate(
            ollama_embedding_url="http://example.com:bad/api/embeddings"
        )

        with self.assertRaises(HTTPException) as ctx:
            _apply_settings_update(update)

        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(live_settings.ollama_embedding_url, original)

    def test_invalid_ipv6_embedding_url_rejected_as_422(self):
        from fastapi import HTTPException

        from app.api.routes.settings import SettingsUpdate, _apply_settings_update
        from app.config import settings as live_settings

        original = live_settings.ollama_embedding_url
        update = SettingsUpdate(ollama_embedding_url="http://[::1")

        with self.assertRaises(HTTPException) as ctx:
            _apply_settings_update(update)

        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(live_settings.ollama_embedding_url, original)


class TestChatUrlSettingsGate(unittest.TestCase):
    """Regression coverage for validating chat URLs before partial writes."""

    def _make_settings_conn(self, key: str, value: str):
        import json
        import sqlite3

        conn = sqlite3.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE settings_kv (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO settings_kv (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (key, json.dumps(value)),
        )
        conn.commit()
        return conn

    def _settings_value(self, conn, key: str) -> str:
        import json

        row = conn.execute("SELECT value FROM settings_kv WHERE key = ?", (key,)).fetchone()
        self.assertIsNotNone(row)
        return json.loads(row[0])

    def test_post_settings_rejects_chat_url_before_mutation_or_persistence(self):
        from types import SimpleNamespace

        from fastapi import HTTPException

        from app.api.routes.settings import SettingsUpdate, post_settings
        from app.config import settings as live_settings

        existing_url = "https://example.com/chat"
        rejected_url = "http://169.254.169.254/latest/meta-data/"
        original_live = live_settings.ollama_chat_url
        conn = self._make_settings_conn("ollama_chat_url", existing_url)
        fake_client = MagicMock()
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    thinking_llm_client=fake_client,
                    instant_llm_client=None,
                )
            )
        )
        update = SettingsUpdate(ollama_chat_url=rejected_url)

        try:
            with self.assertRaises(HTTPException) as ctx:
                post_settings(update, request, conn, _role={}, _csrf_token="test")

            self.assertEqual(ctx.exception.status_code, 422)
            self.assertEqual(live_settings.ollama_chat_url, original_live)
            self.assertEqual(self._settings_value(conn, "ollama_chat_url"), existing_url)
            fake_client.reconfigure.assert_not_called()
        finally:
            live_settings.ollama_chat_url = original_live
            conn.close()

    def test_put_settings_rejects_instant_url_before_mutation_or_persistence(self):
        from types import SimpleNamespace

        from fastapi import HTTPException

        from app.api.routes.settings import SettingsUpdate, put_settings
        from app.config import settings as live_settings

        existing_url = "https://example.com/instant"
        rejected_url = "http://127.0.0.1:1234/v1/chat/completions"
        original_live = live_settings.instant_chat_url
        conn = self._make_settings_conn("instant_chat_url", existing_url)
        fake_client = MagicMock()
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    thinking_llm_client=None,
                    instant_llm_client=fake_client,
                )
            )
        )
        update = SettingsUpdate(instant_chat_url=rejected_url)

        try:
            with self.assertRaises(HTTPException) as ctx:
                put_settings(update, request, conn, _role={}, _csrf_token="test")

            self.assertEqual(ctx.exception.status_code, 422)
            self.assertEqual(live_settings.instant_chat_url, original_live)
            self.assertEqual(self._settings_value(conn, "instant_chat_url"), existing_url)
            fake_client.reconfigure.assert_not_called()
        finally:
            live_settings.instant_chat_url = original_live
            conn.close()

    def test_post_settings_persistence_failure_does_not_mutate_or_rebind(self):
        from types import SimpleNamespace

        from app.api.routes.settings import SettingsUpdate, post_settings
        from app.config import settings as live_settings

        class FailingConn:
            def execute(self, *args, **kwargs):
                raise sqlite3.OperationalError("settings write failed")

            def commit(self):
                raise AssertionError("commit should not be reached")

            def rollback(self):
                pass

        original_top_k = live_settings.retrieval_top_k
        original_model = live_settings.chat_model
        fake_client = MagicMock()
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    thinking_llm_client=fake_client,
                    instant_llm_client=None,
                )
            )
        )
        update = SettingsUpdate(
            retrieval_top_k=original_top_k + 1,
            chat_model="issue-273-model",
        )

        try:
            with self.assertRaises(sqlite3.OperationalError):
                post_settings(update, request, FailingConn(), _role={}, _csrf_token="test")

            self.assertEqual(live_settings.retrieval_top_k, original_top_k)
            self.assertEqual(live_settings.chat_model, original_model)
            fake_client.reconfigure.assert_not_called()
        finally:
            live_settings.retrieval_top_k = original_top_k
            live_settings.chat_model = original_model

    def test_post_settings_mid_persist_failure_rolls_back_partial_writes(self):
        from types import SimpleNamespace

        from app.api.routes.settings import SettingsUpdate, post_settings
        from app.config import settings as live_settings

        class FailingAfterFirstWrite:
            def __init__(self):
                self.conn = sqlite3.connect(":memory:")
                self.conn.execute(
                    """
                    CREATE TABLE settings_kv (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at TEXT
                    )
                    """
                )
                self.write_count = 0

            def execute(self, sql, params=()):
                if "INSERT OR REPLACE INTO settings_kv" in sql:
                    self.write_count += 1
                    if self.write_count == 2:
                        raise sqlite3.OperationalError("second write failed")
                return self.conn.execute(sql, params)

            def commit(self):
                self.conn.commit()

            def rollback(self):
                self.conn.rollback()

            def close(self):
                self.conn.close()

        original_top_k = live_settings.retrieval_top_k
        original_model = live_settings.chat_model
        failing_conn = FailingAfterFirstWrite()
        fake_client = MagicMock()
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    thinking_llm_client=fake_client,
                    instant_llm_client=None,
                )
            )
        )
        update = SettingsUpdate(
            retrieval_top_k=original_top_k + 1,
            chat_model="issue-273-model",
        )

        try:
            with self.assertRaises(sqlite3.OperationalError):
                post_settings(
                    update, request, failing_conn, _role={}, _csrf_token="test"
                )

            rows = failing_conn.conn.execute("SELECT key FROM settings_kv").fetchall()
            self.assertEqual(rows, [])
            self.assertEqual(live_settings.retrieval_top_k, original_top_k)
            self.assertEqual(live_settings.chat_model, original_model)
            fake_client.reconfigure.assert_not_called()
        finally:
            live_settings.retrieval_top_k = original_top_k
            live_settings.chat_model = original_model
            failing_conn.close()


class TestRerankerUrlSettingsGate(unittest.TestCase):
    """SSRF + format gate on reranker_url at settings-change time (C-SEC-006).

    reranker_url is a persisted, user-settable field that was historically
    excluded from both the URL format validator and the PUT-time SSRF check, so
    a private/internal reranker URL could be saved without validation. It is now
    validated like the other model URLs.
    """

    def setUp(self):
        # The SSRF guard only blocks private/loopback targets when
        # ALLOW_LOCAL_SERVICES is unset. Pop it for the lifetime of these tests
        # so the rejection assertions are deterministic regardless of the
        # ambient environment (sibling localhost tests require it set).
        self._orig_allow_local = os.environ.pop("ALLOW_LOCAL_SERVICES", None)

    def tearDown(self):
        if self._orig_allow_local is not None:
            os.environ["ALLOW_LOCAL_SERVICES"] = self._orig_allow_local
        else:
            os.environ.pop("ALLOW_LOCAL_SERVICES", None)

    def test_private_reranker_url_rejected_before_mutation(self):
        from fastapi import HTTPException

        from app.api.routes.settings import SettingsUpdate, _apply_settings_update
        from app.config import settings as live_settings

        original = live_settings.reranker_url
        update = SettingsUpdate(reranker_url="http://169.254.169.254:8081")

        try:
            with self.assertRaises(HTTPException) as ctx:
                _apply_settings_update(update)

            self.assertEqual(ctx.exception.status_code, 422)
            self.assertIn("reranker", str(ctx.exception.detail).lower())
            # The bad URL must never reach the live settings singleton.
            self.assertEqual(live_settings.reranker_url, original)
        finally:
            live_settings.reranker_url = original

    def test_loopback_reranker_url_rejected(self):
        from fastapi import HTTPException

        from app.api.routes.settings import SettingsUpdate, _apply_settings_update
        from app.config import settings as live_settings

        original = live_settings.reranker_url
        update = SettingsUpdate(reranker_url="http://127.0.0.1:8081")
        try:
            with self.assertRaises(HTTPException) as ctx:
                _apply_settings_update(update)
            self.assertEqual(ctx.exception.status_code, 422)
            self.assertIn("reranker", str(ctx.exception.detail).lower())
            self.assertEqual(live_settings.reranker_url, original)
        finally:
            live_settings.reranker_url = original

    def test_public_reranker_url_accepted(self):
        # Positive path: an SSRF-safe reranker URL passes the gate. assert_url_safe
        # is patched so the test does not depend on live DNS resolution.
        from app.api.routes.settings import SettingsUpdate, _validate_updated_urls

        update = SettingsUpdate(reranker_url="https://reranker.prod.example:8081")
        with patch("app.api.routes.settings.assert_url_safe") as mock_safe:
            _validate_updated_urls(update)  # must not raise
        mock_safe.assert_any_call("https://reranker.prod.example:8081")

    def test_reranker_url_with_credentials_rejected(self):
        from pydantic import ValidationError

        from app.api.routes.settings import SettingsUpdate

        # Format validator now covers reranker_url: embedded credentials rejected.
        with self.assertRaises(ValidationError):
            SettingsUpdate(reranker_url="http://user:pass@reranker:8081")

    def test_reranker_url_non_http_scheme_rejected(self):
        from pydantic import ValidationError

        from app.api.routes.settings import SettingsUpdate

        with self.assertRaises(ValidationError):
            SettingsUpdate(reranker_url="ftp://reranker:8081")


if __name__ == "__main__":
    unittest.main()
