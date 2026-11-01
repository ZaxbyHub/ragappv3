"""Frozen acceptance checks for issue #1025 AC1/AC2 (PRR-001).

Single-admin mode (USERS_ENABLED=false): the real admin-token auth path mints
the sentinel principal {"id": 0, "role": "superadmin"} (deps.py
_resolve_active_user), and every vault-sharing write route binds
``granted_by = 0`` — an id with no ``users`` row. Pooled connections enforce
``PRAGMA foreign_keys = ON``, so the INSERT raises IntegrityError and, at the
base commit, the routes answer:

* PUT  /api/vaults/{id}/groups        -> 500 (update_vault_groups re-raises)
* POST /api/vaults/{id}/members       -> 409 "already a member" (misleading;
  nothing exists yet — add_vault_member converts the FK error to 409)
* POST /api/vaults/{id}/group-access  -> 409 "already has access" (same class)

After the fix each route must answer 200 and persist the grant with
``granted_by IS NULL`` (or a real bootstrap user id) — never the sentinel 0.

RED at base for all three checks.
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

_SINGLE_ADMIN_TOKEN = "t1025sa-admin-secret-" + "x" * 48


class _ShareHarness:
    """Full-app harness (d07 pattern): own temp DB, get_db override, csrf
    override, users.get_pool patch, settings save and restore. Single-admin
    mode only — get_current_active_user is NOT overridden so the real
    admin-token path mints the id-0 sentinel principal."""

    def __init__(self):
        self.temp_dir = tempfile.mkdtemp(prefix="t1025sa_")
        self.db_path = os.path.join(self.temp_dir, "t1025sa.db")
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
        app.dependency_overrides[csrf_protect] = lambda: "t1025sa-csrf"

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
        settings.users_enabled = False
        settings.admin_secret_token = _SINGLE_ADMIN_TOKEN
        self.auth = {"Authorization": "Bearer " + _SINGLE_ADMIN_TOKEN}

        self.client = TestClient(app)
        self.client.headers["user-agent"] = ""

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

    def seed_org_and_group(self, tag):
        org_id = self.insert(
            "INSERT INTO organizations (name) VALUES (?)", ("T1025SA Org " + tag,)
        )
        group_id = self.insert(
            "INSERT INTO groups (org_id, name) VALUES (?, ?)",
            (org_id, "T1025SA Group " + tag),
        )
        return org_id, group_id

    def seed_member_user(self, user_id):
        self.insert(
            "INSERT INTO users (id, username, hashed_password, role, is_active)"
            " VALUES (?, ?, 'x', 'member', 1)",
            (user_id, "t1025sauser%d" % user_id),
        )

    def seed_org_less_vault(self, name):
        return self.insert("INSERT INTO vaults (name) VALUES (?)", (name,))

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
def single_admin():
    harness = _ShareHarness()
    yield harness
    harness.close()


# AC1 ---------------------------------------------------------------------------


def test_single_admin_can_grant_group_access(single_admin):
    org_id, group_id = single_admin.seed_org_and_group("replace")
    single_admin.seed_member_user(2)
    vault_id = single_admin.seed_org_less_vault("T1025SA Replace-Set Vault")

    client = single_admin.no_raise()
    try:
        resp = client.put(
            "/api/vaults/%d/groups" % vault_id,
            json={"group_ids": [group_id]},
            headers=single_admin.auth,
        )
    finally:
        client.close()
    assert resp.status_code == 200

    row = single_admin.fetch_one(
        "SELECT permission, granted_by FROM vault_group_access"
        " WHERE vault_id = ? AND group_id = ?",
        (vault_id, group_id),
    )
    assert row is not None
    assert row[1] is None


# AC2 ---------------------------------------------------------------------------


def test_single_admin_can_add_member(single_admin):
    single_admin.seed_member_user(2)
    vault_id = single_admin.seed_org_less_vault("T1025SA Member Vault")

    client = single_admin.no_raise()
    try:
        resp = client.post(
            "/api/vaults/%d/members" % vault_id,
            json={"member_user_id": 2, "permission": "read"},
            headers=single_admin.auth,
        )
    finally:
        client.close()
    assert resp.status_code == 200

    row = single_admin.fetch_one(
        "SELECT permission, granted_by FROM vault_members"
        " WHERE vault_id = ? AND user_id = 2",
        (vault_id,),
    )
    assert row is not None
    assert row[1] is None


# AC1 sibling (per-group grant route, same PRR-001 class) -----------------------


def test_single_admin_can_grant_group_access_per_group_route(single_admin):
    org_id, group_id = single_admin.seed_org_and_group("single")
    single_admin.seed_member_user(2)
    vault_id = single_admin.seed_org_less_vault("T1025SA Single-Grant Vault")

    client = single_admin.no_raise()
    try:
        resp = client.post(
            "/api/vaults/%d/group-access" % vault_id,
            json={"group_id": group_id, "permission": "read"},
            headers=single_admin.auth,
        )
    finally:
        client.close()
    assert resp.status_code == 200

    row = single_admin.fetch_one(
        "SELECT permission, granted_by FROM vault_group_access"
        " WHERE vault_id = ? AND group_id = ?",
        (vault_id, group_id),
    )
    assert row is not None
    assert row[1] is None
