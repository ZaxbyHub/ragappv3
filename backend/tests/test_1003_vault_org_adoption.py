"""Frozen acceptance checks for issue #1003 (vault organization adoption).

Multi-user mode. At the base commit ``VaultUpdateRequest`` has no ``org_id``
field (a PUT carrying it is silently dropped), ``update_vault`` applies
``visibility`` with no organization-coherence check, and create accepts
``visibility: "org"`` for a caller with no organization — producing org-less
org-visible vaults that no access-query branch can ever match:

* AC6  PUT /api/vaults/{id} with org_id on an org-less vault returns 200 but
  keeps org_id NULL at base; it must persist and echo the organization.
  Sibling: a non-superadmin vault admin who is NOT a member of the target org
  must be refused 403 (mirroring the create flow's org-membership check).
* AC7  visibility "org" on an org-less vault (update) and on a create by a
  user with no org membership must both be rejected 400 with the row left
  unchanged. Sibling: a combined adoption + org-visibility PUT in one request
  must be allowed once the vault gains the org.
* AC9  PRESERVING (GREEN at base): group-access grants for an org-less
  (global/unscoped) vault keep working — the sanctioned semantics this issue
  must not tighten.
"""

import os
import shutil
import tempfile

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_current_active_user, get_db
from app.main import app
from app.models.database import SQLiteConnectionPool, _pool_cache, init_db
from app.security import csrf_protect


class _AdoptionHarness:
    """Full-app harness (d07 pattern): own temp DB, get_db override, csrf
    override, users.get_pool patch, settings save and restore. Multi-user
    mode with a MUTABLE acting-user dict so each test can act as a different
    principal."""

    def __init__(self):
        self.temp_dir = tempfile.mkdtemp(prefix="t1003_")
        self.db_path = os.path.join(self.temp_dir, "t1003.db")
        init_db(self.db_path)
        _pool_cache.clear()
        self.pool = SQLiteConnectionPool(self.db_path, max_size=3)

        conn = self.pool.get_connection()
        conn.execute(
            "INSERT INTO users (id, username, hashed_password, role, is_active)"
            " VALUES (1, 'superadmin', 'abc123', 'superadmin', 1)"
        )
        conn.commit()
        self.pool.release_connection(conn)

        def override_get_db():
            c = self.pool.get_connection()
            try:
                yield c
            finally:
                self.pool.release_connection(c)

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[csrf_protect] = lambda: "t1003-csrf"

        from app.api.routes import users as users_module

        self._users_module = users_module
        self._orig_get_pool = users_module.get_pool
        users_module.get_pool = lambda path: self.pool

        from app.config import settings

        self._settings = settings
        self._saved = {
            k: getattr(settings, k)
            for k in ("users_enabled", "admin_secret_token", "data_dir")
        }
        settings.data_dir = type(settings.data_dir)(self.temp_dir)
        settings.users_enabled = True

        self.acting_user = {
            "id": 1,
            "username": "superadmin",
            "full_name": "S",
            "role": "superadmin",
            "is_active": True,
            "must_change_password": False,
        }
        app.dependency_overrides[get_current_active_user] = lambda: self.acting_user

        self.client = TestClient(app)
        self.client.headers["user-agent"] = ""

    def act_as(self, user_id, role):
        self.acting_user["id"] = user_id
        self.acting_user["username"] = "user%d" % user_id
        self.acting_user["role"] = role

    def no_raise(self) -> TestClient:
        client = TestClient(app, raise_server_exceptions=False)
        client.headers["user-agent"] = ""
        return client

    def insert(self, statement, params=()):
        conn = self.pool.get_connection()
        try:
            cur = conn.execute(statement, params)
            conn.commit()
            return cur.lastrowid
        finally:
            self.pool.release_connection(conn)

    def count(self, statement, params=()) -> int:
        conn = self.pool.get_connection()
        try:
            return conn.execute(statement, params).fetchone()[0]
        finally:
            self.pool.release_connection(conn)

    def fetch_one(self, statement, params=()):
        conn = self.pool.get_connection()
        try:
            return conn.execute(statement, params).fetchone()
        finally:
            self.pool.release_connection(conn)

    def seed_user(self, user_id, username):
        self.insert(
            "INSERT INTO users (id, username, hashed_password, role, is_active)"
            " VALUES (?, ?, 'x', 'member', 1)",
            (user_id, username),
        )

    def seed_org(self, name):
        return self.insert("INSERT INTO organizations (name) VALUES (?)", (name,))

    def seed_org_less_vault(self, name):
        return self.insert(
            "INSERT INTO vaults (name, visibility) VALUES (?, 'private')", (name,)
        )

    def close(self):
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_active_user, None)
        app.dependency_overrides.pop(csrf_protect, None)
        self._users_module.get_pool = self._orig_get_pool
        for key, value in self._saved.items():
            setattr(self._settings, key, value)
        self.client.close()
        self.pool.close_all()
        _pool_cache.clear()
        shutil.rmtree(self.temp_dir, ignore_errors=True)


@pytest.fixture
def adopt():
    harness = _AdoptionHarness()
    yield harness
    harness.close()


# AC6 ---------------------------------------------------------------------------


def test_update_adopts_org_less_vault(adopt):
    org_id = adopt.seed_org("T1003 Target Org")
    vault_id = adopt.seed_org_less_vault("T1003 Legacy Vault")
    # Acting superadmin (user 1) by default.

    client = adopt.no_raise()
    try:
        resp = client.put(
            "/api/vaults/%d" % vault_id, json={"org_id": org_id}
        )
    finally:
        client.close()
    assert resp.status_code == 200
    assert resp.json()["org_id"] == org_id

    row = adopt.fetch_one("SELECT org_id FROM vaults WHERE id = ?", (vault_id,))
    assert row is not None
    assert row[0] == org_id


def test_update_adoption_requires_org_membership_for_non_superadmin(adopt):
    adopt.seed_user(2, "t1003vaultadmin")
    adopt.act_as(2, "member")
    org_id = adopt.seed_org("T1003 Foreign Org")
    vault_id = adopt.seed_org_less_vault("T1003 Foreign-Adoption Vault")
    adopt.insert(
        "INSERT INTO vault_members (vault_id, user_id, permission, granted_by)"
        " VALUES (?, 2, 'admin', 1)",
        (vault_id,),
    )
    # User 2 is NOT a member of org_id and is not a superadmin.

    client = adopt.no_raise()
    try:
        resp = client.put(
            "/api/vaults/%d" % vault_id, json={"org_id": org_id}
        )
    finally:
        client.close()
    assert resp.status_code == 403


# AC7 ---------------------------------------------------------------------------


def test_org_visibility_requires_org(adopt):
    vault_id = adopt.seed_org_less_vault("T1003 Org-Vis Vault")

    client = adopt.no_raise()
    try:
        resp = client.put(
            "/api/vaults/%d" % vault_id, json={"visibility": "org"}
        )
    finally:
        client.close()
    assert resp.status_code == 400

    row = adopt.fetch_one("SELECT visibility FROM vaults WHERE id = ?", (vault_id,))
    assert row is not None
    assert row[0] == "private"


def test_create_rejects_org_visibility_without_org(adopt):
    # Acting superadmin (user 1) belongs to no organization.
    assert adopt.count("SELECT COUNT(*) FROM org_members WHERE user_id = 1") == 0

    client = adopt.no_raise()
    try:
        resp = client.post(
            "/api/vaults",
            json={"name": "T1003 Org-Vis Create Vault", "visibility": "org"},
        )
    finally:
        client.close()
    assert resp.status_code == 400
    assert (
        adopt.count(
            "SELECT COUNT(*) FROM vaults WHERE name = ?",
            ("T1003 Org-Vis Create Vault",),
        )
        == 0
    )


def test_combined_adoption_and_org_visibility_allowed(adopt):
    org_id = adopt.seed_org("T1003 Combined Org")
    vault_id = adopt.seed_org_less_vault("T1003 Combined Vault")

    client = adopt.no_raise()
    try:
        resp = client.put(
            "/api/vaults/%d" % vault_id,
            json={"org_id": org_id, "visibility": "org"},
        )
    finally:
        client.close()
    assert resp.status_code == 200

    row = adopt.fetch_one(
        "SELECT org_id, visibility FROM vaults WHERE id = ?", (vault_id,)
    )
    assert row is not None
    assert row[0] == org_id
    assert row[1] == "org"


# AC9 (PRESERVING — GREEN at base) ---------------------------------------------


def test_group_access_still_allowed_for_org_less_vault(adopt):
    org_id = adopt.seed_org("T1003 Group Org")
    group_id = adopt.insert(
        "INSERT INTO groups (org_id, name) VALUES (?, ?)",
        (org_id, "T1003 Group"),
    )
    vault_id = adopt.seed_org_less_vault("T1003 Global Vault")

    client = adopt.no_raise()
    try:
        resp = client.post(
            "/api/vaults/%d/group-access" % vault_id,
            json={"group_id": group_id, "permission": "read"},
        )
    finally:
        client.close()
    assert resp.status_code == 200

    row = adopt.fetch_one(
        "SELECT permission, granted_by FROM vault_group_access"
        " WHERE vault_id = ? AND group_id = ?",
        (vault_id, group_id),
    )
    assert row is not None
    assert row[0] == "read"
