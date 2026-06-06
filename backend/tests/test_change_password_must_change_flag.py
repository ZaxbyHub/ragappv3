"""Tests for must_change_password flag clearing on password change.

Tests cover:
- User with must_change_password=1 changes password → flag is cleared to 0
- User with must_change_password=0 changes password → flag stays 0 (no-op)
- Existing change-password tests still pass (regression)

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


class TestMustChangePasswordFlagClearing(unittest.TestCase):
    """Test suite for must_change_password flag clearing on password change."""

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

    def test_change_password_clears_must_change_password_flag(self):
        """User with must_change_password=1 changes password → flag is cleared to 0.

        Regression test: the change-password endpoint now clears the
        must_change_password flag after successfully updating the password.
        """
        # Register user
        self.client.post(
            "/api/auth/register",
            json={"username": "forcepwuser", "password": "OldPass123"},
        )

        # Login to get access token
        login_response = self.client.post(
            "/api/auth/login",
            json={"username": "forcepwuser", "password": "OldPass123"},
        )
        self.assertEqual(login_response.status_code, 200)
        access_token = login_response.json()["access_token"]

        # Get user ID and verify must_change_password is initially 0 (default)
        user_id = self._get_user_id("forcepwuser")
        initial_flag = self._get_must_change_password(user_id)
        self.assertEqual(initial_flag, 0)

        # Set must_change_password = 1 (simulate admin password reset)
        self._set_must_change_password(user_id, 1)
        flag_before = self._get_must_change_password(user_id)
        self.assertEqual(flag_before, 1)

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
        self.assertIn("refresh_token", response.cookies)

        # must_change_password should now be 0 (flag cleared)
        flag_after = self._get_must_change_password(user_id)
        self.assertEqual(flag_after, 0)

    def test_change_password_keeps_must_change_password_zero(self):
        """User with must_change_password=0 changes password → flag stays 0 (no-op).

        This verifies the UPDATE ... SET must_change_password = 0 is safe
        when the flag is already 0 (no false-positive failures).
        """
        # Register user
        self.client.post(
            "/api/auth/register",
            json={"username": "normalpwuser", "password": "OldPass123"},
        )

        # Login to get access token
        login_response = self.client.post(
            "/api/auth/login",
            json={"username": "normalpwuser", "password": "OldPass123"},
        )
        self.assertEqual(login_response.status_code, 200)
        access_token = login_response.json()["access_token"]

        # Get user ID and verify must_change_password is 0 (default)
        user_id = self._get_user_id("normalpwuser")
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
        self.assertIn("refresh_token", response.cookies)

        # must_change_password should still be 0 (unchanged)
        flag_after = self._get_must_change_password(user_id)
        self.assertEqual(flag_after, 0)


if __name__ == "__main__":
    unittest.main()
