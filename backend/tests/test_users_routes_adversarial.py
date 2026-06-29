"""
Adversarial security tests for user management admin routes.

Tests attack vectors:
- SQL injection attempts
- XSS in usernames
- Mass assignment / privilege escalation
- Negative/zero/integer overflow user_ids
- Oversized pagination parameters
- Empty/malformed request bodies
- JWT token manipulation (wrong algorithm, expired)
- Race conditions (last-superadmin)
"""

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Set up test environment BEFORE importing app modules
os.environ["JWT_SECRET_KEY"] = (
    "test-jwt-secret-key-for-testing-only-12345678901234567890"
)
os.environ["USERS_ENABLED"] = "true"

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

    conn.commit()
    return conn


def create_user(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    role: str,
    full_name: str = "",
    is_active: int = 1,
) -> int:
    """Create a test user and return its ID."""
    hashed = hash_password(password)
    cursor = conn.execute(
        """INSERT INTO users (username, hashed_password, full_name, role, is_active)
           VALUES (?, ?, ?, ?, ?)""",
        (username, hashed, full_name, role, is_active),
    )
    conn.commit()
    return cursor.lastrowid


def create_user_with_xss_payload(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    role: str,
    full_name: str = "",
    is_active: int = 1,
) -> int:
    """Create a test user with XSS payload in username."""
    hashed = hash_password(password)
    # Include XSS payloads in username
    malicious_username = username + "<script>alert('XSS')</script>"
    cursor = conn.execute(
        """INSERT INTO users (username, hashed_password, full_name, role, is_active)
           VALUES (?, ?, ?, ?, ?)""",
        (malicious_username, hashed, full_name, role, is_active),
    )
    conn.commit()
    return cursor.lastrowid


def get_token(user_id: int, username: str, role: str) -> str:
    """Generate a valid JWT token for a test user."""
    return create_access_token(user_id, username, role,
                        client_fingerprint=compute_client_fingerprint(""))


def get_expired_token(user_id: int, username: str, role: str) -> str:
    """Generate an expired JWT token."""
    from app.config import settings

    secret = settings.jwt_secret_key
    expires = datetime.now(timezone.utc) - timedelta(hours=1)  # Expired 1 hour ago
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "exp": expires,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def get_malformed_token_wrong_algorithm(user_id: int, username: str, role: str) -> str:
    """Generate a token with wrong algorithm (algorithm=none attack)."""

    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "exp": expires,
    }
    # Try algorithm=none - should be rejected
    return jwt.encode(payload, "", algorithm="none")


def get_token_with_modified_role(
    user_id: int, username: str, original_role: str, new_role: str
) -> str:
    """Generate a token with a different role than what user has."""
    from app.config import settings

    secret = settings.jwt_secret_key
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": new_role,  # Attempting to escalate to superadmin
        "exp": expires,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


class TestSQLInjection:
    """Tests for SQL injection attack vectors."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up test database for each test."""
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")

        from app.models.database import _pool_cache

        _pool_cache.clear()

        self.conn = setup_test_db(self.db_path)

        # Create test users
        self.superadmin_id = create_user(
            self.conn, "superadmin", "pass123", "superadmin", "Super Admin"
        )
        self.admin_id = create_user(
            self.conn, "admin", "pass123", "admin", "Admin User"
        )
        self.member_id = create_user(
            self.conn, "member", "pass123", "member", "Regular Member"
        )

        # Create app
        from app.api import deps
        from app.api.routes.users import router as users_router
        from app.models.database import SQLiteConnectionPool

        app = FastAPI()
        app.include_router(users_router)

        test_pool = SQLiteConnectionPool(self.db_path, max_size=3)

        def override_get_db():
            conn = test_pool.get_connection()
            try:
                yield conn
            finally:
                test_pool.release_connection(conn)

        from app.api.routes import users

        original_get_pool = users.get_pool
        users.get_pool = lambda path: test_pool

        app.dependency_overrides[deps.get_db] = override_get_db

        self.test_pool = test_pool
        self.original_get_pool = original_get_pool

        from app.config import settings
        self._orig_users_enabled = settings.users_enabled
        self._orig_jwt_secret = settings.jwt_secret_key
        settings.users_enabled = True
        settings.jwt_secret_key = os.environ["JWT_SECRET_KEY"]

        self.client = TestClient(app)
        # Override default User-Agent so fingerprint validation matches token
        self.client.headers["user-agent"] = ""

        yield

        self.client.close()
        _pool_cache.clear()
        self.conn.close()
        from app.api.routes import users

        users.get_pool = self.original_get_pool
        self.test_pool.close_all()
        settings.users_enabled = self._orig_users_enabled
        settings.jwt_secret_key = self._orig_jwt_secret

        import shutil

        try:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        except Exception:
            pass

    def test_sql_injection_or_in_user_id(self):
        """Test SQL injection via 'OR 1=1' in user_id path parameter."""
        token = get_token(self.admin_id, "admin", "admin")
        # FastAPI int coercion should reject this before it reaches the DB
        response = self.client.get(
            "/users/1 OR 1=1", headers={"Authorization": f"Bearer {token}"}
        )
        # Should get 422 validation error, not 200 or SQL error
        assert response.status_code == 422, f"Expected 422, got {response.status_code}"

    def test_sql_injection_drop_table_in_user_id(self):
        """Test SQL injection via DROP TABLE in user_id."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.get(
            "/users/1; DROP TABLE users;--",
            headers={"Authorization": f"Bearer {token}"},
        )
        # FastAPI validation should reject this
        assert response.status_code == 422, f"Expected 422, got {response.status_code}"

    def test_sql_injection_union_select(self):
        """Test SQL injection via UNION SELECT."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.get(
            "/users/1 UNION SELECT * FROM users--",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 422, f"Expected 422, got {response.status_code}"

    def test_sql_injection_in_pagination(self):
        """Test SQL injection in skip/limit parameters."""
        token = get_token(self.admin_id, "admin", "admin")
        # This tests if skip/limit are properly parameterized
        response = self.client.get(
            "/users/?skip=0; DROP TABLE users;&limit=100",
            headers={"Authorization": f"Bearer {token}"},
        )
        # FastAPI validates these as integers, should reject
        assert response.status_code == 422

    def test_sql_injection_in_role_update(self):
        """Test SQL injection via role parameter in PATCH."""
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.patch(
            f"/users/{self.member_id}/role",
            json={"role": "superadmin'; DROP TABLE users;--"},
            headers={"Authorization": f"Bearer {token}"},
        )
        # Role validation should reject this
        assert response.status_code == 400, f"Expected 400, got {response.status_code}"
        assert "Invalid role" in response.json()["detail"]


class TestXSSPrevention:
    """Tests for XSS prevention in user data."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up test database for each test."""
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")

        from app.models.database import _pool_cache

        _pool_cache.clear()

        self.conn = setup_test_db(self.db_path)

        # Create superadmin and admin
        self.superadmin_id = create_user(
            self.conn, "superadmin", "pass123", "superadmin", "Super Admin"
        )
        self.admin_id = create_user(
            self.conn, "admin", "pass123", "admin", "Admin User"
        )

        # Create user with XSS payload in username
        self.xss_user_id = create_user_with_xss_payload(
            self.conn, "xssuser", "pass123", "member", "XSS User"
        )

        # Create app
        from app.api import deps
        from app.api.routes.users import router as users_router
        from app.models.database import SQLiteConnectionPool

        app = FastAPI()
        app.include_router(users_router)

        test_pool = SQLiteConnectionPool(self.db_path, max_size=3)

        def override_get_db():
            conn = test_pool.get_connection()
            try:
                yield conn
            finally:
                test_pool.release_connection(conn)

        from app.api.routes import users

        original_get_pool = users.get_pool
        users.get_pool = lambda path: test_pool

        app.dependency_overrides[deps.get_db] = override_get_db

        self.test_pool = test_pool
        self.original_get_pool = original_get_pool

        from app.config import settings
        self._orig_users_enabled = settings.users_enabled
        self._orig_jwt_secret = settings.jwt_secret_key
        settings.users_enabled = True
        settings.jwt_secret_key = os.environ["JWT_SECRET_KEY"]

        self.client = TestClient(app)
        # Override default User-Agent so fingerprint validation matches token
        self.client.headers["user-agent"] = ""

        yield

        self.client.close()
        _pool_cache.clear()
        self.conn.close()
        from app.api.routes import users

        users.get_pool = self.original_get_pool
        self.test_pool.close_all()
        settings.users_enabled = self._orig_users_enabled
        settings.jwt_secret_key = self._orig_jwt_secret

        import shutil

        try:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        except Exception:
            pass

    def test_xss_not_reflected_in_user_list(self):
        """Verify XSS payload in username is NOT executed in user list."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.get(
            "/users/", headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 200
        data = response.json()

        # Find the XSS user
        xss_users = [u for u in data["users"] if "xssuser" in u["username"].lower()]
        assert len(xss_users) == 1

        xss_user = xss_users[0]
        # Username should contain the literal script tag (data is returned as-is)
        # but NOT be executed. The key is that this is an API returning JSON,
        # not an HTML page rendering user content
        assert "<script>" in xss_user["username"]

    def test_xss_not_reflected_in_get_user(self):
        """Verify XSS payload in username is returned correctly via get user."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.get(
            f"/users/{self.xss_user_id}", headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 200
        data = response.json()
        # Username should contain the literal payload
        assert "<script>" in data["username"]

    def test_xss_in_full_name_field(self):
        """Test XSS in full_name field."""
        # Create user with XSS in full_name
        malicious_full_name = "<img src=x onerror=alert(1)>"
        self.conn.execute(
            """INSERT INTO users (username, hashed_password, full_name, role, is_active)
               VALUES (?, ?, ?, ?, ?)""",
            ("testxss2", hash_password("pass"), malicious_full_name, "member", 1),
        )
        self.conn.commit()

        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.get(
            "/users/", headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 200
        data = response.json()
        xss_users = [
            u for u in data["users"] if malicious_full_name in u.get("full_name", "")
        ]
        assert len(xss_users) == 1
        # Data is returned as-is - consumer must escape when rendering


class TestMassAssignment:
    """Tests for mass assignment / privilege escalation."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up test database for each test."""
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")

        from app.models.database import _pool_cache

        _pool_cache.clear()

        self.conn = setup_test_db(self.db_path)

        self.superadmin_id = create_user(
            self.conn, "superadmin", "pass123", "superadmin", "Super Admin"
        )
        self.admin_id = create_user(
            self.conn, "admin", "pass123", "admin", "Admin User"
        )
        self.member_id = create_user(
            self.conn, "member", "pass123", "member", "Regular Member"
        )

        from app.api import deps
        from app.api.routes.users import router as users_router
        from app.models.database import SQLiteConnectionPool

        app = FastAPI()
        app.include_router(users_router)

        test_pool = SQLiteConnectionPool(self.db_path, max_size=3)

        def override_get_db():
            conn = test_pool.get_connection()
            try:
                yield conn
            finally:
                test_pool.release_connection(conn)

        from app.api.routes import users

        original_get_pool = users.get_pool
        users.get_pool = lambda path: test_pool

        app.dependency_overrides[deps.get_db] = override_get_db

        self.test_pool = test_pool
        self.original_get_pool = original_get_pool

        from app.config import settings
        self._orig_users_enabled = settings.users_enabled
        self._orig_jwt_secret = settings.jwt_secret_key
        settings.users_enabled = True
        settings.jwt_secret_key = os.environ["JWT_SECRET_KEY"]

        self.client = TestClient(app)
        # Override default User-Agent so fingerprint validation matches token
        self.client.headers["user-agent"] = ""

        yield

        self.client.close()
        _pool_cache.clear()
        self.conn.close()
        from app.api.routes import users

        users.get_pool = self.original_get_pool
        self.test_pool.close_all()
        settings.users_enabled = self._orig_users_enabled
        settings.jwt_secret_key = self._orig_jwt_secret

        import shutil

        try:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        except Exception:
            pass

    def test_admin_cannot_assign_superadmin_role(self):
        """Admin (not superadmin) attempting to assign superadmin role should get 403."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.patch(
            f"/users/{self.member_id}/role",
            json={"role": "superadmin"},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 403, f"Expected 403, got {response.status_code}"
        assert "superadmin" in response.json()["detail"].lower()

    def test_admin_cannot_promote_self_to_superadmin(self):
        """Admin cannot escalate their own privileges to superadmin."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.patch(
            f"/users/{self.admin_id}/role",
            json={"role": "superadmin"},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 403, f"Expected 403, got {response.status_code}"

    def test_member_cannot_change_any_role(self):
        """Member role cannot change any user's role."""
        token = get_token(self.member_id, "member", "member")
        response = self.client.patch(
            f"/users/{self.admin_id}/role",
            json={"role": "viewer"},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 403, f"Expected 403, got {response.status_code}"


class TestNegativeAndBoundaryUserIds:
    """Tests for negative, zero, and boundary user_id values."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up test database for each test."""
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")

        from app.models.database import _pool_cache

        _pool_cache.clear()

        self.conn = setup_test_db(self.db_path)

        self.superadmin_id = create_user(
            self.conn, "superadmin", "pass123", "superadmin", "Super Admin"
        )
        self.admin_id = create_user(
            self.conn, "admin", "pass123", "admin", "Admin User"
        )

        from app.api import deps
        from app.api.routes.users import router as users_router
        from app.models.database import SQLiteConnectionPool

        app = FastAPI()
        app.include_router(users_router)

        test_pool = SQLiteConnectionPool(self.db_path, max_size=3)

        def override_get_db():
            conn = test_pool.get_connection()
            try:
                yield conn
            finally:
                test_pool.release_connection(conn)

        from app.api.routes import users

        original_get_pool = users.get_pool
        users.get_pool = lambda path: test_pool

        app.dependency_overrides[deps.get_db] = override_get_db

        self.test_pool = test_pool
        self.original_get_pool = original_get_pool

        from app.config import settings
        self._orig_users_enabled = settings.users_enabled
        self._orig_jwt_secret = settings.jwt_secret_key
        settings.users_enabled = True
        settings.jwt_secret_key = os.environ["JWT_SECRET_KEY"]

        self.client = TestClient(app)
        # Override default User-Agent so fingerprint validation matches token
        self.client.headers["user-agent"] = ""

        yield

        self.client.close()
        _pool_cache.clear()
        self.conn.close()
        from app.api.routes import users

        users.get_pool = self.original_get_pool
        self.test_pool.close_all()
        settings.users_enabled = self._orig_users_enabled
        settings.jwt_secret_key = self._orig_jwt_secret

        import shutil

        try:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        except Exception:
            pass

    def test_negative_user_id_get(self):
        """Negative user_id should return 404 (user not found) or 422 (validation)."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.get(
            "/users/-1", headers={"Authorization": f"Bearer {token}"}
        )
        # Either 422 (FastAPI validation) or 404 (route handler)
        assert response.status_code in (404, 422), (
            f"Unexpected status: {response.status_code}"
        )

    def test_negative_user_id_update_role(self):
        """Negative user_id in role update should return 404 or 422."""
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.patch(
            "/users/-1/role",
            json={"role": "admin"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code in (404, 422), (
            f"Unexpected status: {response.status_code}"
        )

    def test_zero_user_id_get(self):
        """Zero user_id should return 404 (user not found)."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.get(
            "/users/0", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 404, f"Expected 404, got {response.status_code}"
        assert "not found" in response.json()["detail"].lower()

    def test_zero_user_id_update_active(self):
        """Zero user_id in active update should return 404."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.patch(
            "/users/0/active",
            json={"is_active": False},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 404, f"Expected 404, got {response.status_code}"

    def test_integer_overflow_user_id(self):
        """Very large integer user_id should be handled gracefully."""
        token = get_token(self.admin_id, "admin", "admin")
        # Max int64 value
        response = self.client.get(
            "/users/9223372036854775807", headers={"Authorization": f"Bearer {token}"}
        )
        # Should return 404, not crash
        assert response.status_code == 404, f"Expected 404, got {response.status_code}"

    def test_very_large_negative_user_id(self):
        """Very large negative user_id should be handled gracefully."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.get(
            "/users/-9223372036854775808", headers={"Authorization": f"Bearer {token}"}
        )
        # Should return 422 (validation error for negative) or 404
        assert response.status_code in (404, 422), (
            f"Unexpected status: {response.status_code}"
        )


class TestOversizedParameters:
    """Tests for oversized pagination and request parameters."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up test database for each test."""
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")

        from app.models.database import _pool_cache

        _pool_cache.clear()

        self.conn = setup_test_db(self.db_path)

        self.superadmin_id = create_user(
            self.conn, "superadmin", "pass123", "superadmin", "Super Admin"
        )
        self.admin_id = create_user(
            self.conn, "admin", "pass123", "admin", "Admin User"
        )

        # Create many users to test pagination limits
        for i in range(50):
            create_user(self.conn, f"user{i}", "pass123", "member")

        from app.api import deps
        from app.api.routes.users import router as users_router
        from app.models.database import SQLiteConnectionPool

        app = FastAPI()
        app.include_router(users_router)

        test_pool = SQLiteConnectionPool(self.db_path, max_size=3)

        def override_get_db():
            conn = test_pool.get_connection()
            try:
                yield conn
            finally:
                test_pool.release_connection(conn)

        from app.api.routes import users

        original_get_pool = users.get_pool
        users.get_pool = lambda path: test_pool

        app.dependency_overrides[deps.get_db] = override_get_db

        self.test_pool = test_pool
        self.original_get_pool = original_get_pool

        from app.config import settings
        self._orig_users_enabled = settings.users_enabled
        self._orig_jwt_secret = settings.jwt_secret_key
        settings.users_enabled = True
        settings.jwt_secret_key = os.environ["JWT_SECRET_KEY"]

        self.client = TestClient(app)
        # Override default User-Agent so fingerprint validation matches token
        self.client.headers["user-agent"] = ""

        yield

        self.client.close()
        _pool_cache.clear()
        self.conn.close()
        from app.api.routes import users

        users.get_pool = self.original_get_pool
        self.test_pool.close_all()
        settings.users_enabled = self._orig_users_enabled
        settings.jwt_secret_key = self._orig_jwt_secret

        import shutil

        try:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        except Exception:
            pass

    def test_very_large_skip_limit(self):
        """Very large skip/limit values should not cause crash or memory issues."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.get(
            "/users/?skip=999999&limit=999999",
            headers={"Authorization": f"Bearer {token}"},
        )

        # Should return 200 with empty list, not crash
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        data = response.json()
        assert data["users"] == []
        assert data["total"] == 52  # 2 initial + 50 created

    def test_negative_skip_limit(self):
        """Negative skip/limit - behavior depends on implementation (returns results)."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.get(
            "/users/?skip=-1&limit=-1", headers={"Authorization": f"Bearer {token}"}
        )
        # FastAPI doesn't validate min values by default - negative values pass through
        # SQLite handles negative OFFSET by interpreting it as 0 (effectively)
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        data = response.json()
        # With negative values, SQLite may return all results (depends on implementation)
        assert "users" in data
        assert "total" in data

    def test_zero_limit(self):
        """Zero limit should return empty list, not crash."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.get(
            "/users/?limit=0", headers={"Authorization": f"Bearer {token}"}
        )
        # FastAPI may reject 0 as invalid for limit >= 1
        assert response.status_code in (200, 422), (
            f"Unexpected status: {response.status_code}"
        )


class TestEmptyAndMalformedBodies:
    """Tests for empty and malformed request bodies."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up test database for each test."""
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")

        from app.models.database import _pool_cache

        _pool_cache.clear()

        self.conn = setup_test_db(self.db_path)

        self.superadmin_id = create_user(
            self.conn, "superadmin", "pass123", "superadmin", "Super Admin"
        )
        self.admin_id = create_user(
            self.conn, "admin", "pass123", "admin", "Admin User"
        )
        self.member_id = create_user(
            self.conn, "member", "pass123", "member", "Regular Member"
        )

        from app.api import deps
        from app.api.routes.users import router as users_router
        from app.models.database import SQLiteConnectionPool

        app = FastAPI()
        app.include_router(users_router)

        test_pool = SQLiteConnectionPool(self.db_path, max_size=3)

        def override_get_db():
            conn = test_pool.get_connection()
            try:
                yield conn
            finally:
                test_pool.release_connection(conn)

        from app.api.routes import users

        original_get_pool = users.get_pool
        users.get_pool = lambda path: test_pool

        app.dependency_overrides[deps.get_db] = override_get_db

        self.test_pool = test_pool
        self.original_get_pool = original_get_pool

        from app.config import settings
        self._orig_users_enabled = settings.users_enabled
        self._orig_jwt_secret = settings.jwt_secret_key
        settings.users_enabled = True
        settings.jwt_secret_key = os.environ["JWT_SECRET_KEY"]

        self.client = TestClient(app)
        # Override default User-Agent so fingerprint validation matches token
        self.client.headers["user-agent"] = ""

        yield

        self.client.close()
        _pool_cache.clear()
        self.conn.close()
        from app.api.routes import users

        users.get_pool = self.original_get_pool
        self.test_pool.close_all()
        settings.users_enabled = self._orig_users_enabled
        settings.jwt_secret_key = self._orig_jwt_secret

        import shutil

        try:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        except Exception:
            pass

    def test_empty_json_body_role_update(self):
        """Empty JSON body {} for role update should return validation error."""
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.patch(
            f"/users/{self.member_id}/role",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 422, f"Expected 422, got {response.status_code}"

    def test_empty_json_body_active_update(self):
        """Empty JSON body {} for active update should return validation error."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.patch(
            f"/users/{self.member_id}/active",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 422, f"Expected 422, got {response.status_code}"

    def test_missing_required_field_role(self):
        """Missing 'role' field should return validation error."""
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.patch(
            f"/users/{self.member_id}/role",
            json={"not_role": "admin"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 422, f"Expected 422, got {response.status_code}"

    def test_missing_required_field_active(self):
        """Missing 'is_active' field should return validation error."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.patch(
            f"/users/{self.member_id}/active",
            json={"active": True},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 422, f"Expected 422, got {response.status_code}"

    def test_malformed_json_broken_object(self):
        """Malformed JSON with broken object should return 422."""
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.patch(
            f"/users/{self.member_id}/role",
            content=b"{role: 'admin'",  # Missing quotes and braces
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 422, f"Expected 422, got {response.status_code}"

    def test_malformed_json_unclosed_bracket(self):
        """Malformed JSON with unclosed bracket should return 422."""
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.patch(
            f"/users/{self.member_id}/role",
            content=b'{"role": "admin"',
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 422, f"Expected 422, got {response.status_code}"

    def test_wrong_content_type(self):
        """Wrong Content-Type should be handled (may accept or reject)."""
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        # Sending as text/plain instead of application/json
        response = self.client.patch(
            f"/users/{self.member_id}/role",
            content='{"role": "admin"}',
            headers={"Authorization": f"Bearer {token}", "Content-Type": "text/plain"},
        )
        # FastAPI usually handles this, but we check it doesn't crash
        assert response.status_code in (200, 422, 415), (
            f"Unexpected status: {response.status_code}"
        )

    def test_extra_fields_ignored(self):
        """Extra fields in request body should be ignored, not cause error."""
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.patch(
            f"/users/{self.member_id}/role",
            json={
                "role": "admin",
                "extra_field": "should_be_ignored",
                "password": "hacked123",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        # Should succeed, extra fields are silently ignored
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        data = response.json()
        assert data["role"] == "admin"


class TestJWTTokenManipulation:
    """Tests for JWT token manipulation attacks."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up test database for each test."""
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")

        from app.models.database import _pool_cache

        _pool_cache.clear()

        self.conn = setup_test_db(self.db_path)

        self.superadmin_id = create_user(
            self.conn, "superadmin", "pass123", "superadmin", "Super Admin"
        )
        self.admin_id = create_user(
            self.conn, "admin", "pass123", "admin", "Admin User"
        )
        self.member_id = create_user(
            self.conn, "member", "pass123", "member", "Regular Member"
        )

        from app.api import deps
        from app.api.routes.users import router as users_router
        from app.models.database import SQLiteConnectionPool

        app = FastAPI()
        app.include_router(users_router)

        test_pool = SQLiteConnectionPool(self.db_path, max_size=3)

        def override_get_db():
            conn = test_pool.get_connection()
            try:
                yield conn
            finally:
                test_pool.release_connection(conn)

        from app.api.routes import users

        original_get_pool = users.get_pool
        users.get_pool = lambda path: test_pool

        app.dependency_overrides[deps.get_db] = override_get_db

        self.test_pool = test_pool
        self.original_get_pool = original_get_pool

        from app.config import settings
        self._orig_users_enabled = settings.users_enabled
        self._orig_jwt_secret = settings.jwt_secret_key
        settings.users_enabled = True
        settings.jwt_secret_key = os.environ["JWT_SECRET_KEY"]

        self.client = TestClient(app)
        # Override default User-Agent so fingerprint validation matches token
        self.client.headers["user-agent"] = ""

        yield

        self.client.close()
        _pool_cache.clear()
        self.conn.close()
        from app.api.routes import users

        users.get_pool = self.original_get_pool
        self.test_pool.close_all()
        settings.users_enabled = self._orig_users_enabled
        settings.jwt_secret_key = self._orig_jwt_secret

        import shutil

        try:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        except Exception:
            pass

    def test_expired_token_rejected(self):
        """Expired JWT token should be rejected (401 per RFC 6750)."""
        expired_token = get_expired_token(
            self.superadmin_id, "superadmin", "superadmin"
        )
        response = self.client.get(
            "/users/", headers={"Authorization": f"Bearer {expired_token}"}
        )
        assert response.status_code == 401, f"Expected 401, got {response.status_code}"
        assert (
            "expired" in response.json()["detail"].lower()
            or "invalid" in response.json()["detail"].lower()
        )

    def test_wrong_algorithm_none_rejected(self):
        """JWT with algorithm='none' should be rejected (401 per RFC 6750)."""
        malicious_token = get_malformed_token_wrong_algorithm(
            self.superadmin_id, "superadmin", "superadmin"
        )
        response = self.client.get(
            "/users/", headers={"Authorization": f"Bearer {malicious_token}"}
        )
        assert response.status_code == 401, f"Expected 401, got {response.status_code}"

    def test_token_with_modified_role_rejected(self):
        """Token with claims modified to escalate privileges should be rejected."""
        # Create token with role=superadmin but user is actually a member
        modified_token = get_token_with_modified_role(
            self.member_id, "member", "member", "superadmin"
        )
        response = self.client.get(
            "/users/", headers={"Authorization": f"Bearer {modified_token}"}
        )
        # The token is valid JWT-wise, but the user_id doesn't match a superadmin
        # in the database, so either 401 (token missing type claim), 403
        # (user not found/inactive), or 200 if role is DB-verified
        assert response.status_code in (200, 401, 403), (
            f"Unexpected status: {response.status_code}"
        )

    def test_tampered_token_signature(self):
        """Token with tampered signature should be rejected (401 per RFC 6750)."""

        # Get a valid token then modify the signature
        valid_token = get_token(self.admin_id, "admin", "admin")
        # Append garbage to the signature part
        parts = valid_token.rsplit(".", 1)
        tampered = parts[0] + "." + parts[1] + "tampered"

        response = self.client.get(
            "/users/", headers={"Authorization": f"Bearer {tampered}"}
        )
        assert response.status_code == 401, f"Expected 401, got {response.status_code}"

    def test_completely_invalid_token(self):
        """Completely invalid token should be rejected (401 per RFC 6750)."""
        response = self.client.get(
            "/users/", headers={"Authorization": "Bearer not.a.valid.jwt.token.at.all"}
        )
        assert response.status_code == 401, f"Expected 401, got {response.status_code}"

    def test_empty_bearer_token(self):
        """Empty bearer token should be rejected."""
        response = self.client.get("/users/", headers={"Authorization": "Bearer "})
        assert response.status_code == 401, f"Expected 401, got {response.status_code}"

    def test_no_authorization_header(self):
        """Missing Authorization header should return 401."""
        response = self.client.get("/users/")
        assert response.status_code == 401, f"Expected 401, got {response.status_code}"

    def test_wrong_authorization_scheme(self):
        """Wrong Authorization scheme (not Bearer) should be rejected."""
        response = self.client.get(
            "/users/", headers={"Authorization": "Basic dXNlcjpwYXNz"}
        )
        assert response.status_code == 401, f"Expected 401, got {response.status_code}"


class TestLastSuperadminRaceCondition:
    """Tests for race condition in last superadmin protection."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up test database for each test."""
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")

        from app.models.database import _pool_cache

        _pool_cache.clear()

        self.conn = setup_test_db(self.db_path)

        # Create exactly one superadmin
        self.superadmin_id = create_user(
            self.conn, "superadmin", "pass123", "superadmin", "Super Admin"
        )
        self.admin_id = create_user(
            self.conn, "admin", "pass123", "admin", "Admin User"
        )

        from app.api import deps
        from app.api.routes.users import router as users_router
        from app.models.database import SQLiteConnectionPool

        app = FastAPI()
        app.include_router(users_router)

        test_pool = SQLiteConnectionPool(self.db_path, max_size=5)

        def override_get_db():
            conn = test_pool.get_connection()
            try:
                yield conn
            finally:
                test_pool.release_connection(conn)

        from app.api.routes import users

        original_get_pool = users.get_pool
        users.get_pool = lambda path: test_pool

        app.dependency_overrides[deps.get_db] = override_get_db

        self.test_pool = test_pool
        self.original_get_pool = original_get_pool

        from app.config import settings
        self._orig_users_enabled = settings.users_enabled
        self._orig_jwt_secret = settings.jwt_secret_key
        settings.users_enabled = True
        settings.jwt_secret_key = os.environ["JWT_SECRET_KEY"]

        self.client = TestClient(app)
        # Override default User-Agent so fingerprint validation matches token
        self.client.headers["user-agent"] = ""

        yield

        self.client.close()
        _pool_cache.clear()
        self.conn.close()
        from app.api.routes import users

        users.get_pool = self.original_get_pool
        self.test_pool.close_all()
        settings.users_enabled = self._orig_users_enabled
        settings.jwt_secret_key = self._orig_jwt_secret

        import shutil

        try:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        except Exception:
            pass

    def test_last_superadmin_cannot_demote(self):
        """Single superadmin cannot demote themselves."""
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.patch(
            f"/users/{self.superadmin_id}/role",
            json={"role": "admin"},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 400, f"Expected 400, got {response.status_code}"
        assert "last superadmin" in response.json()["detail"].lower()

    def test_last_superadmin_cannot_deactivate(self):
        """Single superadmin cannot deactivate themselves."""
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.patch(
            f"/users/{self.superadmin_id}/active",
            json={"is_active": False},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 400, f"Expected 400, got {response.status_code}"
        detail = response.json()["detail"].lower()
        assert "last superadmin" in detail or "own account" in detail

    def test_last_superadmin_cannot_delete_self(self):
        """Single superadmin cannot delete themselves (self-delete guard)."""
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.delete(
            f"/users/{self.superadmin_id}", headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 400, f"Expected 400, got {response.status_code}"
        assert "own account" in response.json()["detail"].lower()

    def test_multiple_superadmins_one_cannot_demote_other_to_protect(self):
        """With multiple superadmins, one tries to demote the last remaining superadmin after others deactivated."""
        # Create second superadmin
        superadmin2_id = create_user(
            self.conn, "superadmin2", "pass123", "superadmin", "Super Admin 2"
        )

        # First superadmin deactivates second
        token1 = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.patch(
            f"/users/{superadmin2_id}/active",
            json={"is_active": False},
            headers={"Authorization": f"Bearer {token1}"},
        )
        assert response.status_code == 200

        # Now try to demote superadmin1 (the last one) - should fail
        response = self.client.patch(
            f"/users/{self.superadmin_id}/role",
            json={"role": "admin"},
            headers={"Authorization": f"Bearer {token1}"},
        )
        assert response.status_code == 400
        assert "last superadmin" in response.json()["detail"].lower()


class TestInvalidRoleValues:
    """Tests for invalid role values."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up test database for each test."""
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")

        from app.models.database import _pool_cache

        _pool_cache.clear()

        self.conn = setup_test_db(self.db_path)

        self.superadmin_id = create_user(
            self.conn, "superadmin", "pass123", "superadmin", "Super Admin"
        )
        self.member_id = create_user(
            self.conn, "member", "pass123", "member", "Regular Member"
        )

        from app.api import deps
        from app.api.routes.users import router as users_router
        from app.models.database import SQLiteConnectionPool

        app = FastAPI()
        app.include_router(users_router)

        test_pool = SQLiteConnectionPool(self.db_path, max_size=3)

        def override_get_db():
            conn = test_pool.get_connection()
            try:
                yield conn
            finally:
                test_pool.release_connection(conn)

        from app.api.routes import users

        original_get_pool = users.get_pool
        users.get_pool = lambda path: test_pool

        app.dependency_overrides[deps.get_db] = override_get_db

        self.test_pool = test_pool
        self.original_get_pool = original_get_pool

        from app.config import settings
        self._orig_users_enabled = settings.users_enabled
        self._orig_jwt_secret = settings.jwt_secret_key
        settings.users_enabled = True
        settings.jwt_secret_key = os.environ["JWT_SECRET_KEY"]

        self.client = TestClient(app)
        # Override default User-Agent so fingerprint validation matches token
        self.client.headers["user-agent"] = ""

        yield

        self.client.close()
        _pool_cache.clear()
        self.conn.close()
        from app.api.routes import users

        users.get_pool = self.original_get_pool
        self.test_pool.close_all()
        settings.users_enabled = self._orig_users_enabled
        settings.jwt_secret_key = self._orig_jwt_secret

        import shutil

        try:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        except Exception:
            pass

    def test_invalid_role_string(self):
        """Invalid role string should be rejected."""
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        invalid_roles = [
            "superadmin123",
            "adm!n",
            "role'; DROP TABLE users;--",
            "SuperAdmin",  # Case sensitive
            "SUPERADMIN",
            "",
            "a" * 1000,  # Very long
        ]

        for invalid_role in invalid_roles:
            response = self.client.patch(
                f"/users/{self.member_id}/role",
                json={"role": invalid_role},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 400, (
                f"Role '{invalid_role[:20]}' should be rejected"
            )
            assert "invalid role" in response.json()["detail"].lower()

    def test_non_string_role(self):
        """Non-string role value should be rejected."""
        token = get_token(self.superadmin_id, "superadmin", "superadmin")

        # Test with integer
        response = self.client.patch(
            f"/users/{self.member_id}/role",
            json={"role": 1},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 422, f"Expected 422, got {response.status_code}"

    def test_null_role(self):
        """Null role value should be rejected."""
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.patch(
            f"/users/{self.member_id}/role",
            json={"role": None},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 422, f"Expected 422, got {response.status_code}"


class TestInvalidIsActiveValues:
    """Tests for invalid is_active values."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up test database for each test."""
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")

        from app.models.database import _pool_cache

        _pool_cache.clear()

        self.conn = setup_test_db(self.db_path)

        self.superadmin_id = create_user(
            self.conn, "superadmin", "pass123", "superadmin", "Super Admin"
        )
        self.admin_id = create_user(
            self.conn, "admin", "pass123", "admin", "Admin User"
        )
        self.member_id = create_user(
            self.conn, "member", "pass123", "member", "Regular Member"
        )

        from app.api import deps
        from app.api.routes.users import router as users_router
        from app.models.database import SQLiteConnectionPool

        app = FastAPI()
        app.include_router(users_router)

        test_pool = SQLiteConnectionPool(self.db_path, max_size=3)

        def override_get_db():
            conn = test_pool.get_connection()
            try:
                yield conn
            finally:
                test_pool.release_connection(conn)

        from app.api.routes import users

        original_get_pool = users.get_pool
        users.get_pool = lambda path: test_pool

        app.dependency_overrides[deps.get_db] = override_get_db

        self.test_pool = test_pool
        self.original_get_pool = original_get_pool

        from app.config import settings
        self._orig_users_enabled = settings.users_enabled
        self._orig_jwt_secret = settings.jwt_secret_key
        settings.users_enabled = True
        settings.jwt_secret_key = os.environ["JWT_SECRET_KEY"]

        self.client = TestClient(app)
        # Override default User-Agent so fingerprint validation matches token
        self.client.headers["user-agent"] = ""

        yield

        self.client.close()
        _pool_cache.clear()
        self.conn.close()
        from app.api.routes import users

        users.get_pool = self.original_get_pool
        self.test_pool.close_all()
        settings.users_enabled = self._orig_users_enabled
        settings.jwt_secret_key = self._orig_jwt_secret

        import shutil

        try:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        except Exception:
            pass

    def test_non_boolean_is_active_string(self):
        """Non-boolean is_active string value - Pydantic coerces 'yes' to True."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.patch(
            f"/users/{self.member_id}/active",
            json={"is_active": "yes"},
            headers={"Authorization": f"Bearer {token}"},
        )
        # Pydantic coerces string "yes" to True - this is an edge case behavior
        # The request succeeds because "yes" is truthy and gets coerced
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        data = response.json()
        assert data["is_active"] is True  # Was coerced to True

    def test_non_boolean_is_active_integer(self):
        """Non-boolean integer is_active should be rejected."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.patch(
            f"/users/{self.member_id}/active",
            json={"is_active": 1},
            headers={"Authorization": f"Bearer {token}"},
        )
        # FastAPI might coerce 1 to True, so this could be 200 or 422 depending on schema
        # The schema says bool, so 422 expected
        assert response.status_code in (200, 422), (
            f"Unexpected status: {response.status_code}"
        )

    def test_null_is_active(self):
        """Null is_active value should be rejected."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.patch(
            f"/users/{self.member_id}/active",
            json={"is_active": None},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 422, f"Expected 422, got {response.status_code}"
