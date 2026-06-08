"""Tests for per-org role assignment in PUT /users/{user_id}/organizations.

Verifies the canonical ``memberships`` format with per-org roles works correctly,
legacy ``org_ids`` format still works, and invalid roles are rejected.
"""

import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-for-testing-only")
os.environ.setdefault("USERS_ENABLED", "true")

from app.api.routes import users as users_router_module
from app.services.auth_service import create_access_token, hash_password

TEST_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    hashed_password TEXT NOT NULL,
    full_name TEXT DEFAULT '',
    role TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('superadmin','admin','member','viewer')),
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_login_at TIMESTAMP,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until TIMESTAMP
);

CREATE TABLE IF NOT EXISTS organizations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    slug TEXT UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS org_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'member',
    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(org_id, user_id)
);

CREATE TABLE IF NOT EXISTS csrf_tokens (
    token TEXT PRIMARY KEY,
    user_id INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP
);
"""

ADMIN_ID = 1
TARGET_ID = 2
SUPERADMIN_ID = 3
ORG1_ID = 1
ORG2_ID = 2

admin_token = create_access_token(ADMIN_ID, "admin1", "admin")
superadmin_token = create_access_token(SUPERADMIN_ID, "superadmin1", "superadmin")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def setup_db(monkeypatch):
    """Create isolated test DB and patch settings."""
    temp_dir = tempfile.mkdtemp()
    db_path = str(Path(temp_dir) / "app.db")

    from app.models.database import _pool_cache, _pool_cache_lock

    with _pool_cache_lock:
        for pool in list(_pool_cache.values()):
            pool.close_all()
        _pool_cache.clear()

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(TEST_SCHEMA)

    pw = hash_password("Password1!")
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, role) VALUES (?,?,?,?)",
        (ADMIN_ID, "admin1", pw, "admin"),
    )
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, role) VALUES (?,?,?,?)",
        (TARGET_ID, "targetuser", pw, "member"),
    )
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, role) VALUES (?,?,?,?)",
        (SUPERADMIN_ID, "superadmin1", pw, "superadmin"),
    )
    conn.execute(
        "INSERT INTO organizations (id, name, slug) VALUES (?,?,?)",
        (ORG1_ID, "Org1", "org1"),
    )
    conn.execute(
        "INSERT INTO organizations (id, name, slug) VALUES (?,?,?)",
        (ORG2_ID, "Org2", "org2"),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr("app.config.settings.data_dir", Path(temp_dir))
    monkeypatch.setattr("app.config.settings.users_enabled", True)

    yield db_path

    with _pool_cache_lock:
        if db_path in _pool_cache:
            _pool_cache[db_path].close_all()
            del _pool_cache[db_path]
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def client():
    from app.security import csrf_protect
    app = FastAPI()
    app.dependency_overrides[csrf_protect] = lambda: "test-bypass"
    app.include_router(users_router_module.router)  # router has prefix="/users" built-in
    return TestClient(app)


def _db_orgs(setup_db: str, user_id: int) -> dict:
    """Return {org_id: role} from DB directly."""
    conn = sqlite3.connect(setup_db)
    cursor = conn.execute(
        "SELECT org_id, role FROM org_members WHERE user_id = ?", (user_id,)
    )
    result = {row[0]: row[1] for row in cursor.fetchall()}
    conn.close()
    return result


class TestPerOrgRoleMemberships:
    """Canonical memberships format with per-org roles."""

    def test_assign_different_roles_to_two_orgs(self, client, setup_db):
        resp = client.put(
            f"/users/{TARGET_ID}/organizations",
            json={"memberships": [
                {"org_id": ORG1_ID, "role": "admin"},
                {"org_id": ORG2_ID, "role": "member"},
            ]},
            headers=_auth(admin_token),
        )
        assert resp.status_code == 200, resp.text
        orgs = {o["id"]: o["role"] for o in resp.json()["organizations"]}
        assert orgs[ORG1_ID] == "admin"
        assert orgs[ORG2_ID] == "member"

    def test_db_reflects_per_org_roles(self, client, setup_db):
        client.put(
            f"/users/{TARGET_ID}/organizations",
            json={"memberships": [
                {"org_id": ORG1_ID, "role": "admin"},
                {"org_id": ORG2_ID, "role": "member"},
            ]},
            headers=_auth(admin_token),
        )
        db_orgs = _db_orgs(setup_db, TARGET_ID)
        assert db_orgs[ORG1_ID] == "admin"
        assert db_orgs[ORG2_ID] == "member"

    def test_update_changes_existing_role(self, client, setup_db):
        client.put(
            f"/users/{TARGET_ID}/organizations",
            json={"memberships": [{"org_id": ORG1_ID, "role": "member"}]},
            headers=_auth(admin_token),
        )
        resp = client.put(
            f"/users/{TARGET_ID}/organizations",
            json={"memberships": [{"org_id": ORG1_ID, "role": "admin"}]},
            headers=_auth(admin_token),
        )
        assert resp.status_code == 200
        orgs = {o["id"]: o["role"] for o in resp.json()["organizations"]}
        assert orgs[ORG1_ID] == "admin"

    def test_empty_memberships_removes_all(self, client, setup_db):
        client.put(
            f"/users/{TARGET_ID}/organizations",
            json={"memberships": [{"org_id": ORG1_ID, "role": "member"}]},
            headers=_auth(admin_token),
        )
        resp = client.put(
            f"/users/{TARGET_ID}/organizations",
            json={"memberships": []},
            headers=_auth(admin_token),
        )
        assert resp.status_code == 200
        assert resp.json()["organizations"] == []

    def test_invalid_role_owner_returns_400(self, client, setup_db):
        resp = client.put(
            f"/users/{TARGET_ID}/organizations",
            json={"memberships": [{"org_id": ORG1_ID, "role": "owner"}]},
            headers=_auth(admin_token),
        )
        assert resp.status_code == 400
        assert "Invalid role" in resp.json()["detail"]

    def test_invalid_role_superadmin_returns_400(self, client, setup_db):
        resp = client.put(
            f"/users/{TARGET_ID}/organizations",
            json={"memberships": [{"org_id": ORG1_ID, "role": "superadmin"}]},
            headers=_auth(admin_token),
        )
        assert resp.status_code == 400

    def test_nonexistent_org_returns_400(self, client, setup_db):
        resp = client.put(
            f"/users/{TARGET_ID}/organizations",
            json={"memberships": [{"org_id": 9999, "role": "member"}]},
            headers=_auth(admin_token),
        )
        assert resp.status_code == 400
        assert "9999" in resp.json()["detail"]

    def test_nonexistent_user_returns_404(self, client, setup_db):
        resp = client.put(
            "/users/9999/organizations",
            json={"memberships": [{"org_id": ORG1_ID, "role": "member"}]},
            headers=_auth(admin_token),
        )
        assert resp.status_code == 404

    def test_unauthenticated_returns_401(self, client, setup_db):
        resp = client.put(
            f"/users/{TARGET_ID}/organizations",
            json={"memberships": []},
        )
        assert resp.status_code == 401


class TestLegacyOrgIdsFormat:
    """Legacy org_ids format should still work (single role applied to all)."""

    def test_legacy_org_ids_defaults_to_member_role(self, client, setup_db):
        resp = client.put(
            f"/users/{TARGET_ID}/organizations",
            json={"org_ids": [ORG1_ID, ORG2_ID]},
            headers=_auth(admin_token),
        )
        assert resp.status_code == 200, resp.text
        orgs = {o["id"]: o["role"] for o in resp.json()["organizations"]}
        assert orgs[ORG1_ID] == "member"
        assert orgs[ORG2_ID] == "member"

    def test_legacy_org_ids_with_explicit_admin_role(self, client, setup_db):
        resp = client.put(
            f"/users/{TARGET_ID}/organizations",
            json={"org_ids": [ORG1_ID, ORG2_ID], "role": "admin"},
            headers=_auth(admin_token),
        )
        assert resp.status_code == 200
        orgs = {o["id"]: o["role"] for o in resp.json()["organizations"]}
        assert orgs[ORG1_ID] == "admin"
        assert orgs[ORG2_ID] == "admin"

    def test_memberships_takes_precedence_over_org_ids(self, client, setup_db):
        """When both are sent, memberships wins and org_ids is ignored."""
        resp = client.put(
            f"/users/{TARGET_ID}/organizations",
            json={
                "memberships": [{"org_id": ORG1_ID, "role": "admin"}],
                "org_ids": [ORG2_ID],
            },
            headers=_auth(admin_token),
        )
        assert resp.status_code == 200
        orgs = {o["id"]: o["role"] for o in resp.json()["organizations"]}
        assert ORG1_ID in orgs
        assert orgs[ORG1_ID] == "admin"
        assert ORG2_ID not in orgs


class TestDeleteUserOwnerGuard:
    """DELETE /users/{id} must block deletion of org owners."""

    def test_cannot_delete_org_owner(self, client, setup_db):
        # Make TARGET_ID an org owner
        conn = sqlite3.connect(setup_db)
        conn.execute(
            "INSERT INTO org_members (org_id, user_id, role) VALUES (?,?,?)",
            (ORG1_ID, TARGET_ID, "owner"),
        )
        conn.commit()
        conn.close()

        resp = client.delete(
            f"/users/{TARGET_ID}",
            headers=_auth(superadmin_token),
        )
        assert resp.status_code == 400
        assert "transfer ownership" in resp.json()["detail"].lower()

    def test_can_delete_non_owner_user(self, client, setup_db):
        resp = client.delete(
            f"/users/{TARGET_ID}",
            headers=_auth(superadmin_token),
        )
        assert resp.status_code == 200
        assert resp.json()["user_id"] == TARGET_ID

    def test_delete_requires_superadmin(self, client, setup_db):
        resp = client.delete(
            f"/users/{TARGET_ID}",
            headers=_auth(admin_token),
        )
        assert resp.status_code == 403
