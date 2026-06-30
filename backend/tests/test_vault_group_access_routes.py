"""Tests for vault_group_access API routes."""

import sqlite3
import tempfile
from pathlib import Path

import pytest
from backend.tests.schema_constants import TEST_SCHEMA
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.auth import router as auth_router
from app.api.routes.vault_members import (
    group_access_router,
)
from app.api.routes.vault_members import (
    router as vault_members_router,
)
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
    # Seed test organization
    conn.execute(
        "INSERT INTO organizations (id, name, slug) VALUES (1, 'Test Org', 'test-org')"
    )
    # Seed test groups
    conn.execute("INSERT INTO groups (id, org_id, name) VALUES (1, 1, 'Admins')")
    conn.execute("INSERT INTO groups (id, org_id, name) VALUES (2, 1, 'Developers')")
    conn.execute("INSERT INTO groups (id, org_id, name) VALUES (3, 1, 'Viewers')")
    # Seed test vault
    conn.execute("INSERT INTO vaults (id, name) VALUES (1, 'Test Vault')")
    # Seed admin user as vault admin (needed for vault routes)
    conn.execute(
        "INSERT INTO vault_members (vault_id, user_id, permission, granted_by) VALUES (?, ?, ?, ?)",
        (1, 2, "admin", 1),
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


def superadmin_token():
    return create_access_token(1, "superadmin", "superadmin",
                            client_fingerprint=compute_client_fingerprint(""))


def admin_token():
    return create_access_token(2, "admin1", "admin",
                            client_fingerprint=compute_client_fingerprint(""))


def member_token():
    return create_access_token(3, "member1", "member",
                            client_fingerprint=compute_client_fingerprint(""))


def auth_headers(token_fn):
    return {"Authorization": f"Bearer {token_fn()}"}


@pytest.fixture
def client():
    """Create test client with routers."""
    app = FastAPI()
    app.include_router(auth_router, prefix="/api")
    app.include_router(vault_members_router, prefix="/api")
    app.include_router(group_access_router, prefix="/api")
    tc = TestClient(app)
    # Override default User-Agent so fingerprint validation matches token
    tc.headers["user-agent"] = ""
    return tc


class TestListVaultGroupAccess:
    """Tests for GET /vaults/{vault_id}/group-access endpoint."""

    def test_returns_empty_list_when_no_group_access(self, client):
        """Returns empty list when no group access exists (200)."""
        response = client.get(
            "/api/vaults/1/group-access", headers=auth_headers(admin_token)
        )
        assert response.status_code == 200
        data = response.json()
        assert data["group_access"] == []
        assert data["total"] == 0

    def test_returns_group_access_list(self, client):
        """Returns group access list when entries exist (200)."""
        # Add group access first
        conn = _get_db_conn()
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO vault_group_access (vault_id, group_id, permission, granted_by) VALUES (?, ?, ?, ?)",
            (1, 1, "read", 1),
        )
        conn.commit()
        conn.close()

        response = client.get(
            "/api/vaults/1/group-access", headers=auth_headers(admin_token)
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["group_access"]) == 1
        assert data["group_access"][0]["group_id"] == 1
        assert data["group_access"][0]["group_name"] == "Admins"
        assert data["group_access"][0]["permission"] == "read"
        assert data["total"] == 1

    def test_returns_total_count(self, client):
        """Returns total count accurately."""
        # Add multiple group access entries
        conn = _get_db_conn()
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO vault_group_access (vault_id, group_id, permission, granted_by) VALUES (?, ?, ?, ?)",
            (1, 1, "read", 1),
        )
        conn.execute(
            "INSERT INTO vault_group_access (vault_id, group_id, permission, granted_by) VALUES (?, ?, ?, ?)",
            (1, 2, "write", 1),
        )
        conn.commit()
        conn.close()

        response = client.get(
            "/api/vaults/1/group-access", headers=auth_headers(admin_token)
        )
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2

    def test_returns_404_when_vault_not_found(self, client):
        """Returns 404 when vault does not exist."""
        response = client.get(
            "/api/vaults/999/group-access", headers=auth_headers(admin_token)
        )
        assert response.status_code == 404

    def test_requires_vault_permission(self, client):
        """User without vault permission gets 403."""
        response = client.get(
            "/api/vaults/1/group-access", headers=auth_headers(member_token)
        )
        assert response.status_code == 403


class TestGrantVaultGroupAccessCrossOrg:
    """Tests for cross-org validation in grant_vault_group_access."""

    def test_rejects_cross_org_vault_group_access(self, client):
        """Cross-org vault group access attempt returns 400."""
        # Create second org
        conn = _get_db_conn()
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("INSERT INTO organizations (id, name, slug) VALUES (2, 'Other Org', 'other-org')")
        # Create group in org 2
        conn.execute("INSERT INTO groups (id, org_id, name) VALUES (4, 2, 'OtherAdmins')")
        # Create vault in org 1 (vault 1 is in org 1 via group setup)
        conn.execute("INSERT INTO vaults (id, name, org_id) VALUES (2, 'Org1Vault', 1)")
        conn.commit()
        conn.close()

        # Attempt to grant org 2 group access to org 1 vault -> should fail
        response = client.post(
            "/api/vaults/2/group-access",
            json={"group_id": 4, "permission": "read"},
            headers=auth_headers(superadmin_token),
        )
        assert response.status_code == 400
        assert "different organization" in response.json()["detail"]

    def test_allows_same_org_vault_group_access(self, client):
        """Same-org vault group access succeeds."""
        # Vault 1 has no org_id (NULL) but groups 1-3 are all in org 1
        # Add group 2 (Developers, org 1) to vault 1 -> should succeed
        response = client.post(
            "/api/vaults/1/group-access",
            json={"group_id": 2, "permission": "read"},
            headers=auth_headers(superadmin_token),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["group_id"] == 2
        assert data["group_name"] == "Developers"
        assert data["permission"] == "read"

    def test_allows_null_org_id_vault_accepts_any_org_group(self, client):
        """NULL org_id vault accepts any org group."""
        # Vault 1 has NULL org_id
        # Group 1 is in org 1 -> should succeed
        response = client.post(
            "/api/vaults/1/group-access",
            json={"group_id": 1, "permission": "read"},
            headers=auth_headers(superadmin_token),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["group_id"] == 1

    @pytest.mark.xfail(
        reason="groups.org_id is NOT NULL in the production schema; a null-org-id group cannot be created",
        strict=True,
    )
    def test_allows_null_org_id_group_assigned_to_any_vault(self, client):
        """NULL org_id group can be assigned to any vault."""
        # Create a vault with org_id = 1
        conn = _get_db_conn()
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("INSERT INTO vaults (id, name, org_id) VALUES (3, 'OrgVault', 1)")
        conn.commit()
        conn.close()

        # Create a group with NULL org_id
        # Note: groups table has NOT NULL constraint, so we need to test the schema allows this
        # If the schema is enforced strictly, we test via direct DB insertion bypassing constraints
        conn = _get_db_conn()
        conn.execute("PRAGMA foreign_keys = ON")
        # Insert directly to bypass potential app-level validation
        conn.execute("INSERT INTO groups (id, org_id, name) VALUES (5, NULL, 'GlobalGroup')")
        conn.commit()
        conn.close()

        # Grant NULL org_id group access to org vault -> should succeed per NULL-safe logic
        response = client.post(
            "/api/vaults/3/group-access",
            json={"group_id": 5, "permission": "read"},
            headers=auth_headers(superadmin_token),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["group_id"] == 5


class TestGrantVaultGroupAccess:
    """Tests for POST /vaults/{vault_id}/group-access endpoint."""

    def test_grants_group_access(self, client):
        """Successfully grants group access (200)."""
        response = client.post(
            "/api/vaults/1/group-access",
            json={"group_id": 1, "permission": "read"},
            headers=auth_headers(superadmin_token),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["group_id"] == 1
        assert data["group_name"] == "Admins"
        assert data["permission"] == "read"
        assert "granted_at" in data
        assert data["granted_by"] == 1

    def test_grants_write_permission(self, client):
        """Successfully grants write permission (200)."""
        response = client.post(
            "/api/vaults/1/group-access",
            json={"group_id": 2, "permission": "write"},
            headers=auth_headers(superadmin_token),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["permission"] == "write"

    def test_grants_admin_permission(self, client):
        """Successfully grants admin permission (200)."""
        response = client.post(
            "/api/vaults/1/group-access",
            json={"group_id": 3, "permission": "admin"},
            headers=auth_headers(superadmin_token),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["permission"] == "admin"

    def test_returns_409_on_duplicate(self, client):
        """Returns 409 when group already has access."""
        # Add group access first
        conn = _get_db_conn()
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO vault_group_access (vault_id, group_id, permission, granted_by) VALUES (?, ?, ?, ?)",
            (1, 1, "read", 1),
        )
        conn.commit()
        conn.close()

        response = client.post(
            "/api/vaults/1/group-access",
            json={"group_id": 1, "permission": "write"},
            headers=auth_headers(superadmin_token),
        )
        assert response.status_code == 409
        assert "already has access" in response.json()["detail"]

    def test_returns_404_when_vault_not_found(self, client):
        """Returns 404 when vault does not exist."""
        response = client.post(
            "/api/vaults/999/group-access",
            json={"group_id": 1, "permission": "read"},
            headers=auth_headers(superadmin_token),
        )
        assert response.status_code == 404

    def test_returns_404_when_group_not_found(self, client):
        """Returns 404 when group does not exist."""
        response = client.post(
            "/api/vaults/1/group-access",
            json={"group_id": 999, "permission": "read"},
            headers=auth_headers(superadmin_token),
        )
        assert response.status_code == 404

    def test_rejects_invalid_permission(self, client):
        """Invalid permission is rejected with 422."""
        response = client.post(
            "/api/vaults/1/group-access",
            json={"group_id": 1, "permission": "invalid"},
            headers=auth_headers(superadmin_token),
        )
        assert response.status_code == 422


class TestUpdateVaultGroupAccess:
    """Tests for PATCH /vaults/{vault_id}/group-access/{group_id} endpoint."""

    def test_updates_group_permission(self, client):
        """Successfully updates group permission (200)."""
        # Add group access first
        conn = _get_db_conn()
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO vault_group_access (vault_id, group_id, permission, granted_by) VALUES (?, ?, ?, ?)",
            (1, 1, "read", 1),
        )
        conn.commit()
        conn.close()

        response = client.patch(
            "/api/vaults/1/group-access/1",
            json={"permission": "write"},
            headers=auth_headers(superadmin_token),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["group_id"] == 1
        assert data["permission"] == "write"
        assert data["granted_by"] == 1

    def test_updates_to_admin_permission(self, client):
        """Successfully updates to admin permission (200)."""
        # Add group access first
        conn = _get_db_conn()
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO vault_group_access (vault_id, group_id, permission, granted_by) VALUES (?, ?, ?, ?)",
            (1, 2, "read", 1),
        )
        conn.commit()
        conn.close()

        response = client.patch(
            "/api/vaults/1/group-access/2",
            json={"permission": "admin"},
            headers=auth_headers(superadmin_token),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["permission"] == "admin"

    def test_returns_404_when_vault_not_found(self, client):
        """Returns 404 when vault does not exist."""
        response = client.patch(
            "/api/vaults/999/group-access/1",
            json={"permission": "write"},
            headers=auth_headers(superadmin_token),
        )
        assert response.status_code == 404

    def test_returns_404_when_group_access_not_found(self, client):
        """Returns 404 when group access does not exist."""
        response = client.patch(
            "/api/vaults/1/group-access/999",
            json={"permission": "write"},
            headers=auth_headers(superadmin_token),
        )
        assert response.status_code == 404

    def test_rejects_invalid_permission(self, client):
        """Invalid permission is rejected with 422."""
        # Add group access first
        conn = _get_db_conn()
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO vault_group_access (vault_id, group_id, permission, granted_by) VALUES (?, ?, ?, ?)",
            (1, 1, "read", 1),
        )
        conn.commit()
        conn.close()

        response = client.patch(
            "/api/vaults/1/group-access/1",
            json={"permission": "superadmin"},
            headers=auth_headers(superadmin_token),
        )
        assert response.status_code == 422


class TestRevokeVaultGroupAccess:
    """Tests for DELETE /vaults/{vault_id}/group-access/{group_id} endpoint."""

    def test_revokes_group_access(self, client):
        """Successfully revokes group access (200)."""
        # Add group access first
        conn = _get_db_conn()
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO vault_group_access (vault_id, group_id, permission, granted_by) VALUES (?, ?, ?, ?)",
            (1, 1, "read", 1),
        )
        conn.commit()
        conn.close()

        response = client.delete(
            "/api/vaults/1/group-access/1", headers=auth_headers(superadmin_token)
        )
        assert response.status_code == 200
        data = response.json()
        assert data["message"] == "Group access revoked"
        assert data["vault_id"] == 1
        assert data["group_id"] == 1

    def test_verify_access_removed(self, client):
        """Verifies group access is actually removed from database."""
        # Add group access first
        conn = _get_db_conn()
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO vault_group_access (vault_id, group_id, permission, granted_by) VALUES (?, ?, ?, ?)",
            (1, 2, "write", 1),
        )
        conn.commit()
        conn.close()

        # Revoke access
        response = client.delete(
            "/api/vaults/1/group-access/2", headers=auth_headers(superadmin_token)
        )
        assert response.status_code == 200

        # Verify access is removed
        conn = _get_db_conn()
        cursor = conn.execute(
            "SELECT * FROM vault_group_access WHERE vault_id = 1 AND group_id = 2"
        )
        row = cursor.fetchone()
        conn.close()
        assert row is None

    def test_returns_404_when_vault_not_found(self, client):
        """Returns 404 when vault does not exist."""
        response = client.delete(
            "/api/vaults/999/group-access/1", headers=auth_headers(superadmin_token)
        )
        assert response.status_code == 404

    def test_returns_404_when_group_access_not_found(self, client):
        """Returns 404 when group access does not exist."""
        response = client.delete(
            "/api/vaults/1/group-access/999", headers=auth_headers(superadmin_token)
        )
        assert response.status_code == 404
