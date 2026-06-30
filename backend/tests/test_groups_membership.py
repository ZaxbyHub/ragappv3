"""
Tests for group membership endpoints in users.py and vaults.py.

Tests cover:
- users.py: get_user_groups, update_user_groups
- vaults.py: get_vault_groups, update_vault_groups

Uses temp directory, settings.data_dir = Path(temp_dir), init_db(db_path)
"""

import os
import shutil
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

from backend.tests.schema_constants import TEST_SCHEMA

from app.services.auth_service import (
    compute_client_fingerprint,
    create_access_token,
    hash_password,
)


def setup_test_db(db_path: str) -> sqlite3.Connection:
    """Set up test database with full schema including groups and vault_group_access."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(TEST_SCHEMA)

    # Insert default vault (local supplement)
    conn.execute(
        "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (1, 'Default', 'Default vault')"
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


def create_organization(conn: sqlite3.Connection, name: str, slug: str = None) -> int:
    """Create a test organization and return its ID."""
    if slug is None:
        slug = name.lower().replace(" ", "-")
    cursor = conn.execute(
        "INSERT INTO organizations (name, slug) VALUES (?, ?)",
        (name, slug),
    )
    conn.commit()
    return cursor.lastrowid


def add_user_to_org(
    conn: sqlite3.Connection, user_id: int, org_id: int, role: str = "member"
) -> None:
    """Add a user to an organization."""
    conn.execute(
        "INSERT INTO org_members (user_id, org_id, role) VALUES (?, ?, ?)",
        (user_id, org_id, role),
    )
    conn.commit()


def create_group(
    conn: sqlite3.Connection, name: str, org_id: int, description: str = ""
) -> int:
    """Create a test group and return its ID."""
    cursor = conn.execute(
        "INSERT INTO groups (name, org_id, description) VALUES (?, ?, ?)",
        (name, org_id, description),
    )
    conn.commit()
    return cursor.lastrowid


def add_user_to_group(conn: sqlite3.Connection, user_id: int, group_id: int) -> None:
    """Add a user to a group."""
    conn.execute(
        "INSERT INTO group_members (user_id, group_id) VALUES (?, ?)",
        (user_id, group_id),
    )
    conn.commit()


def create_vault(
    conn: sqlite3.Connection, name: str, org_id: int = None, visibility: str = "private"
) -> int:
    """Create a test vault and return its ID."""
    cursor = conn.execute(
        "INSERT INTO vaults (name, org_id, visibility) VALUES (?, ?, ?)",
        (name, org_id, visibility),
    )
    conn.commit()
    return cursor.lastrowid


def add_group_to_vault(
    conn: sqlite3.Connection, vault_id: int, group_id: int, granted_by: int = None
) -> None:
    """Add a group to vault access list."""
    conn.execute(
        "INSERT INTO vault_group_access (vault_id, group_id, granted_by) VALUES (?, ?, ?)",
        (vault_id, group_id, granted_by),
    )
    conn.commit()


def add_user_to_vault(
    conn: sqlite3.Connection,
    vault_id: int,
    user_id: int,
    permission: str = "admin",
    granted_by: int = None,
) -> None:
    """Add a user directly to vault access list."""
    conn.execute(
        "INSERT INTO vault_members (vault_id, user_id, permission, granted_by) VALUES (?, ?, ?, ?)",
        (vault_id, user_id, permission, granted_by),
    )
    conn.commit()


def get_token(user_id: int, username: str, role: str) -> str:
    """Generate a JWT token for a test user."""
    return create_access_token(user_id, username, role,
                          client_fingerprint=compute_client_fingerprint(""))


class TestUserGroupMembershipSetup:
    """Setup class for user group membership tests."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up test database for each test."""
        # Create temp directory and database
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")

        # Ensure settings.users_enabled=True so JWT tokens are accepted.
        # The conftest may have loaded settings with users_enabled=False via a
        # prior test file; mutate it here and restore on teardown.
        from app.config import settings as _app_settings
        self._prev_users_enabled = _app_settings.users_enabled
        _app_settings.users_enabled = True

        # Clear the global pool cache to ensure test isolation
        from app.models.database import _pool_cache

        _pool_cache.clear()

        # Set up test database
        self.conn = setup_test_db(self.db_path)

        # Create organizations
        self.org1_id = create_organization(self.conn, "Org One", "org-one")
        self.org2_id = create_organization(self.conn, "Org Two", "org-two")

        # Create users with different roles
        self.superadmin_id = create_user(
            self.conn, "superadmin", "pass123", "superadmin", "Super Admin"
        )
        self.admin_org1_id = create_user(
            self.conn, "admin_org1", "pass123", "admin", "Admin Org1"
        )
        self.admin_org2_id = create_user(
            self.conn, "admin_org2", "pass123", "admin", "Admin Org2"
        )
        self.member_org1_id = create_user(
            self.conn, "member_org1", "pass123", "member", "Member Org1"
        )
        self.member_org2_id = create_user(
            self.conn, "member_org2", "pass123", "member", "Member Org2"
        )
        self.viewer_id = create_user(
            self.conn, "viewer", "pass123", "viewer", "Viewer User"
        )

        # Add admins to orgs
        add_user_to_org(self.conn, self.admin_org1_id, self.org1_id, "admin")
        add_user_to_org(self.conn, self.admin_org2_id, self.org2_id, "admin")
        add_user_to_org(self.conn, self.member_org1_id, self.org1_id)
        add_user_to_org(self.conn, self.member_org2_id, self.org2_id)

        # Create groups in each org
        self.group1_id = create_group(
            self.conn, "Group One", self.org1_id, "Group in Org1"
        )
        self.group2_id = create_group(
            self.conn, "Group Two", self.org1_id, "Another Group in Org1"
        )
        self.group3_id = create_group(
            self.conn, "Group Three", self.org2_id, "Group in Org2"
        )

        # Create app with users router
        from app.api.routes.users import router as users_router
        from app.models.database import SQLiteConnectionPool

        app = FastAPI()
        app.include_router(users_router)

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

        # Also patch deps.get_pool for evaluate_policy
        from app.api import deps

        original_deps_pool = deps.get_pool
        deps.get_pool = lambda path: test_pool

        app.dependency_overrides[deps.get_db] = override_get_db

        # Store for cleanup
        self.test_pool = test_pool
        self.original_get_pool = original_get_pool
        self.original_deps_pool = original_deps_pool

        self.client = TestClient(app)
        # Override default User-Agent so fingerprint validation matches token
        self.client.headers["user-agent"] = ""

        yield

        # Cleanup
        self.client.close()
        _pool_cache.clear()
        self.conn.close()

        # Restore settings
        from app.config import settings as _app_settings
        _app_settings.users_enabled = self._prev_users_enabled

        # Restore original get_pool
        users.get_pool = self.original_get_pool
        deps.get_pool = self.original_deps_pool
        self.test_pool.close_all()

        # Clean up temp directory
        try:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        except Exception:
            pass


class TestGetUserGroups(TestUserGroupMembershipSetup):
    """Tests for GET /users/{user_id}/groups endpoint."""

    def test_admin_can_list_users_groups(self):
        """Admin can list a user's group memberships."""
        # Add member to a group first
        add_user_to_group(self.conn, self.member_org1_id, self.group1_id)

        token = get_token(self.admin_org1_id, "admin_org1", "admin")
        response = self.client.get(
            f"/users/{self.member_org1_id}/groups",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "groups" in data
        assert len(data["groups"]) == 1
        assert data["groups"][0]["id"] == self.group1_id
        assert data["groups"][0]["name"] == "Group One"
        assert data["groups"][0]["org_id"] == self.org1_id

    def test_admin_can_list_multiple_groups(self):
        """Admin can list a user with multiple group memberships."""
        add_user_to_group(self.conn, self.member_org1_id, self.group1_id)
        add_user_to_group(self.conn, self.member_org1_id, self.group2_id)

        token = get_token(self.admin_org1_id, "admin_org1", "admin")
        response = self.client.get(
            f"/users/{self.member_org1_id}/groups",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["groups"]) == 2
        group_ids = {g["id"] for g in data["groups"]}
        assert group_ids == {self.group1_id, self.group2_id}

    def test_admin_gets_empty_list_for_user_with_no_groups(self):
        """User with no groups returns empty list."""
        token = get_token(self.admin_org1_id, "admin_org1", "admin")
        response = self.client.get(
            f"/users/{self.member_org1_id}/groups",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["groups"] == []

    def test_non_admin_gets_403(self):
        """Non-admin (member role) gets 403 when listing groups."""
        token = get_token(self.member_org1_id, "member_org1", "member")
        response = self.client.get(
            f"/users/{self.admin_org1_id}/groups",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 403

    def test_viewer_gets_403(self):
        """Viewer role gets 403 when listing groups."""
        token = get_token(self.viewer_id, "viewer", "viewer")
        response = self.client.get(
            f"/users/{self.member_org1_id}/groups",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 403

    def test_superadmin_bypasses_org_check(self):
        """Superadmin can list groups of users in any org."""
        # member_org2 is in org2, superadmin is superadmin (no org membership)
        add_user_to_group(self.conn, self.member_org2_id, self.group3_id)

        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.get(
            f"/users/{self.member_org2_id}/groups",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["groups"]) == 1
        assert data["groups"][0]["id"] == self.group3_id

    def test_org_isolation_admin_in_different_org_gets_403(self):
        """Admin in different org than target user gets 403."""
        # member_org2 is in org2
        add_user_to_group(self.conn, self.member_org2_id, self.group3_id)

        # admin_org1 is in org1, not org2
        token = get_token(self.admin_org1_id, "admin_org1", "admin")
        response = self.client.get(
            f"/users/{self.member_org2_id}/groups",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 403
        assert "outside your organization" in response.json()["detail"]

    def test_user_not_found_returns_404(self):
        """Getting groups for non-existent user returns 404."""
        token = get_token(self.admin_org1_id, "admin_org1", "admin")
        response = self.client.get(
            "/users/99999/groups", headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 404
        assert "User not found" in response.json()["detail"]

    def test_unauthenticated_gets_401(self):
        """Unauthenticated request returns 401."""
        response = self.client.get(f"/users/{self.member_org1_id}/groups")
        assert response.status_code == 401


class TestUpdateUserGroups(TestUserGroupMembershipSetup):
    """Tests for PUT /users/{user_id}/groups endpoint."""

    def test_admin_can_replace_memberships(self):
        """Admin can replace a user's group memberships."""
        token = get_token(self.admin_org1_id, "admin_org1", "admin")
        response = self.client.put(
            f"/users/{self.member_org1_id}/groups",
            json={"group_ids": [self.group1_id, self.group2_id]},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "groups" in data
        assert len(data["groups"]) == 2
        group_ids = {g["id"] for g in data["groups"]}
        assert group_ids == {self.group1_id, self.group2_id}

    def test_admin_can_add_single_group(self):
        """Admin can add a user to a single group."""
        token = get_token(self.admin_org1_id, "admin_org1", "admin")
        response = self.client.put(
            f"/users/{self.member_org1_id}/groups",
            json={"group_ids": [self.group1_id]},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["groups"]) == 1
        assert data["groups"][0]["id"] == self.group1_id

    def test_empty_group_ids_clears_all_memberships(self):
        """Empty group_ids list clears all memberships."""
        # First add some memberships
        add_user_to_group(self.conn, self.member_org1_id, self.group1_id)
        add_user_to_group(self.conn, self.member_org1_id, self.group2_id)

        # Verify they exist
        cursor = self.conn.execute(
            "SELECT COUNT(*) FROM group_members WHERE user_id = ?",
            (self.member_org1_id,),
        )
        assert cursor.fetchone()[0] == 2

        # Clear memberships
        token = get_token(self.admin_org1_id, "admin_org1", "admin")
        response = self.client.put(
            f"/users/{self.member_org1_id}/groups",
            json={"group_ids": []},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["groups"] == []

        # Verify DB was updated
        cursor = self.conn.execute(
            "SELECT COUNT(*) FROM group_members WHERE user_id = ?",
            (self.member_org1_id,),
        )
        assert cursor.fetchone()[0] == 0

    def test_validates_missing_groups_returns_400(self):
        """Non-existent group IDs return 400."""
        token = get_token(self.admin_org1_id, "admin_org1", "admin")
        response = self.client.put(
            f"/users/{self.member_org1_id}/groups",
            json={"group_ids": [99999, 88888]},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 400
        assert "Groups not found" in response.json()["detail"]

    def test_validates_partial_missing_groups_returns_400(self):
        """Mix of valid and invalid group IDs returns 400."""
        token = get_token(self.admin_org1_id, "admin_org1", "admin")
        response = self.client.put(
            f"/users/{self.member_org1_id}/groups",
            json={"group_ids": [self.group1_id, 99999]},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 400
        assert "Groups not found" in response.json()["detail"]

    def test_validates_user_is_org_member_for_each_group(self):
        """Admin must be member of each group's org (non-superadmin)."""
        # group3 is in org2, admin_org1 is only in org1
        token = get_token(self.admin_org1_id, "admin_org1", "admin")
        response = self.client.put(
            f"/users/{self.member_org1_id}/groups",
            json={"group_ids": [self.group3_id]},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 400
        assert "not a member of organization" in response.json()["detail"]

    def test_superadmin_bypasses_org_membership_check(self):
        """Superadmin can add user to any group's org without being a member themselves."""
        # group3 is in org2, superadmin is not a member of org2
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.put(
            f"/users/{self.member_org1_id}/groups",
            json={"group_ids": [self.group3_id]},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["groups"]) == 1
        assert data["groups"][0]["id"] == self.group3_id

    def test_user_not_found_returns_404(self):
        """Updating groups for non-existent user returns 404."""
        token = get_token(self.admin_org1_id, "admin_org1", "admin")
        response = self.client.put(
            "/users/99999/groups",
            json={"group_ids": [self.group1_id]},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 404
        assert "User not found" in response.json()["detail"]

    def test_non_admin_gets_403(self):
        """Non-admin (member role) gets 403 when updating groups."""
        token = get_token(self.member_org1_id, "member_org1", "member")
        response = self.client.put(
            f"/users/{self.admin_org1_id}/groups",
            json={"group_ids": [self.group1_id]},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 403

    def test_unauthenticated_gets_401(self):
        """Unauthenticated request returns 401."""
        response = self.client.put(
            f"/users/{self.member_org1_id}/groups", json={"group_ids": [self.group1_id]}
        )
        assert response.status_code == 401

    def test_removes_previous_memberships_and_adds_new(self):
        """Update replaces all previous memberships with new ones."""
        # Start with one group
        add_user_to_group(self.conn, self.member_org1_id, self.group1_id)

        # Update to a different group
        token = get_token(self.admin_org1_id, "admin_org1", "admin")
        response = self.client.put(
            f"/users/{self.member_org1_id}/groups",
            json={"group_ids": [self.group2_id]},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["groups"]) == 1
        assert data["groups"][0]["id"] == self.group2_id

        # Verify old membership was removed
        cursor = self.conn.execute(
            "SELECT group_id FROM group_members WHERE user_id = ?",
            (self.member_org1_id,),
        )
        remaining_groups = {row[0] for row in cursor.fetchall()}
        assert self.group1_id not in remaining_groups
        assert self.group2_id in remaining_groups


class TestVaultGroupMembershipSetup:
    """Setup class for vault group access tests.

    NOTE: Vault group endpoints require 'admin' permission on the vault.
    Per evaluate_policy logic, admin role only gets read/write, NOT admin.
    So we use superadmin for vault admin operations.
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up test database for each test."""
        # Create temp directory and database
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")

        # Ensure settings.users_enabled=True so JWT tokens are accepted.
        from app.config import settings as _app_settings
        self._prev_users_enabled = _app_settings.users_enabled
        _app_settings.users_enabled = True

        # Clear the global pool cache to ensure test isolation
        from app.models.database import _pool_cache

        _pool_cache.clear()

        # Set up test database
        self.conn = setup_test_db(self.db_path)

        # Create organizations
        self.org1_id = create_organization(self.conn, "Org One", "org-one")
        self.org2_id = create_organization(self.conn, "Org Two", "org-two")

        # Create users
        self.superadmin_id = create_user(
            self.conn, "superadmin", "pass123", "superadmin", "Super Admin"
        )
        self.admin_org1_id = create_user(
            self.conn, "admin_org1", "pass123", "admin", "Admin Org1"
        )
        self.admin_org2_id = create_user(
            self.conn, "admin_org2", "pass123", "admin", "Admin Org2"
        )
        self.member_org1_id = create_user(
            self.conn, "member_org1", "pass123", "member", "Member Org1"
        )
        self.viewer_id = create_user(
            self.conn, "viewer", "pass123", "viewer", "Viewer User"
        )

        # Add admins to orgs
        add_user_to_org(self.conn, self.admin_org1_id, self.org1_id, "admin")
        add_user_to_org(self.conn, self.admin_org2_id, self.org2_id, "admin")

        # Create groups
        self.group1_id = create_group(
            self.conn, "Group One", self.org1_id, "Group in Org1"
        )
        self.group2_id = create_group(
            self.conn, "Group Two", self.org1_id, "Another Group in Org1"
        )
        self.group3_id = create_group(
            self.conn, "Group Three", self.org2_id, "Group in Org2"
        )

        # Create vaults
        self.vault1_id = create_vault(self.conn, "Vault One", self.org1_id, "private")
        self.vault2_id = create_vault(self.conn, "Vault Two", self.org2_id, "private")
        self.vault_no_org_id = create_vault(self.conn, "Vault No Org", None, "private")

        # NOTE: We give direct admin access to vault_members for admin users,
        # but since evaluate_policy grants admin role only read/write (not admin action),
        # vault admin endpoints need superadmin. The direct vault_members permission
        # grants actual 'admin' permission level to the user.

        # Create app with vaults router
        from app.api.routes.vaults import router as vaults_router
        from app.models.database import SQLiteConnectionPool

        app = FastAPI()
        app.include_router(vaults_router)

        # Create a test pool
        test_pool = SQLiteConnectionPool(self.db_path, max_size=3)

        def override_get_db():
            """Override get_db to return a connection from test pool."""
            conn = test_pool.get_connection()
            try:
                yield conn
            finally:
                test_pool.release_connection(conn)

        # Patch get_pool in deps module
        from app.api import deps

        original_deps_pool = deps.get_pool
        deps.get_pool = lambda path: test_pool

        app.dependency_overrides[deps.get_db] = override_get_db

        # Store for cleanup
        self.test_pool = test_pool
        self.original_deps_pool = original_deps_pool

        self.client = TestClient(app)
        # Override default User-Agent so fingerprint validation matches token
        self.client.headers["user-agent"] = ""

        yield

        # Cleanup
        self.client.close()
        _pool_cache.clear()
        self.conn.close()

        # Restore settings
        from app.config import settings as _app_settings
        _app_settings.users_enabled = self._prev_users_enabled

        # Restore original get_pool
        deps.get_pool = self.original_deps_pool
        self.test_pool.close_all()

        # Clean up temp directory
        try:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        except Exception:
            pass


class TestGetVaultGroups(TestVaultGroupMembershipSetup):
    """Tests for GET /vaults/{vault_id}/groups endpoint.

    Uses superadmin for vault admin operations since evaluate_policy
    only grants admin role read/write, not admin action.
    """

    def test_admin_can_list_vault_groups(self):
        """Admin with vault access can list groups with vault access."""
        add_group_to_vault(
            self.conn, self.vault1_id, self.group1_id, self.superadmin_id
        )

        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.get(
            f"/vaults/{self.vault1_id}/groups",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "groups" in data
        assert len(data["groups"]) == 1
        assert data["groups"][0]["id"] == self.group1_id
        assert data["groups"][0]["name"] == "Group One"

    def test_admin_can_list_multiple_groups(self):
        """Admin can list multiple groups with vault access."""
        add_group_to_vault(
            self.conn, self.vault1_id, self.group1_id, self.superadmin_id
        )
        add_group_to_vault(
            self.conn, self.vault1_id, self.group2_id, self.superadmin_id
        )

        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.get(
            f"/vaults/{self.vault1_id}/groups",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["groups"]) == 2
        group_ids = {g["id"] for g in data["groups"]}
        assert group_ids == {self.group1_id, self.group2_id}

    def test_gets_empty_list_for_vault_with_no_groups(self):
        """Vault with no groups returns empty list."""
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.get(
            f"/vaults/{self.vault1_id}/groups",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["groups"] == []

    def test_non_admin_gets_403(self):
        """Non-admin (member role) gets 403 when listing vault groups."""
        token = get_token(self.member_org1_id, "member_org1", "member")
        response = self.client.get(
            f"/vaults/{self.vault1_id}/groups",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 403
        assert "No admin access" in response.json()["detail"]

    def test_404_for_nonexistent_vault(self):
        """Getting groups for non-existent vault returns 404."""
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.get(
            "/vaults/99999/groups", headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 404
        assert "not found" in response.json()["detail"]

    def test_superadmin_can_list_any_vault_groups(self):
        """Superadmin can list groups for any vault."""
        add_group_to_vault(
            self.conn, self.vault2_id, self.group3_id, self.superadmin_id
        )

        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.get(
            f"/vaults/{self.vault2_id}/groups",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["groups"]) == 1
        assert data["groups"][0]["id"] == self.group3_id

    def test_admin_no_access_to_other_org_vault(self):
        """Admin without vault access gets 403."""
        # superadmin bypasses this, so use admin_org1 which has no access to vault2
        token = get_token(self.admin_org1_id, "admin_org1", "admin")
        response = self.client.get(
            f"/vaults/{self.vault2_id}/groups",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 403

    def test_unauthenticated_gets_401(self):
        """Unauthenticated request returns 401."""
        response = self.client.get(f"/vaults/{self.vault1_id}/groups")
        assert response.status_code == 401


class TestUpdateVaultGroups(TestVaultGroupMembershipSetup):
    """Tests for PUT /vaults/{vault_id}/groups endpoint.

    Uses superadmin for vault admin operations since evaluate_policy
    only grants admin role read/write, not admin action.
    """

    def test_admin_can_replace_vault_groups(self):
        """Admin can replace vault's group access list."""
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.put(
            f"/vaults/{self.vault1_id}/groups",
            json={"group_ids": [self.group1_id, self.group2_id]},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "groups" in data
        assert len(data["groups"]) == 2
        group_ids = {g["id"] for g in data["groups"]}
        assert group_ids == {self.group1_id, self.group2_id}

    def test_admin_can_add_single_group(self):
        """Admin can add a single group to vault access."""
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.put(
            f"/vaults/{self.vault1_id}/groups",
            json={"group_ids": [self.group1_id]},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["groups"]) == 1
        assert data["groups"][0]["id"] == self.group1_id

    def test_empty_group_ids_clears_all_access(self):
        """Empty group_ids list clears all vault group access."""
        # First add some access
        add_group_to_vault(
            self.conn, self.vault1_id, self.group1_id, self.superadmin_id
        )
        add_group_to_vault(
            self.conn, self.vault1_id, self.group2_id, self.superadmin_id
        )

        # Verify they exist
        cursor = self.conn.execute(
            "SELECT COUNT(*) FROM vault_group_access WHERE vault_id = ?",
            (self.vault1_id,),
        )
        assert cursor.fetchone()[0] == 2

        # Clear access
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.put(
            f"/vaults/{self.vault1_id}/groups",
            json={"group_ids": []},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["groups"] == []

        # Verify DB was updated
        cursor = self.conn.execute(
            "SELECT COUNT(*) FROM vault_group_access WHERE vault_id = ?",
            (self.vault1_id,),
        )
        assert cursor.fetchone()[0] == 0

    def test_validates_missing_groups_returns_400(self):
        """Non-existent group IDs return 400."""
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.put(
            f"/vaults/{self.vault1_id}/groups",
            json={"group_ids": [99999, 88888]},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 400
        assert "not found" in response.json()["detail"]

    def test_validates_partial_missing_groups_returns_400(self):
        """Mix of valid and invalid group IDs returns 400."""
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.put(
            f"/vaults/{self.vault1_id}/groups",
            json={"group_ids": [self.group1_id, 99999]},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 400
        assert "not found" in response.json()["detail"]

    def test_validates_groups_belong_to_same_org_as_vault(self):
        """Groups must belong to the same org as the vault."""
        # group3 is in org2, vault1 is in org1
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.put(
            f"/vaults/{self.vault1_id}/groups",
            json={"group_ids": [self.group3_id]},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 400
        assert "different organization" in response.json()["detail"]

    def test_validates_multiple_groups_cross_org_returns_400(self):
        """Multiple groups with one from different org returns 400."""
        # group1 is in org1, group3 is in org2, vault1 is in org1
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.put(
            f"/vaults/{self.vault1_id}/groups",
            json={"group_ids": [self.group1_id, self.group3_id]},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 400
        assert "different organization" in response.json()["detail"]

    def test_transaction_rollback_on_error(self):
        """If validation fails mid-operation, no changes are made."""
        # Add initial group access
        add_group_to_vault(
            self.conn, self.vault1_id, self.group1_id, self.superadmin_id
        )

        # Try to update with cross-org group (should fail)
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.put(
            f"/vaults/{self.vault1_id}/groups",
            json={"group_ids": [self.group3_id]},  # group3 is in org2
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 400

        # Verify original access was not changed
        cursor = self.conn.execute(
            "SELECT group_id FROM vault_group_access WHERE vault_id = ?",
            (self.vault1_id,),
        )
        remaining_groups = {row[0] for row in cursor.fetchall()}
        assert self.group1_id in remaining_groups

    def test_vault_not_found_returns_404(self):
        """Updating groups for non-existent vault returns 404."""
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.put(
            "/vaults/99999/groups",
            json={"group_ids": [self.group1_id]},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 404
        assert "not found" in response.json()["detail"]

    def test_non_admin_gets_403(self):
        """Non-admin (member role) gets 403 when updating vault groups."""
        token = get_token(self.member_org1_id, "member_org1", "member")
        response = self.client.put(
            f"/vaults/{self.vault1_id}/groups",
            json={"group_ids": [self.group1_id]},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 403

    def test_unauthenticated_gets_401(self):
        """Unauthenticated request returns 401."""
        response = self.client.put(
            f"/vaults/{self.vault1_id}/groups", json={"group_ids": [self.group1_id]}
        )
        assert response.status_code == 401

    def test_removes_previous_access_and_adds_new(self):
        """Update replaces all previous access with new groups."""
        # Start with one group
        add_group_to_vault(
            self.conn, self.vault1_id, self.group1_id, self.superadmin_id
        )

        # Update to a different group
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.put(
            f"/vaults/{self.vault1_id}/groups",
            json={"group_ids": [self.group2_id]},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["groups"]) == 1
        assert data["groups"][0]["id"] == self.group2_id

        # Verify old access was removed
        cursor = self.conn.execute(
            "SELECT group_id FROM vault_group_access WHERE vault_id = ?",
            (self.vault1_id,),
        )
        remaining_groups = {row[0] for row in cursor.fetchall()}
        assert self.group1_id not in remaining_groups
        assert self.group2_id in remaining_groups

    def test_vault_with_null_org_accepts_any_group(self):
        """Vault with NULL org_id can accept any group after NULL comparison fix.

        With the fix (row[1] is not None and vault_org_id is not None and row[1] != vault_org_id),
        vaults with NULL org_id no longer incorrectly reject groups. When vault_org_id is NULL,
        the condition short-circuits and doesn't reject any groups.
        """
        token = get_token(self.superadmin_id, "superadmin", "superadmin")

        # vault_no_org_id has org_id = NULL, group3_id is in org2
        response = self.client.put(
            f"/vaults/{self.vault_no_org_id}/groups",
            json={"group_ids": [self.group3_id]},
            headers={"Authorization": f"Bearer {token}"},
        )

        # After fix: should succeed (200) since vault_org_id is NULL
        # and the NULL comparison short-circuits to not reject any groups
        assert response.status_code == 200
        data = response.json()
        assert len(data["groups"]) == 1
        assert data["groups"][0]["id"] == self.group3_id

    def test_vault_with_null_org_can_have_multiple_groups(self):
        """Vault with NULL org_id can accept groups from multiple orgs."""
        token = get_token(self.superadmin_id, "superadmin", "superadmin")

        # Add group1 (org1) and group3 (org2) to vault with NULL org_id
        response = self.client.put(
            f"/vaults/{self.vault_no_org_id}/groups",
            json={"group_ids": [self.group1_id, self.group3_id]},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["groups"]) == 2
        group_ids = {g["id"] for g in data["groups"]}
        assert group_ids == {self.group1_id, self.group3_id}

    def test_get_vault_groups_with_null_org(self):
        """Can list groups for vault with NULL org_id."""
        # First add some groups
        add_group_to_vault(
            self.conn, self.vault_no_org_id, self.group1_id, self.superadmin_id
        )

        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.get(
            f"/vaults/{self.vault_no_org_id}/groups",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["groups"]) == 1
        assert data["groups"][0]["id"] == self.group1_id
