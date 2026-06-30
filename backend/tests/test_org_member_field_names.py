"""Tests for consistent org member response field names.

Validates that POST /organizations/{id}/members, PATCH /organizations/{id}/members/{user_id},
and GET /organizations/{id}/members all return "user_id" (not "id") in their response.
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest
from backend.tests.schema_constants import TEST_SCHEMA
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
    app.include_router(organizations_router, prefix="/api")
    tc = TestClient(app)
    # Override default User-Agent so fingerprint validation matches token
    tc.headers["user-agent"] = ""
    return tc


class TestAddOrgMemberUserIdField:
    """Tests for POST /organizations/{org_id}/members response field names."""

    def test_add_org_member_response_contains_user_id_key(self, client):
        """add_org_member response must contain 'user_id' key (not 'id')."""
        org_id = _create_org("UserId Test Org", 2)  # admin1 as owner

        response = client.post(
            f"/api/organizations/{org_id}/members",
            json={"user_id": 3, "role": "member"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200
        data = response.json()

        # MUST have 'user_id' key
        assert "user_id" in data, f"Response missing 'user_id' key. Keys: {list(data.keys())}"
        assert data["user_id"] == 3

        # MUST NOT have 'id' key (old incorrect field name)
        assert "id" not in data, "Response should not contain 'id' key (use 'user_id' instead)"

    def test_add_org_member_user_id_value_matches_request(self, client):
        """The user_id in response must match the user_id in the request."""
        org_id = _create_org("UserId Value Org", 2)

        response = client.post(
            f"/api/organizations/{org_id}/members",
            json={"user_id": 4, "role": "admin"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200
        data = response.json()

        # Exact value check
        assert data["user_id"] == 4


class TestUpdateOrgMemberRoleUserIdField:
    """Tests for PATCH /organizations/{org_id}/members/{user_id} response field names."""

    def test_update_org_member_role_response_contains_user_id_key(self, client):
        """update_org_member_role response must contain 'user_id' key (not 'id')."""
        org_id = _create_org("Update UserId Org", 2)  # admin1 as owner
        _add_org_member(org_id, 3, "member")  # member1 as member

        response = client.patch(
            f"/api/organizations/{org_id}/members/3",
            json={"role": "admin"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200
        data = response.json()

        # MUST have 'user_id' key
        assert "user_id" in data, f"Response missing 'user_id' key. Keys: {list(data.keys())}"
        assert data["user_id"] == 3

        # MUST NOT have 'id' key (old incorrect field name)
        assert "id" not in data, "Response should not contain 'id' key (use 'user_id' instead)"

    def test_update_org_member_role_user_id_matches_url_param(self, client):
        """The user_id in response must match the member_user_id in the URL."""
        org_id = _create_org("Update UserId Match Org", 2)
        _add_org_member(org_id, 4, "member")

        response = client.patch(
            f"/api/organizations/{org_id}/members/4",
            json={"role": "admin"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200
        data = response.json()

        # Exact value check - must match URL param
        assert data["user_id"] == 4


class TestListOrgMembersUserIdField:
    """Tests for GET /organizations/{org_id}/members response field names."""

    def test_list_org_members_response_contains_user_id_key(self, client):
        """list_org_members response must contain 'user_id' key (not 'id')."""
        org_id = _create_org("List UserId Org", 2)  # admin1 as owner
        _add_org_member(org_id, 3, "member")
        _add_org_member(org_id, 4, "admin")

        response = client.get(
            f"/api/organizations/{org_id}/members",
            headers=auth_headers(member_token),
        )
        assert response.status_code == 200
        data = response.json()

        assert "members" in data
        members = data["members"]

        # Each member MUST have 'user_id' key
        for member in members:
            assert "user_id" in member, f"Member missing 'user_id' key. Keys: {list(member.keys())}"
            # MUST NOT have bare 'id' key
            assert "id" not in member, "Member should not contain bare 'id' key (use 'user_id' instead)"

        # Verify specific user_ids are present
        user_ids = {m["user_id"] for m in members}
        assert 2 in user_ids  # admin1 (owner)
        assert 3 in user_ids  # member1
        assert 4 in user_ids  # member2

    def test_list_org_members_user_id_values_are_correct(self, client):
        """The user_id values in list response must be correct."""
        org_id = _create_org("List UserId Values Org", 2)
        _add_org_member(org_id, 3, "member")

        response = client.get(
            f"/api/organizations/{org_id}/members",
            headers=auth_headers(member_token),
        )
        assert response.status_code == 200
        data = response.json()

        members = data["members"]
        # Find member1's entry
        member1 = next((m for m in members if m["username"] == "member1"), None)
        assert member1 is not None
        assert member1["user_id"] == 3


class TestOrgMemberFieldConsistencyAcrossEndpoints:
    """Tests that all three org member endpoints use consistent field names."""

    def test_all_three_endpoints_use_user_id_field(self, client):
        """POST, PATCH, and GET /organizations/{id}/members must all use 'user_id' field."""
        org_id = _create_org("Consistency Org", 2)  # admin1 as owner
        _add_org_member(org_id, 3, "member")  # member1 as member

        # 1. POST /organizations/{id}/members - add another member
        post_response = client.post(
            f"/api/organizations/{org_id}/members",
            json={"user_id": 4, "role": "member"},
            headers=auth_headers(admin_token),
        )
        assert post_response.status_code == 200
        post_data = post_response.json()

        # 2. PATCH /organizations/{id}/members/{user_id} - update a role
        patch_response = client.patch(
            f"/api/organizations/{org_id}/members/4",
            json={"role": "admin"},
            headers=auth_headers(admin_token),
        )
        assert patch_response.status_code == 200
        patch_data = patch_response.json()

        # 3. GET /organizations/{id}/members - list members
        get_response = client.get(
            f"/api/organizations/{org_id}/members",
            headers=auth_headers(member_token),
        )
        assert get_response.status_code == 200
        get_data = get_response.json()

        # All three responses must have 'user_id' and NOT 'id'
        for name, data in [("POST", post_data), ("PATCH", patch_data)]:
            assert "user_id" in data, f"{name} response missing 'user_id' key"
            assert "id" not in data, f"{name} response should not have 'id' key"

        for member in get_data["members"]:
            assert "user_id" in member, "GET response member missing 'user_id' key"
            assert "id" not in member, "GET response member should not have 'id' key"

    def test_user_id_field_type_is_integer(self, client):
        """The user_id field must be an integer, not a string."""
        org_id = _create_org("Type Test Org", 2)

        response = client.post(
            f"/api/organizations/{org_id}/members",
            json={"user_id": 3, "role": "member"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200
        data = response.json()

        assert "user_id" in data
        assert isinstance(data["user_id"], int), f"user_id should be int, got {type(data['user_id'])}"

    def test_response_structure_matches_across_endpoints(self, client):
        """POST and PATCH responses should have the same shape (both use user_id)."""
        org_id = _create_org("Shape Test Org", 2)
        _add_org_member(org_id, 3, "member")

        post_response = client.post(
            f"/api/organizations/{org_id}/members",
            json={"user_id": 4, "role": "member"},
            headers=auth_headers(admin_token),
        )
        assert post_response.status_code == 200
        post_data = post_response.json()

        patch_response = client.patch(
            f"/api/organizations/{org_id}/members/3",
            json={"role": "admin"},
            headers=auth_headers(admin_token),
        )
        assert patch_response.status_code == 200
        patch_data = patch_response.json()

        # Both should have the same set of keys
        expected_keys = {"user_id", "username", "full_name", "role", "joined_at"}
        assert set(post_data.keys()) == expected_keys, f"POST keys: {set(post_data.keys())}"
        assert set(patch_data.keys()) == expected_keys, f"PATCH keys: {set(patch_data.keys())}"
