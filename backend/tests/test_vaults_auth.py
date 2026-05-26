"""Tests for vault routes authentication and RBAC protection.

This test suite verifies:
- Authentication: All vault endpoints return 401 when unauthenticated
- Authorization GET /vaults: Role-based filtering (member, admin, superadmin)
- Authorization GET /vaults/{id}: Permission-based access control
- Authorization GET /vaults/accessible: Accessible vaults list
- Authorization PUT/DELETE: Admin permission checks
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

from app.api.deps import get_db, get_vector_store
from app.config import settings
from app.main import app
from app.services.auth_service import create_access_token, hash_password


class TestVaultAuthBase(unittest.TestCase):
    """Base test class for vault auth tests."""

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

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_vector_store] = lambda: self._mock_vector_store

        # Seed test users and vaults
        conn = self._connection_pool.get_connection()
        try:
            conn.execute("PRAGMA foreign_keys = ON")

            # Clear existing data
            conn.execute("DELETE FROM vault_members")
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
            # member1 (user 3) has ADMIN access to vault 2
            conn.execute(
                "INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission, granted_by) VALUES (?, ?, ?, ?)",
                (2, 3, "admin", 1),
            )
            # member1 (user 3) has READ access to vault 3
            conn.execute(
                "INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission, granted_by) VALUES (?, ?, ?, ?)",
                (3, 3, "read", 1),
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


class TestAuthentication(TestVaultAuthBase):
    """Tests for unauthenticated access - all endpoints should return 401."""

    def test_get_vaults_unauthenticated(self):
        """GET /vaults without auth returns 401."""
        response = self.client.get("/api/vaults")
        self.assertEqual(response.status_code, 401)

    def test_get_vault_by_id_unauthenticated(self):
        """GET /vaults/{id} without auth returns 401."""
        response = self.client.get("/api/vaults/1")
        self.assertEqual(response.status_code, 401)

    def test_post_vaults_unauthenticated(self):
        """POST /vaults without auth returns 401."""
        response = self.client.post("/api/vaults", json={"name": "New Vault"})
        self.assertEqual(response.status_code, 401)

    def test_put_vaults_unauthenticated(self):
        """PUT /vaults/{id} without auth returns 401."""
        response = self.client.put("/api/vaults/1", json={"name": "Updated"})
        self.assertEqual(response.status_code, 401)

    def test_delete_vaults_unauthenticated(self):
        """DELETE /vaults/{id} without auth returns 401."""
        response = self.client.delete("/api/vaults/2")
        self.assertEqual(response.status_code, 401)

    def test_get_vaults_accessible_unauthenticated(self):
        """GET /vaults/accessible without auth returns 401."""
        response = self.client.get("/api/vaults/accessible")
        self.assertEqual(response.status_code, 401)


class TestListVaultsAuthorization(TestVaultAuthBase):
    """Tests for GET /vaults authorization based on user role."""

    def test_member_sees_only_accessible_vaults(self):
        """Member user sees only vaults they have access to."""
        response = self.client.get(
            "/api/vaults", headers=self._auth_headers(self._member_token())
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("vaults", data)
        vault_ids = [v["id"] for v in data["vaults"]]
        # member1 has access to vault 2 and 3
        self.assertIn(2, vault_ids)
        self.assertIn(3, vault_ids)
        # member1 does NOT have access to vault 1 (Default)
        self.assertNotIn(1, vault_ids)

    def test_admin_sees_all_vaults(self):
        """Admin user sees all vaults."""
        response = self.client.get(
            "/api/vaults", headers=self._auth_headers(self._admin_token())
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("vaults", data)
        vault_ids = [v["id"] for v in data["vaults"]]
        # Admin sees all vaults: 1, 2, 3
        self.assertIn(1, vault_ids)
        self.assertIn(2, vault_ids)
        self.assertIn(3, vault_ids)

    def test_superadmin_sees_all_vaults(self):
        """Superadmin user sees all vaults."""
        response = self.client.get(
            "/api/vaults", headers=self._auth_headers(self._superadmin_token())
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("vaults", data)
        vault_ids = [v["id"] for v in data["vaults"]]
        # Superadmin sees all vaults: 1, 2, 3
        self.assertIn(1, vault_ids)
        self.assertIn(2, vault_ids)
        self.assertIn(3, vault_ids)

    def test_member_with_no_vault_access_sees_empty_list(self):
        """Member with no vault access sees empty list."""
        response = self.client.get(
            "/api/vaults", headers=self._auth_headers(self._member_no_access_token())
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("vaults", data)
        self.assertEqual(data["vaults"], [])


class TestGetVaultByIdAuthorization(TestVaultAuthBase):
    """Tests for GET /vaults/{id} authorization based on vault access."""

    def test_member_with_read_access_to_vault_returns_200(self):
        """Member with admin access to vault → 200."""
        response = self.client.get(
            "/api/vaults/2", headers=self._auth_headers(self._member_token())
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["id"], 2)
        self.assertEqual(data["name"], "Private Vault")

    def test_member_without_access_to_vault_returns_403(self):
        """Member without access to vault → 403."""
        # member2 (token) has no vault_members entries, so no access to vault 2
        response = self.client.get(
            "/api/vaults/2", headers=self._auth_headers(self._member_no_access_token())
        )
        self.assertEqual(response.status_code, 403)

    def test_admin_with_no_direct_access_can_read_any_vault(self):
        """Admin can read any vault even without explicit vault_members entry."""
        response = self.client.get(
            "/api/vaults/2", headers=self._auth_headers(self._admin_token())
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["id"], 2)


class TestListAccessibleVaultsAuthorization(TestVaultAuthBase):
    """Tests for GET /vaults/accessible authorization."""

    def test_member_sees_only_accessible_vaults(self):
        """Member sees only accessible vaults."""
        response = self.client.get(
            "/api/vaults/accessible", headers=self._auth_headers(self._member_token())
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("vaults", data)
        vault_ids = [v["id"] for v in data["vaults"]]
        # member1 has access to vault 2 and 3
        self.assertIn(2, vault_ids)
        self.assertIn(3, vault_ids)
        # member1 does NOT have access to vault 1
        self.assertNotIn(1, vault_ids)

    def test_admin_sees_all_vaults_in_accessible(self):
        """Admin sees all vaults in accessible endpoint."""
        response = self.client.get(
            "/api/vaults/accessible", headers=self._auth_headers(self._admin_token())
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("vaults", data)
        vault_ids = [v["id"] for v in data["vaults"]]
        # Admin sees all vaults
        self.assertIn(1, vault_ids)
        self.assertIn(2, vault_ids)
        self.assertIn(3, vault_ids)


class TestPutDeleteVaultAuthorization(TestVaultAuthBase):
    """Tests for PUT/DELETE vault authorization based on admin permission."""

    def test_member_without_admin_permission_put_returns_403(self):
        """Member without admin permission on vault → 403 on PUT."""
        # member1 has READ permission on vault 3, not admin
        response = self.client.put(
            "/api/vaults/3",
            json={"name": "Updated Name"},
            headers=self._auth_headers(self._member_token()),
        )
        self.assertEqual(response.status_code, 403)

    def test_member_without_admin_permission_delete_returns_403(self):
        """Member without admin permission on vault → 403 on DELETE."""
        # member1 has READ permission on vault 3, not admin
        response = self.client.delete(
            "/api/vaults/3", headers=self._auth_headers(self._member_token())
        )
        self.assertEqual(response.status_code, 403)

    def test_member_with_admin_permission_can_put(self):
        """Member with admin permission on vault → can PUT (200)."""
        # member1 has ADMIN permission on vault 2
        response = self.client.put(
            "/api/vaults/2",
            json={"name": "Updated Vault Name"},
            headers=self._auth_headers(self._member_token()),
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["name"], "Updated Vault Name")

    def test_superadmin_can_put_any_vault(self):
        """Superadmin can PUT any vault."""
        response = self.client.put(
            "/api/vaults/2",
            json={"name": "Superadmin Updated"},
            headers=self._auth_headers(self._superadmin_token()),
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["name"], "Superadmin Updated")

    def test_superadmin_can_delete_any_vault(self):
        """Superadmin can DELETE any vault (except vault 1 which is protected)."""
        # Create a new vault to delete
        conn = self._get_db_conn()
        try:
            conn.execute(
                "INSERT INTO vaults (name, description) VALUES (?, ?)",
                ("Test Vault 99", "Test vault for deletion"),
            )
            conn.commit()
            # Get the vault id
            cursor = conn.execute(
                "SELECT id FROM vaults WHERE name = ?", ("Test Vault 99",)
            )
            row = cursor.fetchone()
            vault_id = row[0]
        finally:
            self._connection_pool.release_connection(conn)

        response = self.client.delete(
            f"/api/vaults/{vault_id}",
            headers=self._auth_headers(self._superadmin_token()),
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("deleted", data["message"].lower())

    def test_admin_without_vault_membership_cannot_put(self):
        """Admin user without explicit vault membership cannot PUT vault."""
        response = self.client.put(
            "/api/vaults/2",
            json={"name": "Vault Updated By Admin"},
            headers=self._auth_headers(self._admin_token()),
        )
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
