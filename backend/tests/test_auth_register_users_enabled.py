"""
Verification tests for the /register endpoint users_enabled guard.

Covers:
- 403 + "Registration disabled" when users_enabled=False
- Happy-path registration when users_enabled=True
- users_enabled guard fires before other validation (username/password)
- setup-status reflects users_enabled=False correctly
- Toggling users_enabled between requests

Source: backend/app/api/routes/auth.py lines 230-231
"""

import os
import sys
import tempfile
import unittest

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies (same pattern as conftest.py / other test files)
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

from app.config import settings
from app.models.database import SQLiteConnectionPool, init_db, run_migrations


class TestRegisterUsersEnabledGuard(unittest.TestCase):
    """Tests for the /register endpoint users_enabled guard (auth.py lines 230-231)."""

    def setUp(self):
        """Set up test client with temporary database and users_enabled=True."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")

        init_db(self.db_path)
        run_migrations(self.db_path)

        # Save originals
        self._orig_jwt_secret = settings.jwt_secret_key
        self._orig_users_enabled = settings.users_enabled
        self._orig_app_root_path = settings.app_root_path

        # Configure for testing
        settings.jwt_secret_key = "test-secret-key-for-testing-at-least-32-chars-long"
        settings.users_enabled = True
        settings.app_root_path = ""

        # Test pool and app
        self.test_pool = SQLiteConnectionPool(self.db_path, max_size=5)

        from app.api.deps import get_db
        from app.main import app as main_app
        from app.security import csrf_protect

        class TestCSRFManager:
            def generate_token(self):
                return "test-csrf-token"

            def validate_token(self, token):
                return token == "test-csrf-token"

        def get_test_db():
            conn = self.test_pool.get_connection()
            try:
                yield conn
            finally:
                self.test_pool.release_connection(conn)

        main_app.dependency_overrides[get_db] = get_test_db
        main_app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
        main_app.state.csrf_manager = TestCSRFManager()

        self.client = TestClient(main_app)
        self.app = main_app

    def tearDown(self):
        """Restore settings and clean up resources."""
        settings.jwt_secret_key = self._orig_jwt_secret
        settings.users_enabled = self._orig_users_enabled
        settings.app_root_path = self._orig_app_root_path

        self.app.dependency_overrides.clear()
        self.test_pool.close_all()

        import shutil
        try:
            shutil.rmtree(self.temp_dir)
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # users_enabled=False → 403 "Registration disabled"
    # -------------------------------------------------------------------------

    def test_register_returns_403_when_users_enabled_false(self):
        """POST /auth/register with users_enabled=False → 403 + detail 'Registration disabled'."""
        settings.users_enabled = False

        response = self.client.post(
            "/api/auth/register",
            json={"username": "anyuser", "password": "Password123"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "Registration disabled")

    def test_register_users_enabled_false_does_not_create_user(self):
        """When users_enabled=False, no user record should be created."""
        settings.users_enabled = False

        self.client.post(
            "/api/auth/register",
            json={"username": "ghostuser", "password": "Password123"},
        )

        # Verify no user was created in the database
        from app.api.deps import get_db
        conn = self.test_pool.get_connection()
        try:
            cursor = conn.execute("SELECT id FROM users WHERE username = ?", ("ghostuser",))
            row = cursor.fetchone()
            self.assertIsNone(row, "No user should be created when users_enabled=False")
        finally:
            self.test_pool.release_connection(conn)

    def test_register_users_enabled_false_allows_any_username_and_password(self):
        """Guard fires before username/password validation — weak creds still get 403, not 400."""
        settings.users_enabled = False

        # These would be 400 if validation ran first; guard short-circuits first
        response = self.client.post(
            "/api/auth/register",
            json={"username": "ab", "password": "weak"},  # too short for both fields
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "Registration disabled")

    # -------------------------------------------------------------------------
    # users_enabled=True → registration proceeds normally
    # -------------------------------------------------------------------------

    def test_register_succeeds_when_users_enabled_true(self):
        """POST /auth/register with users_enabled=True → 200 + user object."""
        response = self.client.post(
            "/api/auth/register",
            json={"username": "newuser", "password": "Password123"},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["user"]["username"], "newuser")
        self.assertEqual(data["user"]["role"], "superadmin")  # first user
        self.assertIn("access_token", data)
        self.assertEqual(data["token_type"], "bearer")

    def test_register_first_user_is_superadmin_when_users_enabled_true(self):
        """First registered user gets superadmin role."""
        response = self.client.post(
            "/api/auth/register",
            json={"username": "firstadmin", "password": "Password123"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"]["role"], "superadmin")

    def test_register_second_user_is_member_when_users_enabled_true(self):
        """Second registered user gets member role."""
        self.client.post(
            "/api/auth/register",
            json={"username": "admin1", "password": "Password123"},
        )

        response = self.client.post(
            "/api/auth/register",
            json={"username": "member1", "password": "Password456"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"]["role"], "member")

    # -------------------------------------------------------------------------
    # Toggle: disabled → enabled
    # -------------------------------------------------------------------------

    def test_register_toggle_from_disabled_to_enabled(self):
        """Can re-enable registration after testing disabled state within same test."""
        # Disabled first
        settings.users_enabled = False
        resp_disabled = self.client.post(
            "/api/auth/register",
            json={"username": "user_disabled", "password": "Password123"},
        )
        self.assertEqual(resp_disabled.status_code, 403)

        # Re-enable
        settings.users_enabled = True
        resp_enabled = self.client.post(
            "/api/auth/register",
            json={"username": "user_reenabled", "password": "Password123"},
        )
        self.assertEqual(resp_enabled.status_code, 200)
        self.assertEqual(resp_enabled.json()["user"]["username"], "user_reenabled")

    # -------------------------------------------------------------------------
    # setup-status reflects users_enabled correctly
    # -------------------------------------------------------------------------

    def test_setup_status_reports_single_admin_when_users_enabled_false(self):
        """GET /auth/setup-status with users_enabled=False returns auth_mode='single_admin'."""
        settings.users_enabled = False

        response = self.client.get("/api/auth/setup-status")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["users_enabled"], False)
        self.assertEqual(data["auth_mode"], "single_admin")
        self.assertEqual(data["needs_setup"], False)

    def test_setup_status_reports_jwt_when_users_enabled_true(self):
        """GET /auth/setup-status with users_enabled=True returns auth_mode='jwt'."""
        settings.users_enabled = True

        response = self.client.get("/api/auth/setup-status")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["users_enabled"], True)
        self.assertEqual(data["auth_mode"], "jwt")

    # -------------------------------------------------------------------------
    # Guard fires before other validations
    # -------------------------------------------------------------------------

    def test_users_enabled_guard_precedes_username_validation(self):
        """users_enabled=False check runs before username length check."""
        settings.users_enabled = False

        response = self.client.post(
            "/api/auth/register",
            json={"username": "x", "password": "Password123"},
        )

        # Would be 400 "Username must be at least 3 characters" if guard didn't fire first
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "Registration disabled")

    def test_users_enabled_guard_precedes_password_validation(self):
        """users_enabled=False check runs before password strength check."""
        settings.users_enabled = False

        response = self.client.post(
            "/api/auth/register",
            json={"username": "validuser", "password": "weak"},
        )

        # Would be 400 if guard didn't short-circuit
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "Registration disabled")

    def test_users_enabled_guard_precedes_username_uniqueness_check(self):
        """users_enabled=False check runs before duplicate-username check."""
        # Pre-create a user (normal flow)
        settings.users_enabled = True
        self.client.post(
            "/api/auth/register",
            json={"username": "duplicate", "password": "Password123"},
        )

        # Now disable and try to register same username
        settings.users_enabled = False
        response = self.client.post(
            "/api/auth/register",
            json={"username": "duplicate", "password": "Password456"},
        )

        # Would be 409 "Username already exists" if guard didn't fire first
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "Registration disabled")


if __name__ == "__main__":
    unittest.main()
