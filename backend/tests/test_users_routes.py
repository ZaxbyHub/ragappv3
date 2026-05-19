"""
Tests for user management admin routes (backend/app/api/routes/users.py).

These tests verify:
- list_users: admin can list, pagination works, non-admin rejected
- get_user: admin can get by id, not found returns 404
- update_user_role: superadmin can change role, invalid role rejected, last superadmin guard
- update_user_active: admin can deactivate, last superadmin guard
- delete_user: superadmin can delete, self-delete rejected, last superadmin guard
- unauthenticated access rejected
"""

import os
import sqlite3
import tempfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Set up test environment BEFORE importing app modules
os.environ["JWT_SECRET_KEY"] = (
    "test-jwt-secret-key-for-testing-only-12345678901234567890"
)
os.environ["USERS_ENABLED"] = "true"

# Now safe to import app modules
from app.services.auth_service import create_access_token, hash_password


def setup_test_db(db_path: str) -> sqlite3.Connection:
    """Set up test database with schema and initial users."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")

    # Create users table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            hashed_password TEXT NOT NULL,
            full_name TEXT DEFAULT '',
            role TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('superadmin','admin','member','viewer')),
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_login_at TIMESTAMP,
            must_change_password INTEGER NOT NULL DEFAULT 0
        )
    """)

    # Create indexes
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_role ON users(role)")

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


def get_token(user_id: int, username: str, role: str) -> str:
    """Generate a JWT token for a test user."""
    return create_access_token(user_id, username, role)


class TestUserRoutes:
    """Test class for user management routes with shared setup."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up test database for each test."""
        # Create temp directory and database
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")

        # Clear the global pool cache to ensure test isolation
        from app.models.database import _pool_cache

        _pool_cache.clear()

        # Set up test database
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
        self.viewer_id = create_user(
            self.conn, "viewer", "pass123", "viewer", "Viewer User"
        )

        # Create app with users router
        from app.api.routes.users import router as users_router

        app = FastAPI()
        app.include_router(users_router)

        # Override the get_db dependency to use our test database
        from app.api import deps
        from app.models.database import SQLiteConnectionPool

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

        # Store for cleanup
        self.test_pool = test_pool
        self.original_get_pool = original_get_pool

        self.client = TestClient(app)

        yield

        # Cleanup
        self.client.close()
        _pool_cache.clear()
        self.conn.close()

        # Restore original get_pool
        from app.api.routes import users

        users.get_pool = self.original_get_pool
        self.test_pool.close_all()

        # Clean up temp directory
        import shutil

        try:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        except Exception:
            pass


class TestListUsers(TestUserRoutes):
    """Tests for GET /users/ endpoint."""

    def test_list_users_admin_can_list(self):
        """Admin can list all users and see correct fields (no password hash)."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.get(
            "/users/", headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 200
        data = response.json()
        assert "users" in data
        assert "total" in data
        assert data["total"] == 4  # superadmin, admin, member, viewer

        users = data["users"]
        assert len(users) == 4

        # Verify user fields (no password hash)
        for user in users:
            assert "id" in user
            assert "username" in user
            assert "full_name" in user
            assert "role" in user
            assert "is_active" in user
            assert "created_at" in user
            assert "hashed_password" not in user

    def test_list_users_pagination(self):
        """Pagination works correctly: skip=2, limit=2 returns correct users."""
        token = get_token(self.admin_id, "admin", "admin")

        # First page
        response = self.client.get(
            "/users/?skip=0&limit=2", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["users"]) == 2
        assert data["total"] == 4

        # Second page
        response = self.client.get(
            "/users/?skip=2&limit=2", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["users"]) == 2
        assert data["total"] == 4

    def test_list_users_non_admin_rejected(self):
        """Member role is rejected with 403."""
        token = get_token(self.member_id, "member", "member")
        response = self.client.get(
            "/users/", headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 403

    def test_list_users_viewer_rejected(self):
        """Viewer role is rejected with 403."""
        token = get_token(self.viewer_id, "viewer", "viewer")
        response = self.client.get(
            "/users/", headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 403

    def test_list_users_superadmin_allowed(self):
        """Superadmin can list users (higher role than required)."""
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.get(
            "/users/", headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 4


class TestGetUser(TestUserRoutes):
    """Tests for GET /users/{user_id} endpoint."""

    def test_get_user_admin_can_get_by_id(self):
        """Admin can get user details by ID."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.get(
            f"/users/{self.member_id}", headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == self.member_id
        assert data["username"] == "member"
        assert data["role"] == "member"
        assert data["is_active"] is True
        assert "hashed_password" not in data

    def test_get_user_not_found(self):
        """GET non-existent user returns 404."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.get(
            "/users/999", headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 404
        assert "User not found" in response.json()["detail"]

    def test_get_user_unauthenticated_rejected(self):
        """No token returns 401."""
        response = self.client.get(f"/users/{self.member_id}")
        assert response.status_code == 401


class TestUpdateUserRole(TestUserRoutes):
    """Tests for PATCH /users/{user_id}/role endpoint."""

    def test_update_user_role_superadmin_changes_role(self):
        """Superadmin can change member's role to viewer."""
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.patch(
            f"/users/{self.member_id}/role",
            json={"role": "viewer"},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["role"] == "viewer"
        assert data["user_id"] == self.member_id
        assert "message" in data

        # Verify DB was updated
        cursor = self.conn.execute(
            "SELECT role FROM users WHERE id = ?", (self.member_id,)
        )
        assert cursor.fetchone()[0] == "viewer"

    def test_update_user_role_invalid_role_rejected(self):
        """Invalid role value returns 400."""
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.patch(
            f"/users/{self.member_id}/role",
            json={"role": "hacker"},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 400
        assert "Invalid role" in response.json()["detail"]

    def test_update_user_role_admin_cannot_change_role(self):
        """Admin role is rejected when changing roles (requires superadmin)."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.patch(
            f"/users/{self.member_id}/role",
            json={"role": "viewer"},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 403

    def test_update_user_role_last_superadmin_guard(self):
        """Cannot demote the last superadmin."""
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.patch(
            f"/users/{self.superadmin_id}/role",
            json={"role": "admin"},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 400
        assert "last superadmin" in response.json()["detail"]

    def test_update_user_role_not_found(self):
        """Updating non-existent user returns 404."""
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.patch(
            "/users/999/role",
            json={"role": "viewer"},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 404


class TestUpdateUserActive(TestUserRoutes):
    """Tests for PATCH /users/{user_id}/active endpoint."""

    def test_update_user_active_deactivate_member(self):
        """Admin can deactivate a member."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.patch(
            f"/users/{self.member_id}/active",
            json={"is_active": False},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["is_active"] is False
        assert "message" in data

        # Verify DB was updated
        cursor = self.conn.execute(
            "SELECT is_active FROM users WHERE id = ?", (self.member_id,)
        )
        assert cursor.fetchone()[0] == 0

    def test_update_user_active_activate_member(self):
        """Admin can reactivate a deactivated member."""
        # First deactivate
        self.conn.execute(
            "UPDATE users SET is_active = 0 WHERE id = ?", (self.member_id,)
        )
        self.conn.commit()

        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.patch(
            f"/users/{self.member_id}/active",
            json={"is_active": True},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["is_active"] is True

    def test_update_user_active_last_superadmin_guard(self):
        """Cannot deactivate the last superadmin (self-deactivation caught first)."""
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.patch(
            f"/users/{self.superadmin_id}/active",
            json={"is_active": False},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 400
        # Self-deactivation guard fires before last-superadmin guard
        assert "Cannot deactivate your own account" in response.json()["detail"]

    def test_update_user_active_admin_cannot_deactivate(self):
        """Admin cannot be deactivated by regular admin (only superadmin)."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.patch(
            f"/users/{self.superadmin_id}/active",
            json={"is_active": False},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 400
        assert "last superadmin" in response.json()["detail"]

    def test_update_user_active_not_found(self):
        """Updating non-existent user returns 404."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.patch(
            "/users/999/active",
            json={"is_active": False},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 404


class TestDeleteUser(TestUserRoutes):
    """Tests for DELETE /users/{user_id} endpoint."""

    def test_delete_user_superadmin_deletes_member(self):
        """Superadmin can delete a member."""
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.delete(
            f"/users/{self.member_id}", headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["user_id"] == self.member_id
        assert "message" in data

        # Verify user was deleted
        cursor = self.conn.execute(
            "SELECT id FROM users WHERE id = ?", (self.member_id,)
        )
        assert cursor.fetchone() is None

    def test_delete_user_self_delete_rejected(self):
        """Cannot delete your own account."""
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.delete(
            f"/users/{self.superadmin_id}", headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 400
        assert "own account" in response.json()["detail"]

    def test_delete_user_admin_cannot_delete(self):
        """Admin role cannot delete users (requires superadmin)."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.delete(
            f"/users/{self.member_id}", headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 403

    def test_delete_user_last_superadmin_guard(self):
        """Cannot delete the last superadmin (self-delete takes precedence)."""
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.delete(
            f"/users/{self.superadmin_id}", headers={"Authorization": f"Bearer {token}"}
        )

        # Self-deletion check happens before last-superadmin check
        assert response.status_code == 400
        assert "own account" in response.json()["detail"]

    def test_delete_user_not_found(self):
        """Deleting non-existent user returns 404."""
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.delete(
            "/users/999", headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 404


class TestUnauthenticatedAccess(TestUserRoutes):
    """Tests for unauthenticated access rejection."""

    def test_list_users_no_token(self):
        """GET /users/ without token returns 401."""
        response = self.client.get("/users/")
        assert response.status_code == 401

    def test_list_users_invalid_token(self):
        """GET /users/ with invalid token returns 401."""
        response = self.client.get(
            "/users/", headers={"Authorization": "Bearer invalid_token"}
        )
        assert response.status_code == 401

    def test_get_user_no_token(self):
        """GET /users/{id} without token returns 401."""
        response = self.client.get(f"/users/{self.member_id}")
        assert response.status_code == 401

    def test_update_role_no_token(self):
        """PATCH /users/{id}/role without token returns 401."""
        response = self.client.patch(
            f"/users/{self.member_id}/role", json={"role": "viewer"}
        )
        assert response.status_code == 401

    def test_update_active_no_token(self):
        """PATCH /users/{id}/active without token returns 401."""
        response = self.client.patch(
            f"/users/{self.member_id}/active", json={"is_active": False}
        )
        assert response.status_code == 401

    def test_delete_user_no_token(self):
        """DELETE /users/{id} without token returns 401."""
        response = self.client.delete(f"/users/{self.member_id}")
        assert response.status_code == 401


class TestLastSuperadminEdgeCases(TestUserRoutes):
    """Edge case tests for last superadmin protection."""

    def test_multiple_superadmins_can_change_roles(self):
        """With multiple superadmins, one can demote another."""
        # Create a second superadmin
        superadmin2_id = create_user(
            self.conn, "superadmin2", "pass123", "superadmin", "Super Admin 2"
        )

        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.patch(
            f"/users/{superadmin2_id}/role",
            json={"role": "admin"},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["role"] == "admin"

        # Verify DB was updated
        cursor = self.conn.execute(
            "SELECT role FROM users WHERE id = ?", (superadmin2_id,)
        )
        assert cursor.fetchone()[0] == "admin"

    def test_multiple_superadmins_can_deactivate(self):
        """With multiple superadmins, one can deactivate another."""
        # Create a second superadmin
        superadmin2_id = create_user(
            self.conn, "superadmin2", "pass123", "superadmin", "Super Admin 2"
        )

        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.patch(
            f"/users/{superadmin2_id}/active",
            json={"is_active": False},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["is_active"] is False

        # Verify DB was updated
        cursor = self.conn.execute(
            "SELECT is_active FROM users WHERE id = ?", (superadmin2_id,)
        )
        assert cursor.fetchone()[0] == 0

    def test_multiple_superadmins_can_delete(self):
        """With multiple superadmins, one can delete another."""
        # Create a second superadmin
        superadmin2_id = create_user(
            self.conn, "superadmin2", "pass123", "superadmin", "Super Admin 2"
        )

        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.delete(
            f"/users/{superadmin2_id}", headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 200

        # Verify user was deleted
        cursor = self.conn.execute(
            "SELECT id FROM users WHERE id = ?", (superadmin2_id,)
        )
        assert cursor.fetchone() is None


class TestRoleHierarchy(TestUserRoutes):
    """Tests to verify role hierarchy enforcement."""

    def test_superadmin_can_access_admin_routes(self):
        """Superadmin can access routes requiring admin role."""
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.get(
            "/users/", headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 200

    def test_member_cannot_access_admin_routes(self):
        """Member cannot access routes requiring admin role."""
        token = get_token(self.member_id, "member", "member")
        response = self.client.get(
            "/users/", headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 403

    def test_admin_cannot_access_superadmin_routes(self):
        """Admin cannot access routes requiring superadmin role."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.patch(
            f"/users/{self.member_id}/role",
            json={"role": "viewer"},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 403


class TestUpdateUserActiveSelfDeactivation(TestUserRoutes):
    """Tests for self-deactivation guard in PATCH /users/{user_id}/active."""

    def test_update_user_active_self_deactivate_returns_400(self):
        """Admin cannot deactivate their own account - returns 400."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.patch(
            f"/users/{self.admin_id}/active",
            json={"is_active": False},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 400
        assert "own account" in response.json()["detail"]

        # Verify DB was NOT updated
        cursor = self.conn.execute(
            "SELECT is_active FROM users WHERE id = ?", (self.admin_id,)
        )
        assert cursor.fetchone()[0] == 1  # Still active

    def test_update_user_active_self_reactivate_returns_200(self):
        """Admin calling activate on themselves (already active) returns 200."""
        # Admin is already active by default. Calling activate on self should succeed.
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.patch(
            f"/users/{self.admin_id}/active",
            json={"is_active": True},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["is_active"] is True

    def test_update_user_active_superadmin_self_deactivate_returns_400(self):
        """Superadmin cannot deactivate their own account - returns 400."""
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.patch(
            f"/users/{self.superadmin_id}/active",
            json={"is_active": False},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 400
        assert "own account" in response.json()["detail"]

    def test_update_user_active_other_user_returns_200(self):
        """Admin can deactivate another user - returns 200."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.patch(
            f"/users/{self.member_id}/active",
            json={"is_active": False},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["is_active"] is False

        # Verify DB was updated
        cursor = self.conn.execute(
            "SELECT is_active FROM users WHERE id = ?", (self.member_id,)
        )
        assert cursor.fetchone()[0] == 0


class TestUpdateUser(TestUserRoutes):
    """Tests for PATCH /users/{user_id} endpoint - username uniqueness."""

    def test_update_user_duplicate_username_returns_409(self):
        """Updating username to an existing username returns 409."""
        # Create another member with a known username
        _ = create_user(
            self.conn, "existinguser", "pass123", "member", "Existing User"
        )

        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.patch(
            f"/users/{self.member_id}",
            json={"username": "existinguser"},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 409
        assert "already taken" in response.json()["detail"]

        # Verify DB was NOT updated
        cursor = self.conn.execute(
            "SELECT username FROM users WHERE id = ?", (self.member_id,)
        )
        assert cursor.fetchone()[0] == "member"  # Still original

    def test_update_user_duplicate_username_case_insensitive_returns_409(self):
        """Updating username to existing username (different case) returns 409."""
        # Create another member with a known username
        create_user(
            self.conn, "testuser", "pass123", "member", "Test User"
        )

        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.patch(
            f"/users/{self.member_id}",
            json={"username": "TESTUSER"},  # Different case
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 409
        assert "already taken" in response.json()["detail"]

    def test_update_user_unique_username_returns_200(self):
        """Updating username to a unique username returns 200."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.patch(
            f"/users/{self.member_id}",
            json={"username": "newuniqueusername"},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["username"] == "newuniqueusername"

        # Verify DB was updated
        cursor = self.conn.execute(
            "SELECT username FROM users WHERE id = ?", (self.member_id,)
        )
        assert cursor.fetchone()[0] == "newuniqueusername"

    def test_update_user_username_same_as_before_returns_200(self):
        """Updating username to its current value returns 200."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.patch(
            f"/users/{self.member_id}",
            json={"username": "member"},  # Same as current
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["username"] == "member"

    def test_update_user_without_username_returns_200(self):
        """Updating other fields without username returns 200."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.patch(
            f"/users/{self.member_id}",
            json={"full_name": "Updated Name"},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["full_name"] == "Updated Name"


class TestCreateUser(TestUserRoutes):
    """Tests for POST /users/ endpoint."""

    def test_create_user_admin_can_create_member(self):
        """Admin can create a new member user."""
        from app.security import csrf_protect
        self.client.app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.post(
            "/users/",
            json={
                "username": "newmember",
                "password": "SecurePass123!",
                "full_name": "New Member",
                "role": "member",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["username"] == "newmember"
        assert data["full_name"] == "New Member"
        assert data["role"] == "member"
        assert data["is_active"] is True
        assert "id" in data
        assert "created_at" in data
        assert "hashed_password" not in data

    def test_create_user_superadmin_can_create_superadmin(self):
        """Superadmin can create another superadmin."""
        from app.security import csrf_protect
        self.client.app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.post(
            "/users/",
            json={
                "username": "newsuperadmin",
                "password": "SecurePass123!",
                "full_name": "New Superadmin",
                "role": "superadmin",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["username"] == "newsuperadmin"
        assert data["role"] == "superadmin"

    def test_create_user_admin_cannot_create_superadmin(self):
        """Admin cannot create superadmin - returns 403."""
        from app.security import csrf_protect
        self.client.app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.post(
            "/users/",
            json={
                "username": "newsuperadmin",
                "password": "SecurePass123!",
                "role": "superadmin",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 403
        assert "superadmin" in response.json()["detail"].lower()

    def test_create_user_duplicate_username_returns_400(self):
        """Duplicate username returns 400."""
        from app.security import csrf_protect
        self.client.app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.post(
            "/users/",
            json={
                "username": "member",  # Already exists
                "password": "SecurePass123!",
                "role": "member",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 400
        assert "already exists" in response.json()["detail"].lower()

    def test_create_user_invalid_role_returns_400(self):
        """Invalid role returns 400."""
        from app.security import csrf_protect
        self.client.app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.post(
            "/users/",
            json={
                "username": "newuser",
                "password": "SecurePass123!",
                "role": "invalid_role",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 400
        assert "invalid role" in response.json()["detail"].lower()

    def test_create_user_weak_password_returns_400(self):
        """Weak password (no uppercase) returns 400 from password_strength_check."""
        from app.security import csrf_protect
        self.client.app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.post(
            "/users/",
            json={
                "username": "newuser",
                "password": "weak1234",  # Passes Pydantic length check but no uppercase
                "role": "member",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 400
        assert "password" in response.json()["detail"].lower()

    def test_create_user_username_too_short_returns_422(self):
        """Username too short returns 422."""
        from app.security import csrf_protect
        self.client.app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.post(
            "/users/",
            json={
                "username": "ab",  # min_length=3
                "password": "SecurePass123!",
                "role": "member",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 422

    def test_create_user_member_rejected(self):
        """Member role cannot create users - returns 403."""
        from app.security import csrf_protect
        self.client.app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
        token = get_token(self.member_id, "member", "member")
        response = self.client.post(
            "/users/",
            json={
                "username": "newuser",
                "password": "SecurePass123!",
                "role": "member",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 403


class TestAdminResetPassword(TestUserRoutes):
    """Tests for PATCH /users/{user_id}/password endpoint."""

    def test_admin_reset_password_member(self):
        """Admin can reset a member's password."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.patch(
            f"/users/{self.member_id}/password",
            json={"new_password": "NewSecurePass123!"},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["message"] == "Password reset successfully"
        assert data["must_change_password"] is True

    def test_admin_reset_password_not_found(self):
        """Resetting password for non-existent user returns 404."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.patch(
            "/users/999/password",
            json={"new_password": "NewSecurePass123!"},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_admin_reset_password_weak_password_returns_400(self):
        """Weak password returns 400."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.patch(
            f"/users/{self.member_id}/password",
            json={"new_password": "weak"},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 400
        assert "password" in response.json()["detail"].lower()

    def test_admin_reset_password_sets_must_change_password(self):
        """Password reset sets must_change_password=1."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.patch(
            f"/users/{self.member_id}/password",
            json={"new_password": "NewSecurePass123!"},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200

        # Verify DB was updated
        cursor = self.conn.execute(
            "SELECT must_change_password FROM users WHERE id = ?", (self.member_id,)
        )
        assert cursor.fetchone()[0] == 1


class TestGetUserOrganizations(TestUserRoutes):
    """Tests for GET /users/{user_id}/organizations endpoint."""

    @pytest.fixture(autouse=True)
    def setup_orgs(self):
        """Set up organizations and org_members table."""
        # Create organizations table and org_members table
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS organizations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS org_members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                org_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL DEFAULT 'member',
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (org_id) REFERENCES organizations(id),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_org_members_user ON org_members(user_id)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_org_members_org ON org_members(org_id)")
        self.conn.commit()

        # Create test organizations
        cursor = self.conn.execute(
            "INSERT INTO organizations (name, description) VALUES (?, ?)",
            ("Test Org 1", "First test organization"),
        )
        self.org1_id = cursor.lastrowid

        cursor = self.conn.execute(
            "INSERT INTO organizations (name, description) VALUES (?, ?)",
            ("Test Org 2", "Second test organization"),
        )
        self.org2_id = cursor.lastrowid

        # Add member to org1 as admin
        self.conn.execute(
            "INSERT INTO org_members (org_id, user_id, role) VALUES (?, ?, ?)",
            (self.org1_id, self.member_id, "admin"),
        )
        # Add member to org2 as member
        self.conn.execute(
            "INSERT INTO org_members (org_id, user_id, role) VALUES (?, ?, ?)",
            (self.org2_id, self.member_id, "member"),
        )
        self.conn.commit()

        yield

        # Clean up org tables
        self.conn.execute("DROP TABLE IF EXISTS org_members")
        self.conn.execute("DROP TABLE IF EXISTS organizations")
        self.conn.commit()

    def test_get_user_organizations_returns_orgs(self):
        """Admin can get user's organization memberships."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.get(
            f"/users/{self.member_id}/organizations",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "organizations" in data
        orgs = data["organizations"]
        assert len(orgs) == 2

        # Check org1
        org1 = next(o for o in orgs if o["id"] == self.org1_id)
        assert org1["name"] == "Test Org 1"
        assert org1["role"] == "admin"

        # Check org2
        org2 = next(o for o in orgs if o["id"] == self.org2_id)
        assert org2["name"] == "Test Org 2"
        assert org2["role"] == "member"

    def test_get_user_organizations_user_not_found(self):
        """Non-existent user returns 404."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.get(
            "/users/999/organizations",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_get_user_organizations_no_orgs(self):
        """User with no org memberships returns empty list."""
        token = get_token(self.admin_id, "admin", "admin")
        # viewer has no org memberships
        response = self.client.get(
            f"/users/{self.viewer_id}/organizations",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["organizations"] == []

    def test_get_user_organizations_member_rejected(self):
        """Member cannot view other users' organizations - returns 403."""
        token = get_token(self.member_id, "member", "member")
        response = self.client.get(
            f"/users/{self.admin_id}/organizations",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 403
