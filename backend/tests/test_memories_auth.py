"""Tests for memories routes authentication and RBAC protection.

This test suite verifies:
- Authentication: All memory endpoints return 401 when unauthenticated
- Authorization POST /memories: Requires vault write access
- Authorization PUT /memories/{id}: Requires vault write access
- Authorization DELETE /memories/{id}: Requires vault admin access
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

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

import sqlite3

from _db_pool import SimpleConnectionPool
from fastapi.testclient import TestClient

from app.api.deps import get_db, get_memory_store, get_vector_store
from app.config import settings
from app.main import app
from app.services.auth_service import create_access_token, hash_password


class TestMemoriesAuthBase(unittest.TestCase):
    """Base test class for memories auth tests."""

    def setUp(self):
        """Set up test client with temporary database."""
        self.client = TestClient(app)
        self._temp_dir = tempfile.mkdtemp()

        # Store original settings BEFORE modifying
        self._original_jwt_secret = settings.jwt_secret_key
        self._original_users_enabled = settings.users_enabled
        self._original_data_dir = settings.data_dir

        # CRITICAL: Update settings.data_dir so sqlite_path points to test db
        settings.data_dir = Path(self._temp_dir)
        settings.jwt_secret_key = "test-secret-key-for-testing-at-least-32-chars-long"
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
        self._mock_vector_store.delete_by_vault = MagicMock(return_value=0)

        # Mock memory store for tests
        self._mock_memory_store = MagicMock()

        class MockMemoryRecord:
            def __init__(
                self,
                id,
                content,
                category,
                tags,
                source,
                vault_id,
                importance,
                expires_at,
                created_at,
                updated_at,
            ):
                self.id = id
                self.content = content
                self.category = category
                self.tags = tags
                self.source = source
                self.vault_id = vault_id
                self.importance = importance
                self.expires_at = expires_at
                self.created_at = created_at
                self.updated_at = updated_at

        def mock_add_memory(*args, **kwargs):
            """Mock add_memory that returns a MockMemoryRecord."""
            return MockMemoryRecord(
                id=1,
                content=kwargs.get("content", ""),
                category=kwargs.get("category"),
                tags=kwargs.get("tags"),
                source=kwargs.get("source"),
                vault_id=kwargs.get("vault_id"),
                importance=kwargs.get("importance", 0.5),
                expires_at=kwargs.get("expires_at"),
                created_at="2024-01-01T00:00:00",
                updated_at="2024-01-01T00:00:00",
            )

        self._mock_memory_store.add_memory = mock_add_memory

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_vector_store] = lambda: self._mock_vector_store
        app.dependency_overrides[get_memory_store] = lambda: self._mock_memory_store

        # Seed test users and vaults
        conn = self._connection_pool.get_connection()
        try:
            conn.execute("PRAGMA foreign_keys = ON")

            # Clear existing data
            conn.execute("DELETE FROM vault_members")
            conn.execute("DELETE FROM memories")
            conn.execute("DELETE FROM users WHERE id != 0")  # Keep admin user if exists

            pw = hash_password("testpass")

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
            # User 4: member without vault access
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
                (4, "member2", pw, "Member Two", "member"),
            )

            # Create additional vaults for testing (Vault 1 is the Default vault)
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (?, ?, ?)",
                (2, "Private Vault", "A private vault"),
            )
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (?, ?, ?)",
                (3, "Vault Three", "Third vault"),
            )

            # Seed vault_members
            # member1 (user 3) has WRITE access to vault 2
            conn.execute(
                "INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission, granted_by) VALUES (?, ?, ?, ?)",
                (2, 3, "write", 1),
            )
            # member1 (user 3) has ADMIN access to vault 3
            conn.execute(
                "INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission, granted_by) VALUES (?, ?, ?, ?)",
                (3, 3, "admin", 1),
            )

            # Seed memories for testing
            conn.execute(
                "INSERT INTO memories (content, category, tags, source, vault_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                ("Test memory 1", "test", '["tag1"]', "test", 2),
            )
            conn.execute(
                "INSERT INTO memories (content, category, tags, source, vault_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                ("Test memory 2", "test", '["tag2"]', "test", 3),
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

        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_vector_store, None)
        app.dependency_overrides.pop(get_memory_store, None)
        if hasattr(self, "_connection_pool"):
            self._connection_pool.close_all()
        import shutil

        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _get_db_conn(self):
        """Get a raw connection for test data setup."""
        return self._connection_pool.get_connection()

    def _superadmin_token(self):
        """Generate access token for superadmin user."""
        return create_access_token(1, "superadmin", "superadmin")

    def _admin_token(self):
        """Generate access token for admin user."""
        return create_access_token(2, "admin1", "admin")

    def _member_token(self):
        """Generate access token for member1 user (has vault access)."""
        return create_access_token(3, "member1", "member")

    def _member_no_access_token(self):
        """Generate access token for member2 user (no vault access)."""
        return create_access_token(4, "member2", "member")

    def _auth_headers(self, token):
        """Create authorization headers with token."""
        return {"Authorization": f"Bearer {token}"}


class TestMemoriesAuthentication(TestMemoriesAuthBase):
    """Tests for unauthenticated access - all endpoints should return 401."""

    def test_post_memories_unauthenticated(self):
        """POST /memories without auth returns 401."""
        response = self.client.post(
            "/api/memories", json={"content": "Test", "vault_id": 2}
        )
        self.assertEqual(response.status_code, 401)

    def test_put_memories_unauthenticated(self):
        """PUT /memories/{id} without auth returns 401."""
        response = self.client.put("/api/memories/1", json={"content": "Updated"})
        self.assertEqual(response.status_code, 401)

    def test_delete_memories_unauthenticated(self):
        """DELETE /memories/{id} without auth returns 401."""
        response = self.client.delete("/api/memories/1")
        self.assertEqual(response.status_code, 401)


class TestMemoriesAuthorization(TestMemoriesAuthBase):
    """Tests for memories endpoint authorization."""

    def test_post_memories_member_without_vault_write_returns_403(self):
        """POST /memories by member without vault write access returns 403."""
        # member2 has no vault access, attempting to create memory in vault 2
        response = self.client.post(
            "/api/memories",
            json={"content": "Test memory", "vault_id": 2},
            headers=self._auth_headers(self._member_no_access_token()),
        )
        self.assertEqual(response.status_code, 403)
        data = response.json()
        self.assertIn("No write access", data.get("detail", ""))

    def test_post_memories_member_with_vault_write_succeeds(self):
        """POST /memories by member with vault write access succeeds."""
        # member1 has write access to vault 2
        response = self.client.post(
            "/api/memories",
            json={"content": "Test memory", "vault_id": 2},
            headers=self._auth_headers(self._member_token()),
        )
        # Should succeed since we mocked memory_store
        self.assertEqual(response.status_code, 200)

    def test_delete_memories_member_without_vault_admin_returns_403(self):
        """DELETE /memories/{id} by member without vault admin access returns 403."""
        # member1 has write access to vault 2, but not admin
        # Need to get the memory ID for vault 2
        conn = self._get_db_conn()
        try:
            cursor = conn.execute("SELECT id FROM memories WHERE vault_id = 2")
            row = cursor.fetchone()
            memory_id = row[0] if row else 1
        finally:
            self._connection_pool.release_connection(conn)

        response = self.client.delete(
            f"/api/memories/{memory_id}",
            headers=self._auth_headers(self._member_token()),
        )
        # member1 has write access but not admin, so should get 403
        self.assertEqual(response.status_code, 403)
        data = response.json()
        self.assertIn("No admin access", data.get("detail", ""))

    def test_delete_memories_member_with_vault_admin_succeeds(self):
        """DELETE /memories/{id} by member with vault admin access succeeds."""
        # member1 has admin access to vault 3
        # Need to get the memory ID for vault 3
        conn = self._get_db_conn()
        try:
            cursor = conn.execute("SELECT id FROM memories WHERE vault_id = 3")
            row = cursor.fetchone()
            memory_id = row[0] if row else 2
        finally:
            self._connection_pool.release_connection(conn)

        response = self.client.delete(
            f"/api/memories/{memory_id}",
            headers=self._auth_headers(self._member_token()),
        )
        # Should succeed since member1 has admin access to vault 3
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("deleted successfully", data.get("message", ""))


if __name__ == "__main__":
    unittest.main()
