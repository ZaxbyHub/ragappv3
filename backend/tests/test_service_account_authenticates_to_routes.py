"""
Regression tests for FR-004 / MEDIUM B5-001: Wire require_service_account
to document-read and search routes.

Tests that service-account Bearer keys (sak_*) can authenticate to the
wired routes when the SA has the required scope, and are rejected otherwise.

Covers:
- SA with documents:read scope authenticates to:
  * GET  /api/documents/{file_id}             → 200
  * POST /api/search                          → 200
  * GET  /api/documents                       → 200
  * GET  /api/documents/{file_id}/status     → 200
  * GET  /api/documents/{file_id}/raw        → (file-not-on-disk; verifies SA auth accepted)
  * GET  /api/documents/stats                → 200
  * GET  /api/search/chunks/{chunk_id}/context → 200
  * GET  /api/tags/documents/{file_id}      → 200
- SA with only documents:write scope is rejected on read routes           → 403
- Missing Authorization header                                        → 401
"""

import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# Add parent directory to path for imports
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

from _db_pool import SimpleConnectionPool
from fastapi.testclient import TestClient

from app.api.deps import (
    get_background_processor,
    get_db,
    get_db_pool,
    get_embedding_service,
    get_secret_manager,
    get_vector_store,
)
from app.config import settings
from app.main import app
from app.models.database import _pool_cache, _pool_cache_lock, init_db, run_migrations

# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------


class TestSAClientAuthentication(unittest.TestCase):
    """Regression tests for SA authentication on document-read and search routes."""

    def setUp(self):
        """Set up test client with temporary database and SA fixtures."""
        # Set JWT_SECRET_KEY BEFORE importing settings
        self._original_jwt_env = os.environ.get("JWT_SECRET_KEY")
        os.environ["JWT_SECRET_KEY"] = "test-sa-auth-regression-key-32chars!"

        self._temp_dir = tempfile.mkdtemp()
        self._db_path = str(Path(self._temp_dir) / "app.db")

        # Store original settings
        self._original_jwt_secret = settings.jwt_secret_key
        self._original_users_enabled = settings.users_enabled
        self._original_data_dir = settings.data_dir

        # Override settings
        settings.users_enabled = True
        settings.data_dir = Path(self._temp_dir)
        settings.jwt_secret_key = os.environ["JWT_SECRET_KEY"]

        # Clear pool cache
        with _pool_cache_lock:
            for path, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()

        # Initialize database
        init_db(self._db_path)
        run_migrations(self._db_path)
        self._connection_pool = SimpleConnectionPool(self._db_path)

        def override_get_db():
            conn = self._connection_pool.get_connection()
            try:
                yield conn
            finally:
                self._connection_pool.release_connection(conn)

        # Mock vector store and embedding service
        self._mock_vector_store = MagicMock()
        self._mock_vector_store.db = None
        self._mock_vector_store.delete_by_file = AsyncMock(return_value=1)
        self._mock_vector_store.init_table = AsyncMock()
        self._mock_vector_store.search = AsyncMock(return_value=[])
        # Mock get_chunks_by_uid for chunk-context endpoint.
        # Chunk needs: metadata (dict-like), record (dict-like), and _record_get
        # support (file_id, vault_id as attrs or dict keys).
        _mock_chunk = MagicMock()
        _mock_chunk.metadata = {"source_file": "sa_test_doc.txt", "file_id": 1, "vault_id": 1, "parent_window_text": "test context"}
        _mock_chunk.record = {"file_id": 1, "vault_id": 1}
        # Also set file_id and vault_id as attrs so _record_get sees them
        _mock_chunk.file_id = 1
        _mock_chunk.vault_id = 1
        self._mock_vector_store.get_chunks_by_uid = AsyncMock(return_value=[_mock_chunk])

        self._mock_embedding_service = MagicMock()
        self._mock_embedding_service.embed_single = AsyncMock(return_value=[0.0] * 384)

        # Mock db pool
        self._mock_db_pool = self._connection_pool

        # Mock background processor
        self._mock_background_processor = MagicMock()
        self._mock_background_processor.is_running = True
        self._mock_background_processor.enqueue = AsyncMock()

        # Apply dependency overrides
        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_vector_store] = lambda: self._mock_vector_store
        app.dependency_overrides[get_embedding_service] = lambda: self._mock_embedding_service
        app.dependency_overrides[get_db_pool] = lambda: self._mock_db_pool
        app.dependency_overrides[get_background_processor] = lambda: self._mock_background_processor
        _mock_sm = MagicMock()
        _mock_sm.get_hmac_key.return_value = (b"test-hmac-key-32bytes-padding!!", "v1")
        app.dependency_overrides[get_secret_manager] = lambda: _mock_sm

        # Seed database: superadmin user and a vault with a document
        conn = self._connection_pool.get_connection()
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("DELETE FROM files")
            conn.execute("DELETE FROM vault_members")
            conn.execute("DELETE FROM users WHERE id != 0")
            conn.execute("DELETE FROM service_accounts")

            pw = "test-password-hash"
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
                (1, "superadmin", pw, "Super Admin", "superadmin"),
            )
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (?, ?, ?)",
                (1, "Test Vault", "A test vault"),
            )
            # Document in vault 1
            conn.execute(
                "INSERT OR IGNORE INTO files (id, file_name, file_path, file_size, status, chunk_count, vault_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (1, "sa_test_doc.txt", "/uploads/sa_test_doc.txt", 100, "indexed", 0, 1),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        self.client = TestClient(app)
        self.client.headers["user-agent"] = ""

        # Create service accounts directly in DB (hash the raw key as production does)
        self._sa_read_key = "sak_test_sa_read_key_1234567890123456"
        self._sa_read_key_hash = hashlib.sha256(self._sa_read_key.encode()).hexdigest()
        self._sa_write_key = "sak_test_sa_write_key_123456789012345678"
        self._sa_write_key_hash = hashlib.sha256(self._sa_write_key.encode()).hexdigest()

        conn = self._connection_pool.get_connection()
        try:
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO service_accounts (name, key_hash, scopes, created_at) VALUES (?, ?, ?, ?)",
                ("SA-Read-Only", self._sa_read_key_hash, "documents:read", now),
            )
            conn.execute(
                "INSERT INTO service_accounts (name, key_hash, scopes, created_at) VALUES (?, ?, ?, ?)",
                ("SA-Write-Only", self._sa_write_key_hash, "documents:write", now),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def tearDown(self):
        """Clean up after each test."""
        # Clear pool cache
        with _pool_cache_lock:
            for path, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()

        # Restore settings
        settings.jwt_secret_key = self._original_jwt_secret
        settings.users_enabled = self._original_users_enabled
        settings.data_dir = self._original_data_dir

        # Remove dependency overrides
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_vector_store, None)
        app.dependency_overrides.pop(get_embedding_service, None)
        app.dependency_overrides.pop(get_db_pool, None)
        app.dependency_overrides.pop(get_background_processor, None)
        app.dependency_overrides.pop(get_secret_manager, None)

        self.client.close()
        self._connection_pool.close_all()
        shutil.rmtree(self._temp_dir, ignore_errors=True)

        # Restore env var to its original state so other test classes
        # relying on module-level os.environ["JWT_SECRET_KEY"] are not
        # poisoned by the deletion.
        if self._original_jwt_env is not None:
            os.environ["JWT_SECRET_KEY"] = self._original_jwt_env
        else:
            os.environ.pop("JWT_SECRET_KEY", None)

    # -------------------------------------------------------------------------
    # Test: GET /api/documents/{file_id}
    # -------------------------------------------------------------------------

    def test_get_document_with_sa_read_key_returns_200(self):
        """SA with documents:read scope can fetch a document → 200."""
        response = self.client.get(
            "/api/documents/1",
            headers={"Authorization": f"Bearer {self._sa_read_key}"},
        )
        if response.status_code != 200:
            raise AssertionError(
                f"Expected 200 for SA with documents:read on GET /api/documents/1, "
                f"got {response.status_code}: {response.json()}"
            )
        data = response.json()
        if data.get("file_name") != "sa_test_doc.txt":
            raise AssertionError(
                f"Expected file_name 'sa_test_doc.txt', got {data.get('file_name')}"
            )

    def test_get_document_with_sa_write_key_returns_403(self):
        """SA with only documents:write scope cannot fetch a document → 403."""
        response = self.client.get(
            "/api/documents/1",
            headers={"Authorization": f"Bearer {self._sa_write_key}"},
        )
        if response.status_code != 403:
            raise AssertionError(
                f"Expected 403 for SA with documents:write on GET /api/documents/1, "
                f"got {response.status_code}: {response.json()}"
            )

    def test_get_document_without_auth_returns_401(self):
        """Request without Authorization header → 401."""
        response = self.client.get("/api/documents/1")
        if response.status_code != 401:
            raise AssertionError(
                f"Expected 401 for unauthenticated GET /api/documents/1, "
                f"got {response.status_code}: {response.json()}"
            )

    # -------------------------------------------------------------------------
    # Test: POST /api/search
    # -------------------------------------------------------------------------

    def test_search_with_sa_read_key_returns_200(self):
        """SA with documents:read scope can search → 200."""
        response = self.client.post(
            "/api/search",
            json={"query": "test query", "limit": 10},
            headers={"Authorization": f"Bearer {self._sa_read_key}"},
        )
        if response.status_code != 200:
            raise AssertionError(
                f"Expected 200 for SA with documents:read on POST /api/search, "
                f"got {response.status_code}: {response.json()}"
            )

    def test_search_with_sa_write_key_returns_403(self):
        """SA with only documents:write scope cannot search → 403."""
        response = self.client.post(
            "/api/search",
            json={"query": "test query", "limit": 10},
            headers={"Authorization": f"Bearer {self._sa_write_key}"},
        )
        if response.status_code != 403:
            raise AssertionError(
                f"Expected 403 for SA with documents:write on POST /api/search, "
                f"got {response.status_code}: {response.json()}"
            )

    def test_search_without_auth_returns_401(self):
        """Request without Authorization header → 401."""
        response = self.client.post(
            "/api/search",
            json={"query": "test query", "limit": 10},
        )
        if response.status_code != 401:
            raise AssertionError(
                f"Expected 401 for unauthenticated POST /api/search, "
                f"got {response.status_code}: {response.json()}"
            )

    # -------------------------------------------------------------------------
    # Test: GET /api/documents (list_documents)
    # -------------------------------------------------------------------------

    def test_list_documents_with_sa_read_key_returns_200(self):
        """SA with documents:read scope can list documents → 200."""
        response = self.client.get(
            "/api/documents?page=1&per_page=10",
            headers={"Authorization": f"Bearer {self._sa_read_key}"},
        )
        if response.status_code != 200:
            raise AssertionError(
                f"Expected 200 for SA with documents:read on GET /api/documents, "
                f"got {response.status_code}: {response.json()}"
            )
        data = response.json()
        if "documents" not in data:
            raise AssertionError(
                f"Expected 'documents' key in response, got: {data}"
            )

    def test_list_documents_with_sa_write_key_returns_403(self):
        """SA with only documents:write scope cannot list documents → 403."""
        response = self.client.get(
            "/api/documents?page=1&per_page=10",
            headers={"Authorization": f"Bearer {self._sa_write_key}"},
        )
        if response.status_code != 403:
            raise AssertionError(
                f"Expected 403 for SA with documents:write on GET /api/documents, "
                f"got {response.status_code}: {response.json()}"
            )

    def test_list_documents_without_auth_returns_401(self):
        """Request without Authorization header → 401."""
        response = self.client.get("/api/documents?page=1&per_page=10")
        if response.status_code != 401:
            raise AssertionError(
                f"Expected 401 for unauthenticated GET /api/documents, "
                f"got {response.status_code}: {response.json()}"
            )

    # -------------------------------------------------------------------------
    # Test: GET /api/documents/{file_id}/status
    # -------------------------------------------------------------------------

    def test_document_status_with_sa_read_key_returns_200(self):
        """SA with documents:read scope can fetch document status → 200."""
        response = self.client.get(
            "/api/documents/1/status",
            headers={"Authorization": f"Bearer {self._sa_read_key}"},
        )
        if response.status_code != 200:
            raise AssertionError(
                f"Expected 200 for SA with documents:read on GET /api/documents/1/status, "
                f"got {response.status_code}: {response.json()}"
            )

    def test_document_status_with_sa_write_key_returns_403(self):
        """SA with only documents:write scope cannot fetch document status → 403."""
        response = self.client.get(
            "/api/documents/1/status",
            headers={"Authorization": f"Bearer {self._sa_write_key}"},
        )
        if response.status_code != 403:
            raise AssertionError(
                f"Expected 403 for SA with documents:write on GET /api/documents/1/status, "
                f"got {response.status_code}: {response.json()}"
            )

    def test_document_status_without_auth_returns_401(self):
        """Request without Authorization header → 401."""
        response = self.client.get("/api/documents/1/status")
        if response.status_code != 401:
            raise AssertionError(
                f"Expected 401 for unauthenticated GET /api/documents/1/status, "
                f"got {response.status_code}: {response.json()}"
            )

    # -------------------------------------------------------------------------
    # Test: GET /api/documents/{file_id}/raw
    # -------------------------------------------------------------------------

    def test_document_raw_with_sa_read_key_reaches_endpoint(self):
        """SA with documents:read scope reaches the raw endpoint (file not on disk → 404)."""
        # File won't exist on disk in test env; the key point is SA auth is accepted.
        response = self.client.get(
            "/api/documents/1/raw",
            headers={"Authorization": f"Bearer {self._sa_read_key}"},
        )
        # 404 because the temp file doesn't exist on disk — but SA auth succeeded.
        if response.status_code == 401:
            raise AssertionError(
                f"SA with documents:read was rejected with 401 on GET /api/documents/1/raw "
                f"(auth was accepted but file was not found): {response.json()}"
            )
        if response.status_code not in (200, 404):
            raise AssertionError(
                f"Expected 200 or 404 for SA with documents:read on GET /api/documents/1/raw, "
                f"got {response.status_code}: {response.json()}"
            )

    def test_document_raw_with_sa_write_key_returns_403(self):
        """SA with only documents:write scope cannot access document raw → 403."""
        response = self.client.get(
            "/api/documents/1/raw",
            headers={"Authorization": f"Bearer {self._sa_write_key}"},
        )
        if response.status_code != 403:
            raise AssertionError(
                f"Expected 403 for SA with documents:write on GET /api/documents/1/raw, "
                f"got {response.status_code}: {response.json()}"
            )

    def test_document_raw_without_auth_returns_401(self):
        """Request without Authorization header → 401."""
        response = self.client.get("/api/documents/1/raw")
        if response.status_code != 401:
            raise AssertionError(
                f"Expected 401 for unauthenticated GET /api/documents/1/raw, "
                f"got {response.status_code}: {response.json()}"
            )

    # -------------------------------------------------------------------------
    # Test: GET /api/documents/stats
    # -------------------------------------------------------------------------

    def test_document_stats_with_sa_read_key_returns_200(self):
        """SA with documents:read scope can fetch document stats → 200."""
        response = self.client.get(
            "/api/documents/stats",
            headers={"Authorization": f"Bearer {self._sa_read_key}"},
        )
        if response.status_code != 200:
            raise AssertionError(
                f"Expected 200 for SA with documents:read on GET /api/documents/stats, "
                f"got {response.status_code}: {response.json()}"
            )
        data = response.json()
        if "total_documents" not in data:
            raise AssertionError(
                f"Expected 'total_documents' key in response, got: {data}"
            )

    def test_document_stats_with_sa_write_key_returns_403(self):
        """SA with only documents:write scope cannot fetch document stats → 403."""
        response = self.client.get(
            "/api/documents/stats",
            headers={"Authorization": f"Bearer {self._sa_write_key}"},
        )
        if response.status_code != 403:
            raise AssertionError(
                f"Expected 403 for SA with documents:write on GET /api/documents/stats, "
                f"got {response.status_code}: {response.json()}"
            )

    def test_document_stats_without_auth_returns_401(self):
        """Request without Authorization header → 401."""
        response = self.client.get("/api/documents/stats")
        if response.status_code != 401:
            raise AssertionError(
                f"Expected 401 for unauthenticated GET /api/documents/stats, "
                f"got {response.status_code}: {response.json()}"
            )

    # -------------------------------------------------------------------------
    # Test: GET /api/search/chunks/{chunk_id}/context
    # -------------------------------------------------------------------------

    def test_chunk_context_with_sa_read_key_returns_200(self):
        """SA with documents:read scope can fetch chunk context → 200."""
        response = self.client.get(
            "/api/search/chunks/test-chunk-uid-123/context",
            headers={"Authorization": f"Bearer {self._sa_read_key}"},
        )
        if response.status_code != 200:
            raise AssertionError(
                f"Expected 200 for SA with documents:read on GET /api/search/chunks/{{chunk_id}}/context, "
                f"got {response.status_code}: {response.json()}"
            )

    def test_chunk_context_with_sa_write_key_returns_403(self):
        """SA with only documents:write scope cannot fetch chunk context → 403."""
        response = self.client.get(
            "/api/search/chunks/test-chunk-uid-123/context",
            headers={"Authorization": f"Bearer {self._sa_write_key}"},
        )
        if response.status_code != 403:
            raise AssertionError(
                f"Expected 403 for SA with documents:write on GET /api/search/chunks/{{chunk_id}}/context, "
                f"got {response.status_code}: {response.json()}"
            )

    def test_chunk_context_without_auth_returns_401(self):
        """Request without Authorization header → 401."""
        response = self.client.get("/api/search/chunks/test-chunk-uid-123/context")
        if response.status_code != 401:
            raise AssertionError(
                f"Expected 401 for unauthenticated GET /api/search/chunks/{{chunk_id}}/context, "
                f"got {response.status_code}: {response.json()}"
            )

    # -------------------------------------------------------------------------
    # Test: GET /api/tags/documents/{file_id}
    # -------------------------------------------------------------------------

    def test_tags_document_with_sa_read_key_returns_200(self):
        """SA with documents:read scope can list document tags → 200."""
        response = self.client.get(
            "/api/tags/documents/1?vault_id=1",
            headers={"Authorization": f"Bearer {self._sa_read_key}"},
        )
        if response.status_code != 200:
            raise AssertionError(
                f"Expected 200 for SA with documents:read on GET /api/tags/documents/1, "
                f"got {response.status_code}: {response.json()}"
            )
        data = response.json()
        if "tags" not in data:
            raise AssertionError(
                f"Expected 'tags' key in response, got: {data}"
            )

    def test_tags_document_with_sa_write_key_returns_403(self):
        """SA with only documents:write scope cannot list document tags → 403."""
        response = self.client.get(
            "/api/tags/documents/1?vault_id=1",
            headers={"Authorization": f"Bearer {self._sa_write_key}"},
        )
        if response.status_code != 403:
            raise AssertionError(
                f"Expected 403 for SA with documents:write on GET /api/tags/documents/1, "
                f"got {response.status_code}: {response.json()}"
            )

    def test_tags_document_without_auth_returns_401(self):
        """Request without Authorization header → 401."""
        response = self.client.get("/api/tags/documents/1?vault_id=1")
        if response.status_code != 401:
            raise AssertionError(
                f"Expected 401 for unauthenticated GET /api/tags/documents/1, "
                f"got {response.status_code}: {response.json()}"
            )

    # -------------------------------------------------------------------------
    # F-6 negative tests: non-sak_ prefix and revoked service account
    # -------------------------------------------------------------------------

    def test_bearer_key_without_sak_prefix_returns_401(self):
        """F-6: a Bearer key lacking the sak_ prefix is rejected with 401.

        Covers the `if not raw_key.startswith("sak_")` branch in
        get_current_user_or_service_account (deps.py). Without this test, a
        regression removing the prefix check would silently pass all 24
        existing SA assertions (which all use sak_ keys).
        """
        # A plausible-looking Bearer key that does NOT start with sak_.
        response = self.client.get(
            "/api/documents/1",
            headers={"Authorization": "Bearer not_a_sak_key_12345"},
        )
        if response.status_code != 401:
            raise AssertionError(
                f"Expected 401 for Bearer key without sak_ prefix, "
                f"got {response.status_code}: {response.json()}"
            )

    def test_revoked_service_account_returns_401(self):
        """F-6: a revoked service account (revoked_at IS NOT NULL) is rejected with 401.

        Covers the `if revoked_at is not None` branch in
        get_current_user_or_service_account (deps.py). Without this test, a
        regression removing the revoked_at check would silently pass all 24
        existing SA assertions (which never set revoked_at).
        """
        # Insert a service account that is explicitly revoked but otherwise valid.
        revoked_key = "sak_test_revoked_key_12345678901234567890"
        revoked_key_hash = hashlib.sha256(revoked_key.encode()).hexdigest()
        from datetime import datetime, timezone

        conn = self._connection_pool.get_connection()
        try:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO service_accounts (name, key_hash, scopes, created_at, revoked_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("SA-Revoked", revoked_key_hash, "documents:read", now, now),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        response = self.client.get(
            "/api/documents/1",
            headers={"Authorization": f"Bearer {revoked_key}"},
        )
        if response.status_code != 401:
            raise AssertionError(
                f"Expected 401 for revoked service account, "
                f"got {response.status_code}: {response.json()}"
            )

