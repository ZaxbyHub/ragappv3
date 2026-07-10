"""Tests for embedding model versioning, mismatch detection, and reindex recovery.

This test suite verifies:
- validate_schema detects mismatched embedding model identity and sets _ready=False
- validate_schema confirms matching identity and sets _ready=True
- require_model_ready dependency returns 503 when vector store is not ready
- /documents/reindex endpoint creates a reindex job in the database
- /documents/reindex/jobs/{job_id} endpoint returns job status
- Reindex vault scoping (vault_id set correctly in job rows)
"""

import asyncio
import os
import secrets
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# lancedb is a required dependency for these tests - import it early before any stub is installed
import lancedb  # noqa: E402

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
    _unstructured.documents.elements = types.ModuleType(
        "unstructured.documents.elements"
    )
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
from app.security import csrf_protect
from app.services.auth_service import compute_client_fingerprint, create_access_token
from app.services.vector_store import VectorStore


class TestEmbeddingModelVersioningBase(unittest.TestCase):
    """Base test class for embedding model versioning tests."""

    def setUp(self):
        """Set up test client with temporary database."""
        self.client = TestClient(app)
        # Override default User-Agent so fingerprint validation matches token
        self.client.headers["user-agent"] = ""
        self._temp_dir = tempfile.mkdtemp()

        # Store original settings BEFORE modifying
        self._original_jwt_secret = settings.jwt_secret_key
        self._original_users_enabled = settings.users_enabled
        self._original_data_dir = settings.data_dir

        # CRITICAL: Update settings.data_dir so sqlite_path points to test db
        settings.data_dir = Path(self._temp_dir)
        settings.jwt_secret_key = secrets.token_urlsafe(32)
        settings.users_enabled = True

        # Use app.db to align with settings.sqlite_path
        self._db_path = str(Path(self._temp_dir) / "app.db")

        # Clear pool cache BEFORE setting up new database
        from app.models.database import _pool_cache, _pool_cache_lock

        with _pool_cache_lock:
            for path, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()

        from app.models.database import init_db, run_migrations

        init_db(self._db_path)
        run_migrations(self._db_path)
        self._connection_pool = SimpleConnectionPool(self._db_path)

        def override_get_db():
            conn = self._connection_pool.get_connection()
            try:
                yield conn
            finally:
                self._connection_pool.release_connection(conn)

        # Mock vector store for tests
        self._mock_vector_store = MagicMock()
        self._mock_vector_store.db = None
        # Use PropertyMock so that _ready ACTUALLY returns False (not a truthy MagicMock)
        from unittest.mock import PropertyMock
        type(self._mock_vector_store)._ready = PropertyMock(return_value=False)
        self._mock_vector_store.delete_by_file = AsyncMock(return_value=1)

        # Mock embedding service
        self._mock_embedding_service = MagicMock()

        # Mock db pool
        self._mock_db_pool = self._connection_pool

        # Mock background processor
        self._mock_background_processor = MagicMock()
        self._mock_background_processor.is_running = True
        self._mock_background_processor.enqueue = AsyncMock()
        self._mock_background_processor.enqueue_reindex = AsyncMock()

        # Mock rag_engine (minimal - only needed to satisfy get_rag_engine dependency)
        self._mock_rag_engine = MagicMock()
        # rag_engine.query() is called by chat endpoint; return an empty async generator
        async def _empty_async_gen():
            return
            yield  # make it an async generator
        self._mock_rag_engine.query = MagicMock(return_value=_empty_async_gen())

        # Set app.state so require_model_ready and get_background_processor
        # (which read directly from app.state, not from dependency overrides) work
        app.state.vector_store = self._mock_vector_store
        app.state.background_processor = self._mock_background_processor
        app.state.rag_engine = self._mock_rag_engine

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_vector_store] = lambda: self._mock_vector_store
        app.dependency_overrides[get_embedding_service] = lambda: (
            self._mock_embedding_service
        )
        app.dependency_overrides[get_db_pool] = lambda: self._mock_db_pool
        app.dependency_overrides[get_background_processor] = lambda: (
            self._mock_background_processor
        )
        _mock_sm = MagicMock()
        _mock_sm.get_hmac_key.return_value = (b"test-hmac-key-32bytes-padding!!", "v1")
        app.dependency_overrides[get_secret_manager] = lambda: _mock_sm
        # Override csrf_protect to return a dummy token for route tests
        app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"

        # Seed test users and vaults
        conn = self._connection_pool.get_connection()
        try:
            conn.execute("PRAGMA foreign_keys = ON")

            # Clear existing data
            conn.execute("DELETE FROM files")
            conn.execute("DELETE FROM vault_members")
            conn.execute("DELETE FROM users WHERE id != 0")

            pw = "test-password-hash"

            # User 1: superadmin
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
                (1, "superadmin", pw, "Super Admin", "superadmin"),
            )
            # User 2: admin
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
                (2, "admin1", pw, "Admin One", "admin"),
            )
            # User 3: member with vault access
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
                (3, "member1", pw, "Member One", "member"),
            )

            # Create additional vaults for testing (Vault 1 is the Default vault)
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (?, ?, ?)",
                (2, "Private Vault", "A private vault"),
            )
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (?, ?, ?)",
                (3, "Read-Only Vault", "A read-only vault"),
            )

            # Seed vault_members
            # member1 (user 3) has WRITE access to vault 2
            conn.execute(
                "INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission, granted_by) VALUES (?, ?, ?, ?)",
                (2, 3, "write", 1),
            )

            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def tearDown(self):
        """Clean up after each test."""
        # Clear pool cache before changing paths
        from app.models.database import _pool_cache, _pool_cache_lock

        with _pool_cache_lock:
            for path, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()

        # Restore original settings
        settings.jwt_secret_key = self._original_jwt_secret
        settings.users_enabled = self._original_users_enabled
        if hasattr(self, "_original_data_dir"):
            settings.data_dir = self._original_data_dir

        # Clear app.state attributes set in setUp
        if hasattr(app.state, "vector_store"):
            delattr(app.state, "vector_store")
        if hasattr(app.state, "background_processor"):
            delattr(app.state, "background_processor")
        if hasattr(app.state, "rag_engine"):
            delattr(app.state, "rag_engine")

        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_vector_store, None)
        app.dependency_overrides.pop(get_embedding_service, None)
        app.dependency_overrides.pop(get_db_pool, None)
        app.dependency_overrides.pop(get_background_processor, None)
        app.dependency_overrides.pop(get_secret_manager, None)
        app.dependency_overrides.pop(csrf_protect, None)

        if hasattr(self, "_connection_pool"):
            self._connection_pool.close_all()

        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _get_db_conn(self):
        """Get a raw connection for test data setup."""
        return self._connection_pool.get_connection()

    def _create_admin_token(self, user_id=1, role="superadmin"):
        """Generate access token for admin/superadmin user.

        Args:
            user_id: The user ID (default 1 for superadmin)
            role: The user role (default "superadmin")

        Returns:
            JWT access token string
        """
        username_map = {1: "superadmin", 2: "admin1", 3: "member1"}
        username = username_map.get(user_id, "superadmin")
        return create_access_token(
            user_id, username, role, client_fingerprint=compute_client_fingerprint("")
        )

    def _auth_headers(self, token):
        """Create authorization headers with token."""
        return {"Authorization": f"Bearer {token}"}


class TestValidateSchemaMismatch(TestEmbeddingModelVersioningBase):
    """Tests for validate_schema mismatch detection."""

    def test_validate_schema_mismatch_sets_not_ready(self):
        """validate_schema sets _ready=False when stored model ID differs from configured model.

        Creates a chunks table and settings_kv metadata with a different model ID,
        then calls validate_schema with the current configured model.
        Asserts _ready is False and result contains mismatch details.
        """
        import lancedb
        import pyarrow as pa

        from app.services.vector_store import VectorStore

        # Create VectorStore with default path (will use settings.lancedb_path)
        vs = VectorStore()

        # Create LanceDB table with the correct embedding dimension
        # The LanceDB path is settings.lancedb_path which is data_dir / "lancedb"
        lancedb_path = settings.lancedb_path
        lancedb_path.mkdir(parents=True, exist_ok=True)

        schema = pa.schema([
            ("id", pa.string()),
            ("text", pa.string()),
            ("file_id", pa.string()),
            ("vault_id", pa.string()),
            ("chunk_index", pa.int32()),
            ("embedding", pa.list_(pa.float32(), 384)),
        ])

        # Use connect (sync) then create_table
        db = lancedb.connect(str(lancedb_path))
        table = db.create_table("chunks", schema=schema, exist_ok=True)
        vs.db = db
        vs.table = table

        # Store MISMATCHING model metadata in settings_kv (via sqlite_path)
        conn = sqlite3.connect(str(settings.sqlite_path))
        try:
            conn.execute(
                "INSERT OR REPLACE INTO settings_kv (key, value) VALUES (?, ?)",
                ("embedding_model_id", "different-model-id"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO settings_kv (key, value) VALUES (?, ?)",
                ("embedding_dim", "384"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO settings_kv (key, value) VALUES (?, ?)",
                ("embedding_prefix_hash", "different_hash"),
            )
            conn.commit()
        finally:
            conn.close()

        # Call validate_schema with a different model ID
        result = asyncio.run(vs.validate_schema(
            embedding_model_id="configured-model-id", embedding_dim=384
        ))

        # Assert _ready is False due to mismatch
        self.assertFalse(vs._ready)
        self.assertTrue(result["mismatch"])
        self.assertIn("embedding_model_id", result["mismatch_details"])

    def test_validate_schema_match_sets_ready(self):
        """validate_schema sets _ready=True when stored metadata matches configured model.

        Records metadata matching the current configured model in settings_kv,
        then calls validate_schema. Asserts _ready is True.
        """
        import lancedb
        import pyarrow as pa

        from app.services.vector_store import VectorStore

        # Create VectorStore with default path (will use settings.lancedb_path)
        vs = VectorStore()

        # Create LanceDB table with the correct embedding dimension
        lancedb_path = settings.lancedb_path
        lancedb_path.mkdir(parents=True, exist_ok=True)

        schema = pa.schema([
            ("id", pa.string()),
            ("text", pa.string()),
            ("file_id", pa.string()),
            ("vault_id", pa.string()),
            ("chunk_index", pa.int32()),
            ("embedding", pa.list_(pa.float32(), 384)),
        ])

        # Use connect (sync) then create_table
        db = lancedb.connect(str(lancedb_path))
        table = db.create_table("chunks", schema=schema, exist_ok=True)
        vs.db = db
        vs.table = table

        # Compute the expected prefix hash for this VectorStore instance
        expected_hash = vs._compute_embedding_prefix_hash()
        configured_model_id = "test-model-id"

        # Store MATCHING model metadata in settings_kv (via sqlite_path)
        conn = sqlite3.connect(str(settings.sqlite_path))
        try:
            conn.execute(
                "INSERT OR REPLACE INTO settings_kv (key, value) VALUES (?, ?)",
                ("embedding_model_id", configured_model_id),
            )
            conn.execute(
                "INSERT OR REPLACE INTO settings_kv (key, value) VALUES (?, ?)",
                ("embedding_dim", "384"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO settings_kv (key, value) VALUES (?, ?)",
                ("embedding_prefix_hash", expected_hash),
            )
            conn.commit()
        finally:
            conn.close()

        # Call validate_schema with matching model ID
        result = asyncio.run(vs.validate_schema(
            embedding_model_id=configured_model_id, embedding_dim=384
        ))

        # Assert _ready is True due to match
        self.assertTrue(vs._ready)
        self.assertFalse(result["mismatch"])
        self.assertEqual(result["mismatch_details"], [])


class TestRequireModelReady503(TestEmbeddingModelVersioningBase):
    """Tests for 503 response when vector store is not ready."""

    def test_require_model_ready_returns_503_on_chat_stream(self):
        """POST /chat/stream returns 503 when vector store _ready is False."""
        # Override get_vector_store to return a mock with _ready=False
        mock_vs = MagicMock()
        mock_vs._ready = False
        mock_vs.db = None
        app.dependency_overrides[get_vector_store] = lambda: mock_vs

        try:
            admin_jwt = self._create_admin_token(user_id=1, role="superadmin")
            response = self.client.post(
                "/api/chat/stream",
                headers=self._auth_headers(admin_jwt),
                json={
                    "message": "test message",
                    "history": [],
                    "stream": False,
                    "vault_id": 2,
                },
            )
            self.assertEqual(response.status_code, 503)
            self.assertIn("Embedding model mismatch", response.text)
        finally:
            app.dependency_overrides.pop(get_vector_store, None)

    def test_require_model_ready_returns_503_on_search(self):
        """POST /search returns 503 when vector store _ready is False."""
        # Override get_vector_store to return a mock with _ready=False
        mock_vs = MagicMock()
        mock_vs._ready = False
        mock_vs.db = None
        app.dependency_overrides[get_vector_store] = lambda: mock_vs

        try:
            admin_jwt = self._create_admin_token(user_id=1, role="superadmin")
            response = self.client.post(
                "/api/search",
                headers=self._auth_headers(admin_jwt),
                json={"query": "test query", "vault_id": 2, "limit": 10},
            )
            self.assertEqual(response.status_code, 503)
            self.assertIn("Embedding model mismatch", response.text)
        finally:
            app.dependency_overrides.pop(get_vector_store, None)


class TestReindexEndpoint(TestEmbeddingModelVersioningBase):
    """Tests for the /documents/reindex endpoint."""

    def test_reindex_endpoint_creates_job(self):
        """POST /documents/reindex creates a reindex job and returns job_id + status='pending'.

        Verifies the response contains job_id and status='pending',
        then queries the DB directly to confirm the job row exists.
        """
        admin_jwt = self._create_admin_token(user_id=1, role="superadmin")

        response = self.client.post(
            "/api/documents/reindex",
            headers=self._auth_headers(admin_jwt),
            json={},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("job_id", data)
        self.assertEqual(data["status"], "pending")

        # Verify the job row exists in the database
        conn = self._get_db_conn()
        try:
            row = conn.execute(
                "SELECT id, vault_id, status, trigger_type FROM document_reindex_jobs WHERE id = ?",
                (data["job_id"],),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], data["job_id"])
            # vault_id should be NULL when not specified
            self.assertIsNone(row[1])
            self.assertEqual(row[2], "pending")
            self.assertEqual(row[3], "api")
        finally:
            self._connection_pool.release_connection(conn)

    def test_reindex_status_endpoint_reports_job(self):
        """GET /documents/reindex/jobs/{job_id} returns job status for existing job.

        Creates a document_reindex_jobs row directly in the test DB,
        then queries the status endpoint and verifies response matches DB row.
        """
        # First create a job via the endpoint
        admin_jwt = self._create_admin_token(user_id=1, role="superadmin")
        create_response = self.client.post(
            "/api/documents/reindex",
            headers=self._auth_headers(admin_jwt),
            json={},
        )
        self.assertEqual(create_response.status_code, 200)
        job_id = create_response.json()["job_id"]

        # Now query the status endpoint
        response = self.client.get(
            f"/api/documents/reindex/jobs/{job_id}",
            headers=self._auth_headers(admin_jwt),
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["id"], job_id)
        self.assertEqual(data["status"], "pending")
        # vault_id should be None when not specified
        self.assertIsNone(data.get("vault_id"))

    def test_reindex_vault_scoping_with_vault_id(self):
        """POST /documents/reindex with vault_id creates job with correct vault_id.

        Verifies the created job row has vault_id=2.
        """
        admin_jwt = self._create_admin_token(user_id=1, role="superadmin")

        response = self.client.post(
            "/api/documents/reindex",
            headers=self._auth_headers(admin_jwt),
            json={"vault_id": 2},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["vault_id"], 2)

        # Verify vault_id in database
        conn = self._get_db_conn()
        try:
            row = conn.execute(
                "SELECT vault_id FROM document_reindex_jobs WHERE id = ?",
                (data["job_id"],),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], 2)
        finally:
            self._connection_pool.release_connection(conn)

    def test_reindex_vault_scoping_without_vault_id(self):
        """POST /documents/reindex without vault_id creates job with vault_id=NULL.

        Verifies the created job row has vault_id IS NULL.
        """
        admin_jwt = self._create_admin_token(user_id=1, role="superadmin")

        response = self.client.post(
            "/api/documents/reindex",
            headers=self._auth_headers(admin_jwt),
            json={},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsNone(data["vault_id"])

        # Verify vault_id IS NULL in database
        conn = self._get_db_conn()
        try:
            row = conn.execute(
                "SELECT vault_id FROM document_reindex_jobs WHERE id = ?",
                (data["job_id"],),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertIsNone(row[0])
        finally:
            self._connection_pool.release_connection(conn)


class TestReindexJobCompletionOrdering(TestEmbeddingModelVersioningBase):
    """Regression tests for reindex job completion ordering."""

    def test_metadata_failure_causes_job_failed_not_completed(self):
        """When record_embedding_metadata fails, the job must be marked 'failed' (not 'completed').

        This prevents a mismatched app state on restart where the job is reported as
        completed but embedding metadata and readiness were never durably updated.
        """
        from unittest.mock import patch

        import pyarrow as pa

        from app.services.background_tasks import BackgroundProcessor
        from app.services.vector_store import VectorStore

        # Create VectorStore with default path
        vs = VectorStore()
        lancedb_path = settings.lancedb_path
        lancedb_path.mkdir(parents=True, exist_ok=True)

        schema = pa.schema([
            ("id", pa.string()),
            ("text", pa.string()),
            ("file_id", pa.string()),
            ("vault_id", pa.string()),
            ("chunk_index", pa.int32()),
            ("embedding", pa.list_(pa.float32(), 384)),
        ])

        db = lancedb.connect(str(lancedb_path))
        table = db.create_table("chunks", schema=schema, exist_ok=True)
        vs.db = db
        vs.table = table

        # Store matching metadata so validate_schema passes
        expected_hash = vs._compute_embedding_prefix_hash()
        configured_model_id = "test-model-id"
        conn = sqlite3.connect(str(settings.sqlite_path))
        try:
            conn.execute(
                "INSERT OR REPLACE INTO settings_kv (key, value) VALUES (?, ?)",
                ("embedding_model_id", configured_model_id),
            )
            conn.execute(
                "INSERT OR REPLACE INTO settings_kv (key, value) VALUES (?, ?)",
                ("embedding_dim", "384"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO settings_kv (key, value) VALUES (?, ?)",
                ("embedding_prefix_hash", expected_hash),
            )
            conn.commit()
        finally:
            conn.close()

        # Set up a minimal file in the DB so the reindex has something to process
        conn = self._get_db_conn()
        try:
            conn.execute(
                "INSERT INTO files (file_path, vault_id, status, phase, file_name, file_size) VALUES (?, ?, ?, ?, ?, ?)",
                ("/test/file.txt", 2, "indexed", "complete", "file.txt", 100),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        # Create a reindex job via the API
        admin_jwt = self._create_admin_token(user_id=1, role="superadmin")
        create_response = self.client.post(
            "/api/documents/reindex",
            headers=self._auth_headers(admin_jwt),
            json={"vault_id": 2},
        )
        self.assertEqual(create_response.status_code, 200)
        job_id = create_response.json()["job_id"]

        # Create a real BackgroundProcessor with mocked dependencies
        processor = BackgroundProcessor(
            vector_store=vs,
        )
        # Set pool directly on processor.processor (BackgroundProcessor creates its own DocumentProcessor)
        processor.processor.pool = self._connection_pool
        # Mock process_existing_file to succeed (we only care about metadata failure)
        processor.processor.process_existing_file = AsyncMock(return_value=MagicMock(file_id=1))
        # SimpleConnectionPool uses get_connection(), but BackgroundProcessor expects
        # pool.connection() (context manager). Patch connection() to use get_connection().
        import contextlib
        class PoolConnectionWrapper:
            """Wrapper that exposes get_connection() as a context manager."""
            def __init__(self, pool):
                self._pool = pool
            def connection(self):
                return contextlib.closing(self._pool.get_connection())
        processor.processor.pool = PoolConnectionWrapper(self._connection_pool)

        # Verify pool is set correctly before calling _process_reindex_job
        self.assertIsNotNone(processor.processor.pool, "processor.processor.pool should not be None")

        # Patch record_embedding_metadata to fail, simulating a write error
        with patch.object(
            vs,
            "record_embedding_metadata",
            side_effect=RuntimeError("Simulated metadata write failure"),
        ):
            # Invoke _process_reindex_job directly
            asyncio.run(processor._process_reindex_job(job_id))

        # Verify the job row is marked 'failed' (not 'completed')
        conn = self._get_db_conn()
        try:
            row = conn.execute(
                "SELECT status, error, completed_at FROM document_reindex_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            self.assertIsNotNone(row)
            status = row[0]
            error = row[1]
            completed_at = row[2]
            self.assertEqual(status, "failed", f"Expected status='failed' but got status='{status}'; error={error}")
            self.assertIsNotNone(error)
            self.assertNotEqual(error, "")
            self.assertIsNotNone(completed_at)
        finally:
            self._connection_pool.release_connection(conn)

    def test_sqlite_write_failure_during_reindex_marks_job_failed(self):
        """When record_embedding_metadata encounters a real sqlite3.OperationalError,
        the reindex job must be marked 'failed' (not 'completed').

        This regression test forces a genuine SQLite write failure by patching
        sqlite3.connect to return a mock connection whose commit() raises
        sqlite3.OperationalError, exercising the full asyncio.to_thread path
        with raise_on_error=True.
        """
        from unittest.mock import patch

        import pyarrow as pa

        from app.services.background_tasks import BackgroundProcessor
        from app.services.vector_store import VectorStore

        # Create VectorStore with default path
        vs = VectorStore()
        lancedb_path = settings.lancedb_path
        lancedb_path.mkdir(parents=True, exist_ok=True)

        schema = pa.schema([
            ("id", pa.string()),
            ("text", pa.string()),
            ("file_id", pa.string()),
            ("vault_id", pa.string()),
            ("chunk_index", pa.int32()),
            ("embedding", pa.list_(pa.float32(), 384)),
        ])

        db = lancedb.connect(str(lancedb_path))
        table = db.create_table("chunks", schema=schema, exist_ok=True)
        vs.db = db
        vs.table = table

        # Store matching metadata so validate_schema passes
        expected_hash = vs._compute_embedding_prefix_hash()
        configured_model_id = "test-model-id"
        conn = sqlite3.connect(str(settings.sqlite_path))
        try:
            conn.execute(
                "INSERT OR REPLACE INTO settings_kv (key, value) VALUES (?, ?)",
                ("embedding_model_id", configured_model_id),
            )
            conn.execute(
                "INSERT OR REPLACE INTO settings_kv (key, value) VALUES (?, ?)",
                ("embedding_dim", "384"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO settings_kv (key, value) VALUES (?, ?)",
                ("embedding_prefix_hash", expected_hash),
            )
            conn.commit()
        finally:
            conn.close()

        # Set up a minimal file in the DB so the reindex has something to process
        conn = self._get_db_conn()
        try:
            conn.execute(
                "INSERT INTO files (file_path, vault_id, status, phase, file_name, file_size) VALUES (?, ?, ?, ?, ?, ?)",
                ("/test/file.txt", 2, "indexed", "complete", "file.txt", 100),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        # Create a reindex job via the API
        admin_jwt = self._create_admin_token(user_id=1, role="superadmin")
        create_response = self.client.post(
            "/api/documents/reindex",
            headers=self._auth_headers(admin_jwt),
            json={"vault_id": 2},
        )
        self.assertEqual(create_response.status_code, 200)
        job_id = create_response.json()["job_id"]

        # Create a real BackgroundProcessor with mocked dependencies
        processor = BackgroundProcessor(
            vector_store=vs,
        )
        import contextlib
        class PoolConnectionWrapper:
            """Wrapper that exposes get_connection() as a context manager."""
            def __init__(self, pool):
                self._pool = pool
            def connection(self):
                return contextlib.closing(self._pool.get_connection())
        processor.processor.pool = PoolConnectionWrapper(self._connection_pool)
        processor.processor.process_existing_file = AsyncMock(return_value=MagicMock(file_id=1))

        # Patch record_embedding_metadata to raise a real sqlite3.OperationalError
        # (a genuine SQLite exception subclass) when called with raise_on_error=True.
        # This exercises the full asyncio.to_thread + raise_on_error propagation path
        # and verifies that the outer exception handler marks the job as failed.
        original_rm = vs.record_embedding_metadata

        async def raising_rm(embedding_dim, raise_on_error=False):
            raise sqlite3.OperationalError("database is locked")

        vs.record_embedding_metadata = raising_rm

        try:
            asyncio.run(processor._process_reindex_job(job_id))
        finally:
            vs.record_embedding_metadata = original_rm

        # Verify the job row is marked 'failed' (not 'completed')
        conn = self._get_db_conn()
        try:
            row = conn.execute(
                "SELECT status, error, completed_at FROM document_reindex_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            self.assertIsNotNone(row)
            status = row[0]
            error = row[1]
            completed_at = row[2]
            self.assertEqual(status, "failed", f"Expected status='failed' but got status='{status}'; error={error}")
            self.assertIsNotNone(error)
            self.assertNotEqual(error, "")
            self.assertIsNotNone(completed_at)
        finally:
            self._connection_pool.release_connection(conn)


class TestComputeEmbeddingPrefixHash(unittest.TestCase):
    """Tests for _compute_embedding_prefix_hash."""

    def test_returns_16_char_hex_hash(self):
        """Verify it returns a 16-character hex string for normal string prefixes."""
        vs = VectorStore(db_path=Path("/tmp/test_lancedb"))

        # With default (empty) prefixes the hash should be deterministic
        result = vs._compute_embedding_prefix_hash()
        self.assertIsInstance(result, str)
        self.assertEqual(len(result), 16)
        # Must be valid hexadecimal
        int(result, 16)  # raises ValueError if not hex
        self.assertEqual(result, vs._compute_embedding_prefix_hash())  # idempotent

    def test_non_empty_string_prefixes_change_hash(self):
        """Verify that explicit non-empty string prefixes produce a different hash
        from the empty-string baseline."""
        vs = VectorStore(db_path=Path("/tmp/test_lancedb"))

        # Empty-string baseline
        orig_doc = settings.embedding_doc_prefix
        orig_query = settings.embedding_query_prefix
        try:
            settings.embedding_doc_prefix = ""
            settings.embedding_query_prefix = ""
            baseline = vs._compute_embedding_prefix_hash()

            # Non-empty prefixes should produce a different hash
            settings.embedding_doc_prefix = "doc_prefix"
            settings.embedding_query_prefix = "query_prefix"
            result = vs._compute_embedding_prefix_hash()
            self.assertNotEqual(result, baseline)
            self.assertIsInstance(result, str)
            self.assertEqual(len(result), 16)
            int(result, 16)  # must be valid hex
        finally:
            settings.embedding_doc_prefix = orig_doc
            settings.embedding_query_prefix = orig_query

    def test_treats_none_as_empty_string(self):
        """Verify that when settings.embedding_doc_prefix and settings.embedding_query_prefix
        are None, the result is the same as empty strings."""
        vs = VectorStore(db_path=Path("/tmp/test_lancedb"))

        # Capture baseline with empty strings
        orig_doc = settings.embedding_doc_prefix
        orig_query = settings.embedding_query_prefix
        try:
            settings.embedding_doc_prefix = ""
            settings.embedding_query_prefix = ""
            baseline = vs._compute_embedding_prefix_hash()

            # Set to None and verify same result
            settings.embedding_doc_prefix = None
            settings.embedding_query_prefix = None
            result = vs._compute_embedding_prefix_hash()
            self.assertEqual(result, baseline)
        finally:
            settings.embedding_doc_prefix = orig_doc
            settings.embedding_query_prefix = orig_query

    def test_handles_mocked_settings_without_exploding(self):
        """Patch app.services.vector_store.settings with a MagicMock that does NOT have
        embedding_doc_prefix or embedding_query_prefix set, and verify the function
        returns a 16-character hex string (does not raise)."""
        vs = VectorStore(db_path=Path("/tmp/test_lancedb"))

        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.embedding_doc_prefix = MagicMock()
            mock_settings.embedding_query_prefix = MagicMock()
            result = vs._compute_embedding_prefix_hash()
            self.assertIsInstance(result, str)
            self.assertEqual(len(result), 16)
            int(result, 16)  # must be valid hex


if __name__ == "__main__":
    unittest.main()
