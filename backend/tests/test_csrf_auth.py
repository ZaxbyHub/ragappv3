"""
CSRF protection wiring tests for auth routes.

Tests verify that:
- POST endpoints (register, login, refresh, logout, PATCH /me) require CSRF tokens
- GET endpoints (setup-status) do NOT require CSRF tokens
- GET /me requires auth (401) but NOT CSRF (403)

Uses FastAPI TestClient with CSRF manager mocked to test protection wiring.
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
from app.security import CSRFManager


class TestCSRFProtection(unittest.TestCase):
    """Test suite for CSRF protection wiring on auth routes."""

    def setUp(self):
        """Set up test client with temporary database and CSRF manager."""
        # Create temporary database
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.maxDiff = None

        # Initialize database with schema
        init_db(self.db_path)
        run_migrations(self.db_path)

        # Store original settings to restore later
        self._original_jwt_secret = settings.jwt_secret_key
        self._original_users_enabled = settings.users_enabled

        # Override JWT secret for testing
        settings.jwt_secret_key = "test-secret-key-for-testing-at-least-32-chars-long"
        settings.users_enabled = True

        # Create a test pool for the temporary database
        self.test_pool = SQLiteConnectionPool(self.db_path, max_size=5)

        # Create FastAPI app and configure dependency overrides
        from app.api.deps import get_db
        from app.main import app as main_app

        # Override the get_db dependency to use our test pool
        def get_test_db():
            conn = self.test_pool.get_connection()
            try:
                yield conn
            finally:
                self.test_pool.release_connection(conn)

        main_app.dependency_overrides[get_db] = get_test_db

        # Create a real CSRF manager with Redis fallback
        self.csrf_manager = CSRFManager(redis_url="redis://localhost:6379/0", ttl=900)

        # Set the csrf_manager on app state
        main_app.state.csrf_manager = self.csrf_manager

        # Create test client with dependency overrides
        self.client = TestClient(main_app)
        self.app = main_app

    def tearDown(self):
        """Clean up after each test."""
        # Restore original settings
        settings.jwt_secret_key = self._original_jwt_secret
        settings.users_enabled = self._original_users_enabled

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

    def _get_csrf_cookie_and_header(self):
        """
        Get a valid CSRF token from the csrf-token endpoint.
        The csrf-token endpoint is unprotected to allow bootstrapping.
        """
        response = self.client.get("/api/csrf-token")
        self.assertEqual(response.status_code, 200)
        csrf_token = response.json()["csrf_token"]
        # Get the cookie from response
        csrf_cookie = response.cookies.get("X-CSRF-Token")
        self.assertIsNotNone(csrf_cookie, "CSRF cookie should be set")
        self.assertIsNotNone(csrf_token, "CSRF token should be returned")
        return csrf_cookie, csrf_token

    def test_post_register_without_csrf_returns_403(self):
        """POST /auth/register without CSRF token should return 403."""
        response = self.client.post(
            "/api/auth/register",
            json={"username": "testuser", "password": "Password123"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("CSRF", response.json()["detail"])

    def test_post_login_without_csrf_returns_403(self):
        """POST /auth/login without CSRF token should return 403."""
        # First register a user (with CSRF token)
        csrf_cookie, csrf_token = self._get_csrf_cookie_and_header()
        reg_response = self.client.post(
            "/api/auth/register",
            json={"username": "logintest", "password": "Password123"},
            cookies={"X-CSRF-Token": csrf_cookie},
            headers={"X-CSRF-Token": csrf_token},
        )
        self.assertEqual(
            reg_response.status_code, 200, f"Registration failed: {reg_response.json()}"
        )

        # Try to login without CSRF
        response = self.client.post(
            "/api/auth/login", json={"username": "logintest", "password": "Password123"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("CSRF", response.json()["detail"])

    def test_post_refresh_without_csrf_returns_403(self):
        """POST /auth/refresh without CSRF token should return 403."""
        # First register and login to get refresh cookie
        csrf_cookie, csrf_token = self._get_csrf_cookie_and_header()

        # Register a user
        reg_response = self.client.post(
            "/api/auth/register",
            json={"username": "refreshtest", "password": "Password123"},
            cookies={"X-CSRF-Token": csrf_cookie},
            headers={"X-CSRF-Token": csrf_token},
        )
        self.assertEqual(
            reg_response.status_code, 200, f"Registration failed: {reg_response.json()}"
        )

        # Login to get refresh cookie
        login_response = self.client.post(
            "/api/auth/login",
            json={"username": "refreshtest", "password": "Password123"},
            cookies={"X-CSRF-Token": csrf_cookie},
            headers={"X-CSRF-Token": csrf_token},
        )
        self.assertEqual(
            login_response.status_code, 200, f"Login failed: {login_response.json()}"
        )
        refresh_cookies = login_response.cookies

        # Try refresh without CSRF (just the refresh cookie)
        refresh_token = refresh_cookies.get("refresh_token")
        self.assertIsNotNone(refresh_token, "Refresh token should be set")
        response = self.client.post(
            "/api/auth/refresh", cookies={"refresh_token": refresh_token}
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("CSRF", response.json()["detail"])

    def test_post_logout_without_csrf_returns_403(self):
        """POST /auth/logout without CSRF token should return 403."""
        # First register and login to get refresh cookie
        csrf_cookie, csrf_token = self._get_csrf_cookie_and_header()

        # Register a user
        reg_response = self.client.post(
            "/api/auth/register",
            json={"username": "logouttest", "password": "Password123"},
            cookies={"X-CSRF-Token": csrf_cookie},
            headers={"X-CSRF-Token": csrf_token},
        )
        self.assertEqual(
            reg_response.status_code, 200, f"Registration failed: {reg_response.json()}"
        )

        # Login to get refresh cookie
        login_response = self.client.post(
            "/api/auth/login",
            json={"username": "logouttest", "password": "Password123"},
            cookies={"X-CSRF-Token": csrf_cookie},
            headers={"X-CSRF-Token": csrf_token},
        )
        self.assertEqual(
            login_response.status_code, 200, f"Login failed: {login_response.json()}"
        )
        refresh_cookies = login_response.cookies

        # Try logout without CSRF
        refresh_token = refresh_cookies.get("refresh_token")
        self.assertIsNotNone(refresh_token, "Refresh token should be set")
        response = self.client.post(
            "/api/auth/logout", cookies={"refresh_token": refresh_token}
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("CSRF", response.json()["detail"])

    def test_patch_me_without_csrf_returns_403(self):
        """PATCH /auth/me without CSRF token should return 403."""
        # First register and login to get access token
        csrf_cookie, csrf_token = self._get_csrf_cookie_and_header()

        # Register a user
        reg_response = self.client.post(
            "/api/auth/register",
            json={"username": "updatetest", "password": "Password123"},
            cookies={"X-CSRF-Token": csrf_cookie},
            headers={"X-CSRF-Token": csrf_token},
        )
        self.assertEqual(
            reg_response.status_code, 200, f"Registration failed: {reg_response.json()}"
        )

        # Login to get access token and new CSRF token
        login_response = self.client.post(
            "/api/auth/login",
            json={"username": "updatetest", "password": "Password123"},
            cookies={"X-CSRF-Token": csrf_cookie},
            headers={"X-CSRF-Token": csrf_token},
        )
        self.assertEqual(
            login_response.status_code, 200, f"Login failed: {login_response.json()}"
        )
        access_token = login_response.json()["access_token"]
        self.assertIsNotNone(access_token, "Access token should be returned")

        # Try PATCH without CSRF header
        response = self.client.patch(
            "/api/auth/me",
            json={"full_name": "Updated Name"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("CSRF", response.json()["detail"])

    def test_post_change_password_without_csrf_returns_403(self):
        """POST /auth/change-password without CSRF token should return 403.

        Regression guard: test_change_password.py exercises this endpoint with
        the test-only CSRF bypass enabled, so it never proves CSRF is enforced.
        This suite runs the real validator and locks that contract in.
        """
        csrf_cookie, csrf_token = self._get_csrf_cookie_and_header()

        # Register + login (with CSRF) to obtain an access token.
        reg_response = self.client.post(
            "/api/auth/register",
            json={"username": "cpwcsrf", "password": "Password123"},
            cookies={"X-CSRF-Token": csrf_cookie},
            headers={"X-CSRF-Token": csrf_token},
        )
        self.assertEqual(
            reg_response.status_code, 200, f"Registration failed: {reg_response.json()}"
        )
        login_response = self.client.post(
            "/api/auth/login",
            json={"username": "cpwcsrf", "password": "Password123"},
            cookies={"X-CSRF-Token": csrf_cookie},
            headers={"X-CSRF-Token": csrf_token},
        )
        self.assertEqual(
            login_response.status_code, 200, f"Login failed: {login_response.json()}"
        )
        access_token = login_response.json()["access_token"]

        # Change-password without a CSRF header must be rejected.
        response = self.client.post(
            "/api/auth/change-password",
            json={"current_password": "Password123", "new_password": "NewPass456"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("CSRF", response.json()["detail"])

    def test_get_setup_status_without_csrf_returns_200(self):
        """GET /auth/setup-status should NOT require CSRF token and return 200."""
        response = self.client.get("/api/auth/setup-status")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["needs_setup"], True)

    def test_get_me_without_csrf_returns_401_not_403(self):
        """
        GET /auth/me without CSRF token should return 401 (auth required),
        NOT 403 (CSRF specific).
        """
        response = self.client.get("/api/auth/me")
        # Should be 401 Unauthorized, NOT 403
        self.assertEqual(response.status_code, 401)
        detail = response.json().get("detail", "")
        # Should mention auth, not CSRF
        self.assertNotIn("CSRF", detail)

    def test_get_setup_status_returns_200_with_users(self):
        """GET /auth/setup-status after registration should return needs_setup=False."""
        csrf_cookie, csrf_token = self._get_csrf_cookie_and_header()

        # Register a user
        reg_response = self.client.post(
            "/api/auth/register",
            json={"username": "setupuser", "password": "Password123"},
            cookies={"X-CSRF-Token": csrf_cookie},
            headers={"X-CSRF-Token": csrf_token},
        )
        self.assertEqual(
            reg_response.status_code, 200, f"Registration failed: {reg_response.json()}"
        )

        # GET should still work without CSRF
        response = self.client.get("/api/auth/setup-status")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["needs_setup"], False)

    def test_register_with_valid_csrf_succeeds(self):
        """Registration with valid CSRF token should succeed."""
        csrf_cookie, csrf_token = self._get_csrf_cookie_and_header()

        response = self.client.post(
            "/api/auth/register",
            json={"username": "validuser", "password": "Password123"},
            cookies={"X-CSRF-Token": csrf_cookie},
            headers={"X-CSRF-Token": csrf_token},
        )
        self.assertEqual(
            response.status_code, 200, f"Registration failed: {response.json()}"
        )
        data = response.json()
        self.assertEqual(data["user"]["username"], "validuser")

    def test_csrf_cookie_mismatch_returns_403(self):
        """CSRF cookie and header mismatch should return 403."""
        csrf_cookie1, csrf_token1 = self._get_csrf_cookie_and_header()
        csrf_cookie2, csrf_token2 = self._get_csrf_cookie_and_header()

        # Send mismatched cookie and header
        response = self.client.post(
            "/api/auth/register",
            json={"username": "mismatch", "password": "Password123"},
            cookies={"X-CSRF-Token": csrf_cookie1},
            headers={"X-CSRF-Token": csrf_token2},  # Different token
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("CSRF", response.json()["detail"])

    def test_invalid_csrf_token_returns_403(self):
        """Invalid CSRF token should return 403."""
        response = self.client.post(
            "/api/auth/register",
            json={"username": "invalidcsrf", "password": "Password123"},
            cookies={"X-CSRF-Token": "invalid-cookie"},
            headers={"X-CSRF-Token": "invalid-header"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("CSRF", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
