"""
Adversarial security tests for auth routes.

Tests cover attack vectors:
- SQL injection attempts
- Empty/malformed inputs
- Boundary violations (very long inputs)
- Auth bypass attempts
- Missing credentials/cookies

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


class TestAuthRoutesAdversarial(unittest.TestCase):
    """Adversarial security tests for authentication routes."""

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

        # Bypass CSRF checks: override the dependency and provide a stub token manager
        # so auth endpoints that issue new tokens (login, register) can complete.
        from unittest.mock import MagicMock

        from app.security import csrf_protect
        main_app.dependency_overrides[csrf_protect] = lambda: "test-bypass"
        _stub = MagicMock()
        _stub.generate_token.return_value = "test-token"
        _stub.validate_token.return_value = True
        self._original_state_mgr = getattr(main_app.state, "csrf_manager", None)
        main_app.state.csrf_manager = _stub

        # Create test client with dependency overrides
        self.client = TestClient(main_app)
        self.app = main_app

    def tearDown(self):
        """Clean up after each test."""
        # Restore original settings
        settings.jwt_secret_key = self._original_jwt_secret
        settings.users_enabled = self._original_users_enabled

        # Clear dependency overrides and restore token manager state
        self.app.dependency_overrides.clear()
        self.app.state.csrf_manager = self._original_state_mgr

        # Close the test pool
        self.test_pool.close_all()

        # Clean up temp directory
        import shutil

        try:
            shutil.rmtree(self.temp_dir)
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────
    # ATTACK VECTOR 1: SQL Injection in Username
    # ─────────────────────────────────────────────────────────────────
    def test_register_sql_injection_username(self):
        """SQL injection patterns in username should be rejected, not executed."""
        injection_payloads = [
            "'; DROP TABLE users; --",
            "' OR '1'='1",
            "admin'--",
            "'; INSERT INTO users VALUES (999,'hacker','pw','','superadmin',1); --",
            "' UNION SELECT * FROM users --",
        ]

        for payload in injection_payloads:
            # Register should either succeed (safe against injection) or fail with proper validation
            response = self.client.post(
                "/api/auth/register",
                json={"username": payload, "password": "validpassword123"},
            )
            # Expect 400 (validation) or 409 (already exists) - NOT 500 (server error from injection)
            self.assertIn(
                response.status_code,
                [200, 400, 409],
                f"SQL injection payload '{payload[:20]}...' caused server error: {response.status_code}",
            )

            # If 200, verify the injection wasn't executed (user shouldn't be superadmin unless first)
            if response.status_code == 200:
                # Verify the username is stored literally, not interpreted as SQL
                data = response.json()
                self.assertEqual(data["username"], payload)

    # ─────────────────────────────────────────────────────────────────
    # ATTACK VECTOR 2: Empty JSON Body
    # ─────────────────────────────────────────────────────────────────
    def test_register_empty_body(self):
        """Register with empty JSON body should return 422 (validation error)."""
        response = self.client.post(
            "/api/auth/register",
            json={},
        )

        self.assertEqual(response.status_code, 422)
        # FastAPI validation error for missing required fields
        data = response.json()
        self.assertIn("detail", data)

    # ─────────────────────────────────────────────────────────────────
    # ATTACK VECTOR 3: Very Long Username (Boundary Violation)
    # ─────────────────────────────────────────────────────────────────
    def test_register_very_long_username(self):
        """Register with username of 1000 characters should return 400 or 422."""
        long_username = "a" * 1000

        response = self.client.post(
            "/api/auth/register",
            json={"username": long_username, "password": "validpassword123"},
        )

        # Should be rejected due to max_length validation (255 chars in model)
        self.assertIn(
            response.status_code,
            [400, 422],
            f"Very long username should be rejected, got {response.status_code}",
        )

    # ─────────────────────────────────────────────────────────────────
    # ATTACK VECTOR 4: Login Nonexistent User
    # ─────────────────────────────────────────────────────────────────
    def test_login_nonexistent_user(self):
        """Login with nonexistent username should return 401, not leak user existence."""
        response = self.client.post(
            "/api/auth/login",
            json={
                "username": "definitely_not_a_real_user_12345",
                "password": "anypassword",
            },
        )

        self.assertEqual(response.status_code, 401)
        data = response.json()
        # Error message should be generic, not revealing whether user exists
        self.assertIn("Invalid username or password", data["detail"])

    # ─────────────────────────────────────────────────────────────────
    # ATTACK VECTOR 5: Login with No Body
    # ─────────────────────────────────────────────────────────────────
    def test_login_with_no_body(self):
        """Login with empty JSON body should return 422 (validation error)."""
        response = self.client.post(
            "/api/auth/login",
            json={},
        )

        self.assertEqual(response.status_code, 422)
        data = response.json()
        self.assertIn("detail", data)

    # ─────────────────────────────────────────────────────────────────
    # ATTACK VECTOR 6: Refresh with Invalid Cookie
    # ─────────────────────────────────────────────────────────────────
    def test_refresh_with_invalid_cookie(self):
        """Refresh with malformed/invalid cookie value should return 401."""
        # First register a user to ensure there's something in the DB
        self.client.post(
            "/api/auth/register",
            json={"username": "validuser1", "password": "password123"},
        )

        # Try to refresh with invalid cookie values
        invalid_tokens = [
            "invalid_token_string",
            "a" * 100,  # Long invalid token
            "\x00\x01\x02",  # Binary characters
            "jwt.signature.here",  # Looks like JWT but invalid
            "",  # Empty string
        ]

        for invalid_token in invalid_tokens:
            response = self.client.post(
                "/api/auth/refresh",
                cookies={"refresh_token": invalid_token},
            )

            self.assertEqual(
                response.status_code,
                401,
                f"Invalid refresh token '{invalid_token[:20]}...' should return 401, got {response.status_code}",
            )

    # ─────────────────────────────────────────────────────────────────
    # ATTACK VECTOR 7: Refresh with No Cookie
    # ─────────────────────────────────────────────────────────────────
    def test_refresh_with_no_cookie(self):
        """Refresh without any cookie should return 401."""
        response = self.client.post("/api/auth/refresh")

        self.assertEqual(response.status_code, 401)
        data = response.json()
        self.assertIn("Refresh token missing", data["detail"])

    # ─────────────────────────────────────────────────────────────────
    # ATTACK VECTOR 8: Get /me Without Auth
    # ─────────────────────────────────────────────────────────────────
    def test_me_without_auth(self):
        """GET /auth/me without Authorization header should return 401."""
        response = self.client.get("/api/auth/me")

        self.assertEqual(response.status_code, 401)

    # ─────────────────────────────────────────────────────────────────
    # ATTACK VECTOR 9: Update Profile with No Body
    # ─────────────────────────────────────────────────────────────────
    def test_update_me_with_no_body(self):
        """PATCH /auth/me with empty JSON should return 400 (no fields to update)."""
        # First register and login
        self.client.post(
            "/api/auth/register",
            json={"username": "updateuser1", "password": "Password123"},
        )

        login_response = self.client.post(
            "/api/auth/login",
            json={"username": "updateuser1", "password": "Password123"},
        )

        access_token = login_response.json()["access_token"]

        # Try to update with empty body
        response = self.client.patch(
            "/api/auth/me",
            json={},
            headers={"Authorization": f"Bearer {access_token}"},
        )

        # Should return 400 "No fields to update" not 422 (FastAPI validates empty dict differently)
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertIn("No fields to update", data["detail"])

    # ─────────────────────────────────────────────────────────────────
    # ATTACK VECTOR 10: Update Profile with Short Password
    # ─────────────────────────────────────────────────────────────────
    def test_update_me_short_password(self):
        """PATCH /auth/me with password < 8 chars should return 400."""
        # First register and login
        self.client.post(
            "/api/auth/register",
            json={"username": "updateuser2", "password": "Password123"},
        )

        login_response = self.client.post(
            "/api/auth/login",
            json={"username": "updateuser2", "password": "Password123"},
        )

        access_token = login_response.json()["access_token"]

        # Try to update with short password
        short_passwords = ["", "a", "ab", "1234567"]

        for short_pw in short_passwords:
            response = self.client.patch(
                "/api/auth/me",
                json={"password": short_pw},
                headers={"Authorization": f"Bearer {access_token}"},
            )

            self.assertEqual(
                response.status_code,
                400,
                f"Short password '{short_pw}' should return 400, got {response.status_code}",
            )
            data = response.json()
            detail = data["detail"].lower()
            self.assertTrue(
                "8 characters" in detail or "empty" in detail or "whitespace" in detail,
                f"Expected password strength error for '{short_pw}', got: {data['detail']}",
            )


if __name__ == "__main__":
    unittest.main()
