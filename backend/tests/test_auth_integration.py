"""
Authentication integration tests using unittest and FastAPI TestClient.

Tests cover:
- Unauthenticated requests are rejected with 401
- Valid JWT Bearer token authentication works
- Invalid/expired JWT token is rejected with 403
- JWT authentication flow with users_enabled=True
"""

import os
import shutil
import sqlite3
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
from app.services.auth_service import compute_client_fingerprint, create_access_token


class TestAuthIntegration(unittest.TestCase):
    """Test suite for authentication integration."""

    def setUp(self):
        """Set up test client with temporary database."""
        # Create temporary database
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")

        # Initialize database with schema
        init_db(self.db_path)
        run_migrations(self.db_path)

        # Store original settings
        self._original_jwt_secret = settings.jwt_secret_key
        self._original_users_enabled = settings.users_enabled

        # Enable JWT mode and set test JWT secret
        settings.jwt_secret_key = "test-secret-key-for-testing-at-least-32-chars-long"
        settings.users_enabled = True

        # Create a test user in the database
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO users (username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?)",
                ("testuser", "hashed_password", "Test User", "admin", True),
            )
            conn.commit()
            # Get the user id
            cursor.execute("SELECT id FROM users WHERE username = ?", ("testuser",))
            self.test_user_id = cursor.fetchone()[0]
        finally:
            conn.close()

        # Generate a valid JWT access token for the test user
        self.access_token = create_access_token(
            user_id=self.test_user_id, username="testuser", role="admin",
            client_fingerprint=compute_client_fingerprint(""),
        )
        self.auth_header = {"Authorization": f"Bearer {self.access_token}"}

        # Create a test pool for the temporary database
        self.test_pool = SQLiteConnectionPool(self.db_path, max_size=5)

        # Import app and configure dependency overrides
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

        # Create test client with dependency overrides
        self.client = TestClient(main_app)
        # Override default User-Agent so fingerprint validation matches token
        self.client.headers["user-agent"] = ""
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
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_unauthenticated_request_rejected_with_401(self):
        """Test that requests without auth header return 401 when JWT auth is enabled."""
        # Make request without Authorization header to a protected endpoint
        response = self.client.get("/api/auth/me")

        self.assertEqual(response.status_code, 401)
        data = response.json()
        self.assertIn("detail", data)

    def test_valid_bearer_token_authentication_works(self):
        """Test that valid JWT Bearer token allows access to protected endpoints."""
        # Make request with valid JWT Bearer token
        response = self.client.get("/api/auth/me", headers=self.auth_header)

        # Should succeed with 200 and return user info
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["username"], "testuser")
        self.assertEqual(data["role"], "admin")

    def test_invalid_bearer_token_rejected_with_403(self):
        """Test that invalid JWT Bearer token returns 401."""
        # Make request with invalid Bearer token
        headers = {"Authorization": "Bearer invalid-token"}
        response = self.client.get("/api/auth/me", headers=headers)

        self.assertEqual(response.status_code, 401)
        data = response.json()
        self.assertIn("detail", data)

    def test_expired_jwt_token_rejected_with_403(self):
        """Test that expired JWT token returns 401."""
        from datetime import datetime, timedelta, timezone

        import jwt

        # Create an expired token manually
        secret = settings.jwt_secret_key
        expired_time = datetime.now(timezone.utc) - timedelta(minutes=1)
        payload = {
            "sub": str(self.test_user_id),
            "username": "testuser",
            "role": "admin",
            "exp": expired_time,
        }
        expired_token = jwt.encode(payload, secret, algorithm="HS256")

        headers = {"Authorization": f"Bearer {expired_token}"}
        response = self.client.get("/api/auth/me", headers=headers)

        self.assertEqual(response.status_code, 401)
        data = response.json()
        self.assertIn("detail", data)

    def test_auth_header_case_insensitive_bearer(self):
        """Test that 'Bearer' prefix is case-insensitive."""
        # Test lowercase 'bearer'
        headers = {"Authorization": f"bearer {self.access_token}"}
        response = self.client.get("/api/auth/me", headers=headers)

        # Should succeed with 200
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["username"], "testuser")

    def test_missing_bearer_prefix_returns_401(self):
        """Test that token without 'Bearer' prefix returns 401."""
        # Send token without Bearer prefix
        headers = {"Authorization": self.access_token}
        response = self.client.get("/api/auth/me", headers=headers)

        self.assertEqual(response.status_code, 401)

    def test_multiple_protected_endpoints_require_auth(self):
        """Test that various protected endpoints all require authentication."""
        # Test endpoints that use get_current_active_user dependency
        protected_endpoints = [
            ("/api/auth/me", "GET"),
            ("/api/auth/me", "PATCH"),
        ]

        for endpoint, method in protected_endpoints:
            with self.subTest(endpoint=endpoint, method=method):
                if method == "GET":
                    response = self.client.get(endpoint)
                elif method == "PATCH":
                    response = self.client.patch(endpoint, json={})
                else:
                    continue

                self.assertEqual(response.status_code, 401)

    def test_valid_token_allows_access_to_multiple_endpoints(self):
        """Test that valid JWT token allows access to various protected endpoints."""
        # Test endpoints that should work with valid auth
        endpoints = [
            ("/api/auth/me", "GET"),
        ]

        for endpoint, method in endpoints:
            with self.subTest(endpoint=endpoint):
                if method == "GET":
                    response = self.client.get(endpoint, headers=self.auth_header)
                else:
                    continue
                # Should not be auth errors
                self.assertNotEqual(response.status_code, 401)
                self.assertNotEqual(response.status_code, 403)


class TestAuthEdgeCases(unittest.TestCase):
    """Test edge cases for authentication."""

    def setUp(self):
        """Set up test client with temporary database."""
        # Create temporary database
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")

        # Initialize database with schema
        init_db(self.db_path)
        run_migrations(self.db_path)

        # Store original settings
        self._original_jwt_secret = settings.jwt_secret_key
        self._original_users_enabled = settings.users_enabled

        # Enable JWT mode and set test JWT secret
        settings.jwt_secret_key = "test-secret-key-for-testing-at-least-32-chars-long"
        settings.users_enabled = True

        # Create a test user in the database
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO users (username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?)",
                ("testuser", "hashed_password", "Test User", "admin", True),
            )
            conn.commit()
            # Get the user id
            cursor.execute("SELECT id FROM users WHERE username = ?", ("testuser",))
            self.test_user_id = cursor.fetchone()[0]
        finally:
            conn.close()

        # Generate a valid JWT access token for the test user
        self.access_token = create_access_token(
            user_id=self.test_user_id, username="testuser", role="admin",
            client_fingerprint=compute_client_fingerprint(""),
        )

        # Create a test pool for the temporary database
        self.test_pool = SQLiteConnectionPool(self.db_path, max_size=5)

        # Import app and configure dependency overrides
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

        # Create test client with dependency overrides
        self.client = TestClient(main_app)
        # Override default User-Agent so fingerprint validation matches token
        self.client.headers["user-agent"] = ""
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
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_empty_authorization_header(self):
        """Test that empty Authorization header is handled correctly."""
        headers = {"Authorization": ""}
        response = self.client.get("/api/auth/me", headers=headers)

        self.assertEqual(response.status_code, 401)

    def test_bearer_with_empty_token(self):
        """Test that 'Bearer ' with empty token returns 401."""
        headers = {"Authorization": "Bearer "}
        response = self.client.get("/api/auth/me", headers=headers)

        self.assertEqual(response.status_code, 401)

    def test_whitespace_token(self):
        """Test that whitespace-only token is handled correctly."""
        headers = {"Authorization": "Bearer    "}
        response = self.client.get("/api/auth/me", headers=headers)

        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
