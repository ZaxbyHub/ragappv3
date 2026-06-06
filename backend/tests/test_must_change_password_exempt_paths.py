"""Tests for must_change_password exempt-paths fix and flag clearing.

Tests cover:
1. User with must_change_password=1 can call POST /api/auth/change-password (403 NOT returned)
2. After successful change-password, DB flag is cleared (must_change_password=0)
3. User can then access protected routes (GET /api/auth/me) without 403
4. User without must_change_password (already 0) changing password stays 0
5. Users without must_change_password can still call login and change-password normally

The exempt_paths fix in deps.py adds /api/auth/change-password and /api/auth/login
so that flagged users can actually reach the change-password endpoint (routers are
mounted at /api in main.py, so the request path is /api/auth/change-password).

Uses FastAPI TestClient with dependency overrides for isolated testing.
"""

import os
import sys
import tempfile
import unittest

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

from fastapi.testclient import TestClient

from app.config import settings
from app.models.database import SQLiteConnectionPool, init_db, run_migrations


class TestMustChangePasswordExemptPaths(unittest.TestCase):
    """Test suite for must_change_password exempt paths fix (deps.py)."""

    def setUp(self):
        """Set up test client with temporary database."""
        # Create temporary database
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")

        # Initialize database with schema
        init_db(self.db_path)
        run_migrations(self.db_path)

        # Store original settings to restore later
        self._original_jwt_secret = settings.jwt_secret_key
        self._original_users_enabled = settings.users_enabled
        self._original_app_root_path = settings.app_root_path

        # Override JWT secret for testing
        settings.jwt_secret_key = "test-secret-key-for-testing-at-least-32-chars-long"
        settings.users_enabled = True
        settings.app_root_path = ""

        # Create a test pool for the temporary database
        self.test_pool = SQLiteConnectionPool(self.db_path, max_size=5)

        # Create FastAPI app and configure dependency overrides
        from app.api.deps import get_db
        from app.main import app as main_app
        from app.security import csrf_protect

        class TestCSRFManager:
            def generate_token(self):
                return "test-csrf-token"

            def validate_token(self, token):
                return token == "test-csrf-token"

        # Override the get_db dependency to use our test pool
        def get_test_db():
            conn = self.test_pool.get_connection()
            try:
                yield conn
            finally:
                self.test_pool.release_connection(conn)

        main_app.dependency_overrides[get_db] = get_test_db
        main_app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
        main_app.state.csrf_manager = TestCSRFManager()

        # Create test client with dependency overrides
        self.client = TestClient(main_app)
        self.app = main_app

    def tearDown(self):
        """Clean up after each test."""
        # Restore original settings
        settings.jwt_secret_key = self._original_jwt_secret
        settings.users_enabled = self._original_users_enabled
        settings.app_root_path = self._original_app_root_path

        # Clear dependency overrides
        self.app.dependency_overrides.clear()

        # Close the test pool
        self.test_pool.close_all()

        # Clean up temp directory
        import shutil

        try:
            shutil.rmtree(self.temp_dir)
        except Exception:
            pass

    def _get_user_id(self, username: str) -> int:
        """Helper to get user ID by username."""
        conn = self.test_pool.get_connection()
        try:
            cursor = conn.execute(
                "SELECT id FROM users WHERE username = ?", (username,)
            )
            row = cursor.fetchone()
            return row[0] if row else None
        finally:
            self.test_pool.release_connection(conn)

    def _get_must_change_password(self, user_id: int) -> int:
        """Helper to get must_change_password flag for a user."""
        conn = self.test_pool.get_connection()
        try:
            cursor = conn.execute(
                "SELECT must_change_password FROM users WHERE id = ?", (user_id,)
            )
            row = cursor.fetchone()
            return int(row[0]) if row else None
        finally:
            self.test_pool.release_connection(conn)

    def _set_must_change_password(self, user_id: int, value: int) -> None:
        """Helper to set must_change_password flag for a user."""
        conn = self.test_pool.get_connection()
        try:
            conn.execute(
                "UPDATE users SET must_change_password = ? WHERE id = ?",
                (value, user_id),
            )
            conn.commit()
        finally:
            self.test_pool.release_connection(conn)

    # ------------------------------------------------------------------
    # Test 1: User with must_change_password=1 CAN call change-password
    # (exempt paths fix: /api/auth/change-password is now in exempt_paths)
    # ------------------------------------------------------------------
    def test_flagged_user_can_call_change_password_endpoint(self):
        """User with must_change_password=1 can call POST /api/auth/change-password.

        Before the exempt_paths fix, the must_change_password check in
        get_current_active_user blocked access to /api/auth/change-password
        because exempt_paths only contained /auth/change-password (without /api
        prefix). Since routers are mounted at /api in main.py, the actual
        request path is /api/auth/change-password, which was NOT exempt.
        This test verifies the fix allows flagged users to reach the endpoint.
        """
        # Register user
        self.client.post(
            "/api/auth/register",
            json={"username": "flaggeduser", "password": "OldPass123"},
        )

        # Login to get access token
        login_response = self.client.post(
            "/api/auth/login",
            json={"username": "flaggeduser", "password": "OldPass123"},
        )
        self.assertEqual(login_response.status_code, 200)
        access_token = login_response.json()["access_token"]

        # Get user ID and set must_change_password = 1 (simulate admin reset)
        user_id = self._get_user_id("flaggeduser")
        self._set_must_change_password(user_id, 1)

        # Verify flag is set
        flag_before = self._get_must_change_password(user_id)
        self.assertEqual(flag_before, 1)

        # Attempt to change password — should NOT return 403
        # This is the key assertion: before the fix, this returned 403
        # "must_change_password" because /api/auth/change-password was not exempt
        response = self.client.post(
            "/api/auth/change-password",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"current_password": "OldPass123", "new_password": "NewPass456"},
        )

        self.assertEqual(
            response.status_code, 200,
            f"Expected 200 but got {response.status_code}: {response.json()}"
        )
        data = response.json()
        self.assertIn("access_token", data)
        self.assertEqual(data["token_type"], "bearer")

    # ------------------------------------------------------------------
    # Test 2: After change-password, flag is cleared in DB
    # ------------------------------------------------------------------
    def test_flag_cleared_in_db_after_successful_change_password(self):
        """After successful change-password, must_change_password=0 in the database.

        Verifies the UPDATE users SET must_change_password = 0 WHERE id = ?
        executes correctly inside _change_password_db.
        """
        # Register user
        self.client.post(
            "/api/auth/register",
            json={"username": "flaggeduser2", "password": "OldPass123"},
        )

        # Login to get access token
        login_response = self.client.post(
            "/api/auth/login",
            json={"username": "flaggeduser2", "password": "OldPass123"},
        )
        self.assertEqual(login_response.status_code, 200)
        access_token = login_response.json()["access_token"]

        # Get user ID and set must_change_password = 1
        user_id = self._get_user_id("flaggeduser2")
        self._set_must_change_password(user_id, 1)

        # Change password
        response = self.client.post(
            "/api/auth/change-password",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"current_password": "OldPass123", "new_password": "NewPass456"},
        )
        self.assertEqual(response.status_code, 200)

        # Verify flag is cleared in the database
        flag_after = self._get_must_change_password(user_id)
        self.assertEqual(flag_after, 0)

    # ------------------------------------------------------------------
    # Test 3: After clearing flag, user can access protected routes (GET /api/auth/me)
    # ------------------------------------------------------------------
    def test_user_can_access_protected_routes_after_flag_cleared(self):
        """After must_change_password is cleared, user can access GET /api/auth/me.

        Before the flag is cleared, accessing /api/auth/me returns 403 with
        "must_change_password". After a successful change-password (which clears
        the flag), the same user can access it without 403.
        """
        # Register user
        self.client.post(
            "/api/auth/register",
            json={"username": "flaggeduser3", "password": "OldPass123"},
        )

        # Login to get access token
        login_response = self.client.post(
            "/api/auth/login",
            json={"username": "flaggeduser3", "password": "OldPass123"},
        )
        self.assertEqual(login_response.status_code, 200)
        access_token = login_response.json()["access_token"]

        # Get user ID and set must_change_password = 1
        user_id = self._get_user_id("flaggeduser3")
        self._set_must_change_password(user_id, 1)

        # Before flag is cleared: accessing /api/auth/me should be blocked
        me_response_before = self.client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(
            me_response_before.status_code, 403,
            f"Expected 403 before flag clear, got {me_response_before.status_code}"
        )
        self.assertEqual(me_response_before.json()["detail"], "must_change_password")

        # Change password (clears the flag)
        cp_response = self.client.post(
            "/api/auth/change-password",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"current_password": "OldPass123", "new_password": "NewPass456"},
        )
        self.assertEqual(cp_response.status_code, 200)

        # Use the new access token to access /api/auth/me
        new_access_token = cp_response.json()["access_token"]
        me_response_after = self.client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {new_access_token}"},
        )
        self.assertEqual(
            me_response_after.status_code, 200,
            f"Expected 200 after flag clear, got {me_response_after.status_code}: {me_response_after.json()}"
        )
        me_data = me_response_after.json()
        self.assertEqual(me_data["username"], "flaggeduser3")

    # ------------------------------------------------------------------
    # Test 4: User without must_change_password (already 0) changing password stays 0
    # ------------------------------------------------------------------
    def test_user_without_flag_changing_password_stays_zero(self):
        """User with must_change_password=0 changes password → flag stays 0.

        This verifies the UPDATE ... SET must_change_password = 0 is safe
        when the flag is already 0 (no false-positive failures / no regression).
        """
        # Register user
        self.client.post(
            "/api/auth/register",
            json={"username": "normaluser", "password": "OldPass123"},
        )

        # Login to get access token
        login_response = self.client.post(
            "/api/auth/login",
            json={"username": "normaluser", "password": "OldPass123"},
        )
        self.assertEqual(login_response.status_code, 200)
        access_token = login_response.json()["access_token"]

        # Get user ID and verify must_change_password is 0 (default)
        user_id = self._get_user_id("normaluser")
        flag_before = self._get_must_change_password(user_id)
        self.assertEqual(flag_before, 0)

        # Change password
        response = self.client.post(
            "/api/auth/change-password",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"current_password": "OldPass123", "new_password": "NewPass456"},
        )

        # Should succeed
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("access_token", data)
        self.assertEqual(data["token_type"], "bearer")

        # must_change_password should still be 0 (unchanged)
        flag_after = self._get_must_change_password(user_id)
        self.assertEqual(flag_after, 0)

    # ------------------------------------------------------------------
    # Test 5: Users without must_change_password can call login and change-password
    # ------------------------------------------------------------------
    def test_unflagged_user_can_still_login_and_change_password(self):
        """User without must_change_password can call login and change-password normally.

        Regression test: the exempt_paths addition should not break normal auth flow.
        """
        # Register user
        register_response = self.client.post(
            "/api/auth/register",
            json={"username": "normaluser2", "password": "OldPass123"},
        )
        self.assertEqual(register_response.status_code, 200)

        # Login should work
        login_response = self.client.post(
            "/api/auth/login",
            json={"username": "normaluser2", "password": "OldPass123"},
        )
        self.assertEqual(login_response.status_code, 200)
        access_token = login_response.json()["access_token"]
        self.assertIn("access_token", login_response.json())

        # Get user ID and verify flag is 0
        user_id = self._get_user_id("normaluser2")
        flag_before = self._get_must_change_password(user_id)
        self.assertEqual(flag_before, 0)

        # Change password should work
        cp_response = self.client.post(
            "/api/auth/change-password",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"current_password": "OldPass123", "new_password": "NewPass456"},
        )
        self.assertEqual(cp_response.status_code, 200)
        self.assertIn("access_token", cp_response.json())

        # Flag should still be 0
        flag_after = self._get_must_change_password(user_id)
        self.assertEqual(flag_after, 0)


class TestMustChangePasswordExemptPathsVariants(unittest.TestCase):
    """Additional edge case tests for must_change_password exempt paths."""

    def setUp(self):
        """Set up test client with temporary database."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")

        init_db(self.db_path)
        run_migrations(self.db_path)

        self._original_jwt_secret = settings.jwt_secret_key
        self._original_users_enabled = settings.users_enabled
        self._original_app_root_path = settings.app_root_path

        settings.jwt_secret_key = "test-secret-key-for-testing-at-least-32-chars-long"
        settings.users_enabled = True
        settings.app_root_path = ""

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
        """Clean up after each test."""
        settings.jwt_secret_key = self._original_jwt_secret
        settings.users_enabled = self._original_users_enabled
        settings.app_root_path = self._original_app_root_path

        self.app.dependency_overrides.clear()
        self.test_pool.close_all()

        import shutil

        try:
            shutil.rmtree(self.temp_dir)
        except Exception:
            pass

    def _get_user_id(self, username: str) -> int:
        conn = self.test_pool.get_connection()
        try:
            cursor = conn.execute(
                "SELECT id FROM users WHERE username = ?", (username,)
            )
            row = cursor.fetchone()
            return row[0] if row else None
        finally:
            self.test_pool.release_connection(conn)

    def _set_must_change_password(self, user_id: int, value: int) -> None:
        conn = self.test_pool.get_connection()
        try:
            conn.execute(
                "UPDATE users SET must_change_password = ? WHERE id = ?",
                (value, user_id),
            )
            conn.commit()
        finally:
            self.test_pool.release_connection(conn)

    def _get_must_change_password(self, user_id: int) -> int:
        conn = self.test_pool.get_connection()
        try:
            cursor = conn.execute(
                "SELECT must_change_password FROM users WHERE id = ?", (user_id,)
            )
            row = cursor.fetchone()
            return int(row[0]) if row else None
        finally:
            self.test_pool.release_connection(conn)

    # ------------------------------------------------------------------
    # Edge case: login endpoint is also exempt (both with and without /api prefix)
    # ------------------------------------------------------------------
    def test_flagged_user_can_still_login(self):
        """User with must_change_password=1 can call POST /api/auth/login.

        The login endpoint is also exempt (for the case where a user was flagged
        after a prior session and needs to re-authenticate after changing password).
        """
        # Register user
        self.client.post(
            "/api/auth/register",
            json={"username": "flaggedloginuser", "password": "OldPass123"},
        )

        # Get user ID and set must_change_password = 1 BEFORE first login
        user_id = self._get_user_id("flaggedloginuser")
        self._set_must_change_password(user_id, 1)

        # Verify flag is set
        flag_before = self._get_must_change_password(user_id)
        self.assertEqual(flag_before, 1)

        # Login should NOT be blocked — login is an exempt path
        login_response = self.client.post(
            "/api/auth/login",
            json={"username": "flaggedloginuser", "password": "OldPass123"},
        )

        self.assertEqual(
            login_response.status_code, 200,
            f"Expected 200 but got {login_response.status_code}: {login_response.json()}"
        )
        self.assertIn("access_token", login_response.json())

    # ------------------------------------------------------------------
    # Edge case: unflagged user cannot access non-exempt routes when flagged
    # ------------------------------------------------------------------
    def test_flagged_user_blocked_on_non_exempt_routes(self):
        """User with must_change_password=1 is blocked on non-exempt routes.

        Verifies that the must_change_password check only exempts the specific
        login and change-password paths; other routes still return 403.
        """
        # Register user
        self.client.post(
            "/api/auth/register",
            json={"username": "flaggedblockuser", "password": "OldPass123"},
        )

        # Login to get access token
        login_response = self.client.post(
            "/api/auth/login",
            json={"username": "flaggedblockuser", "password": "OldPass123"},
        )
        self.assertEqual(login_response.status_code, 200)
        access_token = login_response.json()["access_token"]

        # Get user ID and set must_change_password = 1
        user_id = self._get_user_id("flaggedblockuser")
        self._set_must_change_password(user_id, 1)

        # Attempting to access /api/auth/me (non-exempt) should return 403
        me_response = self.client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(me_response.status_code, 403)
        self.assertEqual(me_response.json()["detail"], "must_change_password")


if __name__ == "__main__":
    unittest.main()
