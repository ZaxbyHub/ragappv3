"""Regression test for FR-007: password_changed_at epoch invalidates access tokens.

Scenario: login → get access token T0 → change_password at T1 > T0 → present
original token to protected route → expect 401.

This test MUST fail against commit 12f1db6 (pre-fix) where the token epoch
check was not implemented.
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

# Set up test environment BEFORE importing app modules
os.environ["JWT_SECRET_KEY"] = (
    "test-jwt-secret-key-for-testing-only-12345678901234567890"
)
os.environ["USERS_ENABLED"] = "true"

from fastapi.testclient import TestClient

from app.config import settings
from app.models.database import SQLiteConnectionPool, init_db, run_migrations


class TestPasswordEpochInvalidatesTokens(unittest.TestCase):
    """Test suite for FR-007: password epoch invalidates tokens."""

    def setUp(self):
        """Set up test client with temporary database."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")

        # Initialize database with schema
        init_db(self.db_path)
        run_migrations(self.db_path)

        # Store original settings to restore later
        self._original_jwt_secret = settings.jwt_secret_key
        self._original_users_enabled = settings.users_enabled

        # Override JWT secret for testing
        settings.jwt_secret_key = os.environ["JWT_SECRET_KEY"]
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
        # on state (Redis-unavailable -> in-memory fallback).
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

    def _create_user_and_login(self, username: str, pwd_value: str) -> tuple:
        """Create a user, register, login, and return (user_id, access_token)."""
        # Register user
        reg_response = self.client.post(
            "/api/auth/register",
            json={"username": username, "password": pwd_value},
        )
        if reg_response.status_code != 200:
            raise AssertionError(
                f"Registration failed: {reg_response.status_code} {reg_response.json()}"
            )

        # Login
        login_response = self.client.post(
            "/api/auth/login",
            json={"username": username, "password": pwd_value},
        )
        if login_response.status_code != 200:
            raise AssertionError(
                f"Login failed: {login_response.status_code} {login_response.json()}"
            )

        data = login_response.json()
        return data["user"]["id"], data["access_token"]

    def _get_password_changed_at(self, user_id: int) -> float:
        """Get the password_changed_at epoch for a user."""
        conn = self.test_pool.get_connection()
        try:
            cursor = conn.execute(
                "SELECT password_changed_at FROM users WHERE id = ?",
                (user_id,),
            )
            row = cursor.fetchone()
            return float(row[0]) if row else 0.0
        finally:
            self.test_pool.release_connection(conn)

    def test_token_invalidated_after_password_change(self):
        """A token issued before a password change is rejected with 401."""
        # Use unique username to avoid conflicts with parallel test runs
        import uuid
        username = f"testuser_epoch_{uuid.uuid4().hex[:8]}"

        # (a) Create user and login — get token T0
        user_id, token_t0 = self._create_user_and_login(
            username, "OldPass123"
        )

        # Verify token_t0 works against a protected endpoint
        me_response = self.client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {token_t0}"},
        )
        if me_response.status_code != 200:
            raise AssertionError(
                f"Precondition: token should be valid, got {me_response.status_code}"
            )

        # Record T0 password_changed_at (should be 0 — never changed password)
        epoch_before = self._get_password_changed_at(user_id)
        if epoch_before != 0.0:
            raise AssertionError(
                f"Precondition: password_changed_at should be 0, got {epoch_before}"
            )

        # (b) Change password at T1 — this bumps password_changed_at
        change_response = self.client.post(
            "/api/auth/change-password",
            headers={"Authorization": f"Bearer {token_t0}"},
            json={"current_password": "OldPass123", "new_password": "NewPass456"},
        )
        if change_response.status_code != 200:
            raise AssertionError(
                f"Password change failed: {change_response.status_code} {change_response.json()}"
            )

        # Verify password_changed_at was bumped
        epoch_after = self._get_password_changed_at(user_id)
        if epoch_after <= 0:
            raise AssertionError(
                f"password_changed_at should be > 0 after password change, got {epoch_after}"
            )

        # (c) Present the OLD token T0 to a protected route — expect 401
        me_with_old_token = self.client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {token_t0}"},
        )
        if me_with_old_token.status_code != 401:
            raise AssertionError(
                f"Expected 401 for stale token, got {me_with_old_token.status_code}: "
                f"{me_with_old_token.json()}"
            )
        detail = me_with_old_token.json().get("detail", "")
        if "password" not in detail.lower() and "token" not in detail.lower():
            raise AssertionError(
                f"Expected 401 about password/token invalidation, got: {detail}"
            )

    def test_token_valid_if_password_never_changed(self):
        """A token is valid if password_changed_at is 0 (column added but no change)."""
        # (a) Create user and login — get token
        user_id, token = self._create_user_and_login(
            "testuser_nochange", "SomePass123"
        )

        # Verify password_changed_at is 0
        epoch = self._get_password_changed_at(user_id)
        if epoch != 0.0:
            raise AssertionError(
                f"Precondition: password_changed_at should be 0, got {epoch}"
            )

        # (b) Token should still work (iat < password_changed_at check is skipped when 0)
        me_response = self.client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        if me_response.status_code != 200:
            raise AssertionError(
                f"Token should be valid when password_changed_at is 0, "
                f"got {me_response.status_code}: {me_response.json()}"
            )

    def test_invalidate_active_user_cache_called_on_password_change(self):
        """change_password calls invalidate_active_user_cache to evict cached principal."""
        # (a) Create user and login to populate the active-user cache
        user_id, token = self._create_user_and_login(
            "testuser_cache", "CachePass123"
        )

        # Verify token works (caches the user)
        me_response = self.client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        if me_response.status_code != 200:
            raise AssertionError(
                f"Precondition: token should be cached, got {me_response.status_code}"
            )

        # (b) Change password — this should invalidate the cache entry
        change_response = self.client.post(
            "/api/auth/change-password",
            headers={"Authorization": f"Bearer {token}"},
            json={"current_password": "CachePass123", "new_password": "NewCachePass456"},
        )
        if change_response.status_code != 200:
            raise AssertionError(
                f"Password change failed: {change_response.status_code} {change_response.json()}"
            )

        # (c) The old token should be rejected (cache was invalidated and epoch check fails)
        me_with_old_token = self.client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        if me_with_old_token.status_code != 401:
            raise AssertionError(
                f"Old token should be rejected after cache invalidation, "
                f"got {me_with_old_token.status_code}: {me_with_old_token.json()}"
            )

    def test_token_invalidated_after_admin_password_reset(self):
        """A token issued before an admin password reset is rejected with 401."""
        import uuid

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.api.routes.users import router as users_router
        from app.models.database import SQLiteConnectionPool
        from app.services.auth_service import (
            compute_client_fingerprint,
            create_access_token,
        )

        # (a) Create a regular user and login — get token T0
        username = f"user_admin_reset_{uuid.uuid4().hex[:8]}"
        user_id, token_t0 = self._create_user_and_login(username, "UserPass123")

        # Verify token_t0 works against a protected endpoint
        me_response = self.client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {token_t0}"},
        )
        if me_response.status_code != 200:
            raise AssertionError(
                f"Precondition: token should be valid, got {me_response.status_code}"
            )

        # Record T0 password_changed_at (should be 0 — never changed password)
        epoch_before = self._get_password_changed_at(user_id)
        if epoch_before != 0.0:
            raise AssertionError(
                f"Precondition: password_changed_at should be 0, got {epoch_before}"
            )

        # (b) Create an admin user directly in DB and generate admin token
        admin_username = f"admin_for_reset_{uuid.uuid4().hex[:8]}"
        admin_conn = self.test_pool.get_connection()
        try:
            from app.services.auth_service import hash_password
            hashed_admin_pw = hash_password("AdminPass123")
            admin_conn.execute(
                """INSERT INTO users (username, hashed_password, full_name, role, is_active, must_change_password)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (admin_username, hashed_admin_pw, "Admin User", "admin", 1, 0),
            )
            admin_conn.commit()
            cursor = admin_conn.execute(
                "SELECT id FROM users WHERE username = ?", (admin_username,)
            )
            admin_row = cursor.fetchone()
            if admin_row is None:
                raise AssertionError("Admin user not found in DB")
            admin_user_id = admin_row[0]
        finally:
            self.test_pool.release_connection(admin_conn)

        # Generate admin token directly (matching test_admin_reset_unlocks_account.py pattern)
        admin_token = create_access_token(
            admin_user_id,
            admin_username,
            "admin",
            client_fingerprint=compute_client_fingerprint(""),
        )

        # (c) Admin calls PATCH /users/{user_id}/password to reset the user's password
        # This should bump password_changed_at and invalidate the user's token
        users_app = FastAPI()
        users_app.include_router(users_router)

        test_pool = SQLiteConnectionPool(self.db_path, max_size=3)

        def override_get_db():
            conn = test_pool.get_connection()
            try:
                yield conn
            finally:
                test_pool.release_connection(conn)

        from app.api import deps
        from app.security import csrf_protect

        users_app.dependency_overrides[deps.get_db] = override_get_db
        users_app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"

        users_client = TestClient(users_app)
        users_client.headers["user-agent"] = ""

        admin_reset_response = users_client.patch(
            f"/users/{user_id}/password",
            json={"new_password": "NewAdminResetPass456"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        if admin_reset_response.status_code != 200:
            raise AssertionError(
                f"Admin password reset failed: {admin_reset_response.status_code} "
                f"{admin_reset_response.json()}"
            )

        # Verify password_changed_at was bumped by admin reset
        epoch_after = self._get_password_changed_at(user_id)
        if epoch_after <= 0:
            raise AssertionError(
                f"password_changed_at should be > 0 after admin reset, got {epoch_after}"
            )

        # (d) Present the OLD token T0 to a protected route — expect 401
        me_with_old_token = self.client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {token_t0}"},
        )
        if me_with_old_token.status_code != 401:
            raise AssertionError(
                f"Expected 401 for token issued before admin password reset, "
                f"got {me_with_old_token.status_code}: {me_with_old_token.json()}"
            )
        detail = me_with_old_token.json().get("detail", "")
        if "password" not in detail.lower() and "token" not in detail.lower():
            raise AssertionError(
                f"Expected 401 about password/token invalidation, got: {detail}"
            )

        test_pool.close_all()
