"""
Regression test for FR-002: Admin password reset clears failed_attempts and locked_until.

Verifies that when an admin resets a user's password:
(a) failed_attempts is reset to 0
(b) locked_until is set to NULL (lockout cleared)
(c) the user can immediately log in with the new password

This test MUST fail against commit 12f1db6 (pre-fix) where the admin reset
did NOT clear locked_until or failed_attempts.
"""

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Set up test environment BEFORE importing app modules
os.environ["JWT_SECRET_KEY"] = (
    "test-jwt-secret-key-for-testing-only-12345678901234567890"
)
os.environ["USERS_ENABLED"] = "true"

# Now safe to import app modules
from backend.tests.schema_constants import TEST_SCHEMA

from app.services.auth_service import (
    compute_client_fingerprint,
    create_access_token,
    hash_password,
)


def setup_test_db(db_path: str) -> sqlite3.Connection:
    """Set up test database with schema and initial users."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(TEST_SCHEMA)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_users_locked_until ON users(locked_until)"
    )
    # user_sessions table is required by the login endpoint
    conn.execute(
        """CREATE TABLE IF NOT EXISTS user_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            refresh_token_hash TEXT NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used_at TIMESTAMP,
            ip_address TEXT,
            user_agent TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )"""
    )
    conn.commit()
    return conn


def create_user(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    role: str,
    full_name: str = "",
    is_active: int = 1,
    must_change_password: int = 0,
) -> int:
    """Create a test user and return its ID."""
    hashed = hash_password(password)
    cursor = conn.execute(
        """INSERT INTO users (username, hashed_password, full_name, role, is_active, must_change_password)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (username, hashed, full_name, role, is_active, must_change_password),
    )
    conn.commit()
    return cursor.lastrowid


def get_token(user_id: int, username: str, role: str) -> str:
    """Generate a JWT token for a test user."""
    return create_access_token(
        user_id, username, role, client_fingerprint=compute_client_fingerprint("")
    )


class TestAdminResetUnlocksAccount:
    """Tests that admin password reset clears lockout state."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up test database with a locked-out user for each test."""
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")

        # Clear the global pool cache to ensure test isolation
        from app.models.database import _pool_cache

        _pool_cache.clear()

        # Set up test database
        self.conn = setup_test_db(self.db_path)

        # Create test users
        self.admin_id = create_user(
            self.conn, "admin", "pass123", "admin", "Admin User"
        )
        self.member_id = create_user(
            self.conn, "member", "pass123", "member", "Regular Member"
        )

        # Simulate a locked-out user by directly inserting lockout state
        # (5 failed attempts + locked_until in the future)
        future_lockout = datetime.now(timezone.utc) + timedelta(minutes=15)
        self.conn.execute(
            "UPDATE users SET failed_attempts = 5, locked_until = ? WHERE id = ?",
            (future_lockout.isoformat(), self.member_id),
        )
        self.conn.commit()

        # Verify the lockout state is set before the test
        cursor = self.conn.execute(
            "SELECT failed_attempts, locked_until FROM users WHERE id = ?",
            (self.member_id,),
        )
        row = cursor.fetchone()
        if row[0] != 5:
            raise AssertionError("Precondition: failed_attempts should be 5")
        if row[1] is None:
            raise AssertionError("Precondition: locked_until should be set")

        # Create app with users router
        from app.api.routes.users import router as users_router
        from app.models.database import SQLiteConnectionPool

        app = FastAPI()
        app.include_router(users_router)

        # Override the get_db dependency to use our test database
        from app.api import deps

        # Create a test pool
        test_pool = SQLiteConnectionPool(self.db_path, max_size=3)

        def override_get_db():
            """Override get_db to return a connection from test pool."""
            conn = test_pool.get_connection()
            try:
                yield conn
            finally:
                test_pool.release_connection(conn)

        # Patch get_pool in users module to return our test pool
        from app.api.routes import users

        original_get_pool = users.get_pool
        users.get_pool = lambda path: test_pool

        app.dependency_overrides[deps.get_db] = override_get_db

        # Mutating user endpoints require csrf_protect — override to pass-through
        from app.security import csrf_protect

        app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"

        # Store for cleanup
        self.test_pool = test_pool
        self.original_get_pool = original_get_pool

        from app.config import settings

        self._orig_users_enabled = settings.users_enabled
        self._orig_jwt_secret = settings.jwt_secret_key
        settings.users_enabled = True
        settings.jwt_secret_key = os.environ["JWT_SECRET_KEY"]

        self.client = TestClient(app)
        self.client.headers["user-agent"] = ""

        yield

        # Teardown
        self.test_pool.close_all()
        settings.users_enabled = self._orig_users_enabled
        settings.jwt_secret_key = self._orig_jwt_secret

        # Restore original get_pool
        users.get_pool = self.original_get_pool

        # Clean up temp directory
        try:
            import shutil

            shutil.rmtree(self.tmpdir, ignore_errors=True)
        except Exception:
            pass

    def test_admin_reset_clears_failed_attempts_and_locked_until(self):
        """Admin password reset clears failed_attempts to 0 and locked_until to NULL."""
        # (a) Verify pre-condition: user is locked out
        cursor = self.conn.execute(
            "SELECT failed_attempts, locked_until FROM users WHERE id = ?",
            (self.member_id,),
        )
        row = cursor.fetchone()
        if row[0] != 5:
            raise AssertionError()
        if row[1] is None:
            raise AssertionError()

        # (b) Admin calls PATCH /users/{id}/password to reset the locked-out user's password
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.patch(
            f"/users/{self.member_id}/password",
            json={"new_password": "NewP@ss123"},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200, (
            f"Admin reset should succeed, got {response.status_code}: {response.json()}"
        )
        data = response.json()
        assert data["message"] == "Password reset successfully"
        assert data["must_change_password"] is True

        # (c) Query the users row directly: assert failed_attempts == 0 and locked_until IS NULL
        cursor = self.conn.execute(
            "SELECT failed_attempts, locked_until FROM users WHERE id = ?",
            (self.member_id,),
        )
        row = cursor.fetchone()
        assert row[0] == 0, (
            f"failed_attempts should be 0 after reset, got {row[0]}"
        )
        assert row[1] is None, (
            f"locked_until should be NULL after reset, got {row[1]}"
        )

    def test_locked_out_user_can_login_immediately_after_reset(self):
        """A locked-out user can log in with the new password immediately after admin reset."""
        # (a) Pre-condition: user is locked out
        cursor = self.conn.execute(
            "SELECT failed_attempts, locked_until FROM users WHERE id = ?",
            (self.member_id,),
        )
        row = cursor.fetchone()
        assert row[0] == 5
        assert row[1] is not None

        # (b) Admin resets the password
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.patch(
            f"/users/{self.member_id}/password",
            json={"new_password": "NewP@ss123"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200

        # (d) The locked-out user can IMMEDIATELY call POST /login with the new password
        # and receive 200 with a session cookie/access token.
        #
        # We test this by using the auth router's login endpoint directly.
        # Set up an auth router client for this assertion.
        from app.api.routes.auth import router as auth_router
        from app.models.database import SQLiteConnectionPool

        auth_app = FastAPI()
        auth_app.include_router(auth_router, prefix="/api")

        test_pool = SQLiteConnectionPool(self.db_path, max_size=3)

        def override_get_db():
            conn = test_pool.get_connection()
            try:
                yield conn
            finally:
                test_pool.release_connection(conn)

        from app.api import deps

        auth_app.dependency_overrides[deps.get_db] = override_get_db

        from app.security import csrf_protect

        auth_app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"

        auth_client = TestClient(auth_app)
        auth_client.headers["user-agent"] = ""

        login_response = auth_client.post(
            "/api/auth/login",
            json={
                "username": "member",
                "password": "NewP@ss123",
            },
        )

        assert login_response.status_code == 200, (
            f"Locked-out user should be able to login immediately after reset. "
            f"Got {login_response.status_code}: {login_response.text}"
        )
        # Should get a session cookie or access token
        assert (
            "set-cookie" in login_response.headers
            or "access_token" in login_response.json()
        ), "Login response should contain a session cookie or access token"

        test_pool.close_all()
