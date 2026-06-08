"""Tests for groups API routes (admin panel)."""

import sqlite3
import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.auth import router as auth_router
from app.api.routes.groups import router as groups_router
from app.services.auth_service import create_access_token, hash_password

# Valid SQLite schema matching production structure
TEST_SCHEMA = """
-- Users table
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    hashed_password TEXT NOT NULL,
    full_name TEXT DEFAULT '',
    role TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('superadmin','admin','member','viewer')),
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_login_at TIMESTAMP,
    must_change_password INTEGER DEFAULT 0,
    failed_attempts INTEGER DEFAULT 0,
    locked_until TIMESTAMP
);

-- Organizations table
CREATE TABLE IF NOT EXISTS organizations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT DEFAULT '',
    slug TEXT UNIQUE,
    created_by INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (created_by) REFERENCES users(id)
);

-- Organization members
CREATE TABLE IF NOT EXISTS org_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('owner','admin','member')),
    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    UNIQUE(org_id, user_id)
);

-- Groups table
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE,
    UNIQUE(org_id, name)
);

-- Group members
CREATE TABLE IF NOT EXISTS group_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    UNIQUE(group_id, user_id)
);

-- Vaults table
CREATE TABLE IF NOT EXISTS vaults (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    owner_id INTEGER,
    org_id INTEGER,
    visibility TEXT DEFAULT 'private' CHECK (visibility IN ('private', 'org', 'public')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (owner_id) REFERENCES users(id),
    FOREIGN KEY (org_id) REFERENCES organizations(id)
);

-- Vault members table
CREATE TABLE IF NOT EXISTS vault_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    permission TEXT NOT NULL DEFAULT 'read' CHECK (permission IN ('read','write','admin')),
    granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    granted_by INTEGER,
    FOREIGN KEY (vault_id) REFERENCES vaults(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (granted_by) REFERENCES users(id) ON DELETE SET NULL,
    UNIQUE(vault_id, user_id)
);

-- Vault group access
CREATE TABLE IF NOT EXISTS vault_group_access (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_id INTEGER NOT NULL,
    group_id INTEGER NOT NULL,
    permission TEXT NOT NULL DEFAULT 'read' CHECK (permission IN ('read','write','admin')),
    granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    granted_by INTEGER,
    FOREIGN KEY (vault_id) REFERENCES vaults(id) ON DELETE CASCADE,
    FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE,
    FOREIGN KEY (granted_by) REFERENCES users(id) ON DELETE SET NULL,
    UNIQUE(vault_id, group_id)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);
CREATE INDEX IF NOT EXISTS idx_vault_members_vault_id ON vault_members(vault_id);
CREATE INDEX IF NOT EXISTS idx_vault_members_user_id ON vault_members(user_id);
CREATE INDEX IF NOT EXISTS idx_org_members_org_id ON org_members(org_id);
CREATE INDEX IF NOT EXISTS idx_org_members_user_id ON org_members(user_id);
CREATE INDEX IF NOT EXISTS idx_groups_org_id ON groups(org_id);
CREATE INDEX IF NOT EXISTS idx_group_members_group_id ON group_members(group_id);
CREATE INDEX IF NOT EXISTS idx_group_members_user_id ON group_members(user_id);
CREATE INDEX IF NOT EXISTS idx_vault_group_access_vault_id ON vault_group_access(vault_id);
CREATE INDEX IF NOT EXISTS idx_vault_group_access_group_id ON vault_group_access(group_id);
"""


@pytest.fixture(autouse=True)
def setup_db(monkeypatch):
    """Set up test database with schema and seed data."""
    temp_dir = tempfile.mkdtemp()
    db_path = str(Path(temp_dir) / "app.db")

    # Clear pool cache BEFORE setting up new database
    from app.models.database import _pool_cache, _pool_cache_lock

    with _pool_cache_lock:
        for path, pool in list(_pool_cache.items()):
            pool.close_all()
        _pool_cache.clear()

    # Initialize schema manually with valid SQL
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(TEST_SCHEMA)
    conn.commit()
    conn.close()

    # Patch settings
    monkeypatch.setattr("app.config.settings.data_dir", Path(temp_dir))
    monkeypatch.setattr(
        "app.config.settings.jwt_secret_key",
        "test-secret-key-for-testing-only-min-32-chars!!",
    )
    monkeypatch.setattr("app.config.settings.users_enabled", True)

    # Seed test users
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    pw = hash_password("testpass")
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
        (1, "superadmin", pw, "Super Admin", "superadmin"),
    )
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
        (2, "admin1", pw, "Admin One", "admin"),
    )
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
        (3, "member1", pw, "Member One", "member"),
    )
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
        (4, "member2", pw, "Member Two", "member"),
    )
    conn.commit()
    conn.close()

    yield db_path

    # Cleanup
    with _pool_cache_lock:
        if db_path in _pool_cache:
            _pool_cache[db_path].close_all()
            del _pool_cache[db_path]

    import shutil

    shutil.rmtree(temp_dir, ignore_errors=True)


def _get_db_conn():
    """Get a direct connection to the test database for setup."""
    from app.config import settings

    return sqlite3.connect(str(settings.sqlite_path))


def _create_org(name: str, owner_user_id: int, description: str = "Test org"):
    """Create an organization and add owner as owner."""
    conn = _get_db_conn()
    conn.execute("PRAGMA foreign_keys = ON")
    cursor = conn.execute(
        "INSERT INTO organizations (name, description, slug, created_by) VALUES (?, ?, ?, ?)",
        (name, description, name.lower().replace(" ", "-"), owner_user_id),
    )
    org_id = cursor.lastrowid
    conn.execute(
        "INSERT INTO org_members (org_id, user_id, role) VALUES (?, ?, 'owner')",
        (org_id, owner_user_id),
    )
    conn.commit()
    conn.close()
    return org_id


def _add_org_member(org_id: int, user_id: int, role: str):
    """Add a member to an organization."""
    conn = _get_db_conn()
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT OR IGNORE INTO org_members (org_id, user_id, role) VALUES (?, ?, ?)",
        (org_id, user_id, role),
    )
    conn.commit()
    conn.close()


def _create_group(org_id: int, name: str, description: str = "Test group"):
    """Create a group within an organization."""
    conn = _get_db_conn()
    conn.execute("PRAGMA foreign_keys = ON")
    cursor = conn.execute(
        "INSERT INTO groups (org_id, name, description) VALUES (?, ?, ?)",
        (org_id, name, description),
    )
    group_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return group_id


def _add_group_member(group_id: int, user_id: int):
    """Add a member to a group."""
    conn = _get_db_conn()
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT OR IGNORE INTO group_members (group_id, user_id) VALUES (?, ?)",
        (group_id, user_id),
    )
    conn.commit()
    conn.close()


def _create_vault(org_id: int, name: str, visibility: str = "private"):
    """Create a vault within an organization."""
    conn = _get_db_conn()
    conn.execute("PRAGMA foreign_keys = ON")
    cursor = conn.execute(
        "INSERT INTO vaults (org_id, name, visibility) VALUES (?, ?, ?)",
        (org_id, name, visibility),
    )
    vault_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return vault_id


def _add_vault_group_access(vault_id: int, group_id: int, permission: str = "read"):
    """Add group access to a vault."""
    conn = _get_db_conn()
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO vault_group_access (vault_id, group_id, permission, granted_by) VALUES (?, ?, ?, ?)",
        (vault_id, group_id, permission, 1),
    )
    conn.commit()
    conn.close()


def superadmin_token():
    return create_access_token(1, "superadmin", "superadmin")


def admin_token():
    return create_access_token(2, "admin1", "admin")


def member_token():
    return create_access_token(3, "member1", "member")


def member2_token():
    return create_access_token(4, "member2", "member")


def auth_headers(token_fn):
    return {"Authorization": f"Bearer {token_fn()}"}


@pytest.fixture
def client():
    """Create test client with routers."""
    app = FastAPI()
    app.include_router(auth_router, prefix="/api")
    app.include_router(groups_router, prefix="/api")
    return TestClient(app)


# =============================================================================
# Test list_groups
# =============================================================================


class TestListGroups:
    """Tests for GET /groups endpoint."""

    def test_list_groups_returns_paginated_response(self, client):
        """List groups returns paginated response with groups and total."""
        # Create org and groups
        org_id = _create_org("List Test Org", 2)
        _create_group(org_id, "Group Alpha")
        _create_group(org_id, "Group Beta")

        response = client.get("/api/groups", headers=auth_headers(admin_token))
        assert response.status_code == 200
        data = response.json()
        assert "groups" in data
        assert "total" in data
        assert data["total"] == 2
        assert len(data["groups"]) == 2

    def test_list_groups_skip_limit(self, client):
        """List groups respects page and per_page parameters."""
        # Create org and multiple groups
        org_id = _create_org("Paginate Org", 2)
        for i in range(5):
            _create_group(org_id, f"Group {i}")

        # Test per_page limit
        response = client.get(
            "/api/groups?per_page=2", headers=auth_headers(admin_token)
        )
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 5
        assert len(data["groups"]) == 2

        # Test page 2 with per_page=2 (equivalent to skip=2&limit=2)
        response = client.get(
            "/api/groups?page=2&per_page=2", headers=auth_headers(admin_token)
        )
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 5
        assert len(data["groups"]) == 2
        assert data["groups"][0]["name"] == "Group 2"

    def test_list_groups_admin_only_403(self, client):
        """Non-admin user gets 403 Forbidden."""
        response = client.get("/api/groups", headers=auth_headers(member_token))
        assert response.status_code == 403

    def test_list_groups_superadmin_allowed(self, client):
        """Superadmin can list all groups."""
        org_id = _create_org("Superadmin Org", 2)
        _create_group(org_id, "Super Group")

        response = client.get("/api/groups", headers=auth_headers(superadmin_token))
        assert response.status_code == 200
        data = response.json()
        assert data["total"] >= 1

    def test_list_groups_includes_organization_name(self, client):
        """List groups includes organization_name in response."""
        org_id = _create_org("Org With Groups", 2)
        _create_group(org_id, "Named Group", "A group with a description")

        response = client.get("/api/groups", headers=auth_headers(admin_token))
        assert response.status_code == 200
        data = response.json()
        group = data["groups"][0]
        assert "organization_name" in group
        assert group["organization_name"] == "Org With Groups"

    def test_list_groups_admin_only_sees_own_org(self, client):
        """Admin in Org A does not see groups from Org B."""
        # Create two orgs
        org_a_id = _create_org("Org A", 2)  # admin1 (user 2) is owner
        org_b_id = _create_org("Org B", 1)  # superadmin (user 1) is owner

        # Create groups in both orgs
        _create_group(org_a_id, "Group in Org A")
        _create_group(org_a_id, "Another Group in Org A")
        _create_group(org_b_id, "Group in Org B")
        _create_group(org_b_id, "Secret Group in Org B")

        # admin1 should only see Org A's groups
        response = client.get("/api/groups", headers=auth_headers(admin_token))
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2
        group_names = {g["name"] for g in data["groups"]}
        assert group_names == {"Group in Org A", "Another Group in Org A"}

        # Verify all groups belong to Org A
        for group in data["groups"]:
            assert group["org_id"] == org_a_id

    def test_list_groups_superadmin_sees_all_orgs(self, client):
        """Superadmin sees groups from all organizations."""
        # Create two orgs with groups
        org_a_id = _create_org("Super Org A", 2)
        org_b_id = _create_org("Super Org B", 1)
        _create_group(org_a_id, "Super Admin Group A")
        _create_group(org_b_id, "Super Admin Group B")

        response = client.get("/api/groups", headers=auth_headers(superadmin_token))
        assert response.status_code == 200
        data = response.json()
        # Should see at least these 2 groups plus any from other tests
        group_names = {g["name"] for g in data["groups"]}
        assert "Super Admin Group A" in group_names
        assert "Super Admin Group B" in group_names

    def test_list_groups_user_no_org_memberships_sees_zero(self, client):
        """User with no org memberships sees 0 groups."""
        # member1 (user 3) is never added to any org
        # Verify they have no org memberships
        conn = _get_db_conn()
        cursor = conn.execute(
            "SELECT COUNT(*) FROM org_members WHERE user_id = 3"
        )
        count = cursor.fetchone()[0]
        conn.close()
        assert count == 0, "member1 should have no org memberships for this test"

        # member1 is a 'member' role, but requires admin role to list groups
        # So we need an admin user with no org memberships - create one
        conn = _get_db_conn()
        conn.execute("PRAGMA foreign_keys = ON")
        pw = hash_password("testpass")
        # Create a new admin user (id=5) with no org memberships
        conn.execute(
            "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
            (5, "orphan_admin", pw, "Orphan Admin", "admin"),
        )
        conn.commit()
        conn.close()

        def orphan_admin_token():
            return create_access_token(5, "orphan_admin", "admin")

        response = client.get("/api/groups", headers=auth_headers(orphan_admin_token))
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert len(data["groups"]) == 0


# =============================================================================
# Test create_group
# =============================================================================


class TestCreateGroup:
    """Tests for POST /groups endpoint."""

    def test_create_group_success(self, client):
        """Admin can create a group in their organization."""
        org_id = _create_org("Create Test Org", 2)

        response = client.post(
            "/api/groups",
            json={
                "name": "New Group",
                "description": "A new test group",
                "org_id": org_id,
            },
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "New Group"
        assert data["description"] == "A new test group"
        assert data["org_id"] == org_id
        assert data["organization_name"] == "Create Test Org"
        assert "id" in data

    def test_create_group_404_nonexistent_org(self, client):
        """Creating group for non-existent org returns 404."""
        response = client.post(
            "/api/groups",
            json={"name": "Orphan Group", "description": "Test", "org_id": 9999},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 404
        assert "Organization not found" in response.json()["detail"]

    def test_create_group_403_non_admin(self, client):
        """Non-admin user gets 403 when creating group."""
        org_id = _create_org("Member Org", 2)
        _add_org_member(org_id, 3, "member")

        response = client.post(
            "/api/groups",
            json={"name": "Member Group", "description": "Test", "org_id": org_id},
            headers=auth_headers(member_token),
        )
        assert response.status_code == 403

    def test_create_group_superadmin_can_create_anywhere(self, client):
        """Superadmin can create group in any organization."""
        org_id = _create_org("Any Org", 2)

        response = client.post(
            "/api/groups",
            json={"name": "Superadmin Group", "description": "Test", "org_id": org_id},
            headers=auth_headers(superadmin_token),
        )
        assert response.status_code == 200

    def test_create_group_admin_must_be_org_member(self, client):
        """Admin must be a member of the org to create groups."""
        # Create org with superadmin so admin1 (user 2) is NOT automatically added
        org_id = _create_org("Other Org", 1)
        # admin1 (user 2) is NOT a member of this org

        response = client.post(
            "/api/groups",
            json={
                "name": "Unauthorized Group",
                "description": "Test",
                "org_id": org_id,
            },
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 403
        assert "member of the organization" in response.json()["detail"]


# =============================================================================
# Test get_group
# =============================================================================


class TestGetGroup:
    """Tests for GET /groups/{group_id} endpoint."""

    def test_get_group_returns_correct_group(self, client):
        """Get group returns the correct group details."""
        org_id = _create_org("Get Test Org", 2)
        group_id = _create_group(org_id, "Specific Group", "A specific description")

        response = client.get(
            f"/api/groups/{group_id}", headers=auth_headers(admin_token)
        )
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == group_id
        assert data["name"] == "Specific Group"
        assert data["description"] == "A specific description"
        assert data["org_id"] == org_id
        assert data["organization_name"] == "Get Test Org"

    def test_get_group_404_nonexistent(self, client):
        """Get group for non-existent group returns 404."""
        response = client.get("/api/groups/9999", headers=auth_headers(admin_token))
        assert response.status_code == 404
        assert "Group not found" in response.json()["detail"]

    def test_get_group_admin_only_403(self, client):
        """Non-admin user gets 403 when getting group."""
        org_id = _create_org("Get Auth Org", 2)
        group_id = _create_group(org_id, "Auth Test Group")

        response = client.get(
            f"/api/groups/{group_id}", headers=auth_headers(member_token)
        )
        assert response.status_code == 403


# =============================================================================
# Test update_group
# =============================================================================


class TestUpdateGroup:
    """Tests for PUT /groups/{group_id} endpoint."""

    def test_update_group_updates_name(self, client):
        """Update group can change the name."""
        org_id = _create_org("Update Test Org", 2)
        group_id = _create_group(org_id, "Old Name", "Original description")

        response = client.put(
            f"/api/groups/{group_id}",
            json={"name": "New Name"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "New Name"
        assert data["description"] == "Original description"

    def test_update_group_updates_description(self, client):
        """Update group can change the description."""
        org_id = _create_org("Desc Update Org", 2)
        group_id = _create_group(org_id, "Test Group", "Old description")

        response = client.put(
            f"/api/groups/{group_id}",
            json={"description": "New description"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Test Group"
        assert data["description"] == "New description"

    def test_update_group_403_non_admin(self, client):
        """Non-admin gets 403 when updating group."""
        org_id = _create_org("Update Auth Org", 2)
        group_id = _create_group(org_id, "Protected Group")

        response = client.put(
            f"/api/groups/{group_id}",
            json={"name": "Hacked"},
            headers=auth_headers(member_token),
        )
        assert response.status_code == 403

    def test_update_group_404_nonexistent(self, client):
        """Update non-existent group returns 404."""
        response = client.put(
            "/api/groups/9999",
            json={"name": "Ghost"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 404

    def test_update_group_admin_must_be_org_member(self, client):
        """Admin must be org member to update group."""
        # Create org with superadmin so admin1 (user 2) is NOT automatically added
        org_id = _create_org("Other Update Org", 1)
        group_id = _create_group(org_id, "Other Group")
        # admin1 (user 2) is NOT a member of this org

        response = client.put(
            f"/api/groups/{group_id}",
            json={"name": "Unauthorized Update"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 403

    def test_update_group_superadmin_can_update_any(self, client):
        """Superadmin can update any group."""
        org_id = _create_org("Super Update Org", 2)
        group_id = _create_group(org_id, "Super Group")

        response = client.put(
            f"/api/groups/{group_id}",
            json={"name": "Updated by Superadmin"},
            headers=auth_headers(superadmin_token),
        )
        assert response.status_code == 200


# =============================================================================
# Test delete_group
# =============================================================================


class TestDeleteGroup:
    """Tests for DELETE /groups/{group_id} endpoint."""

    def test_delete_group_deletes_group(self, client):
        """Delete group removes the group from database."""
        org_id = _create_org("Delete Test Org", 2)
        group_id = _create_group(org_id, "To Delete")

        response = client.delete(
            f"/api/groups/{group_id}", headers=auth_headers(admin_token)
        )
        assert response.status_code == 204

        # Verify group is deleted
        response = client.get(
            f"/api/groups/{group_id}", headers=auth_headers(admin_token)
        )
        assert response.status_code == 404

    def test_delete_group_cascade_group_members(self, client):
        """Delete group cascades to group_members table."""
        org_id = _create_org("Cascade Test Org", 2)
        group_id = _create_group(org_id, "Cascade Group")
        _add_group_member(group_id, 3)
        _add_group_member(group_id, 4)

        # Verify members exist
        conn = _get_db_conn()
        cursor = conn.execute(
            "SELECT COUNT(*) FROM group_members WHERE group_id = ?", (group_id,)
        )
        count = cursor.fetchone()[0]
        conn.close()
        assert count == 2

        # Delete group
        response = client.delete(
            f"/api/groups/{group_id}", headers=auth_headers(admin_token)
        )
        assert response.status_code == 204

        # Verify members are cascade deleted
        conn = _get_db_conn()
        cursor = conn.execute(
            "SELECT COUNT(*) FROM group_members WHERE group_id = ?", (group_id,)
        )
        count = cursor.fetchone()[0]
        conn.close()
        assert count == 0

    def test_delete_group_cascade_vault_group_access(self, client):
        """Delete group cascades to vault_group_access table."""
        org_id = _create_org("Vault Cascade Org", 2)
        group_id = _create_group(org_id, "Vault Access Group")
        vault_id = _create_vault(org_id, "Test Vault")
        _add_vault_group_access(vault_id, group_id, "read")

        # Verify access exists
        conn = _get_db_conn()
        cursor = conn.execute(
            "SELECT COUNT(*) FROM vault_group_access WHERE group_id = ?", (group_id,)
        )
        count = cursor.fetchone()[0]
        conn.close()
        assert count == 1

        # Delete group
        response = client.delete(
            f"/api/groups/{group_id}", headers=auth_headers(admin_token)
        )
        assert response.status_code == 204

        # Verify access is cascade deleted
        conn = _get_db_conn()
        cursor = conn.execute(
            "SELECT COUNT(*) FROM vault_group_access WHERE group_id = ?", (group_id,)
        )
        count = cursor.fetchone()[0]
        conn.close()
        assert count == 0

    def test_delete_group_403_non_admin(self, client):
        """Non-admin gets 403 when deleting group."""
        org_id = _create_org("Delete Auth Org", 2)
        group_id = _create_group(org_id, "Protected Delete Group")

        response = client.delete(
            f"/api/groups/{group_id}", headers=auth_headers(member_token)
        )
        assert response.status_code == 403

    def test_delete_group_404_nonexistent(self, client):
        """Delete non-existent group returns 404."""
        response = client.delete("/api/groups/9999", headers=auth_headers(admin_token))
        assert response.status_code == 404

    def test_delete_group_admin_must_be_org_member(self, client):
        """Admin must be org member to delete group."""
        # Create org with superadmin so admin1 (user 2) is NOT automatically added
        org_id = _create_org("Other Delete Org", 1)
        group_id = _create_group(org_id, "Other Delete Group")
        # admin1 (user 2) is NOT a member

        response = client.delete(
            f"/api/groups/{group_id}", headers=auth_headers(admin_token)
        )
        assert response.status_code == 403


# =============================================================================
# Test get_group_members
# =============================================================================


class TestGetGroupMembers:
    """Tests for GET /groups/{group_id}/members endpoint."""

    def test_get_group_members_returns_members(self, client):
        """Get group members returns list of members."""
        org_id = _create_org("Members Test Org", 2)
        group_id = _create_group(org_id, "Member Group")
        _add_group_member(group_id, 3)
        _add_group_member(group_id, 4)

        response = client.get(
            f"/api/groups/{group_id}/members", headers=auth_headers(admin_token)
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        member_ids = {m["id"] for m in data}
        assert member_ids == {3, 4}

    def test_get_group_members_empty_for_no_members(self, client):
        """Get group members returns empty list for group with no members."""
        org_id = _create_org("Empty Members Org", 2)
        group_id = _create_group(org_id, "Empty Group")

        response = client.get(
            f"/api/groups/{group_id}/members", headers=auth_headers(admin_token)
        )
        assert response.status_code == 200
        data = response.json()
        assert data == []

    def test_get_group_members_includes_username_full_name(self, client):
        """Get group members includes username and full_name."""
        org_id = _create_org("Details Test Org", 2)
        group_id = _create_group(org_id, "Details Group")
        _add_group_member(group_id, 3)

        response = client.get(
            f"/api/groups/{group_id}/members", headers=auth_headers(admin_token)
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        member = data[0]
        assert member["id"] == 3
        assert member["username"] == "member1"
        assert member["full_name"] == "Member One"

    def test_get_group_members_404_nonexistent_group(self, client):
        """Get members for non-existent group returns 404."""
        response = client.get(
            "/api/groups/9999/members", headers=auth_headers(admin_token)
        )
        assert response.status_code == 404

    def test_get_group_members_admin_only_403(self, client):
        """Non-admin gets 403 when getting members."""
        org_id = _create_org("Members Auth Org", 2)
        group_id = _create_group(org_id, "Auth Members Group")

        response = client.get(
            f"/api/groups/{group_id}/members", headers=auth_headers(member_token)
        )
        assert response.status_code == 403


# =============================================================================
# Test update_group_members
# =============================================================================


class TestUpdateGroupMembers:
    """Tests for PUT /groups/{group_id}/members endpoint."""

    def test_update_group_members_replaces_members(self, client):
        """Update group members replaces all existing members."""
        org_id = _create_org("Update Members Org", 2)
        _add_org_member(org_id, 3, "member")
        _add_org_member(org_id, 4, "member")
        group_id = _create_group(org_id, "Update Members Group")
        _add_group_member(group_id, 3)
        _add_group_member(group_id, 4)

        # Replace with just user 4
        response = client.put(
            f"/api/groups/{group_id}/members",
            json={"user_ids": [4]},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == 4

    def test_update_group_members_adds_new_members(self, client):
        """Update group members can add new members."""
        org_id = _create_org("Add Members Org", 2)
        _add_org_member(org_id, 3, "member")
        _add_org_member(org_id, 4, "member")
        group_id = _create_group(org_id, "Add Members Group")

        response = client.put(
            f"/api/groups/{group_id}/members",
            json={"user_ids": [3, 4]},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2

    def test_update_group_members_empty_list_removes_all(self, client):
        """Update with empty user_ids removes all members."""
        org_id = _create_org("Clear Members Org", 2)
        group_id = _create_group(org_id, "Clear Members Group")
        _add_group_member(group_id, 3)

        response = client.put(
            f"/api/groups/{group_id}/members",
            json={"user_ids": []},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200
        data = response.json()
        assert data == []

    def test_update_group_members_validates_users_exist(self, client):
        """Update group members validates that users exist."""
        org_id = _create_org("Valid Users Org", 2)
        group_id = _create_group(org_id, "Valid Users Group")

        response = client.put(
            f"/api/groups/{group_id}/members",
            json={"user_ids": [9999]},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 400
        assert "not found" in response.json()["detail"]

    def test_update_group_members_validates_org_membership(self, client):
        """Update group members validates users are org members."""
        org_id = _create_org("Org Members Test Org", 2)
        group_id = _create_group(org_id, "Org Members Group")
        # member1 (user 3) is NOT an org member

        response = client.put(
            f"/api/groups/{group_id}/members",
            json={"user_ids": [3]},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 400
        assert "not members of this organization" in response.json()["detail"]

    def test_update_group_members_404_nonexistent_group(self, client):
        """Update members for non-existent group returns 404."""
        response = client.put(
            "/api/groups/9999/members",
            json={"user_ids": [3]},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 404

    def test_update_group_members_admin_only_403(self, client):
        """Non-admin gets 403 when updating members."""
        org_id = _create_org("Members Auth Org", 2)
        group_id = _create_group(org_id, "Auth Members Group")

        response = client.put(
            f"/api/groups/{group_id}/members",
            json={"user_ids": [3]},
            headers=auth_headers(member_token),
        )
        assert response.status_code == 403


# =============================================================================
# Test get_group_vaults
# =============================================================================


class TestGetGroupVaults:
    """Tests for GET /groups/{group_id}/vaults endpoint."""

    def test_get_group_vaults_returns_vaults(self, client):
        """Get group vaults returns list of vaults."""
        org_id = _create_org("Vaults Test Org", 2)
        group_id = _create_group(org_id, "Vaults Group")
        vault_id = _create_vault(org_id, "Test Vault 1")
        vault_id2 = _create_vault(org_id, "Test Vault 2")
        _add_vault_group_access(vault_id, group_id, "read")
        _add_vault_group_access(vault_id2, group_id, "write")

        response = client.get(
            f"/api/groups/{group_id}/vaults", headers=auth_headers(admin_token)
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        vault_names = {v["name"] for v in data}
        assert vault_names == {"Test Vault 1", "Test Vault 2"}

    def test_get_group_vaults_empty_for_no_vaults(self, client):
        """Get group vaults returns empty list when no vault access."""
        org_id = _create_org("Empty Vaults Org", 2)
        group_id = _create_group(org_id, "Empty Vaults Group")

        response = client.get(
            f"/api/groups/{group_id}/vaults", headers=auth_headers(admin_token)
        )
        assert response.status_code == 200
        data = response.json()
        assert data == []

    def test_get_group_vaults_404_nonexistent_group(self, client):
        """Get vaults for non-existent group returns 404."""
        response = client.get(
            "/api/groups/9999/vaults", headers=auth_headers(admin_token)
        )
        assert response.status_code == 404

    def test_get_group_vaults_admin_only_403(self, client):
        """Non-admin gets 403 when getting vaults."""
        org_id = _create_org("Vaults Auth Org", 2)
        group_id = _create_group(org_id, "Auth Vaults Group")

        response = client.get(
            f"/api/groups/{group_id}/vaults", headers=auth_headers(member_token)
        )
        assert response.status_code == 403


# =============================================================================
# Test update_group_vaults
# =============================================================================


class TestUpdateGroupVaults:
    """Tests for PUT /groups/{group_id}/vaults endpoint."""

    def test_update_group_vaults_replaces_vault_access(self, client):
        """Update group vaults replaces all existing access."""
        org_id = _create_org("Update Vaults Org", 2)
        group_id = _create_group(org_id, "Update Vaults Group")
        vault1_id = _create_vault(org_id, "Vault One")
        vault2_id = _create_vault(org_id, "Vault Two")
        _add_vault_group_access(vault1_id, group_id, "read")

        # Replace with vault2 only
        response = client.put(
            f"/api/groups/{group_id}/vaults",
            json={"vault_ids": [vault2_id]},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == vault2_id
        assert data[0]["name"] == "Vault Two"

    def test_update_group_vaults_empty_list_removes_all(self, client):
        """Update with empty vault_ids removes all access."""
        org_id = _create_org("Clear Vaults Org", 2)
        group_id = _create_group(org_id, "Clear Vaults Group")
        vault_id = _create_vault(org_id, "To Remove Vault")
        _add_vault_group_access(vault_id, group_id, "read")

        response = client.put(
            f"/api/groups/{group_id}/vaults",
            json={"vault_ids": []},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200
        data = response.json()
        assert data == []

    def test_update_group_vaults_validates_vaults_exist(self, client):
        """Update group vaults validates that vaults exist."""
        org_id = _create_org("Valid Vaults Org", 2)
        group_id = _create_group(org_id, "Valid Vaults Group")

        response = client.put(
            f"/api/groups/{group_id}/vaults",
            json={"vault_ids": [9999]},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 400
        assert "not found" in response.json()["detail"]

    def test_update_group_vaults_validates_same_org(self, client):
        """Update group vaults validates vaults are in same org."""
        org1_id = _create_org("Org One", 2)
        org2_id = _create_org("Org Two", 2)
        group_id = _create_group(org1_id, "Cross Org Group")
        vault_id = _create_vault(org2_id, "Other Org Vault")

        response = client.put(
            f"/api/groups/{group_id}/vaults",
            json={"vault_ids": [vault_id]},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 400
        assert "do not belong to this organization" in response.json()["detail"]

    def test_update_group_vaults_404_nonexistent_group(self, client):
        """Update vaults for non-existent group returns 404."""
        response = client.put(
            "/api/groups/9999/vaults",
            json={"vault_ids": []},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 404

    def test_update_group_vaults_admin_only_403(self, client):
        """Non-admin gets 403 when updating vaults."""
        org_id = _create_org("Vaults Auth Org", 2)
        group_id = _create_group(org_id, "Auth Vaults Group")

        response = client.put(
            f"/api/groups/{group_id}/vaults",
            json={"vault_ids": []},
            headers=auth_headers(member_token),
        )
        assert response.status_code == 403

    def test_update_group_vaults_admin_must_be_org_member(self, client):
        """Admin must be org member to update vaults."""
        # Create org with superadmin so admin1 (user 2) is NOT automatically added
        org_id = _create_org("Other Vaults Org", 1)
        group_id = _create_group(org_id, "Other Vaults Group")
        # admin1 (user 2) is NOT a member

        response = client.put(
            f"/api/groups/{group_id}/vaults",
            json={"vault_ids": []},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 403

    def test_update_group_vaults_superadmin_can_update_any(self, client):
        """Superadmin can update vault access for any group."""
        org_id = _create_org("Super Vaults Org", 2)
        group_id = _create_group(org_id, "Super Vaults Group")
        vault_id = _create_vault(org_id, "Super Vault")

        response = client.put(
            f"/api/groups/{group_id}/vaults",
            json={"vault_ids": [vault_id]},
            headers=auth_headers(superadmin_token),
        )
        assert response.status_code == 200
