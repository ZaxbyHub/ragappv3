"""
Tests for groups API new functionality:
- Pagination (page, per_page)
- Search filtering
- LIKE injection prevention
- create_group auto-assigns org_id from user context
"""

import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.auth import router as auth_router
from app.api.routes.groups import router as groups_router
from app.services.auth_service import (
    compute_client_fingerprint,
    create_access_token,
    hash_password,
)

# Local schema for this file: includes extra org_id column on users for
# org-assignment tests. This is a genuine variation - the test's seed data
# depends on users having org_id as a proper column (not added via ALTER
# which would conflict with the seed-first ordering in setup_db).
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
    locked_until TIMESTAMP,
    org_id INTEGER
);

CREATE TABLE IF NOT EXISTS organizations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT DEFAULT '',
    slug TEXT UNIQUE,
    created_by INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

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

CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE,
    UNIQUE(org_id, name)
);

CREATE TABLE IF NOT EXISTS group_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    UNIQUE(group_id, user_id)
);

CREATE TABLE IF NOT EXISTS vaults (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    owner_id INTEGER,
    org_id INTEGER,
    visibility TEXT DEFAULT 'private' CHECK (visibility IN ('private', 'org', 'public')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

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

-- Access-token denylist (task 1.2 table)
CREATE TABLE IF NOT EXISTS access_token_denylist (
    jti TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_access_token_denylist_expires ON access_token_denylist(expires_at);
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

    # Initialize schema manually
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

    # Seed test users with org_id (for the new auto-assign behavior)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    pw = hash_password("testpass")
    # Create org first
    conn.execute(
        "INSERT INTO organizations (id, name, description, slug, created_by) VALUES (?, ?, ?, ?, ?)",
        (1, "Test Org", "Test Organization", "test-org", 1),
    )
    conn.execute(
        "INSERT INTO organizations (id, name, description, slug, created_by) VALUES (?, ?, ?, ?, ?)",
        (2, "Other Org", "Other Organization", "other-org", 1),
    )
    # Add user-org associations for org_id in token
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, full_name, role, is_active, org_id) VALUES (?, ?, ?, ?, ?, 1, ?)",
        (1, "superadmin", pw, "Super Admin", "superadmin", 1),
    )
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, full_name, role, is_active, org_id) VALUES (?, ?, ?, ?, ?, 1, ?)",
        (2, "admin1", pw, "Admin One", "admin", 1),
    )
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, full_name, role, is_active, org_id) VALUES (?, ?, ?, ?, ?, 1, ?)",
        (3, "admin2", pw, "Admin Two", "admin", 1),
    )
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
        (4, "member1", pw, "Member One", "member"),
    )
    # Add org membership
    conn.execute(
        "INSERT INTO org_members (org_id, user_id, role) VALUES (?, ?, 'owner')",
        (1, 1),
    )
    conn.execute(
        "INSERT INTO org_members (org_id, user_id, role) VALUES (?, ?, 'admin')",
        (1, 2),
    )
    conn.execute(
        "INSERT INTO org_members (org_id, user_id, role) VALUES (?, ?, 'admin')",
        (2, 3),
    )
    conn.commit()
    conn.close()

    yield db_path

    # Cleanup
    with _pool_cache_lock:
        if db_path in _pool_cache:
            _pool_cache[db_path].close_all()
            del _pool_cache[db_path]

    shutil.rmtree(temp_dir, ignore_errors=True)


def _get_db_conn():
    """Get a direct connection to the test database for setup."""
    from app.config import settings

    return sqlite3.connect(str(settings.sqlite_path))


def _create_org(name: str, owner_user_id: int = 1):
    """Create an organization."""
    conn = _get_db_conn()
    conn.execute("PRAGMA foreign_keys = ON")
    cursor = conn.execute(
        "INSERT INTO organizations (name, description, slug, created_by) VALUES (?, ?, ?, ?)",
        (name, "Test org", name.lower().replace(" ", "-"), owner_user_id),
    )
    org_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return org_id


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


def admin1_token():
    """Token for admin1 - org_id=1"""
    return create_access_token(2, "admin1", "admin",
                        client_fingerprint=compute_client_fingerprint(""))


def admin2_token():
    """Token for admin2 - org_id=2"""
    return create_access_token(3, "admin2", "admin",
                        client_fingerprint=compute_client_fingerprint(""))


def superadmin_token():
    """Token for superadmin - org_id=1"""
    return create_access_token(1, "superadmin", "superadmin",
                        client_fingerprint=compute_client_fingerprint(""))


def auth_headers(token_fn):
    return {"Authorization": f"Bearer {token_fn()}"}


@pytest.fixture
def client():
    """Create test client with routers."""
    app = FastAPI()
    app.include_router(auth_router, prefix="/api")
    app.include_router(groups_router, prefix="/api")
    tc = TestClient(app)
    # Override default User-Agent so fingerprint validation matches token
    tc.headers["user-agent"] = ""
    return tc


# =============================================================================
# Test 1: Pagination (page, per_page)
# =============================================================================


class TestPagination:
    """Tests for pagination in list_groups."""

    def test_list_groups_default_pagination(self, client):
        """List groups returns paginated response by default."""
        org_id = 1  # admin1 is a member of org 1
        for i in range(15):
            _create_group(org_id, f"Group {i}")

        response = client.get("/api/groups", headers=auth_headers(admin1_token))
        assert response.status_code == 200
        data = response.json()
        assert "groups" in data
        assert "total" in data
        assert "page" in data
        assert "per_page" in data
        assert data["total"] == 15
        assert data["page"] == 1
        assert data["per_page"] == 100
        # Default per_page=100 should return all 15
        assert len(data["groups"]) == 15

    def test_list_groups_page_parameter(self, client):
        """List groups respects page parameter."""
        org_id = 1  # admin1 is a member of org 1
        for i in range(10):
            _create_group(org_id, f"Group {i}")

        # Request page 2 with per_page=3
        response = client.get(
            "/api/groups?page=2&per_page=3", headers=auth_headers(admin1_token)
        )
        assert response.status_code == 200
        data = response.json()
        assert data["page"] == 2
        assert data["per_page"] == 3
        assert data["total"] == 10
        assert len(data["groups"]) == 3
        # Should have groups 3, 4, 5 (0-indexed: 3, 4, 5)
        assert data["groups"][0]["name"] == "Group 3"

    def test_list_groups_per_page_limit(self, client):
        """List groups caps per_page at 1000."""
        org_id = _create_org("PerPage Limit Org")
        for i in range(5):
            _create_group(org_id, f"Group {i}")

        response = client.get(
            "/api/groups?per_page=5000", headers=auth_headers(admin1_token)
        )
        assert response.status_code == 200
        data = response.json()
        # Should be capped at 1000
        assert data["per_page"] == 1000

    def test_list_groups_page_minimum_1(self, client):
        """List groups ensures page is at least 1."""
        org_id = _create_org("Min Page Org")
        for i in range(5):
            _create_group(org_id, f"Group {i}")

        response = client.get("/api/groups?page=0", headers=auth_headers(admin1_token))
        assert response.status_code == 200
        data = response.json()
        assert data["page"] == 1

    def test_list_groups_empty_page(self, client):
        """List groups returns empty groups for out of range page."""
        org_id = 1  # admin1 is a member of org 1
        _create_group(org_id, "Single Group")

        response = client.get(
            "/api/groups?page=999&per_page=10", headers=auth_headers(admin1_token)
        )
        assert response.status_code == 200
        data = response.json()
        assert data["page"] == 999
        assert data["total"] == 1
        assert data["groups"] == []


# =============================================================================
# Test 2: Search filtering
# =============================================================================


class TestSearch:
    """Tests for search parameter in list_groups."""

    def test_list_groups_search_basic(self, client):
        """List groups filters by name using search parameter."""
        org_id = 1  # admin1 is a member of org 1
        _create_group(org_id, "Alpha Team")
        _create_group(org_id, "Beta Squad")
        _create_group(org_id, "Alpha Leaders")

        response = client.get(
            "/api/groups?search=Alpha", headers=auth_headers(admin1_token)
        )
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2
        names = {g["name"] for g in data["groups"]}
        assert names == {"Alpha Team", "Alpha Leaders"}

    def test_list_groups_search_case_insensitive(self, client):
        """List groups search is case insensitive."""
        org_id = 1  # admin1 is a member of org 1
        _create_group(org_id, "lowercase")
        _create_group(org_id, "UPPERCASE")
        _create_group(org_id, "MixedCase")

        response = client.get(
            "/api/groups?search=UPPER", headers=auth_headers(admin1_token)
        )
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["groups"][0]["name"] == "UPPERCASE"

    def test_list_groups_search_no_results(self, client):
        """List groups returns empty for non-matching search."""
        org_id = _create_org("No Match Org")
        _create_group(org_id, "Group Alpha")
        _create_group(org_id, "Group Beta")

        response = client.get(
            "/api/groups?search=Gamma", headers=auth_headers(admin1_token)
        )
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["groups"] == []

    def test_list_groups_search_partial_match(self, client):
        """List groups search matches partial strings."""
        org_id = 1  # admin1 is a member of org 1
        _create_group(org_id, "Engineering Team")
        _create_group(org_id, "Engineering Managers")
        _create_group(org_id, "Sales Team")

        response = client.get(
            "/api/groups?search=Engine", headers=auth_headers(admin1_token)
        )
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2


# =============================================================================
# Test 3: LIKE injection prevention
# =============================================================================


class TestLikeInjection:
    """Tests for LIKE injection prevention in search."""

    def test_search_literal_percent_matches_percent(self, client):
        """Searching for % matches groups with literal % in name."""
        org_id = 1  # admin1 is a member of org 1
        _create_group(org_id, "100% Complete")
        _create_group(org_id, "Half Done")
        _create_group(org_id, "Zero Percent")

        response = client.get(
            "/api/groups?search=%", headers=auth_headers(admin1_token)
        )
        assert response.status_code == 200
        data = response.json()
        # Should only match the group with literal % in name
        assert data["total"] == 1
        assert data["groups"][0]["name"] == "100% Complete"

    def test_search_literal_underscore_matches_underscore(self, client):
        """Searching for _ matches groups with literal _ in name."""
        org_id = 1  # admin1 is a member of org 1
        _create_group(org_id, "Test_A")
        _create_group(org_id, "TestB")
        _create_group(org_id, "Test_C")

        response = client.get(
            "/api/groups?search=_", headers=auth_headers(admin1_token)
        )
        assert response.status_code == 200
        data = response.json()
        # Should only match groups with literal _
        names = {g["name"] for g in data["groups"]}
        assert "Test_A" in names
        assert "Test_C" in names
        assert "TestB" not in names

    def test_search_escaped_backslash(self, client):
        """Searching for \\ matches groups with literal \\ in name."""
        org_id = 1  # admin1 is a member of org 1
        _create_group(org_id, "path\\to\\file")
        _create_group(org_id, "normal/path")

        response = client.get(
            "/api/groups?search=\\", headers=auth_headers(admin1_token)
        )
        assert response.status_code == 200
        data = response.json()
        # Should only match group with literal backslash
        assert data["total"] == 1
        assert data["groups"][0]["name"] == "path\\to\\file"

    def test_search_wildcards_are_escaped(self, client):
        """Wildcards in search term are treated as literals."""
        org_id = 1  # admin1 is a member of org 1
        _create_group(org_id, "test%value")
        _create_group(org_id, "test_value")
        _create_group(org_id, "testnormal")

        # Search for literal %value
        response = client.get(
            "/api/groups?search=%value", headers=auth_headers(admin1_token)
        )
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["groups"][0]["name"] == "test%value"


# =============================================================================
# Test 4: create_group auto-assigns org_id from user context
# =============================================================================


class TestAutoOrgId:
    """Tests for create_group auto-assigning org_id from user context."""

    def test_create_group_auto_assigns_org_id(self, client):
        """create_group uses user's org_id from token context."""
        # admin1 token has org_id=1 (Test Org)
        response = client.post(
            "/api/groups",
            json={
                "name": "Auto Org Group",
                "description": "Should be in admin's org",
            },
            headers=auth_headers(admin1_token),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["org_id"] == 1  # admin1's org_id
        assert data["organization_name"] == "Test Org"

    def test_create_group_different_user_different_org(self, client):
        """Different admin users get their own org_id assigned."""
        # admin2 token has org_id=2 (Other Org)
        response = client.post(
            "/api/groups",
            json={
                "name": "Admin2 Group",
                "description": "Should be in admin2's org",
            },
            headers=auth_headers(admin2_token),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["org_id"] == 2  # admin2's org_id
        assert data["organization_name"] == "Other Org"

    def test_create_group_explicit_org_id_validated(self, client):
        """Explicit org_id in request body is used but validated to exist."""
        response = client.post(
            "/api/groups",
            json={
                "name": "Test Group",
                "description": "Test",
                "org_id": 999,  # Non-existent org
            },
            headers=auth_headers(admin1_token),
        )
        # Org 999 does not exist; expect 404
        assert response.status_code == 404

    def test_create_group_non_admin_fails(self, client):
        """create_group requires admin role; members get 403."""
        from app.services.auth_service import (
            compute_client_fingerprint,
            create_access_token,
        )

        token_member = create_access_token(4, "member1", "member",
                                      client_fingerprint=compute_client_fingerprint(""))

        response = client.post(
            "/api/groups",
            json={"name": "Should Fail", "description": "No admin"},
            headers={"Authorization": f"Bearer {token_member}"},
        )
        assert response.status_code == 403

    def test_create_group_superadmin_uses_own_org(self, client):
        """Superadmin also gets org_id from their token context."""
        # superadmin token has org_id=1
        response = client.post(
            "/api/groups",
            json={
                "name": "Superadmin Group",
                "description": "Created by superadmin",
            },
            headers=auth_headers(superadmin_token),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["org_id"] == 1


class TestEligibleMembers:
    """Tests for GET /groups/{group_id}/eligible-members endpoint."""

    def _add_org_member(self, org_id: int, user_id: int, role: str = "member"):
        conn = _get_db_conn()
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT OR IGNORE INTO org_members (org_id, user_id, role) VALUES (?, ?, ?)",
            (org_id, user_id, role),
        )
        conn.commit()
        conn.close()

    def test_returns_org_members_only(self, client):
        """Eligible members includes only users in the group's org."""
        # org 1: user 2 (admin1), user 4 (member1) are members
        self._add_org_member(1, 2, "member")
        self._add_org_member(1, 4, "member")
        group_id = _create_group(1, "Test Group Eligible")

        response = client.get(
            f"/api/groups/{group_id}/eligible-members",
            headers=auth_headers(admin1_token),
        )
        assert response.status_code == 200
        data = response.json()
        user_ids = [u["id"] for u in data]
        assert 2 in user_ids
        assert 4 in user_ids
        # user 3 is in org 2, not org 1
        assert 3 not in user_ids

    def test_nonexistent_group_returns_404(self, client):
        """Non-existent group returns 404."""
        response = client.get(
            "/api/groups/9999/eligible-members",
            headers=auth_headers(admin1_token),
        )
        assert response.status_code == 404

    def test_member_role_cannot_access(self, client):
        """Member role cannot access eligible members (403)."""
        group_id = _create_group(1, "Auth Test Group")
        token = create_access_token(4, "user4", "member",
                              client_fingerprint=compute_client_fingerprint(""))

        response = client.get(
            f"/api/groups/{group_id}/eligible-members",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403

    def test_response_shape(self, client):
        """Response items have id, username, full_name fields."""
        self._add_org_member(1, 2, "member")
        group_id = _create_group(1, "Shape Test Group")

        response = client.get(
            f"/api/groups/{group_id}/eligible-members",
            headers=auth_headers(admin1_token),
        )
        assert response.status_code == 200
        for item in response.json():
            assert "id" in item
            assert "username" in item
            assert "full_name" in item
