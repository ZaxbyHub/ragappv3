"""Tests for POST /auth/change-password endpoint.

Tests cover:
- Happy path: user changes password successfully, sessions revoked, new tokens issued
- Wrong current_password returns 400
- Weak new_password (too short, no digit, no uppercase) returns 400
- User not found (edge case)
- Session revocation: verify all old sessions are deleted from user_sessions table
- New session is created after password change

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


class TestChangePassword(unittest.TestCase):
    """Test suite for POST /auth/change-password endpoint."""

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
        from app.security import CSRFManager, csrf_protect

        # Override the get_db dependency to use our test pool
        def get_test_db():
            conn = self.test_pool.get_connection()
            try:
                yield conn
            finally:
                self.test_pool.release_connection(conn)

        main_app.dependency_overrides[get_db] = get_test_db

        # The login/register handlers issue a fresh CSRF token in their body via
        # get_csrf_manager() + issue_csrf_token(), so the app needs a csrf_manager
        # on state (Redis-unavailable -> in-memory fallback) or they 503. These
        # tests exercise the change-password *flow*, not CSRF enforcement (that is
        # covered in test_csrf_auth.py), so we also stub csrf_protect to a
        # pass-through.
        self.csrf_manager = CSRFManager(redis_url="redis://localhost:6379/0", ttl=900)
        main_app.state.csrf_manager = self.csrf_manager
        main_app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"

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

    def _get_session_count(self, user_id: int) -> int:
        """Helper to count sessions for a user."""
        conn = self.test_pool.get_connection()
        try:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM user_sessions WHERE user_id = ?",
                (user_id,),
            )
            row = cursor.fetchone()
            return row[0] if row else 0
        finally:
            self.test_pool.release_connection(conn)

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

    def test_change_password_happy_path(self):
        """User changes password successfully, sessions revoked, new tokens issued."""
        # Register user
        self.client.post(
            "/api/auth/register",
            json={"username": "changepw", "password": "OldPass123"},
        )

        # Login to get access token
        login_response = self.client.post(
            "/api/auth/login",
            json={"username": "changepw", "password": "OldPass123"},
        )
        self.assertEqual(login_response.status_code, 200)
        access_token = login_response.json()["access_token"]

        # Get user ID and verify session exists
        user_id = self._get_user_id("changepw")
        session_count_before = self._get_session_count(user_id)
        self.assertGreaterEqual(session_count_before, 1)

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
        # refresh_token is delivered as an httpOnly cookie, not in the JSON body
        # (asserted below) — matching login/register.
        self.assertEqual(data["token_type"], "bearer")

        # New access token should be different from old one
        self.assertNotEqual(data["access_token"], access_token)

        # Old sessions should be deleted
        session_count_after = self._get_session_count(user_id)
        self.assertEqual(session_count_after, 1)  # Only the new session

        # New refresh token cookie should be set
        cookies = response.cookies
        self.assertIn("refresh_token", cookies)

    def test_change_password_wrong_current_password(self):
        """Wrong current_password returns 400."""
        # Register user
        self.client.post(
            "/api/auth/register",
            json={"username": "wrongpwuser", "password": "CorrectPass123"},
        )

        # Login
        login_response = self.client.post(
            "/api/auth/login",
            json={"username": "wrongpwuser", "password": "CorrectPass123"},
        )
        access_token = login_response.json()["access_token"]

        # Try to change password with wrong current password
        response = self.client.post(
            "/api/auth/change-password",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"current_password": "WrongPassword123", "new_password": "NewPass456"},
        )

        # Should return 400
        self.assertEqual(response.status_code, 400)
        self.assertIn("incorrect", response.json()["detail"].lower())

    def test_change_password_weak_password_too_short(self):
        """Weak new_password (too short) returns 400."""
        # Register user
        self.client.post(
            "/api/auth/register",
            json={"username": "shortpwuser", "password": "ValidPass123"},
        )

        # Login
        login_response = self.client.post(
            "/api/auth/login",
            json={"username": "shortpwuser", "password": "ValidPass123"},
        )
        access_token = login_response.json()["access_token"]

        # Try to change password with too-short password
        response = self.client.post(
            "/api/auth/change-password",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"current_password": "ValidPass123", "new_password": "Short1"},
        )

        # Should return 400
        self.assertEqual(response.status_code, 400)
        self.assertIn("8 characters", response.json()["detail"])

    def test_change_password_weak_password_no_digit(self):
        """Weak new_password (no digit) returns 400."""
        # Register user
        self.client.post(
            "/api/auth/register",
            json={"username": "nodigitpw", "password": "ValidPass123"},
        )

        # Login
        login_response = self.client.post(
            "/api/auth/login",
            json={"username": "nodigitpw", "password": "ValidPass123"},
        )
        access_token = login_response.json()["access_token"]

        # Try to change password without digit
        response = self.client.post(
            "/api/auth/change-password",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"current_password": "ValidPass123", "new_password": "NoDigitPass"},
        )

        # Should return 400
        self.assertEqual(response.status_code, 400)
        self.assertIn("digit", response.json()["detail"].lower())

    def test_change_password_weak_password_no_uppercase(self):
        """Weak new_password (no uppercase) returns 400."""
        # Register user
        self.client.post(
            "/api/auth/register",
            json={"username": "noupperpw", "password": "ValidPass123"},
        )

        # Login
        login_response = self.client.post(
            "/api/auth/login",
            json={"username": "noupperpw", "password": "ValidPass123"},
        )
        access_token = login_response.json()["access_token"]

        # Try to change password without uppercase
        response = self.client.post(
            "/api/auth/change-password",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"current_password": "ValidPass123", "new_password": "nouppercase123"},
        )

        # Should return 400
        self.assertEqual(response.status_code, 400)
        self.assertIn("uppercase", response.json()["detail"].lower())

    def test_change_password_old_sessions_revoked(self):
        """All old sessions are deleted from user_sessions table."""
        # Register user
        self.client.post(
            "/api/auth/register",
            json={"username": "multisession", "password": "InitialPass123"},
        )

        # Login multiple times to create multiple sessions
        for _ in range(3):
            self.client.post(
                "/api/auth/login",
                json={"username": "multisession", "password": "InitialPass123"},
            )

        # Login once more to get access token
        login_response = self.client.post(
            "/api/auth/login",
            json={"username": "multisession", "password": "InitialPass123"},
        )
        access_token = login_response.json()["access_token"]

        # Get user ID and verify multiple sessions exist. register() auto-logs
        # the user in (1 session) and the 4 logins add 4 more → 5 total.
        user_id = self._get_user_id("multisession")
        session_count_before = self._get_session_count(user_id)
        self.assertEqual(session_count_before, 5)

        # Change password
        response = self.client.post(
            "/api/auth/change-password",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"current_password": "InitialPass123", "new_password": "NewPass456"},
        )

        self.assertEqual(response.status_code, 200)

        # All old sessions should be deleted, only new session remains
        session_count_after = self._get_session_count(user_id)
        self.assertEqual(session_count_after, 1)

    def test_change_password_new_session_created(self):
        """New session is created after password change."""
        # Register user
        self.client.post(
            "/api/auth/register",
            json={"username": "newsession", "password": "OldPass123"},
        )

        # Login
        login_response = self.client.post(
            "/api/auth/login",
            json={"username": "newsession", "password": "OldPass123"},
        )
        access_token = login_response.json()["access_token"]
        old_refresh_token = login_response.cookies.get("refresh_token")

        self._get_user_id("newsession")

        # Change password
        change_response = self.client.post(
            "/api/auth/change-password",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"current_password": "OldPass123", "new_password": "NewPass789"},
        )

        self.assertEqual(change_response.status_code, 200)
        new_refresh_token = change_response.cookies.get("refresh_token")

        # New refresh token should be different from old one
        self.assertNotEqual(new_refresh_token, old_refresh_token)
        self.assertIsNotNone(new_refresh_token)

        # New session should be usable for refresh
        refresh_response = self.client.post(
            "/api/auth/refresh", cookies={"refresh_token": new_refresh_token}
        )
        self.assertEqual(refresh_response.status_code, 200)
        self.assertIn("access_token", refresh_response.json())

    def test_change_password_requires_auth(self):
        """Without auth token, should return 401."""
        response = self.client.post(
            "/api/auth/change-password",
            json={"current_password": "OldPass123", "new_password": "NewPass456"},
        )

        # Should return 401 Unauthorized
        self.assertEqual(response.status_code, 401)

    def test_change_password_user_not_found_edge_case(self):
        """Deleted user with a still-valid token is rejected at the auth layer.

        get_current_active_user re-queries the DB on every request, so once the
        user row is gone it raises 401 ``token_invalid`` before the
        change-password handler ever runs. (The handler's own 404 path is
        therefore unreachable via the normal dependency chain — the 401 is the
        real, and stronger, contract.)
        """
        # Register and login to get a valid token
        self.client.post(
            "/api/auth/register",
            json={"username": "willbedeleted", "password": "Password123"},
        )
        login_response = self.client.post(
            "/api/auth/login",
            json={"username": "willbedeleted", "password": "Password123"},
        )
        access_token = login_response.json()["access_token"]

        # Manually delete the user from database
        conn = self.test_pool.get_connection()
        try:
            conn.execute("DELETE FROM users WHERE username = ?", ("willbedeleted",))
            conn.commit()
        finally:
            self.test_pool.release_connection(conn)

        # Try to change password with token for deleted user
        response = self.client.post(
            "/api/auth/change-password",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"current_password": "Password123", "new_password": "NewPass456"},
        )

        # The auth dependency rejects the deleted user before the handler runs.
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "token_invalid")

    def test_change_password_same_password_valid(self):
        """Can change to a different but structurally similar password."""
        # Register user
        self.client.post(
            "/api/auth/register",
            json={"username": "samepattern", "password": "Password123"},
        )

        # Login
        login_response = self.client.post(
            "/api/auth/login",
            json={"username": "samepattern", "password": "Password123"},
        )
        access_token = login_response.json()["access_token"]

        # Change to a structurally similar password (different content)
        response = self.client.post(
            "/api/auth/change-password",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"current_password": "Password123", "new_password": "Password456"},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("access_token", data)
        # refresh_token is delivered as an httpOnly cookie, not in the JSON body.
        self.assertIn("refresh_token", response.cookies)

        # New password should work for login
        login2_response = self.client.post(
            "/api/auth/login",
            json={"username": "samepattern", "password": "Password456"},
        )
        self.assertEqual(login2_response.status_code, 200)

    def test_change_password_empty_request(self):
        """Missing request body fields should return 422."""
        # Register user
        self.client.post(
            "/api/auth/register",
            json={"username": "emptyreq", "password": "Password123"},
        )

        # Login
        login_response = self.client.post(
            "/api/auth/login",
            json={"username": "emptyreq", "password": "Password123"},
        )
        access_token = login_response.json()["access_token"]

        # Try with empty body
        response = self.client.post(
            "/api/auth/change-password",
            headers={"Authorization": f"Bearer {access_token}"},
            json={},
        )

        # Should return 422 validation error
        self.assertEqual(response.status_code, 422)

    def test_change_password_whitespace_password(self):
        """Whitespace-only new_password returns 400."""
        # Register user
        self.client.post(
            "/api/auth/register",
            json={"username": "whitespacepw", "password": "Password123"},
        )

        # Login
        login_response = self.client.post(
            "/api/auth/login",
            json={"username": "whitespacepw", "password": "Password123"},
        )
        access_token = login_response.json()["access_token"]

        # Try with whitespace-only new password
        response = self.client.post(
            "/api/auth/change-password",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"current_password": "Password123", "new_password": "   "},
        )

        # Should return 400
        self.assertEqual(response.status_code, 400)
        self.assertIn("whitespace", response.json()["detail"].lower())


class TestChangePasswordRateLimit(unittest.TestCase):
    """Real-ASGI HTTP 429 regression test for #387 MED-8.

    Drives the actual POST /auth/change-password route through TestClient with
    a fully-wired app (auth + DB + middleware). The @limiter.limit("10/minute")
    decorator on the route enforces per-IP; the TestClient always presents the
    same client IP, so 11 calls within the window trip the limit.

    slowapi checks the limit inside the decorator wrapper AFTER FastAPI resolves
    dependencies, so every call must carry valid auth + a parseable body to
    count. We reuse a single login token and send a deliberately-wrong
    current_password: calls 1-10 return 400 (the limit counter still increments
    because the check runs before the handler body verifies the password), and
    the 11th call returns 429.

    This closes the gap between the source-scan test (proves the decorator
    exists) and the actual HTTP behavior the issue's AC2 requires ("returns 429
    after the Nth attempt"). The RateLimitExceeded -> 429 translation is
    performed by SlowAPIMiddleware's default handler (no explicit exception
    handler registration in main.py is required).
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        init_db(self.db_path)
        run_migrations(self.db_path)

        self._original_jwt_secret = settings.jwt_secret_key
        self._original_users_enabled = settings.users_enabled
        settings.jwt_secret_key = "test-secret-key-for-testing-at-least-32-chars-long"
        settings.users_enabled = True
        self.addCleanup(setattr, settings, "jwt_secret_key", self._original_jwt_secret)
        self.addCleanup(setattr, settings, "users_enabled", self._original_users_enabled)

        self.test_pool = SQLiteConnectionPool(self.db_path, max_size=5)
        self.addCleanup(self.test_pool.close_all)

        from app.api.deps import get_db
        from app.main import app as main_app
        from app.security import CSRFManager, csrf_protect

        def get_test_db():
            conn = self.test_pool.get_connection()
            try:
                yield conn
            finally:
                self.test_pool.release_connection(conn)

        main_app.dependency_overrides[get_db] = get_test_db
        self.addCleanup(main_app.dependency_overrides.pop, get_db, None)
        self.csrf_manager = CSRFManager(
            redis_url="redis://localhost:6379/0", ttl=900
        )
        main_app.state.csrf_manager = self.csrf_manager
        main_app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
        self.addCleanup(main_app.dependency_overrides.pop, csrf_protect, None)

        self.client = TestClient(main_app)
        self.app = main_app

        import shutil
        self.addCleanup(lambda: shutil.rmtree(self.temp_dir, ignore_errors=True))

    def test_change_password_route_endpoint_is_wrapped(self):
        """The router must store the limiter-WRAPPED endpoint, not the raw func.

        This guards the decorator ORDER: ``@router.post`` must be outermost so
        it captures the ``@limiter.limit``-wrapped function. The reverse order
        (limiter above router) silently breaks enforcement because the router
        stores the unwrapped function and the limit check never fires.
        """
        from app.api.routes.auth import router

        for route in router.routes:
            if hasattr(route, "path") and "change-password" in route.path:
                self.assertTrue(
                    hasattr(route.endpoint, "__wrapped__"),
                    "route.endpoint must be the limiter-wrapped function "
                    "(decorator order: @router.post outermost, @limiter.limit below)",
                )
                return
        self.fail("change-password route not found in router")

    def test_change_password_returns_429_after_limit(self):
        """The 11th change-password call within the window returns HTTP 429.

        Drives the real POST /auth/change-password route through TestClient.
        The @limiter.limit("10/minute") decorator (with @router.post outermost
        so the router stores the wrapper) enforces per-IP; the TestClient
        always presents the same client IP, so 11 calls within the window trip
        the limit. Calls 1-10 use a wrong current_password (return 400) but
        still count toward the limit because slowapi checks before the handler
        body runs. The 11th call returns 429.

        The RateLimitExceeded exception (raised by the decorator wrapper) is a
        Starlette HTTPException with status_code=429, so the framework's
        default exception handler converts it to the 429 response.
        """
        # Register + login once; the rate limit is per-IP, not per-token.
        self.client.post(
            "/api/auth/register",
            json={"username": "ratelimituser", "password": "OldPass123"},
        )
        login_response = self.client.post(
            "/api/auth/login",
            json={"username": "ratelimituser", "password": "OldPass123"},
        )
        self.assertEqual(login_response.status_code, 200)
        access_token = login_response.json()["access_token"]
        headers = {"Authorization": f"Bearer {access_token}"}
        body = {"current_password": "WrongPassword999", "new_password": "NewPass456"}

        # First 10 calls: wrong current_password -> 400, but the rate-limit
        # counter increments (slowapi checks before the handler body runs).
        for i in range(10):
            resp = self.client.post(
                "/api/auth/change-password", headers=headers, json=body
            )
            self.assertEqual(
                resp.status_code, 400,
                f"call {i + 1}/10 should return 400 (wrong password), "
                f"got {resp.status_code}",
            )

        # 11th call: rate limit tripped -> HTTP 429.
        resp = self.client.post(
            "/api/auth/change-password", headers=headers, json=body
        )
        self.assertEqual(
            resp.status_code, 429,
            f"11th call should return 429 (rate limited), got {resp.status_code}",
        )


if __name__ == "__main__":
    unittest.main()
