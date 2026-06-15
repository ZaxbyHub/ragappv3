"""Tests for scan_directories endpoint authorization change (Task 4.1).

Verifies that scan_directories uses require_admin_role instead of require_vault_permission("write").

CHANGE: scan_directories endpoint auth changed from require_vault_permission("write") to require_admin_role.

This test suite verifies:
1. scan_directories decorator uses require_admin_role
2. A member WITH vault write permission but WITHOUT admin role → 403 (admin required)
3. An admin/superadmin → can access (no 401/403)
"""

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

from app.api.deps import (
    get_background_processor,
    get_db,
    get_db_pool,
    get_embedding_service,
    get_vector_store,
)
from app.config import settings
from app.main import app
from app.services.auth_service import create_access_token


class TestScanDirectoriesAuthBase(unittest.TestCase):
    """Base test class for scan_directories auth tests."""

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
        self._mock_vector_store.db = None
        self._mock_vector_store.delete_by_file = AsyncMock(return_value=1)

        # Mock embedding service
        self._mock_embedding_service = MagicMock()

        # Mock db pool
        self._mock_db_pool = self._connection_pool

        # Mock background processor
        self._mock_background_processor = MagicMock()
        self._mock_background_processor.is_running = True
        self._mock_background_processor.enqueue = AsyncMock()

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_vector_store] = lambda: self._mock_vector_store
        app.dependency_overrides[get_embedding_service] = lambda: (
            self._mock_embedding_service
        )
        app.dependency_overrides[get_db_pool] = lambda: self._mock_db_pool
        app.dependency_overrides[get_background_processor] = lambda: (
            self._mock_background_processor
        )

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
            # User 3: member WITH write vault access
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
                (3, "member_writer", pw, "Member Writer", "member"),
            )
            # User 4: member with read-only vault access
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
                (4, "member_readonly", pw, "Member Read Only", "member"),
            )
            # User 5: member without any vault access
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
                (5, "member_novault", pw, "Member No Vault", "member"),
            )

            # Create vaults for testing
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (?, ?, ?)",
                (1, "Default Vault", "Default vault"),
            )
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (?, ?, ?)",
                (2, "Private Vault", "A private vault"),
            )
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (?, ?, ?)",
                (3, "Read-Only Vault", "A read-only vault"),
            )

            # Seed vault_members
            # member_writer (user 3) has WRITE access to vault 2
            conn.execute(
                "INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission, granted_by) VALUES (?, ?, ?, ?)",
                (2, 3, "write", 1),
            )
            # member_readonly (user 4) has READ access to vault 3
            conn.execute(
                "INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission, granted_by) VALUES (?, ?, ?, ?)",
                (3, 4, "read", 1),
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
        app.dependency_overrides.pop(get_embedding_service, None)
        app.dependency_overrides.pop(get_db_pool, None)
        app.dependency_overrides.pop(get_background_processor, None)

        if hasattr(self, "_connection_pool"):
            self._connection_pool.close_all()

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

    def _member_writer_token(self):
        """Generate access token for member_writer user (has vault write access)."""
        return create_access_token(3, "member_writer", "member")

    def _member_readonly_token(self):
        """Generate access token for member_readonly user."""
        return create_access_token(4, "member_readonly", "member")

    def _member_no_access_token(self):
        """Generate access token for member_novault user (no vault access)."""
        return create_access_token(5, "member_novault", "member")

    def _auth_headers(self, token):
        """Create authorization headers with token."""
        return {"Authorization": f"Bearer {token}"}


class TestScanDirectoriesRequiresAdminRole(TestScanDirectoriesAuthBase):
    """Tests that scan_directories uses require_admin_role auth decorator.

    CHANGE VERIFICATION: scan_directories changed from require_vault_permission("write")
    to require_admin_role. This means:
    - OLD: member with vault write permission could scan
    - NEW: only admin/superadmin can scan (member with vault write permission gets 403)
    """

    def test_scan_endpoint_requires_admin_role_not_vault_permission(self):
        """scan_directories uses require_admin_role decorator.

        A member WITH vault write permission but WITHOUT admin role should get 403
        with "Admin access required" message.

        This is the KEY TEST for the auth change:
        - OLD behavior: require_vault_permission("write") → member with write access passes
        - NEW behavior: require_admin_role → member with write access gets 403
        """
        # member_writer (user 3) has WRITE permission on vault 2
        # But with require_admin_role, this member should get 403
        response = self.client.post(
            "/api/documents/scan",
            headers=self._auth_headers(self._member_writer_token()),
        )
        self.assertEqual(
            response.status_code,
            403,
            msg=(
                "Member with vault write permission should get 403 with require_admin_role. "
                f"Got {response.status_code}: {response.text}"
            ),
        )
        self.assertIn("admin access required", response.text.lower())

    def test_member_with_vault_write_but_no_admin_role_returns_403(self):
        """Member with vault write permission but no admin role → 403 (admin required)."""
        # member_writer has vault write access but is not admin
        response = self.client.post(
            "/api/documents/scan",
            headers=self._auth_headers(self._member_writer_token()),
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("admin access required", response.text.lower())

    def test_admin_can_access_scan_directories(self):
        """Admin can access POST /documents/scan."""
        response = self.client.post(
            "/api/documents/scan",
            headers=self._auth_headers(self._admin_token()),
        )
        # Admin should not get 401 (unauthenticated) or 403 (forbidden)
        # May get 500 if background processor is not fully set up, but not auth error
        self.assertNotEqual(
            response.status_code,
            401,
            msg="Admin should not get 401 Unauthenticated",
        )
        self.assertNotEqual(
            response.status_code,
            403,
            msg="Admin should not get 403 Forbidden",
        )

    def test_superadmin_can_access_scan_directories(self):
        """Superadmin can access POST /documents/scan."""
        response = self.client.post(
            "/api/documents/scan",
            headers=self._auth_headers(self._superadmin_token()),
        )
        # Superadmin should not get 401 (unauthenticated) or 403 (forbidden)
        self.assertNotEqual(
            response.status_code,
            401,
            msg="Superadmin should not get 401 Unauthenticated",
        )
        self.assertNotEqual(
            response.status_code,
            403,
            msg="Superadmin should not get 403 Forbidden",
        )

    def test_member_readonly_cannot_scan(self):
        """Member with read-only vault access cannot scan (already returns 403)."""
        response = self.client.post(
            "/api/documents/scan",
            headers=self._auth_headers(self._member_readonly_token()),
        )
        self.assertEqual(response.status_code, 403)

    def test_member_no_vault_access_cannot_scan(self):
        """Member without any vault access cannot scan (already returns 403)."""
        response = self.client.post(
            "/api/documents/scan",
            headers=self._auth_headers(self._member_no_access_token()),
        )
        self.assertEqual(response.status_code, 403)


class TestScanDirectoriesDecoratorVerification(TestScanDirectoriesAuthBase):
    """Direct verification that scan_directories route uses require_admin_role.

    This class tests the decorator chain by inspecting FastAPI route dependencies.
    """

    def test_scan_route_uses_require_admin_role_dependency(self):
        """Verify scan_directories route has require_admin_role in its dependency chain.

        FastAPI stores route dependencies in route.dependant.dependencies.
        Each dependency has a name that identifies the dependency function.

        The scan route has path "/scan" within the documents router (full path is "/documents/scan").
        """
        from app.api.routes.documents import router as documents_router

        # Find the scan route from the documents router directly
        # (app.routes may not expose APIRouter routes on all FastAPI versions)
        scan_route = None
        for route in documents_router.routes:
            if hasattr(route, "path") and "/scan" in route.path:
                if hasattr(route, "methods") and "POST" in route.methods:
                    scan_route = route
                    break

        self.assertIsNotNone(
            scan_route,
            f"Could not find POST /scan route in documents router. "
            f"Available routes: {[(r.path, getattr(r, 'methods', None)) for r in documents_router.routes if hasattr(r, 'path')]}",
        )

        # Check the dependencies for require_admin_role
        # In FastAPI, the dependency chain is stored in route.dependant.dependencies
        # We need to check the actual callable's __name__ or module to identify it
        dependency_functions = []
        if hasattr(scan_route, "dependant") and scan_route.dependant:
            for dep in scan_route.dependant.dependencies:
                if hasattr(dep, "call") and dep.call:
                    call = dep.call
                    # Get the function name
                    func_name = getattr(call, "__name__", str(call))
                    # Get the module if available
                    module = getattr(call, "__module__", "")
                    dependency_functions.append(f"{module}.{func_name}" if module else func_name)

        # Verify require_admin_role is in the dependency chain
        # The function is defined in app.api.deps as 'require_admin_role'
        require_admin_role_found = any(
            "require_admin_role" in func for func in dependency_functions
        )
        self.assertTrue(
            require_admin_role_found,
            f"scan_directories should use require_admin_role. Found dependencies: {dependency_functions}",
        )

        # Verify require_vault_permission is NOT in the dependency chain
        require_vault_permission_found = any(
            "require_vault_permission" in func for func in dependency_functions
        )
        self.assertFalse(
            require_vault_permission_found,
            f"scan_directories should NOT use require_vault_permission anymore. Found: {dependency_functions}",
        )


if __name__ == "__main__":
    unittest.main()
