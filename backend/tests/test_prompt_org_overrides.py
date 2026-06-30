"""Tests for per-organization prompt overrides (FR-007 part 2).

Verifies:
- PromptVersionStore: get_for_org / set_org_override / clear_org_override / resolve_for_org
- API endpoints: PUT / DELETE / GET /api/organizations/{org_id}/prompt-override
- Authz: only org admin/owner can set/clear
- Effective resolution: org override > global active > default
"""

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from app.models.database import run_migrations
from app.services.prompt_builder import PromptBuilderService
from app.services.prompt_store import PromptVersionStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_db():
    """Create a temporary DB with the full schema including prompt tables."""
    fd, path = tempfile.mkstemp(suffix=".db")
    # Close the file descriptor immediately; sqlite3.connect reopens it.
    os.close(fd)
    try:
        run_migrations(path)
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        # Insert a test org so FK constraints on prompt_org_overrides pass.
        conn.execute(
            "INSERT INTO organizations (id, name, slug, created_at) "
            "VALUES (?, ?, ?, ?)",
            (10, "Test Org", "test-org", "2024-01-01T00:00:00Z"),
        )
        conn.commit()
        yield conn
    finally:
        conn.close()
        os.unlink(path)


@pytest.fixture
def store(fresh_db):
    """Return a PromptVersionStore backed by the fresh DB."""
    return PromptVersionStore(fresh_db)


# ---------------------------------------------------------------------------
# PromptVersionStore — org override methods
# ---------------------------------------------------------------------------


class TestPromptOverrideStoreQueries:
    """Tests for get_for_org and resolve_for_org."""

    def test_get_for_org_returns_none_when_no_override(self, store):
        """Org with no override returns None from get_for_org."""
        assert store.get_for_org(org_id=999) is None

    def test_get_for_org_returns_override_when_set(self, store):
        """get_for_org returns the override PromptVersion after set_org_override."""
        store.create_version("v1", "v1 content", activate=True)
        store.create_version("v2", "v2 content")
        store.set_org_override(org_id=10, version="v2", set_by="admin")

        result = store.get_for_org(org_id=10)
        assert result is not None
        assert result.version == "v2"
        assert result.content == "v2 content"

    def test_resolve_for_org_returns_override_when_set(self, store):
        """resolve_for_org returns org override when one is set."""
        store.create_version("v1", "v1 global", activate=True)
        store.create_version("v2", "v2 org override")
        store.set_org_override(org_id=10, version="v2")

        result = store.resolve_for_org(org_id=10)
        assert result is not None
        assert result.version == "v2"
        assert result.content == "v2 org override"

    def test_resolve_for_org_returns_global_active_when_no_override(self, store):
        """resolve_for_org falls back to global active when no org override is set."""
        store.create_version("v1", "v1 global", activate=True)

        result = store.resolve_for_org(org_id=999)
        assert result is not None
        assert result.version == "v1"
        assert result.content == "v1 global"

    def test_resolve_for_org_returns_none_when_no_override_and_no_global(self, store):
        """resolve_for_org returns None when no override and no global active."""
        result = store.resolve_for_org(org_id=999)
        assert result is None


class TestPromptOverrideStoreMutations:
    """Tests for set_org_override and clear_org_override."""

    def test_set_org_override_upserts(self, store):
        """Calling set_org_override twice updates the existing override."""
        store.create_version("v1", "v1 content", activate=True)
        store.create_version("v2", "v2 content")

        store.set_org_override(org_id=10, version="v2", set_by="alice")
        assert store.get_for_org(org_id=10).version == "v2"

        # Update to v1
        store.set_org_override(org_id=10, version="v1", set_by="bob")
        assert store.get_for_org(org_id=10).version == "v1"

    def test_set_org_override_records_set_by(self, store):
        """set_org_override records the set_by value."""
        store.create_version("v1", "v1 content", activate=True)
        store.set_org_override(org_id=10, version="v1", set_by="testuser")

        override_row = store._db.execute(
            "SELECT set_by FROM prompt_org_overrides WHERE org_id = ?",
            (10,),
        ).fetchone()
        assert override_row["set_by"] == "testuser"

    def test_set_org_override_raises_for_nonexistent_version(self, store):
        """set_org_override raises ValueError when the version does not exist."""
        store.create_version("v1", "v1 content", activate=True)
        with pytest.raises(ValueError, match="No prompt version"):
            store.set_org_override(org_id=10, version="nonexistent")

    def test_clear_org_override_deletes_existing_override(self, store):
        """clear_org_override removes the override row."""
        store.create_version("v1", "v1 content", activate=True)
        store.set_org_override(org_id=10, version="v1")
        assert store.get_for_org(org_id=10) is not None

        store.clear_org_override(org_id=10)
        assert store.get_for_org(org_id=10) is None

    def test_clear_org_override_is_idempotent(self, store):
        """clear_org_override is safe to call when no override exists."""
        store.create_version("v1", "v1 content", activate=True)
        store.clear_org_override(org_id=999)  # No override ever set
        assert store.resolve_for_org(org_id=999).version == "v1"

    def test_clear_org_override_reverts_to_global_active(self, store):
        """After clearing, resolve_for_org returns the global active version."""
        store.create_version("v1", "v1 global", activate=True)
        store.create_version("v2", "v2 override")
        store.set_org_override(org_id=10, version="v2")
        assert store.resolve_for_org(org_id=10).version == "v2"

        store.clear_org_override(org_id=10)
        assert store.resolve_for_org(org_id=10).version == "v1"


# ---------------------------------------------------------------------------
# PromptBuilderService — org-specific override integration
# ---------------------------------------------------------------------------


class TestPromptBuilderServiceOrgOverride:
    """Tests that build_messages uses system_prompt_override correctly."""

    def test_system_prompt_override_wins_over_constructor(self, fresh_db):
        """system_prompt_override takes absolute precedence over constructor override."""
        store = PromptVersionStore(fresh_db)
        store.create_version("v1", "db v1 content", activate=True)

        builder = PromptBuilderService(
            system_prompt="constructor override",
            db=fresh_db,
        )
        messages = builder.build_messages(
            "hello",
            [],
            [],
            [],
            system_prompt_override="per-query org override",
        )
        # The per-query override should be in the system message
        assert messages[0]["content"] == "per-query org override"

    def test_system_prompt_override_wins_over_cached_db_version(self, fresh_db):
        """system_prompt_override bypasses the builder's cached active version."""
        store = PromptVersionStore(fresh_db)
        store.create_version("v1", "db active content", activate=True)

        builder = PromptBuilderService(db=fresh_db)
        # Trigger lazy resolution and caching
        _ = builder.system_prompt
        assert builder._cached_active_prompt == "db active content"

        # Per-query override should override the cache
        messages = builder.build_messages(
            "hello", [], [], [],
            system_prompt_override="per-query org override",
        )
        assert messages[0]["content"] == "per-query org override"

    def test_no_override_uses_normal_resolution_chain(self, fresh_db):
        """When no override is passed, normal resolution chain applies."""
        store = PromptVersionStore(fresh_db)
        store.create_version("v1", "db active content", activate=True)

        builder = PromptBuilderService(db=fresh_db)
        messages = builder.build_messages("hello", [], [], [])
        assert messages[0]["content"] == "db active content"

    def test_no_override_no_db_uses_built_in_default(self):
        """When no db and no override, built-in default is used."""
        builder = PromptBuilderService()
        messages = builder.build_messages("hello", [], [], [])
        assert "SECURITY BOUNDARY" in messages[0]["content"]


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.auth import router as auth_router
from app.api.routes.organizations import router as organizations_router
from app.services.auth_service import (
    compute_client_fingerprint,
    create_access_token,
    hash_password,
)


class TestPromptOverrideAPIFixtures:
    """Shared fixture setup for API tests."""

    @pytest.fixture(autouse=True)
    def setup_db(self, monkeypatch):
        """Set up test database with full schema + prompt_org_overrides table."""
        temp_dir = tempfile.mkdtemp()
        db_path = str(Path(temp_dir) / "app.db")

        # Clear pool cache
        from app.models.database import _pool_cache, _pool_cache_lock
        with _pool_cache_lock:
            for path_, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()

        # Initialize schema
        run_migrations(db_path)
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
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
            "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (1, "superadmin", pw, "Super Admin", "superadmin"),
        )
        conn.execute(
            "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (2, "admin1", pw, "Admin One", "admin"),
        )
        conn.execute(
            "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (3, "member1", pw, "Member One", "member"),
        )
        conn.execute(
            "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (4, "member2", pw, "Member Two", "member"),
        )
        # Seed test org so FK constraints on prompt_org_overrides pass.
        conn.execute(
            "INSERT INTO organizations (id, name, slug, created_at) "
            "VALUES (?, ?, ?, ?)",
            (10, "Test Org", "test-org", "2024-01-01T00:00:00Z"),
        )
        conn.commit()
        conn.close()

        self._db_path = db_path
        self._temp_dir = temp_dir
        yield db_path

        # Cleanup
        with _pool_cache_lock:
            if db_path in _pool_cache:
                _pool_cache[db_path].close_all()
                del _pool_cache[db_path]
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)

    @pytest.fixture
    def client(self):
        """Create test client with routers."""
        app = FastAPI()
        app.include_router(auth_router, prefix="/api")
        app.include_router(organizations_router, prefix="/api")
        tc = TestClient(app)
        tc.headers["user-agent"] = ""
        return tc

    def _create_org(self, name: str, owner_user_id: int):
        """Create an org with the given user as owner. Returns org_id."""
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        cursor = conn.execute(
            "INSERT INTO organizations (name, description, slug, created_by) "
            "VALUES (?, ?, ?, ?)",
            (name, "desc", name.lower().replace(" ", "-"), owner_user_id),
        )
        org_id = cursor.lastrowid
        conn.execute(
            "INSERT INTO org_members (org_id, user_id, role) VALUES (?, ?, 'owner')",
            (org_id, owner_user_id),
        )
        conn.commit()
        conn.close()
        return org_id

    def _add_org_member(self, org_id: int, user_id: int, role: str):
        """Add a user as a member of an org."""
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT OR IGNORE INTO org_members (org_id, user_id, role) VALUES (?, ?, ?)",
            (org_id, user_id, role),
        )
        conn.commit()
        conn.close()

    def _create_prompt_versions(self):
        """Create two prompt versions for override testing."""
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO prompt_versions (version, content, is_active, created_by) "
            "VALUES (?, ?, ?, ?)",
            ("v1-global", "v1 global content", 1, "setup"),
        )
        conn.execute(
            "INSERT INTO prompt_versions (version, content, is_active, created_by) "
            "VALUES (?, ?, ?, ?)",
            ("v2-override", "v2 override content", 0, "setup"),
        )
        conn.commit()
        conn.close()

    def _token(self, user_id: int, username: str, role: str):
        return create_access_token(
            user_id, username, role,
            client_fingerprint=compute_client_fingerprint(""),
        )

    def _auth_headers(self, user_id: int, username: str, role: str):
        return {"Authorization": f"Bearer {self._token(user_id, username, role)}"}


class TestSetPromptOverrideEndpoint(TestPromptOverrideAPIFixtures):
    """Tests for PUT /api/organizations/{org_id}/prompt-override."""

    def test_owner_can_set_override(self, client):
        """Org owner can set the prompt override."""
        org_id = self._create_org("TestOrg", owner_user_id=3)
        self._create_prompt_versions()

        response = client.put(
            f"/api/organizations/{org_id}/prompt-override",
            json={"version": "v2-override"},
            headers=self._auth_headers(3, "member1", "member"),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["version"] == "v2-override"
        assert data["content"] == "v2 override content"
        assert data["is_override"] is True
        assert data["org_id"] == org_id

    def test_admin_can_set_override(self, client):
        """Org admin can set the prompt override."""
        org_id = self._create_org("TestOrg", owner_user_id=3)
        self._create_prompt_versions()
        # member1 (user 3) is owner; make user 2 (admin1) an admin
        self._add_org_member(org_id, user_id=2, role="admin")

        response = client.put(
            f"/api/organizations/{org_id}/prompt-override",
            json={"version": "v2-override"},
            headers=self._auth_headers(2, "admin1", "admin"),
        )
        assert response.status_code == 200
        assert response.json()["version"] == "v2-override"

    def test_non_admin_member_cannot_set_override(self, client):
        """Org member (non-admin) gets 403 when setting override."""
        org_id = self._create_org("TestOrg", owner_user_id=3)
        self._create_prompt_versions()
        # user 4 is a plain member
        self._add_org_member(org_id, user_id=4, role="member")

        response = client.put(
            f"/api/organizations/{org_id}/prompt-override",
            json={"version": "v2-override"},
            headers=self._auth_headers(4, "member2", "member"),
        )
        assert response.status_code == 403

    def test_nonexistent_version_returns_404(self, client):
        """Setting a nonexistent prompt version returns 404."""
        org_id = self._create_org("TestOrg", owner_user_id=3)
        self._create_prompt_versions()

        response = client.put(
            f"/api/organizations/{org_id}/prompt-override",
            json={"version": "nonexistent"},
            headers=self._auth_headers(3, "member1", "member"),
        )
        assert response.status_code == 404

    def test_org_not_found_returns_404(self, client):
        """PUT on nonexistent org returns 404."""
        self._create_prompt_versions()

        response = client.put(
            "/api/organizations/9999/prompt-override",
            json={"version": "v2-override"},
            headers=self._auth_headers(1, "superadmin", "superadmin"),
        )
        assert response.status_code == 404

    def test_superadmin_can_set_override_for_any_org(self, client):
        """Superadmin can set override for any org without being a member."""
        org_id = self._create_org("TestOrg", owner_user_id=3)
        self._create_prompt_versions()

        response = client.put(
            f"/api/organizations/{org_id}/prompt-override",
            json={"version": "v2-override"},
            headers=self._auth_headers(1, "superadmin", "superadmin"),
        )
        assert response.status_code == 200


class TestClearPromptOverrideEndpoint(TestPromptOverrideAPIFixtures):
    """Tests for DELETE /api/organizations/{org_id}/prompt-override."""

    def test_owner_can_clear_override(self, client):
        """Org owner can clear the prompt override."""
        org_id = self._create_org("TestOrg", owner_user_id=3)
        self._create_prompt_versions()
        # Set override first
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO prompt_org_overrides (org_id, version, set_by) VALUES (?, ?, ?)",
            (org_id, "v2-override", "setup"),
        )
        conn.commit()
        conn.close()

        response = client.delete(
            f"/api/organizations/{org_id}/prompt-override",
            headers=self._auth_headers(3, "member1", "member"),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["version"] == "v1-global"  # Back to global active
        assert data["is_override"] is False

    def test_non_admin_member_cannot_clear_override(self, client):
        """Org member (non-admin) gets 403 when clearing override."""
        org_id = self._create_org("TestOrg", owner_user_id=3)
        self._create_prompt_versions()
        self._add_org_member(org_id, user_id=4, role="member")
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO prompt_org_overrides (org_id, version, set_by) VALUES (?, ?, ?)",
            (org_id, "v2-override", "setup"),
        )
        conn.commit()
        conn.close()

        response = client.delete(
            f"/api/organizations/{org_id}/prompt-override",
            headers=self._auth_headers(4, "member2", "member"),
        )
        assert response.status_code == 403


class TestGetPromptOverrideEndpoint(TestPromptOverrideAPIFixtures):
    """Tests for GET /api/organizations/{org_id}/prompt-override."""

    def test_any_org_member_can_read_effective_version(self, client):
        """Any org member can read the effective version (not just admins)."""
        org_id = self._create_org("TestOrg", owner_user_id=3)
        self._create_prompt_versions()
        # Override set
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO prompt_org_overrides (org_id, version, set_by) VALUES (?, ?, ?)",
            (org_id, "v2-override", "setup"),
        )
        conn.commit()
        conn.close()

        # User 4 is a plain member (not admin)
        self._add_org_member(org_id, user_id=4, role="member")

        response = client.get(
            f"/api/organizations/{org_id}/prompt-override",
            headers=self._auth_headers(4, "member2", "member"),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["version"] == "v2-override"
        assert data["is_override"] is True

    def test_no_override_shows_global_active(self, client):
        """When no override is set, returns global active with is_override=False."""
        org_id = self._create_org("TestOrg", owner_user_id=3)
        self._create_prompt_versions()

        response = client.get(
            f"/api/organizations/{org_id}/prompt-override",
            headers=self._auth_headers(3, "member1", "member"),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["version"] == "v1-global"
        assert data["is_override"] is False

    def test_non_member_gets_403(self, client):
        """User who is not a member of the org gets 403 on GET."""
        org_id = self._create_org("TestOrg", owner_user_id=3)
        self._create_prompt_versions()

        # user 4 is not a member
        response = client.get(
            f"/api/organizations/{org_id}/prompt-override",
            headers=self._auth_headers(4, "member2", "member"),
        )
        assert response.status_code == 403

    def test_superadmin_can_read_any_org(self, client):
        """Superadmin can read effective version of any org."""
        org_id = self._create_org("TestOrg", owner_user_id=3)
        self._create_prompt_versions()

        response = client.get(
            f"/api/organizations/{org_id}/prompt-override",
            headers=self._auth_headers(1, "superadmin", "superadmin"),
        )
        assert response.status_code == 200
        assert response.json()["version"] == "v1-global"


    def test_superadmin_can_clear_override_for_any_org(self, client):
        """Superadmin can clear override for any org without being a member."""
        org_id = self._create_org("TestOrg", owner_user_id=3)
        self._create_prompt_versions()
        # Set override first
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO prompt_org_overrides (org_id, version, set_by) VALUES (?, ?, ?)",
            (org_id, "v2-override", "setup"),
        )
        conn.commit()
        conn.close()

        # Superadmin (user 1) is NOT a member of this org
        response = client.delete(
            f"/api/organizations/{org_id}/prompt-override",
            headers=self._auth_headers(1, "superadmin", "superadmin"),
        )
        assert response.status_code == 200
        assert response.json()["version"] == "v1-global"
        assert response.json()["is_override"] is False

    def test_clear_override_when_no_global_active_returns_404(self, client):
        """DELETE returns 404 when no global active version exists."""
        org_id = self._create_org("TestOrg", owner_user_id=3)
        # No prompt versions seeded; only the override
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO prompt_versions (version, content, is_active, created_by) "
            "VALUES (?, ?, ?, ?)",
            ("orphan", "orphan content", 0, "setup"),
        )
        conn.execute(
            "INSERT INTO prompt_org_overrides (org_id, version, set_by) VALUES (?, ?, ?)",
            (org_id, "orphan", "setup"),
        )
        conn.commit()
        conn.close()

        response = client.delete(
            f"/api/organizations/{org_id}/prompt-override",
            headers=self._auth_headers(3, "member1", "member"),
        )
        assert response.status_code == 404
        assert "no prompt version" in response.json()["detail"].lower()

    def test_nonexistent_version_error_message(self, client):
        """Setting a nonexistent version returns 404 with a specific error message."""
        org_id = self._create_org("TestOrg", owner_user_id=3)
        self._create_prompt_versions()

        response = client.put(
            f"/api/organizations/{org_id}/prompt-override",
            json={"version": "nonexistent-version"},
            headers=self._auth_headers(3, "member1", "member"),
        )
        assert response.status_code == 404
        # Error detail must name the missing version
        assert "nonexistent-version" in response.json()["detail"]


class TestPromptOverrideEffectiveResolution(TestPromptOverrideAPIFixtures):
    """Integration tests for effective prompt resolution (override > global > default)."""

    def test_org_override_wins_over_global_active(self, client):
        """Org override is returned when both override and global active exist."""
        org_id = self._create_org("TestOrg", owner_user_id=3)
        self._create_prompt_versions()
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO prompt_org_overrides (org_id, version, set_by) VALUES (?, ?, ?)",
            (org_id, "v2-override", "setup"),
        )
        conn.commit()
        conn.close()

        response = client.get(
            f"/api/organizations/{org_id}/prompt-override",
            headers=self._auth_headers(3, "member1", "member"),
        )
        assert response.json()["version"] == "v2-override"
        assert response.json()["is_override"] is True

    def test_clear_override_reverts_to_global_active(self, client):
        """After clearing override, effective version returns global active."""
        org_id = self._create_org("TestOrg", owner_user_id=3)
        self._create_prompt_versions()
        # Set override
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO prompt_org_overrides (org_id, version, set_by) VALUES (?, ?, ?)",
            (org_id, "v2-override", "setup"),
        )
        conn.commit()
        conn.close()

        # Clear it
        response = client.delete(
            f"/api/organizations/{org_id}/prompt-override",
            headers=self._auth_headers(3, "member1", "member"),
        )
        assert response.status_code == 200
        assert response.json()["version"] == "v1-global"
        assert response.json()["is_override"] is False


# ---------------------------------------------------------------------------
# Multi-org per-query resolution — two orgs get different prompts
# ---------------------------------------------------------------------------


class TestMultiOrgPerQueryResolution:
    """Tests for per-query org-specific prompt resolution.

    Verifies invariant (f): two different orgs in the same DB each receive
    their own prompt version on a per-query basis (resolve_for_org is called
    per-query from RAGEngine._resolve_effective_prompt, so the store-level
    test is the canonical proof).
    """

    @pytest.fixture
    def multi_org_db(self):
        """DB with two orgs, each with its own override, plus vaults for per-vault resolution."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            run_migrations(path)
            conn = sqlite3.connect(path)
            conn.execute("PRAGMA foreign_keys = ON")
            conn.row_factory = sqlite3.Row
            # Org 1 with override v1-custom
            conn.execute(
                "INSERT INTO organizations (id, name, slug, created_at) "
                "VALUES (?, ?, ?, ?)",
                (1, "Org One", "org-one", "2024-01-01T00:00:00Z"),
            )
            # Org 2 with override v2-custom
            conn.execute(
                "INSERT INTO organizations (id, name, slug, created_at) "
                "VALUES (?, ?, ?, ?)",
                (2, "Org Two", "org-two", "2024-01-01T00:00:00Z"),
            )
            # Global active: v0-global
            conn.execute(
                "INSERT INTO prompt_versions (version, content, is_active, created_by) "
                "VALUES (?, ?, ?, ?)",
                ("v0-global", "global default content", 1, "setup"),
            )
            # v1-custom belongs to org1
            conn.execute(
                "INSERT INTO prompt_versions (version, content, is_active, created_by) "
                "VALUES (?, ?, ?, ?)",
                ("v1-custom", "org1 custom content", 0, "setup"),
            )
            # v2-custom belongs to org2
            conn.execute(
                "INSERT INTO prompt_versions (version, content, is_active, created_by) "
                "VALUES (?, ?, ?, ?)",
                ("v2-custom", "org2 custom content", 0, "setup"),
            )
            # Set org1 override
            conn.execute(
                "INSERT INTO prompt_org_overrides (org_id, version, set_by) "
                "VALUES (?, ?, ?)",
                (1, "v1-custom", "setup"),
            )
            # Set org2 override
            conn.execute(
                "INSERT INTO prompt_org_overrides (org_id, version, set_by) "
                "VALUES (?, ?, ?)",
                (2, "v2-custom", "setup"),
            )
            # Vaults for each org (vault_id → org_id mapping is what _resolve_effective_prompt uses)
            conn.execute(
                "INSERT INTO vaults (id, name, org_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (100, "Vault One", 1, "2024-01-01T00:00:00Z"),
            )
            conn.execute(
                "INSERT INTO vaults (id, name, org_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (200, "Vault Two", 2, "2024-01-01T00:00:00Z"),
            )
            conn.commit()
            yield path
        finally:
            conn.close()
            os.unlink(path)

    def test_different_orgs_get_different_prompts(self, multi_org_db):
        """Per-query resolution: same DB connection, different orgs → different prompts.

        This mirrors what RAGEngine._resolve_effective_prompt does: it opens a
        connection, looks up vault→org, then calls resolve_for_org(org_id).
        """
        conn = sqlite3.connect(multi_org_db)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        try:
            store = PromptVersionStore(conn)

            # Look up org via vault (same as _resolve_effective_prompt does)
            def resolve_via_vault(vault_id: int) -> str:
                row = conn.execute(
                    "SELECT org_id FROM vaults WHERE id = ?",
                    (vault_id,),
                ).fetchone()
                if row is None:
                    return None
                pv = store.resolve_for_org(row["org_id"])
                return pv.content if pv else None

            prompt_for_org1 = resolve_via_vault(100)
            prompt_for_org2 = resolve_via_vault(200)

            assert prompt_for_org1 is not None
            assert prompt_for_org2 is not None
            assert prompt_for_org1 != prompt_for_org2
            assert prompt_for_org1 == "org1 custom content"
            assert prompt_for_org2 == "org2 custom content"
        finally:
            conn.close()

    def test_resolve_for_org_idempotent_per_org(self, multi_org_db):
        """Calling resolve_for_org for the same org twice returns the same version."""
        conn = sqlite3.connect(multi_org_db)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        try:
            store = PromptVersionStore(conn)
            result1 = store.resolve_for_org(org_id=1)
            result2 = store.resolve_for_org(org_id=1)
            assert result1.version == result2.version == "v1-custom"
        finally:
            conn.close()

    def test_org_with_no_override_gets_global_active(self, multi_org_db):
        """Org without an override falls back to the global active version."""
        conn = sqlite3.connect(multi_org_db)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        try:
            store = PromptVersionStore(conn)
            # Org 1 has override → v1-custom
            org1_prompt = store.resolve_for_org(org_id=1)
            assert org1_prompt.version == "v1-custom"

            # Create a brand new org with no override
            conn.execute(
                "INSERT INTO organizations (id, name, slug, created_at) "
                "VALUES (?, ?, ?, ?)",
                (99, "New Org", "new-org", "2024-01-01T00:00:00Z"),
            )
            conn.commit()

            # New org has no override → falls back to global active
            org99_prompt = store.resolve_for_org(org_id=99)
            assert org99_prompt.version == "v0-global"
            assert org99_prompt.content == "global default content"
        finally:
            conn.close()
