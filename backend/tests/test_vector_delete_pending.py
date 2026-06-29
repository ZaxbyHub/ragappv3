"""Tests for the vector-delete retry queue (Issue #219).

Covers:
- migrate_add_vector_delete_pending / migrate_add_chunks_failed_column are
  idempotent (safe to run twice)
- a failed LanceDB chunk delete during a document delete records a
  vector_delete_pending row while the files row is still deleted
- BackgroundProcessor.retry_pending_vector_deletes removes pending rows on
  success and increments attempts on failure (capped for operator review)
"""

import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub optional heavy deps so importing app.main is cheap in CI.
try:
    import lancedb  # noqa: F401
except ImportError:
    import types
    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow  # noqa: F401
except ImportError:
    import types
    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

from _db_pool import SimpleConnectionPool
from fastapi.testclient import TestClient

from app.api.deps import get_db, get_vector_store
from app.config import settings
from app.main import app
from app.models.database import (
    SQLiteConnectionPool,
    init_db,
    migrate_add_chunks_failed_column,
    migrate_add_vector_delete_pending,
    run_migrations,
)
from app.services.auth_service import compute_client_fingerprint, create_access_token


class TestVectorDeletePendingMigration(unittest.TestCase):
    """Migration idempotency for the retry-queue table and chunks_failed column."""

    def setUp(self):
        self._temp_dir = tempfile.mkdtemp()
        self._db_path = str(Path(self._temp_dir) / "migration.db")
        init_db(self._db_path)

    def tearDown(self):
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def test_vector_delete_pending_migration_is_idempotent(self):
        migrate_add_vector_delete_pending(self._db_path)
        migrate_add_vector_delete_pending(self._db_path)

        conn = sqlite3.connect(self._db_path)
        try:
            columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(vector_delete_pending)"
                ).fetchall()
            }
            self.assertEqual(
                columns, {"id", "file_id", "vault_id", "created_at", "attempts"}
            )
            conn.execute(
                "INSERT INTO vector_delete_pending (file_id, vault_id) VALUES (1, 1)"
            )
            row = conn.execute(
                "SELECT attempts FROM vector_delete_pending WHERE file_id = 1"
            ).fetchone()
            self.assertEqual(row[0], 0)
        finally:
            conn.close()

    def test_chunks_failed_migration_is_idempotent(self):
        migrate_add_chunks_failed_column(self._db_path)
        migrate_add_chunks_failed_column(self._db_path)

        conn = sqlite3.connect(self._db_path)
        try:
            columns = [
                row[1]
                for row in conn.execute("PRAGMA table_info(files)").fetchall()
            ]
            self.assertEqual(columns.count("chunks_failed"), 1)
        finally:
            conn.close()

    def test_run_migrations_twice_creates_table_once(self):
        run_migrations(self._db_path)
        run_migrations(self._db_path)

        conn = sqlite3.connect(self._db_path)
        try:
            tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='vector_delete_pending'"
                ).fetchall()
            ]
            self.assertEqual(tables, ["vector_delete_pending"])
        finally:
            conn.close()


class DocumentsDeleteVectorPendingTestBase(unittest.TestCase):
    """Route-level harness mirroring tests/test_documents_delete_audit.py."""

    def setUp(self):
        self.client = TestClient(app)
        # Override default User-Agent so fingerprint validation matches token
        self.client.headers["user-agent"] = ""
        self._temp_dir = tempfile.mkdtemp()

        self._original_jwt_secret = settings.jwt_secret_key
        self._original_users_enabled = settings.users_enabled
        self._original_data_dir = settings.data_dir

        settings.data_dir = Path(self._temp_dir)
        settings.jwt_secret_key = os.urandom(32).hex()
        settings.users_enabled = True

        self._db_path = str(Path(self._temp_dir) / "app.db")

        from app.models.database import _pool_cache, _pool_cache_lock

        with _pool_cache_lock:
            for _path, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()

        init_db(self._db_path)
        run_migrations(self._db_path)
        self._connection_pool = SimpleConnectionPool(self._db_path)

        def override_get_db():
            conn = self._connection_pool.get_connection()
            try:
                yield conn
            finally:
                self._connection_pool.release_connection(conn)

        # Mock vector store whose chunk delete FAILS.
        self._mock_vector_store = MagicMock()
        self._mock_vector_store.db = MagicMock()
        self._mock_vector_store.db.table_names = AsyncMock(return_value=["chunks"])
        self._mock_vector_store.db.open_table = AsyncMock(return_value=MagicMock())
        self._mock_vector_store.delete_by_file = AsyncMock(
            side_effect=RuntimeError("lancedb unavailable")
        )

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_vector_store] = lambda: self._mock_vector_store

        conn = self._connection_pool.get_connection()
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            pw = "test-password-hash"
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (1,'superadmin',?, 'Super','superadmin',1)",
                (pw,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (2,'Test Vault','test')"
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def tearDown(self):
        from app.models.database import _pool_cache, _pool_cache_lock

        with _pool_cache_lock:
            for _path, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()

        settings.jwt_secret_key = self._original_jwt_secret
        settings.users_enabled = self._original_users_enabled
        settings.data_dir = self._original_data_dir
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_vector_store, None)
        if hasattr(self, "_connection_pool"):
            self._connection_pool.close_all()
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _headers(self):
        return {
            "Authorization": f"Bearer {create_access_token(1, 'superadmin', 'superadmin', client_fingerprint=compute_client_fingerprint(''))}"
        }

    def _seed_file(self, vault_id=2, file_name="test.txt"):
        conn = self._connection_pool.get_connection()
        try:
            cur = conn.execute(
                "INSERT INTO files (vault_id, file_path, file_name, file_size, status) VALUES (?,?,?,?,?)",
                (vault_id, f"/uploads/{file_name}", file_name, 12, "indexed"),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            self._connection_pool.release_connection(conn)

    def _pending_rows(self):
        conn = self._connection_pool.get_connection()
        try:
            return conn.execute(
                "SELECT file_id, vault_id, attempts FROM vector_delete_pending"
            ).fetchall()
        finally:
            self._connection_pool.release_connection(conn)

    def _file_exists(self, file_id):
        conn = self._connection_pool.get_connection()
        try:
            return (
                conn.execute(
                    "SELECT 1 FROM files WHERE id = ?", (file_id,)
                ).fetchone()
                is not None
            )
        finally:
            self._connection_pool.release_connection(conn)


class TestDeleteRecordsPendingVectorDelete(DocumentsDeleteVectorPendingTestBase):
    """A failed chunk delete must be durably recorded without blocking the delete."""

    def test_failed_vector_delete_records_pending_row_and_still_deletes_file(self):
        file_id = self._seed_file()

        response = self.client.delete(
            f"/api/documents/{file_id}", headers=self._headers()
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(self._file_exists(file_id))

        rows = self._pending_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["file_id"], file_id)
        self.assertEqual(rows[0]["vault_id"], 2)
        self.assertEqual(rows[0]["attempts"], 0)

    def test_successful_vector_delete_records_no_pending_row(self):
        self._mock_vector_store.delete_by_file = AsyncMock(return_value=3)
        file_id = self._seed_file()

        response = self.client.delete(
            f"/api/documents/{file_id}", headers=self._headers()
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(self._file_exists(file_id))
        self.assertEqual(len(self._pending_rows()), 0)

    def test_delete_all_vault_records_pending_rows_for_failed_deletes(self):
        file_id_1 = self._seed_file(file_name="a.txt")
        file_id_2 = self._seed_file(file_name="b.txt")

        response = self.client.delete(
            "/api/documents/vault/2/all", headers=self._headers()
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["deleted_count"], 2)

        pending_file_ids = {row["file_id"] for row in self._pending_rows()}
        self.assertEqual(pending_file_ids, {file_id_1, file_id_2})


class TestRetryPendingVectorDeletes(unittest.TestCase):
    """BackgroundProcessor sweep retries pending vector deletes."""

    def setUp(self):
        self._temp_dir = tempfile.mkdtemp()
        self._db_path = str(Path(self._temp_dir) / "sweep.db")
        init_db(self._db_path)
        run_migrations(self._db_path)
        self._pool = SQLiteConnectionPool(self._db_path, max_size=2)

    def tearDown(self):
        self._pool.close_all()
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _make_processor(self, vector_store):
        from app.services.background_tasks import BackgroundProcessor

        return BackgroundProcessor(pool=self._pool, vector_store=vector_store)

    def _insert_pending(self, file_id, attempts=0):
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO vector_delete_pending (file_id, vault_id, attempts) VALUES (?, 1, ?)",
                (file_id, attempts),
            )
            conn.commit()

    def _pending_rows(self):
        with self._pool.connection() as conn:
            return conn.execute(
                "SELECT file_id, attempts FROM vector_delete_pending ORDER BY file_id"
            ).fetchall()

    def test_sweep_removes_row_on_successful_delete(self):
        vector_store = MagicMock()
        vector_store.delete_by_file = AsyncMock(return_value=4)
        processor = self._make_processor(vector_store)
        self._insert_pending(file_id=11)

        asyncio.run(processor.retry_pending_vector_deletes())

        vector_store.delete_by_file.assert_awaited_once_with("11")
        self.assertEqual(len(self._pending_rows()), 0)

    def test_sweep_increments_attempts_on_failed_delete(self):
        vector_store = MagicMock()
        vector_store.delete_by_file = AsyncMock(
            side_effect=RuntimeError("still down")
        )
        processor = self._make_processor(vector_store)
        self._insert_pending(file_id=12)

        asyncio.run(processor.retry_pending_vector_deletes())

        rows = self._pending_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["attempts"], 1)

    def test_sweep_skips_rows_past_attempt_cap(self):
        from app.services.background_tasks import MAX_VECTOR_DELETE_ATTEMPTS

        vector_store = MagicMock()
        vector_store.delete_by_file = AsyncMock(return_value=1)
        processor = self._make_processor(vector_store)
        self._insert_pending(file_id=13, attempts=MAX_VECTOR_DELETE_ATTEMPTS)

        asyncio.run(processor.retry_pending_vector_deletes())

        vector_store.delete_by_file.assert_not_awaited()
        rows = self._pending_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["attempts"], MAX_VECTOR_DELETE_ATTEMPTS)


if __name__ == "__main__":
    unittest.main()
