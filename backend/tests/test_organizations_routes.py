"""Tests for organizations API routes."""

import sqlite3
import tempfile
from pathlib import Path

import pytest
from backend.tests.schema_constants import TEST_SCHEMA, build_test_schema
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.auth import router as auth_router
from app.api.routes.organizations import router as organizations_router
from app.services.auth_service import (
    compute_client_fingerprint,
    create_access_token,
    hash_password,
)


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
    build_test_schema(conn)
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


def _create_org(name: str, owner_user_id: int, description: str = "Desc"):
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


def _remove_org_member(org_id: int, user_id: int):
    """Remove a member from an organization."""
    conn = _get_db_conn()
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "DELETE FROM org_members WHERE org_id = ? AND user_id = ?",
        (org_id, user_id),
    )
    conn.commit()
    conn.close()


def superadmin_token():
    return create_access_token(1, "superadmin", "superadmin",
                              client_fingerprint=compute_client_fingerprint(""))


def admin_token():
    return create_access_token(2, "admin1", "admin",
                              client_fingerprint=compute_client_fingerprint(""))


def member_token():
    return create_access_token(3, "member1", "member",
                              client_fingerprint=compute_client_fingerprint(""))


def member2_token():
    return create_access_token(4, "member2", "member",
                              client_fingerprint=compute_client_fingerprint(""))


def auth_headers(token_fn):
    return {"Authorization": f"Bearer {token_fn()}"}


@pytest.fixture
def client():
    """Create test client with routers."""

    app = FastAPI()
    app.include_router(auth_router, prefix="/api")
    app.include_router(organizations_router, prefix="/api")
    tc = TestClient(app)
    # Override default User-Agent so fingerprint validation matches token
    tc.headers["user-agent"] = ""
    return tc


class TestCreateOrganization:
    """Tests for POST /organizations endpoint."""

    def test_admin_creates_org(self, client):
        """Admin creates organization (200)."""
        response = client.post(
            "/api/organizations",
            json={"name": "Test Org", "description": "A test organization"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Test Org"
        assert data["slug"] == "test-org"
        assert data["member_count"] == 1
        assert data["vault_count"] == 0

    def test_creator_auto_added_as_owner(self, client):
        """Creator is auto-added as owner."""
        response = client.post(
            "/api/organizations",
            json={"name": "My Org", "description": "Test"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200
        data = response.json()

        # Check member is in org as owner
        conn = _get_db_conn()
        cursor = conn.execute(
            "SELECT role FROM org_members WHERE org_id = ? AND user_id = ?",
            (data["id"], 2),  # admin1 is user_id 2
        )
        row = cursor.fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "owner"

    def test_duplicate_name_rejected(self, client):
        """Duplicate organization name is rejected (409)."""
        client.post(
            "/api/organizations",
            json={"name": "Duplicate Org"},
            headers=auth_headers(admin_token),
        )
        response = client.post(
            "/api/organizations",
            json={"name": "Duplicate Org"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 409

    def test_member_cannot_create(self, client):
        """Member cannot create organization (403)."""
        response = client.post(
            "/api/organizations",
            json={"name": "Should Fail", "description": "Test"},
            headers=auth_headers(member_token),
        )
        assert response.status_code == 403


class TestListOrganizations:
    """Tests for GET /organizations endpoint."""

    def test_admin_sees_all_orgs(self, client):
        """Admin sees all organizations (200)."""
        org1 = _create_org("Admin Org One", 2)  # admin1 as owner
        org2 = _create_org("Admin Org Two", 1)  # superadmin as owner
        _add_org_member(org1, 3, "member")  # member1 is a member of org1

        response = client.get("/api/organizations", headers=auth_headers(admin_token))
        assert response.status_code == 200
        data = response.json()
        assert "organizations" in data
        assert "total" in data
        # admin should see both orgs (all orgs, not just their own)
        assert data["total"] == 2
        org_ids = [o["id"] for o in data["organizations"]]
        assert org1 in org_ids
        assert org2 in org_ids

    def test_member_sees_their_orgs(self, client):
        """Member sees only their organizations (200)."""
        # Create org with admin1 as owner, add member1
        org_id = _create_org("Test Org", 2)  # admin1 (user_id 2)
        _add_org_member(org_id, 3, "member")  # Add member1

        response = client.get("/api/organizations", headers=auth_headers(member_token))
        assert response.status_code == 200
        data = response.json()
        assert "organizations" in data
        assert "total" in data
        assert data["total"] == 1
        assert data["organizations"][0]["id"] == org_id

    def test_empty_list_for_new_user(self, client):
        """New user with no orgs gets empty list (200)."""
        response = client.get("/api/organizations", headers=auth_headers(member2_token))
        assert response.status_code == 200
        data = response.json()
        assert data["organizations"] == []
        assert data["total"] == 0


class TestGetOrganization:
    """Tests for GET /organizations/{org_id} endpoint."""

    def test_member_can_get_org_detail(self, client):
        """Member can get organization detail (200)."""
        org_id = _create_org("Detail Org", 2)  # admin1 as owner
        _add_org_member(org_id, 3, "member")  # member1 as member

        response = client.get(
            f"/api/organizations/{org_id}", headers=auth_headers(member_token)
        )
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Detail Org"
        assert "members" in data
        assert len(data["members"]) == 2

    def test_non_member_rejected(self, client):
        """Non-member is rejected (403)."""
        org_id = _create_org("Private Org", 2)  # admin1 as owner
        # member1 is NOT added
        _add_org_member(org_id, 3, "member")  # Actually add them first
        _remove_org_member(org_id, 3)  # Then remove to make them non-member

        response = client.get(
            f"/api/organizations/{org_id}", headers=auth_headers(member_token)
        )
        assert response.status_code == 403

    def test_nonexistent_org_returns_404(self, client):
        """Non-existent organization returns 404."""
        response = client.get(
            "/api/organizations/999999", headers=auth_headers(member_token)
        )
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()


class TestUpdateOrganization:
    """Tests for PATCH /organizations/{org_id} endpoint."""

    def test_admin_owner_can_update(self, client):
        """Admin/owner can update organization (200)."""
        org_id = _create_org("Update Org", 2)  # admin1 as owner

        response = client.patch(
            f"/api/organizations/{org_id}",
            json={"name": "Updated Org", "description": "New Description"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Updated Org"
        assert data["description"] == "New Description"

    def test_non_admin_member_cannot_update(self, client):
        """Non-admin member cannot update organization (403)."""
        org_id = _create_org("Member Org", 2)  # admin1 as owner
        _add_org_member(org_id, 3, "member")  # member1 as member (not admin)

        response = client.patch(
            f"/api/organizations/{org_id}",
            json={"description": "Should Fail"},
            headers=auth_headers(member_token),
        )
        assert response.status_code == 403

    def test_nonexistent_org_returns_404(self, client):
        """Non-existent organization returns 404."""
        response = client.patch(
            "/api/organizations/999",
            json={"description": "Test"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 404

    def test_duplicate_name_returns_409(self, client):
        """Updating org name to an existing org's name returns 409."""
        _ = _create_org("Unique Org A", 2)
        org2_id = _create_org("Unique Org B", 2)

        response = client.patch(
            f"/api/organizations/{org2_id}",
            json={"name": "Unique Org A"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 409
        assert "conflict" in response.json()["detail"].lower()


class TestAddOrgMember:
    """Tests for POST /organizations/{org_id}/members endpoint."""

    def test_admin_adds_member(self, client):
        """Admin adds member to organization (200)."""
        org_id = _create_org("Add Member Org", 2)  # admin1 as owner

        response = client.post(
            f"/api/organizations/{org_id}/members",
            json={"user_id": 3, "role": "member"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["user_id"] == 3
        assert data["role"] == "member"

    def test_non_admin_rejected(self, client):
        """Non-admin cannot add members (403)."""
        org_id = _create_org("Non Admin Org", 2)  # admin1 as owner
        _add_org_member(org_id, 3, "member")  # member1 as regular member

        response = client.post(
            f"/api/organizations/{org_id}/members",
            json={"user_id": 4, "role": "member"},
            headers=auth_headers(member_token),
        )
        assert response.status_code == 403

    def test_admin_cannot_assign_owner_role(self, client):
        """Admin cannot assign owner role (403).

        Note: When admin1 creates org, they become owner automatically.
        So this test uses superadmin to create org (no auto-owner),
        then admin1 tries to assign owner role.
        """
        # superadmin creates org - they are NOT added as member automatically
        conn = _get_db_conn()
        conn.execute("PRAGMA foreign_keys = ON")
        cursor = conn.execute(
            "INSERT INTO organizations (name, description, slug, created_by) VALUES (?, ?, ?, ?)",
            ("Owner Test Org", "Desc", "owner-test-org", 1),
        )
        org_id = cursor.lastrowid
        # Add admin1 as admin (not owner)
        conn.execute(
            "INSERT INTO org_members (org_id, user_id, role) VALUES (?, ?, 'admin')",
            (org_id, 2),
        )
        conn.commit()
        conn.close()

        response = client.post(
            f"/api/organizations/{org_id}/members",
            json={"user_id": 3, "role": "owner"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 403

    def test_owner_can_assign_owner_role(self, client):
        """Owner CAN assign owner role (200)."""
        org_id = _create_org("Owner Assign Org", 2)  # admin1 is owner

        # admin1 is the owner, so they can add another owner
        response = client.post(
            f"/api/organizations/{org_id}/members",
            json={"user_id": 3, "role": "owner"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200

    def test_duplicate_member_returns_409(self, client):
        """Adding a user already in the org returns 409."""
        org_id = _create_org("Dup Member Org", 2)  # admin1 as owner
        _add_org_member(org_id, 3, "member")  # add member1

        response = client.post(
            f"/api/organizations/{org_id}/members",
            json={"user_id": 3, "role": "member"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 409
        assert "already a member" in response.json()["detail"].lower()


class TestUpdateOrgMemberRole:
    """Tests for PATCH /organizations/{org_id}/members/{member_user_id} endpoint."""

    def test_admin_changes_member_to_admin(self, client):
        """Admin changes member role to admin (200)."""
        org_id = _create_org("Role Update Org", 2)  # admin1 as owner
        _add_org_member(org_id, 3, "member")  # member1 as member

        response = client.patch(
            f"/api/organizations/{org_id}/members/3",
            json={"role": "admin"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["role"] == "admin"

    def test_cannot_change_owner_role(self, client):
        """Cannot change owner's role (403)."""
        org_id = _create_org("Owner Role Org", 2)  # admin1 as owner

        response = client.patch(
            f"/api/organizations/{org_id}/members/2",
            json={"role": "member"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 403


class TestRemoveOrgMember:
    """Tests for DELETE /organizations/{org_id}/members/{member_user_id} endpoint."""

    def test_admin_removes_member(self, client):
        """Admin removes member (200)."""
        org_id = _create_org("Remove Member Org", 2)  # admin1 as owner
        _add_org_member(org_id, 3, "member")  # member1 as member

        response = client.delete(
            f"/api/organizations/{org_id}/members/3",
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["message"] == "Member removed"

    def test_cannot_remove_owner(self, client):
        """Cannot remove owner (403)."""
        org_id = _create_org("Remove Owner Org", 2)  # admin1 as owner

        response = client.delete(
            f"/api/organizations/{org_id}/members/2",
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 403

    def test_self_removal_rejected(self, client):
        """Self-removal is rejected (403).

        If you're the owner, you can't remove yourself (403 - cannot remove owner).
        If you're an admin/member, you can't remove yourself (403 - not admin/owner).
        """
        # Create org where member1 is a regular member (not owner)
        org_id = _create_org("Self Remove Org", 2)  # admin1 as owner first
        _add_org_member(org_id, 3, "member")  # member1 as member (not owner)

        # member1 tries to remove themselves - they get 403 because they're not admin/owner
        response = client.delete(
            f"/api/organizations/{org_id}/members/3",
            headers=auth_headers(member_token),
        )
        assert response.status_code == 403


class TestDeleteOrganization:
    """Tests for DELETE /organizations/{org_id} endpoint."""

    def test_superadmin_deletes_org(self, client):
        """Superadmin deletes organization (200)."""
        org_id = _create_org("Delete Me Org", 2)  # admin1 as owner

        response = client.delete(
            f"/api/organizations/{org_id}", headers=auth_headers(superadmin_token)
        )
        assert response.status_code == 200
        data = response.json()
        assert data["message"] == "Organization deleted"

    def test_admin_cannot_delete(self, client):
        """Admin cannot delete organization (403)."""
        org_id = _create_org("No Delete Org", 2)  # admin1 as owner

        response = client.delete(
            f"/api/organizations/{org_id}", headers=auth_headers(admin_token)
        )
        assert response.status_code == 403

    def test_nonexistent_returns_404(self, client):
        """Non-existent organization returns 404."""
        response = client.delete(
            "/api/organizations/999", headers=auth_headers(superadmin_token)
        )
        assert response.status_code == 404


class TestTransferOwnership:
    """Tests for POST /organizations/{org_id}/transfer-ownership endpoint."""

    def test_owner_transfers_ownership(self, client):
        """Organization owner can transfer ownership to another member (200)."""
        org_id = _create_org("Transfer Org", 2)  # admin1 (user 2) is owner
        _add_org_member(org_id, 3, "member")  # member1 (user 3) as member

        response = client.post(
            f"/api/organizations/{org_id}/transfer-ownership",
            json={"new_owner_user_id": 3},
            headers=auth_headers(admin_token),  # admin1 is current owner
        )
        assert response.status_code == 200
        data = response.json()
        assert data["new_owner_user_id"] == 3

        # Verify DB state: user 3 is now owner, user 2 is admin
        conn = _get_db_conn()
        row2 = conn.execute(
            "SELECT role FROM org_members WHERE org_id=? AND user_id=2", (org_id,)
        ).fetchone()
        row3 = conn.execute(
            "SELECT role FROM org_members WHERE org_id=? AND user_id=3", (org_id,)
        ).fetchone()
        conn.close()
        assert row2[0] == "admin"
        assert row3[0] == "owner"

    def test_superadmin_can_transfer_any_org(self, client):
        """Superadmin can transfer ownership of any org (200)."""
        org_id = _create_org("Superadmin Transfer Org", 2)
        _add_org_member(org_id, 3, "member")

        response = client.post(
            f"/api/organizations/{org_id}/transfer-ownership",
            json={"new_owner_user_id": 3},
            headers=auth_headers(superadmin_token),
        )
        assert response.status_code == 200

    def test_non_owner_cannot_transfer(self, client):
        """A regular admin (not owner) cannot transfer ownership (403)."""
        org_id = _create_org("No Transfer Org", 2)  # admin1 is owner
        _add_org_member(org_id, 3, "admin")  # member1 as admin (not owner)

        response = client.post(
            f"/api/organizations/{org_id}/transfer-ownership",
            json={"new_owner_user_id": 1},
            headers=auth_headers(member_token),  # member1 is admin, not owner
        )
        assert response.status_code == 403

    def test_cannot_transfer_to_non_member(self, client):
        """Cannot transfer ownership to a user not in the org (400)."""
        org_id = _create_org("Non Member Transfer Org", 2)
        # user 4 is NOT a member of this org

        response = client.post(
            f"/api/organizations/{org_id}/transfer-ownership",
            json={"new_owner_user_id": 4},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 400

    def test_cannot_transfer_to_self(self, client):
        """Cannot transfer ownership to yourself (400)."""
        org_id = _create_org("Self Transfer Org", 2)

        response = client.post(
            f"/api/organizations/{org_id}/transfer-ownership",
            json={"new_owner_user_id": 2},  # admin1 transferring to themselves
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 400

    def test_nonexistent_org_returns_404(self, client):
        """Non-existent org returns 404."""
        response = client.post(
            "/api/organizations/999/transfer-ownership",
            json={"new_owner_user_id": 3},
            headers=auth_headers(superadmin_token),
        )
        assert response.status_code == 404

    def test_cannot_transfer_to_inactive_user(self, client):
        """Cannot transfer ownership to an inactive user (400)."""
        org_id = _create_org("Inactive Transfer Org", 2)  # admin1 is owner
        _add_org_member(org_id, 3, "member")  # member1 is a member but will be deactivated
        # Deactivate member1
        conn = _get_db_conn()
        conn.execute("UPDATE users SET is_active = 0 WHERE id = 3")
        conn.commit()
        conn.close()

        response = client.post(
            f"/api/organizations/{org_id}/transfer-ownership",
            json={"new_owner_user_id": 3},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 400
        assert "active" in response.json()["detail"].lower()
