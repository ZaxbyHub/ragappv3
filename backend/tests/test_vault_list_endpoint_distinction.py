"""Tests for vault list endpoint distinction.

Verifies:
1. GET /vaults requires admin/superadmin role - returns 403 for regular members
2. GET /vaults returns all vaults with permission metadata (including null permissions) for admin/superadmin
3. GET /vaults/accessible returns only vaults where current_user_permission is not null (any authenticated user)
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
from app.services.auth_service import (
    compute_client_fingerprint,
    create_access_token,
    hash_password,
)


class TestVaultListEndpointDistinction(unittest.TestCase):
    """Tests for the distinction between GET /vaults and GET /vaults/accessible."""

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
        settings.jwt_secret_key = "test-secret-key-for-testing-at-least-32-chars-long"
        settings.users_enabled = True

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
            conn.execute("DELETE FROM users WHERE id != 0")

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
            # User 4: member without any vault access
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
                (4, "member_no_access", pw, "Member No Access", "member"),
            )

            # Create vaults
            # Vault 1: Default vault (exists from init)
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (?, ?, ?)",
                (2, "Private Vault", "A private vault"),
            )
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (?, ?, ?)",
                (3, "Org Vault", "An org-scoped vault"),
            )
            # Add a vault that NO user has any permission to (tests null permission metadata)
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (?, ?, ?)",
                (4, "No Access Vault", "Vault nobody has access to"),
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
            # admin1 (user 2) has ADMIN access to vault 3
            conn.execute(
                "INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission, granted_by) VALUES (?, ?, ?, ?)",
                (3, 2, "admin", 1),
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
        return create_access_token(1, "superadmin", "superadmin",
                                client_fingerprint=compute_client_fingerprint(""))

    def _admin_token(self):
        """Generate access token for admin user."""
        return create_access_token(2, "admin1", "admin",
                                client_fingerprint=compute_client_fingerprint(""))

    def _member_token(self):
        """Generate access token for member1 user (has vault access)."""
        return create_access_token(3, "member1", "member",
                                client_fingerprint=compute_client_fingerprint(""))

    def _member_no_access_token(self):
        """Generate access token for member2 user (no vault access)."""
        return create_access_token(4, "member_no_access", "member",
                                client_fingerprint=compute_client_fingerprint(""))

    def _auth_headers(self, token):
        """Create authorization headers with token."""
        return {"Authorization": f"Bearer {token}"}


class TestGetVaultsRequiresAdmin(TestVaultListEndpointDistinction):
    """Verify GET /vaults requires admin/superadmin role."""

    def test_regular_member_get_vaults_returns_403(self):
        """Regular member calling GET /vaults → 403 Forbidden."""
        response = self.client.get(
            "/api/vaults", headers=self._auth_headers(self._member_token())
        )
        self.assertEqual(
            response.status_code,
            403,
            f"Expected 403 for regular member on /vaults, got {response.status_code}: {response.json()}",
        )

    def test_member_with_no_access_get_vaults_returns_403(self):
        """Member with no vault access calling GET /vaults → 403 Forbidden."""
        response = self.client.get(
            "/api/vaults", headers=self._auth_headers(self._member_no_access_token())
        )
        self.assertEqual(
            response.status_code,
            403,
            f"Expected 403 for member with no access on /vaults, got {response.status_code}: {response.json()}",
        )

    def test_admin_get_vaults_returns_200(self):
        """Admin calling GET /vaults → 200 OK with all vaults."""
        response = self.client.get(
            "/api/vaults", headers=self._auth_headers(self._admin_token())
        )
        self.assertEqual(
            response.status_code,
            200,
            f"Expected 200 for admin on /vaults, got {response.status_code}: {response.json()}",
        )

    def test_superadmin_get_vaults_returns_200(self):
        """Superadmin calling GET /vaults → 200 OK with all vaults."""
        response = self.client.get(
            "/api/vaults", headers=self._auth_headers(self._superadmin_token())
        )
        self.assertEqual(
            response.status_code,
            200,
            f"Expected 200 for superadmin on /vaults, got {response.status_code}: {response.json()}",
        )


class TestGetVaultsReturnsAllVaults(TestVaultListEndpointDistinction):
    """Verify GET /vaults returns ALL vaults including those with null permission."""

    def test_admin_get_vaults_includes_vault_with_null_permission(self):
        """Admin sees vault 4 (No Access Vault) even though no one has permission to it."""
        response = self.client.get(
            "/api/vaults", headers=self._auth_headers(self._admin_token())
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        vault_ids = [v["id"] for v in data["vaults"]]
        # Vault 4 exists and admin should see it (even though no user has direct permission)
        self.assertIn(
            4,
            vault_ids,
            f"Admin should see vault 4 (No Access Vault) but got vault_ids: {vault_ids}",
        )

    def test_superadmin_get_vaults_includes_vault_with_null_permission(self):
        """Superadmin sees vault 4 (No Access Vault) even though no one has permission to it."""
        response = self.client.get(
            "/api/vaults", headers=self._auth_headers(self._superadmin_token())
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        vault_ids = [v["id"] for v in data["vaults"]]
        self.assertIn(
            4,
            vault_ids,
            f"Superadmin should see vault 4 (No Access Vault) but got vault_ids: {vault_ids}",
        )

    def test_admin_get_vaults_includes_all_vaults(self):
        """Admin sees all vaults: 1 (Default), 2 (Private), 3 (Org), 4 (No Access)."""
        response = self.client.get(
            "/api/vaults", headers=self._auth_headers(self._admin_token())
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        vault_ids = [v["id"] for v in data["vaults"]]
        self.assertIn(1, vault_ids, "Admin should see vault 1 (Default)")
        self.assertIn(2, vault_ids, "Admin should see vault 2 (Private)")
        self.assertIn(3, vault_ids, "Admin should see vault 3 (Org)")
        self.assertIn(4, vault_ids, "Admin should see vault 4 (No Access)")

    def test_superadmin_get_vaults_includes_all_vaults(self):
        """Superadmin sees all vaults: 1 (Default), 2 (Private), 3 (Org), 4 (No Access)."""
        response = self.client.get(
            "/api/vaults", headers=self._auth_headers(self._superadmin_token())
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        vault_ids = [v["id"] for v in data["vaults"]]
        self.assertIn(1, vault_ids, "Superadmin should see vault 1 (Default)")
        self.assertIn(2, vault_ids, "Superadmin should see vault 2 (Private)")
        self.assertIn(3, vault_ids, "Superadmin should see vault 3 (Org)")
        self.assertIn(4, vault_ids, "Superadmin should see vault 4 (No Access)")

    def test_admin_get_vaults_includes_write_permission_metadata_for_all_vaults(self):
        """Admin sees 'write' permission for all vaults due to baseline write floor.

        Admins get a vault-level write floor so they can read/write all vaults without being
        explicitly added as vault members. So current_user_permission is 'write' for
        all vaults, not null.
        """
        response = self.client.get(
            "/api/vaults", headers=self._auth_headers(self._admin_token())
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        # Find vault 4 (No Access Vault)
        vault_4 = next((v for v in data["vaults"] if v["id"] == 4), None)
        self.assertIsNotNone(vault_4, "Vault 4 should be in response")
        # Admin gets baseline write level for all vaults, so permission is 'write' not null
        self.assertEqual(
            vault_4["current_user_permission"],
            "write",
            "Vault 4 should have current_user_permission='write' for admin due to baseline floor",
        )


class TestGetVaultsAccessibleReturnsOnlyAccessible(
    TestVaultListEndpointDistinction
):
    """Verify GET /vaults/accessible returns only vaults with non-null permission."""

    def test_regular_member_get_vaults_accessible_returns_200(self):
        """Regular member calling GET /vaults/accessible → 200 OK."""
        response = self.client.get(
            "/api/vaults/accessible",
            headers=self._auth_headers(self._member_token()),
        )
        self.assertEqual(
            response.status_code,
            200,
            f"Expected 200 for member on /vaults/accessible, got {response.status_code}: {response.json()}",
        )

    def test_member_with_no_access_get_vaults_accessible_returns_empty(self):
        """Member with no vault access calling GET /vaults/accessible → 200 with empty list."""
        response = self.client.get(
            "/api/vaults/accessible",
            headers=self._auth_headers(self._member_no_access_token()),
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(
            data["vaults"],
            [],
            "Member with no vault access should see empty vaults list",
        )

    def test_member_get_vaults_accessible_excludes_vault_with_null_permission(self):
        """Member does NOT see vault 4 (No Access Vault) in /vaults/accessible."""
        response = self.client.get(
            "/api/vaults/accessible",
            headers=self._auth_headers(self._member_token()),
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        vault_ids = [v["id"] for v in data["vaults"]]
        self.assertNotIn(
            4,
            vault_ids,
            f"Member should NOT see vault 4 (No Access Vault) in /vaults/accessible, got: {vault_ids}",
        )
        self.assertNotIn(
            1,
            vault_ids,
            f"Member should NOT see vault 1 (Default) in /vaults/accessible, got: {vault_ids}",
        )

    def test_member_get_vaults_accessible_includes_only_accessible_vaults(self):
        """Member sees only vaults 2 and 3 (where they have permission)."""
        response = self.client.get(
            "/api/vaults/accessible",
            headers=self._auth_headers(self._member_token()),
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        vault_ids = [v["id"] for v in data["vaults"]]
        # member1 has admin on vault 2 and read on vault 3
        self.assertIn(2, vault_ids, "Member should see vault 2 (admin permission)")
        self.assertIn(3, vault_ids, "Member should see vault 3 (read permission)")
        self.assertEqual(len(vault_ids), 2, "Member should only see 2 vaults")

    def test_admin_get_vaults_accessible_returns_all_vaults_due_to_baseline_permission(self):
        """Admin using /vaults/accessible sees ALL vaults due to write baseline floor.

        Admins get vault-level write floor (level 2) so they can read/write all vaults without
        being explicitly added as vault members. This means current_user_permission is
        'write' for ALL vaults, so /vaults/accessible returns all vaults.
        """
        response = self.client.get(
            "/api/vaults/accessible",
            headers=self._auth_headers(self._admin_token()),
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        vault_ids = [v["id"] for v in data["vaults"]]
        # Admin sees all vaults due to baseline admin floor
        self.assertIn(1, vault_ids, "Admin should see vault 1 (Default)")
        self.assertIn(2, vault_ids, "Admin should see vault 2 (Private)")
        self.assertIn(3, vault_ids, "Admin should see vault 3 (Org)")
        self.assertIn(4, vault_ids, "Admin should see vault 4 (No Access) - baseline admin floor")
        self.assertEqual(len(vault_ids), 4, "Admin should see all 4 vaults")

    def test_superadmin_get_vaults_accessible_returns_all_vaults(self):
        """Superadmin using /vaults/accessible sees ALL vaults because superadmin gets 'admin' for all vaults.

        Superadmins receive 'admin' permission for every vault (see get_effective_vault_permissions).
        So /vaults/accessible returns all vaults for superadmin.
        """
        response = self.client.get(
            "/api/vaults/accessible",
            headers=self._auth_headers(self._superadmin_token()),
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        vault_ids = [v["id"] for v in data["vaults"]]
        # Superadmin gets 'admin' for all vaults, so /vaults/accessible returns all
        self.assertIn(1, vault_ids, "Superadmin should see vault 1 (Default)")
        self.assertIn(2, vault_ids, "Superadmin should see vault 2 (Private)")
        self.assertIn(3, vault_ids, "Superadmin should see vault 3 (Org)")
        self.assertIn(4, vault_ids, "Superadmin should see vault 4 (No Access)")
        self.assertEqual(len(vault_ids), 4, "Superadmin should see all 4 vaults")


class TestEndpointDistinctionSummary(TestVaultListEndpointDistinction):
    """Summary tests capturing the core behavioral distinction between the two endpoints."""

    def test_endpoint_distinction_for_member(self):
        """
        Core distinction test:
        - GET /vaults for regular member → 403 (requires admin)
        - GET /vaults/accessible for regular member → 200 (only accessible vaults)
        """
        # GET /vaults requires admin role
        vaults_response = self.client.get(
            "/api/vaults", headers=self._auth_headers(self._member_token())
        )
        self.assertEqual(vaults_response.status_code, 403)

        # GET /vaults/accessible works for any authenticated user
        accessible_response = self.client.get(
            "/api/vaults/accessible",
            headers=self._auth_headers(self._member_token()),
        )
        self.assertEqual(accessible_response.status_code, 200)
        accessible_data = accessible_response.json()
        # member1 only has access to vaults 2 and 3
        accessible_ids = [v["id"] for v in accessible_data["vaults"]]
        self.assertEqual(sorted(accessible_ids), [2, 3])

    def test_endpoint_distinction_for_admin(self):
        """
        Core distinction test:
        - GET /vaults for admin → 200 (all vaults including null permissions)
        - GET /vaults/accessible for admin → 200 (all vaults due to write baseline floor)
        """
        # GET /vaults returns ALL vaults for admin
        vaults_response = self.client.get(
            "/api/vaults", headers=self._auth_headers(self._admin_token())
        )
        self.assertEqual(vaults_response.status_code, 200)
        vaults_data = vaults_response.json()
        all_vault_ids = [v["id"] for v in vaults_data["vaults"]]
        self.assertIn(4, all_vault_ids, "Admin should see vault 4 in /vaults")

        # GET /vaults/accessible returns ALL vaults for admin due to baseline admin floor
        accessible_response = self.client.get(
            "/api/vaults/accessible",
            headers=self._auth_headers(self._admin_token()),
        )
        self.assertEqual(accessible_response.status_code, 200)
        accessible_data = accessible_response.json()
        accessible_ids = [v["id"] for v in accessible_data["vaults"]]
        # Admin sees all vaults due to baseline admin floor (FR-005)
        self.assertEqual(
            sorted(accessible_ids), [1, 2, 3, 4],
            "Admin should see all vaults in /vaults/accessible due to baseline admin floor",
        )

    def test_endpoint_distinction_for_superadmin(self):
        """
        Core distinction test:
        - GET /vaults for superadmin → 200 (all vaults)
        - GET /vaults/accessible for superadmin → 200 (all vaults, superadmin has admin on all)
        """
        # GET /vaults returns ALL vaults for superadmin
        vaults_response = self.client.get(
            "/api/vaults", headers=self._auth_headers(self._superadmin_token())
        )
        self.assertEqual(vaults_response.status_code, 200)
        vaults_data = vaults_response.json()
        all_vault_ids = [v["id"] for v in vaults_data["vaults"]]
        self.assertEqual(
            sorted(all_vault_ids), [1, 2, 3, 4],
            "Superadmin should see all 4 vaults in /vaults"
        )

        # GET /vaults/accessible returns all vaults for superadmin (admin on all vaults)
        accessible_response = self.client.get(
            "/api/vaults/accessible",
            headers=self._auth_headers(self._superadmin_token()),
        )
        self.assertEqual(accessible_response.status_code, 200)
        accessible_data = accessible_response.json()
        # Superadmin has 'admin' on all vaults, so sees all vaults
        self.assertEqual(
            sorted([v["id"] for v in accessible_data["vaults"]]), [1, 2, 3, 4],
            "Superadmin should see all vaults in /vaults/accessible (admin on all)",
        )


if __name__ == "__main__":
    unittest.main()
