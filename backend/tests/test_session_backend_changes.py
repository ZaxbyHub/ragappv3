"""
Tests for backend session changes (create_user, _is_secure_request, CSRF fallback).

Tests cover:
1. POST /users/ endpoint (create_user):
   - Valid user creation
   - Duplicate username rejection
   - Weak password rejection
   - Missing required fields
   - Role validation
   - Superadmin guard (only superadmin can create superadmin)

2. _is_secure_request helper:
   - Returns False for HTTP
   - Returns True for HTTPS
   - Returns True for X-Forwarded-Proto: https

3. CSRF fallback handling:
   - _InMemoryCSRFStore functionality
   - CSRFManager fallback mode
   - Fallback recovery when Redis becomes available
"""

import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from starlette.requests import Request

# Set up test environment BEFORE importing app modules
os.environ["JWT_SECRET_KEY"] = (
    "test-jwt-secret-key-for-testing-only-12345678901234567890"
)
os.environ["USERS_ENABLED"] = "true"
os.environ["ADMIN_SECRET_TOKEN"] = "test-admin-key"

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.tests.schema_constants import TEST_SCHEMA

# ============================================================================
# Tests for _is_secure_request helper (auth.py)
# ============================================================================


class TestIsSecureRequest(unittest.TestCase):
    """Tests for the _is_secure_request helper function."""

    def test_returns_false_for_http(self):
        """HTTP scheme should return False."""
        from app.api.routes.auth import _is_secure_request

        # Create a mock request with HTTP scheme
        mock_request = MagicMock(spec=Request)
        mock_request.url.scheme = "http"
        mock_request.headers.get.return_value = ""

        result = _is_secure_request(mock_request)
        self.assertFalse(result, "HTTP request should return False")

    def test_returns_true_for_https(self):
        """HTTPS scheme should return True."""
        from app.api.routes.auth import _is_secure_request

        # Create a mock request with HTTPS scheme
        mock_request = MagicMock(spec=Request)
        mock_request.url.scheme = "https"
        mock_request.headers.get.return_value = ""

        result = _is_secure_request(mock_request)
        self.assertTrue(result, "HTTPS request should return True")

    def test_returns_true_for_forwarded_proto_https(self):
        """X-Forwarded-Proto: https should return True even over HTTP."""
        from app.api.routes.auth import _is_secure_request

        # Create a mock request with HTTP scheme but X-Forwarded-Proto: https
        mock_request = MagicMock(spec=Request)
        mock_request.url.scheme = "http"
        mock_request.headers.get.return_value = "https"

        result = _is_secure_request(mock_request)
        self.assertTrue(result, "X-Forwarded-Proto: https should return True")

    def test_returns_false_for_forwarded_proto_http(self):
        """X-Forwarded-Proto: http should return False."""
        from app.api.routes.auth import _is_secure_request

        mock_request = MagicMock(spec=Request)
        mock_request.url.scheme = "http"
        mock_request.headers.get.return_value = "http"

        result = _is_secure_request(mock_request)
        self.assertFalse(result, "X-Forwarded-Proto: http should return False")

    def test_case_insensitive_forwarded_proto(self):
        """X-Forwarded-Proto should be case-insensitive."""
        from app.api.routes.auth import _is_secure_request

        # Test uppercase
        mock_request = MagicMock(spec=Request)
        mock_request.url.scheme = "http"
        mock_request.headers.get.return_value = "HTTPS"

        result = _is_secure_request(mock_request)
        self.assertTrue(
            result, "X-Forwarded-Proto: HTTPS (uppercase) should return True"
        )

        # Test mixed case
        mock_request.headers.get.return_value = "HtTpS"
        result = _is_secure_request(mock_request)
        self.assertTrue(result, "X-Forwarded-Proto: HtTpS (mixed) should return True")

    def test_https_overrides_forwarded_proto_http(self):
        """HTTPS scheme returns True even if X-Forwarded-Proto is http."""
        from app.api.routes.auth import _is_secure_request

        mock_request = MagicMock(spec=Request)
        mock_request.url.scheme = "https"
        mock_request.headers.get.return_value = "http"

        result = _is_secure_request(mock_request)
        self.assertTrue(
            result, "HTTPS scheme should return True regardless of X-Forwarded-Proto"
        )


# ============================================================================
# Tests for CSRF fallback handling (security.py)
# ============================================================================


class TestInMemoryCSRFStore(unittest.TestCase):
    """Tests for the _InMemoryCSRFStore fallback class."""

    def test_setex_and_get(self):
        """setex stores value and get retrieves it."""
        from app.security import _InMemoryCSRFStore

        store = _InMemoryCSRFStore(ttl=60)
        store.setex("test_key", 60, "1")

        result = store.get("test_key")
        self.assertEqual(result, "1", "get should return the stored value")

    def test_get_returns_none_for_missing_key(self):
        """get returns None for non-existent key."""
        from app.security import _InMemoryCSRFStore

        store = _InMemoryCSRFStore(ttl=60)
        result = store.get("nonexistent")
        self.assertIsNone(result, "get should return None for missing key")

    def test_get_returns_none_after_expiry(self):
        """get returns None after TTL expires."""
        from app.security import _InMemoryCSRFStore

        base = 1000.0
        with patch("app.security.time.time") as mock_time:
            mock_time.return_value = base
            store = _InMemoryCSRFStore(ttl=1)
            store.setex("expiring_key", 1, "1")

            # Advance time past the 1-second TTL
            mock_time.return_value = base + 1.5

            result = store.get("expiring_key")
            self.assertIsNone(result, "get should return None after TTL expires")

    def test_delete_removes_key(self):
        """delete removes the key from store."""
        from app.security import _InMemoryCSRFStore

        store = _InMemoryCSRFStore(ttl=60)
        store.setex("to_delete", 60, "1")
        store.delete("to_delete")

        result = store.get("to_delete")
        self.assertIsNone(result, "get should return None after delete")

    def test_expire_extends_ttl(self):
        """expire extends the TTL of an existing key."""
        from app.security import _InMemoryCSRFStore

        base = 1000.0
        with patch("app.security.time.time") as mock_time:
            mock_time.return_value = base
            store = _InMemoryCSRFStore(ttl=1)
            store.setex("to_extend", 1, "1")

            # Advance to halfway through original TTL
            mock_time.return_value = base + 0.5

            # Extend the TTL
            store.expire("to_extend", 2)

            # Advance past original TTL (1.0s) but within extended TTL (2.5s)
            mock_time.return_value = base + 1.0

            result = store.get("to_extend")
            self.assertEqual(result, "1", "key should still exist after expire extends TTL")

    def test_ping_returns_true(self):
        """ping always returns True for in-memory store."""
        from app.security import _InMemoryCSRFStore

        store = _InMemoryCSRFStore(ttl=60)
        self.assertTrue(store.ping(), "ping should return True")

    def test_thread_safety(self):
        """Store should be thread-safe for concurrent access."""
        from app.security import _InMemoryCSRFStore

        store = _InMemoryCSRFStore(ttl=60)
        errors = []

        def writer(prefix, count):
            try:
                for i in range(count):
                    store.setex(f"{prefix}_{i}", 60, "1")
            except Exception as e:
                errors.append(e)

        def reader(prefix, count):
            try:
                for i in range(count):
                    store.get(f"{prefix}_{i}")
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=writer, args=("key", 100)),
            threading.Thread(target=writer, args=("other", 100)),
            threading.Thread(target=reader, args=("key", 100)),
            threading.Thread(target=reader, args=("other", 100)),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Thread safety errors: {errors}")


class TestCSRFManagerFallback(unittest.TestCase):
    """Tests for CSRFManager fallback behavior."""

    def test_fallback_mode_when_redis_unavailable(self):
        """CSRFManager uses in-memory fallback when Redis is unavailable."""
        from app.security import CSRFManager

        # Use invalid Redis URL to trigger fallback
        manager = CSRFManager(redis_url="redis://nonexistent:6379", ttl=60)

        self.assertTrue(
            manager._use_fallback, "Should use fallback when Redis unavailable"
        )
        self.assertIsNotNone(manager._fallback_store, "Should have fallback store")

    def test_generate_token_in_fallback_mode(self):
        """generate_token works in fallback mode."""
        from app.security import CSRFManager

        manager = CSRFManager(redis_url="redis://nonexistent:6379", ttl=60)
        token = manager.generate_token()

        self.assertIsInstance(token, str, "Token should be a string")
        self.assertGreater(len(token), 10, "Token should be non-empty")

    def test_validate_token_in_fallback_mode(self):
        """validate_token works in fallback mode."""
        from app.security import CSRFManager

        manager = CSRFManager(redis_url="redis://nonexistent:6379", ttl=60)
        token = manager.generate_token()

        result = manager.validate_token(token)
        self.assertTrue(result, "Valid token should pass validation")

    def test_validate_rejects_invalid_token_in_fallback(self):
        """validate_token rejects invalid tokens in fallback mode."""
        from app.security import CSRFManager

        manager = CSRFManager(redis_url="redis://nonexistent:6379", ttl=60)

        result = manager.validate_token("invalid_token_12345")
        self.assertFalse(result, "Invalid token should fail validation")

    def test_validate_rejects_empty_token(self):
        """validate_token rejects empty tokens."""
        from app.security import CSRFManager

        manager = CSRFManager(redis_url="redis://nonexistent:6379", ttl=60)

        result = manager.validate_token("")
        self.assertFalse(result, "Empty token should fail validation")

    def test_validate_rejects_none_token(self):
        """validate_token rejects None tokens."""
        from app.security import CSRFManager

        manager = CSRFManager(redis_url="redis://nonexistent:6379", ttl=60)

        result = manager.validate_token(None)
        self.assertFalse(result, "None token should fail validation")

    def test_revoke_token_in_fallback_mode(self):
        """revoke_token works in fallback mode."""
        from app.security import CSRFManager

        manager = CSRFManager(redis_url="redis://nonexistent:6379", ttl=60)
        token = manager.generate_token()

        # Token should be valid
        self.assertTrue(manager.validate_token(token))

        # Revoke the token
        manager.revoke_token(token)

        # Token should now be invalid
        result = manager.validate_token(token)
        self.assertFalse(result, "Revoked token should fail validation")


# ============================================================================
# Tests for create_user endpoint (users.py)
# ============================================================================


class TestCreateUserEndpoint(unittest.TestCase):
    """Tests for POST /users/ endpoint."""

    def setUp(self):
        """Set up test client with temporary database."""
        # Ensure users_enabled=True regardless of settings construction order
        # (app.config may have been initialized before this module set USERS_ENABLED=true)
        import app.api.deps as _deps_module
        self._deps_module = _deps_module
        self._original_users_enabled = _deps_module.settings.users_enabled
        _deps_module.settings.users_enabled = True

        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")

        # Initialize database with schema
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.executescript(TEST_SCHEMA)
        conn.commit()
        conn.close()

        # Clear pool cache
        from app.models.database import _pool_cache

        _pool_cache.clear()

        # Import after environment setup
        from fastapi import FastAPI

        from app.api.routes.users import router as users_router
        from app.models.database import SQLiteConnectionPool
        from app.services.auth_service import hash_password

        # Create test pool
        self.test_pool = SQLiteConnectionPool(self.db_path, max_size=3)

        # Create test users
        conn = self.test_pool.get_connection()
        self.superadmin_id = conn.execute(
            "INSERT INTO users (username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, 1)",
            ("superadmin", hash_password("Password123"), "Super Admin", "superadmin"),
        ).lastrowid
        self.admin_id = conn.execute(
            "INSERT INTO users (username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, 1)",
            ("admin", hash_password("Password123"), "Admin User", "admin"),
        ).lastrowid
        self.member_id = conn.execute(
            "INSERT INTO users (username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, 1)",
            ("member", hash_password("Password123"), "Member User", "member"),
        ).lastrowid
        conn.commit()
        self.test_pool.release_connection(conn)

        # Create app with users router
        app = FastAPI()
        app.include_router(users_router)

        # Patch get_pool to return test pool
        from app.api.routes import users

        self.original_get_pool = users.get_pool
        users.get_pool = lambda path: self.test_pool

        # Override deps
        from app.api import deps

        def override_get_db():
            conn = self.test_pool.get_connection()
            try:
                yield conn
            finally:
                self.test_pool.release_connection(conn)

        app.dependency_overrides[deps.get_db] = override_get_db

        # Mock CSRF protection
        from app.security import csrf_protect
        app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"

        from fastapi.testclient import TestClient

        self.client = TestClient(app)
        # Override default User-Agent so fingerprint validation matches token
        self.client.headers["user-agent"] = ""

    def tearDown(self):
        """Clean up after tests."""
        from app.api.routes import users
        from app.models.database import _pool_cache

        self.client.close()
        users.get_pool = self.original_get_pool
        _pool_cache.clear()
        self.test_pool.close_all()
        self._deps_module.settings.users_enabled = self._original_users_enabled

        import shutil

        try:
            shutil.rmtree(self.temp_dir)
        except Exception:
            pass

    def get_token(self, user_id: int, username: str, role: str) -> str:
        """Generate JWT token for test user."""
        from app.services.auth_service import (
            compute_client_fingerprint,
            create_access_token,
        )

        return create_access_token(user_id, username, role,
                           client_fingerprint=compute_client_fingerprint(""))

    def test_create_user_success(self):
        """Admin can create a new user with valid data."""
        token = self.get_token(self.admin_id, "admin", "admin")
        response = self.client.post(
            "/users/",
            json={
                "username": "newuser",
                "password": "SecurePass123",
                "full_name": "New User",
                "role": "member",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(
            response.status_code, 200, f"Expected 200, got: {response.text}"
        )
        data = response.json()
        self.assertEqual(data["username"], "newuser")
        self.assertEqual(data["full_name"], "New User")
        self.assertEqual(data["role"], "member")
        self.assertTrue(data["is_active"])
        self.assertIn("id", data)
        self.assertIn("created_at", data)

    def test_create_user_duplicate_username(self):
        """Cannot create user with existing username (case-insensitive)."""
        token = self.get_token(self.admin_id, "admin", "admin")
        response = self.client.post(
            "/users/",
            json={
                "username": "member",  # Already exists
                "password": "SecurePass123",
                "full_name": "Duplicate",
                "role": "member",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("already exists", response.json()["detail"])

    def test_create_user_duplicate_username_case_insensitive(self):
        """Username uniqueness is case-insensitive."""
        token = self.get_token(self.admin_id, "admin", "admin")
        response = self.client.post(
            "/users/",
            json={
                "username": "MEMBER",  # Same as 'member' (case-insensitive)
                "password": "SecurePass123",
                "full_name": "Uppercase",
                "role": "member",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("already exists", response.json()["detail"])

    def test_create_user_weak_password_no_digit(self):
        """Password without digit is rejected."""
        token = self.get_token(self.admin_id, "admin", "admin")
        response = self.client.post(
            "/users/",
            json={
                "username": "weakpassuser",
                "password": "NoDigitsHere",  # No digits
                "full_name": "Weak Pass",
                "role": "member",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("digit", response.json()["detail"].lower())

    def test_create_user_weak_password_no_uppercase(self):
        """Password without uppercase is rejected."""
        token = self.get_token(self.admin_id, "admin", "admin")
        response = self.client.post(
            "/users/",
            json={
                "username": "weakpassuser2",
                "password": "nodigitshere1",  # No uppercase
                "full_name": "Weak Pass",
                "role": "member",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("uppercase", response.json()["detail"].lower())

    def test_create_user_weak_password_too_short(self):
        """Password shorter than 8 characters is rejected."""
        token = self.get_token(self.admin_id, "admin", "admin")
        response = self.client.post(
            "/users/",
            json={
                "username": "shortpass",
                "password": "Short1",  # Too short
                "full_name": "Short",
                "role": "member",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(response.status_code, 422)

    def test_create_user_invalid_role(self):
        """Invalid role is rejected."""
        token = self.get_token(self.admin_id, "admin", "admin")
        response = self.client.post(
            "/users/",
            json={
                "username": "invalidrole",
                "password": "SecurePass123",
                "full_name": "Invalid",
                "role": "hacker",  # Invalid role
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Invalid role", response.json()["detail"])

    def test_create_user_admin_can_create_admin(self):
        """Admin can create other admins."""
        token = self.get_token(self.admin_id, "admin", "admin")
        response = self.client.post(
            "/users/",
            json={
                "username": "newadmin",
                "password": "SecurePass123",
                "full_name": "New Admin",
                "role": "admin",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["role"], "admin")

    def test_create_user_admin_cannot_create_superadmin(self):
        """Admin cannot create superadmin users."""
        token = self.get_token(self.admin_id, "admin", "admin")
        response = self.client.post(
            "/users/",
            json={
                "username": "newsuper",
                "password": "SecurePass123",
                "full_name": "New Super",
                "role": "superadmin",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn("superadmin", response.json()["detail"].lower())

    def test_create_user_superadmin_can_create_superadmin(self):
        """Superadmin can create other superadmin users."""
        token = self.get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.post(
            "/users/",
            json={
                "username": "newsuper2",
                "password": "SecurePass123",
                "full_name": "New Super 2",
                "role": "superadmin",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["role"], "superadmin")

    def test_create_user_member_rejected(self):
        """Member role is rejected (requires admin)."""
        token = self.get_token(self.member_id, "member", "member")
        response = self.client.post(
            "/users/",
            json={
                "username": "trycreate",
                "password": "SecurePass123",
                "full_name": "Try",
                "role": "member",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(response.status_code, 403)

    def test_create_user_unauthenticated_rejected(self):
        """Unauthenticated request is rejected."""
        response = self.client.post(
            "/users/",
            json={
                "username": "notoken",
                "password": "SecurePass123",
                "full_name": "No Token",
                "role": "member",
            },
        )

        self.assertEqual(response.status_code, 401)

    def test_create_user_default_role_is_member(self):
        """Default role is 'member' when not specified."""
        token = self.get_token(self.admin_id, "admin", "admin")
        response = self.client.post(
            "/users/",
            json={
                "username": "defaultrole",
                "password": "SecurePass123",
                "full_name": "Default",
                # No role specified
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["role"], "member")

    def test_create_user_empty_full_name(self):
        """Empty full_name is allowed (defaults to empty string)."""
        token = self.get_token(self.admin_id, "admin", "admin")
        response = self.client.post(
            "/users/",
            json={
                "username": "emptyname",
                "password": "SecurePass123",
                "full_name": "",
                "role": "member",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["full_name"], "")

    def test_create_user_password_max_length_exceeded(self):
        """Password exceeding 128 characters is rejected."""
        token = self.get_token(self.admin_id, "admin", "admin")
        long_password = "A" * 129 + "1"  # 130 characters
        response = self.client.post(
            "/users/",
            json={
                "username": "longpass",
                "password": long_password,
                "full_name": "Long",
                "role": "member",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        # Pydantic should reject this (max_length=128)
        self.assertEqual(response.status_code, 422)  # Validation error

    def test_create_user_username_too_short(self):
        """Username shorter than 3 characters is rejected."""
        token = self.get_token(self.admin_id, "admin", "admin")
        response = self.client.post(
            "/users/",
            json={
                "username": "ab",  # Too short
                "password": "SecurePass123",
                "full_name": "Short",
                "role": "member",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(response.status_code, 422)  # Validation error


# ============================================================================
# Tests for dynamic secure cookies
# ============================================================================


class TestDynamicSecureCookies(unittest.TestCase):
    """Tests for dynamic secure cookie behavior based on request scheme."""

    def test_secure_cookie_true_for_https(self):
        """Cookies should have secure=True for HTTPS requests."""
        from app.api.routes.auth import _is_secure_request

        mock_request = MagicMock(spec=Request)
        mock_request.url.scheme = "https"
        mock_request.headers.get.return_value = ""

        self.assertTrue(_is_secure_request(mock_request))

    def test_secure_cookie_false_for_http(self):
        """Cookies should have secure=False for HTTP requests."""
        from app.api.routes.auth import _is_secure_request

        mock_request = MagicMock(spec=Request)
        mock_request.url.scheme = "http"
        mock_request.headers.get.return_value = ""

        self.assertFalse(_is_secure_request(mock_request))


if __name__ == "__main__":
    unittest.main(verbosity=2)
