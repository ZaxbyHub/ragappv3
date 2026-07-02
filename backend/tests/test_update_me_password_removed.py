"""
Regression tests: PATCH /auth/me must not accept or mutate password.

FR-001 (HIGH A1-3): Password mutation removed from PATCH /auth/me.
Tests verify:
(a) UpdateProfileRequest rejects password in body schema (extra='forbid').
(b) PATCH /auth/me with body {password: "new"} returns 400/422 and does NOT mutate hashed_password.
(c) After rejected PATCH, original password still works at /login.
(d) PATCH /auth/me with body {full_name: "Updated"} still returns 200 and updates the row.
"""

import os
import sys
import tempfile
import unittest

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies (same pattern as test_auth_routes.py)
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

from pydantic import ValidationError

from app.models.database import SQLiteConnectionPool, init_db, run_migrations


class TestUpdateMePasswordRemoved(unittest.TestCase):
    """Regression suite for password-mutation removal from PATCH /auth/me."""

    def setUp(self):
        """Set up test client with temporary database."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")

        # Initialize database with schema
        init_db(self.db_path)
        run_migrations(self.db_path)

        # Set JWT_SECRET_KEY in environment BEFORE importing settings
        os.environ["JWT_SECRET_KEY"] = "test-secret-key-for-testing-at-least-32-chars-long"

        # Store original settings
        from app.config import settings
        self._original_users_enabled = settings.users_enabled
        self._original_app_root_path = settings.app_root_path

        # Override settings for testing
        settings.users_enabled = True
        settings.app_root_path = ""

        # Create test pool
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

        from fastapi.testclient import TestClient
        self.client = TestClient(main_app)
        self.app = main_app

    def tearDown(self):
        """Clean up after each test."""
        from app.config import settings
        del os.environ["JWT_SECRET_KEY"]
        settings.users_enabled = self._original_users_enabled
        settings.app_root_path = self._original_app_root_path

        self.app.dependency_overrides.clear()
        self.test_pool.close_all()

        import shutil
        try:
            shutil.rmtree(self.temp_dir)
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────
    # (a) UpdateProfileRequest schema rejects password field
    # ─────────────────────────────────────────────────────────────────
    def test_update_profile_request_rejects_password(self):
        """UpdateProfileRequest raises ValidationError when password is provided."""
        from app.api.routes.auth import UpdateProfileRequest

        with self.assertRaises(ValidationError) as ctx:
            UpdateProfileRequest(**{"full_name": "x", "password": "y"})

        # The error should mention "password" or "extra fields"
        error_str = str(ctx.exception)
        self.assertTrue(
            "password" in error_str.lower() or "extra" in error_str.lower(),
            f"Expected error about password or extra fields, got: {error_str}",
        )

    def test_update_profile_request_rejects_password_only(self):
        """UpdateProfileRequest raises ValidationError when only password is provided."""
        from app.api.routes.auth import UpdateProfileRequest

        with self.assertRaises(ValidationError):
            UpdateProfileRequest(password="somepassword")

    # ─────────────────────────────────────────────────────────────────
    # (b) PATCH /auth/me with password body returns 400/422 and hashed_password unchanged
    # ─────────────────────────────────────────────────────────────────
    def test_patch_me_with_password_returns_error(self):
        """PATCH /auth/me with body {password: ...} returns 400 or 422."""
        # Register and login
        self.client.post(
            "/api/auth/register",
            json={"username": "pwtestuser", "password": "OriginalPass123"},
        )
        login_resp = self.client.post(
            "/api/auth/login",
            json={"username": "pwtestuser", "password": "OriginalPass123"},
        )
        access_token = login_resp.json()["access_token"]

        # Read the hashed_password before the PATCH attempt
        from app.models.database import SQLiteConnectionPool
        pool = SQLiteConnectionPool(self.db_path, max_size=2)
        conn = pool.get_connection()
        try:
            row_before = conn.execute(
                "SELECT hashed_password FROM users WHERE username = ?",
                ("pwtestuser",),
            ).fetchone()
            hashed_before = row_before[0]
        finally:
            pool.release_connection(conn)
        pool.close_all()

        # Attempt PATCH with password
        response = self.client.patch(
            "/api/auth/me",
            json={"password": "newPassword456"},
            headers={"Authorization": f"Bearer {access_token}"},
        )

        # Should be rejected (400 for HTTP layer or 422 for Pydantic validation)
        self.assertIn(
            response.status_code,
            (400, 422),
            f"Expected 400 or 422, got {response.status_code}: {response.json()}",
        )

        # hashed_password must be unchanged
        pool2 = SQLiteConnectionPool(self.db_path, max_size=2)
        conn2 = pool2.get_connection()
        try:
            row_after = conn2.execute(
                "SELECT hashed_password FROM users WHERE username = ?",
                ("pwtestuser",),
            ).fetchone()
            hashed_after = row_after[0]
        finally:
            pool2.release_connection(conn2)
        pool2.close_all()

        self.assertEqual(
            hashed_before,
            hashed_after,
            "hashed_password must not change when PATCH /auth/me is rejected",
        )

    # ─────────────────────────────────────────────────────────────────
    # (c) After rejected PATCH, original password still works at /login
    # ─────────────────────────────────────────────────────────────────
    def test_original_password_works_after_rejected_patch(self):
        """User can still login with original password after rejected PATCH."""
        # Register and login
        self.client.post(
            "/api/auth/register",
            json={"username": "pwtestuser2", "password": "MyOriginalPass99"},
        )
        login_resp = self.client.post(
            "/api/auth/login",
            json={"username": "pwtestuser2", "password": "MyOriginalPass99"},
        )
        access_token = login_resp.json()["access_token"]

        # Reject PATCH with password
        reject_resp = self.client.patch(
            "/api/auth/me",
            json={"password": "anything"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertIn(reject_resp.status_code, (400, 422))

        # Original password must still work at /login
        login_again = self.client.post(
            "/api/auth/login",
            json={"username": "pwtestuser2", "password": "MyOriginalPass99"},
        )
        self.assertEqual(
            login_again.status_code,
            200,
            f"Original password should still work after rejected PATCH: {login_again.json()}",
        )

    # ─────────────────────────────────────────────────────────────────
    # (d) PATCH /auth/me with full_name still works
    # ─────────────────────────────────────────────────────────────────
    def test_patch_me_with_full_name_succeeds(self):
        """PATCH /auth/me with body {full_name: "Updated"} returns 200 and updates row."""
        # Register and login
        self.client.post(
            "/api/auth/register",
            json={"username": "nametestuser", "password": "Password123"},
        )
        login_resp = self.client.post(
            "/api/auth/login",
            json={"username": "nametestuser", "password": "Password123"},
        )
        access_token = login_resp.json()["access_token"]

        # Update full_name
        response = self.client.patch(
            "/api/auth/me",
            json={"full_name": "Updated Name"},
            headers={"Authorization": f"Bearer {access_token}"},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["full_name"], "Updated Name")
        self.assertEqual(data["message"], "Profile updated successfully")

        # Verify DB row was updated
        from app.models.database import SQLiteConnectionPool
        pool = SQLiteConnectionPool(self.db_path, max_size=2)
        conn = pool.get_connection()
        try:
            row = conn.execute(
                "SELECT full_name FROM users WHERE username = ?",
                ("nametestuser",),
            ).fetchone()
            self.assertEqual(row[0], "Updated Name")
        finally:
            pool.release_connection(conn)
        pool.close_all()


if __name__ == "__main__":
    unittest.main()
