"""Tests for bulk delete audit logging fixes from PR 124 review findings.

Covers:
- batch_delete_documents creates audit rows for each deleted document
- delete_all_vault_documents creates audit rows
- User ID is correctly recorded (not "unknown")
"""

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
from app.services.auth_service import compute_client_fingerprint, create_access_token


class DocumentsDeleteAuditTestBase(unittest.TestCase):
    """Base class for document delete audit tests."""

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
        self._mock_vector_store.db = MagicMock()
        self._mock_vector_store.db.table_names = AsyncMock(return_value=["chunks"])
        self._mock_vector_store.db.open_table = AsyncMock(return_value=MagicMock())
        self._mock_vector_store.delete_by_file = AsyncMock(return_value=1)

        # Mock secret manager for audit logging
        self._mock_secret_manager = MagicMock()
        self._mock_secret_manager.get_hmac_key = MagicMock(return_value=(b"test-hmac-key-32-bytes-long!!!!!", "v1"))

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_vector_store] = lambda: self._mock_vector_store

        # Set up mock secret manager on app.state for audit logging
        app.state.secret_manager = self._mock_secret_manager

        conn = self._connection_pool.get_connection()
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            pw = "test-password-hash"
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (1,'superadmin',?, 'Super','superadmin',1)",
                (pw,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (3,'admin_user',?, 'Admin User','member',1)",
                (pw,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (2,'Test Vault','test')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission, granted_by) VALUES (2,3,'admin',1)"
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

    def _headers(self, user_id, username, role):
        return {"Authorization": f"Bearer {create_access_token(user_id, username, role, client_fingerprint=compute_client_fingerprint(''))}"}

    def _admin_headers(self):
        return self._headers(3, "admin_user", "member")

    def _seed_file(self, vault_id=2, file_name="test.txt", parsed_text="test content"):
        """Seed a file record in the database."""
        conn = self._connection_pool.get_connection()
        try:
            cur = conn.execute(
                "INSERT INTO files (vault_id, file_path, file_name, file_size, status, parsed_text) VALUES (?,?,?,?,?,?)",
                (vault_id, f"/uploads/{file_name}", file_name, len(parsed_text), "indexed", parsed_text),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            self._connection_pool.release_connection(conn)

    def _get_audit_count(self, conn, file_id, action="delete"):
        """Get count of audit entries for a given file_id and action."""
        cursor = conn.execute(
            "SELECT COUNT(*) as cnt FROM document_actions WHERE file_id = ? AND action = ?",
            (file_id, action),
        )
        row = cursor.fetchone()
        return row["cnt"] if row else 0

    def _get_audit_user_id(self, conn, file_id, action="delete"):
        """Get the user_id from audit entry for a given file_id and action."""
        cursor = conn.execute(
            "SELECT user_id FROM document_actions WHERE file_id = ? AND action = ? LIMIT 1",
            (file_id, action),
        )
        row = cursor.fetchone()
        return row["user_id"] if row else None


class TestBatchDeleteAuditLogging(DocumentsDeleteAuditTestBase):
    """Tests for batch_delete_documents audit logging (PR 124 review finding)."""

    def test_batch_delete_creates_audit_rows_for_each_deleted_document(self):
        """batch_delete_documents creates audit rows for each successfully deleted document."""
        # Seed 3 files
        file_id_1 = self._seed_file(vault_id=2, file_name="file1.txt", parsed_text="content 1")
        file_id_2 = self._seed_file(vault_id=2, file_name="file2.txt", parsed_text="content 2")
        file_id_3 = self._seed_file(vault_id=2, file_name="file3.txt", parsed_text="content 3")

        # Perform batch delete
        response = self.client.post(
            "/api/documents/batch",
            json={"file_ids": [str(file_id_1), str(file_id_2), str(file_id_3)]},
            headers=self._admin_headers(),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["deleted_count"], 3)
        self.assertEqual(len(body["failed_ids"]), 0)

        # Verify audit rows were created for each deleted file
        conn = self._connection_pool.get_connection()
        try:
            audit_count_1 = self._get_audit_count(conn, file_id_1, "delete")
            audit_count_2 = self._get_audit_count(conn, file_id_2, "delete")
            audit_count_3 = self._get_audit_count(conn, file_id_3, "delete")

            self.assertEqual(audit_count_1, 1, f"Expected 1 audit entry for file {file_id_1}, got {audit_count_1}")
            self.assertEqual(audit_count_2, 1, f"Expected 1 audit entry for file {file_id_2}, got {audit_count_2}")
            self.assertEqual(audit_count_3, 1, f"Expected 1 audit entry for file {file_id_3}, got {audit_count_3}")
        finally:
            self._connection_pool.release_connection(conn)

    def test_batch_delete_records_correct_user_id(self):
        """batch_delete_documents records the correct user ID (not 'unknown')."""
        # Seed a file
        file_id = self._seed_file(vault_id=2, file_name="test_file.txt", parsed_text="test content")

        # Perform batch delete as admin_user (user_id=3)
        response = self.client.post(
            "/api/documents/batch",
            json={"file_ids": [str(file_id)]},
            headers=self._admin_headers(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["deleted_count"], 1)

        # Verify user_id is recorded correctly (not "unknown")
        conn = self._connection_pool.get_connection()
        try:
            recorded_user_id = self._get_audit_user_id(conn, file_id, "delete")
            self.assertIsNotNone(recorded_user_id, "Expected audit user_id to be recorded")
            self.assertNotEqual(
                recorded_user_id,
                "unknown",
                f"Expected user_id to be '3', not 'unknown'. Got: {recorded_user_id}",
            )
            self.assertEqual(
                recorded_user_id,
                "3",
                f"Expected user_id to be '3', got '{recorded_user_id}'",
            )
        finally:
            self._connection_pool.release_connection(conn)

    def test_batch_delete_partial_failure_creates_audit_for_successful_deletes(self):
        """batch_delete_documents creates audit rows for successful deletes even if some fail."""
        # Seed 2 files, but try to delete one that doesn't exist
        file_id_1 = self._seed_file(vault_id=2, file_name="real1.txt", parsed_text="content 1")
        file_id_2 = self._seed_file(vault_id=2, file_name="real2.txt", parsed_text="content 2")

        # Perform batch delete with one non-existent file ID
        response = self.client.post(
            "/api/documents/batch",
            json={"file_ids": [str(file_id_1), "99999", str(file_id_2)]},
            headers=self._admin_headers(),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["deleted_count"], 2)
        self.assertIn("99999", body["failed_ids"])

        # Verify audit rows were created for both successfully deleted files
        conn = self._connection_pool.get_connection()
        try:
            audit_count_1 = self._get_audit_count(conn, file_id_1, "delete")
            audit_count_2 = self._get_audit_count(conn, file_id_2, "delete")

            self.assertEqual(audit_count_1, 1)
            self.assertEqual(audit_count_2, 1)
        finally:
            self._connection_pool.release_connection(conn)


class TestDeleteAllVaultAuditLogging(DocumentsDeleteAuditTestBase):
    """Tests for delete_all_vault_documents audit logging (PR 124 review finding)."""

    def test_delete_all_vault_documents_creates_audit_rows(self):
        """delete_all_vault_documents creates audit rows for each deleted document."""
        # Seed 3 files in vault 2
        file_id_1 = self._seed_file(vault_id=2, file_name="vault2_file1.txt", parsed_text="content 1")
        file_id_2 = self._seed_file(vault_id=2, file_name="vault2_file2.txt", parsed_text="content 2")
        file_id_3 = self._seed_file(vault_id=2, file_name="vault2_file3.txt", parsed_text="content 3")

        # Perform delete all vault documents
        response = self.client.delete(
            "/api/documents/vault/2/all",
            headers=self._admin_headers(),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["deleted_count"], 3)
        self.assertEqual(body["vault_id"], 2)

        # Verify audit rows were created for each deleted file
        conn = self._connection_pool.get_connection()
        try:
            audit_count_1 = self._get_audit_count(conn, file_id_1, "delete")
            audit_count_2 = self._get_audit_count(conn, file_id_2, "delete")
            audit_count_3 = self._get_audit_count(conn, file_id_3, "delete")

            self.assertEqual(audit_count_1, 1, f"Expected 1 audit entry for file {file_id_1}")
            self.assertEqual(audit_count_2, 1, f"Expected 1 audit entry for file {file_id_2}")
            self.assertEqual(audit_count_3, 1, f"Expected 1 audit entry for file {file_id_3}")
        finally:
            self._connection_pool.release_connection(conn)

    def test_delete_all_vault_documents_records_correct_user_id(self):
        """delete_all_vault_documents records the correct user ID (not 'unknown')."""
        # Seed 2 files in vault 2
        file_id_1 = self._seed_file(vault_id=2, file_name="file_a.txt", parsed_text="content a")
        file_id_2 = self._seed_file(vault_id=2, file_name="file_b.txt", parsed_text="content b")

        # Perform delete all as admin_user (user_id=3)
        response = self.client.delete(
            "/api/documents/vault/2/all",
            headers=self._admin_headers(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["deleted_count"], 2)

        # Verify user_id is recorded correctly for each file
        conn = self._connection_pool.get_connection()
        try:
            recorded_user_id_1 = self._get_audit_user_id(conn, file_id_1, "delete")
            recorded_user_id_2 = self._get_audit_user_id(conn, file_id_2, "delete")

            for recorded_id, file_id in [(recorded_user_id_1, file_id_1), (recorded_user_id_2, file_id_2)]:
                self.assertIsNotNone(
                    recorded_id,
                    f"Expected audit user_id to be recorded for file {file_id}",
                )
                self.assertNotEqual(
                    recorded_id,
                    "unknown",
                    f"Expected user_id to be '3', not 'unknown' for file {file_id}. Got: {recorded_id}",
                )
                self.assertEqual(
                    recorded_id,
                    "3",
                    f"Expected user_id to be '3' for file {file_id}, got '{recorded_id}'",
                )
        finally:
            self._connection_pool.release_connection(conn)

    def test_delete_all_vault_with_no_documents_creates_no_audit_rows(self):
        """delete_all_vault_documents on vault with no documents creates no audit rows."""
        # Don't seed any files - vault is empty

        # Perform delete all on empty vault
        response = self.client.delete(
            "/api/documents/vault/2/all",
            headers=self._admin_headers(),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["deleted_count"], 0)

        # Verify no audit rows were created
        conn = self._connection_pool.get_connection()
        try:
            cursor = conn.execute("SELECT COUNT(*) as cnt FROM document_actions WHERE action = 'delete'")
            row = cursor.fetchone()
            self.assertEqual(row["cnt"], 0, "Expected no audit entries for empty vault delete")
        finally:
            self._connection_pool.release_connection(conn)


if __name__ == "__main__":
    unittest.main()
