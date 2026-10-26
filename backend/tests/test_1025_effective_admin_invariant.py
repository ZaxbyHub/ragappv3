"""Frozen acceptance checks for issue #1025 AC3/AC4/AC5 (PRR-010).

Multi-user mode: the last-admin invariant must count EFFECTIVE admins — direct
``vault_members`` admins AND group-derived admins (``vault_group_access`` for a
group whose member also sits in the group's org, exactly what
authz_policy.get_effective_vault_permissions merges). At the base commit the
guards in update_vault_member / remove_vault_member count only direct members,
update/revoke_vault_group_access carry no admin-count check at all, and
delete_user cascades vault_members with no check:

* AC3  downgrade of the ONLY admin coverage (a group grant) -> 200 at base,
  must be refused 409; same for its revocation.
* AC4  a direct-admin demotion with a live group-admin grant is refused 409 at
  base (over-block) because the guard cannot see group-derived admins; it
  must be allowed 200 once one shared effective-admin inventory exists.
* AC5  DELETE /api/users/{id} cascading away the sole direct admin succeeds
  200 at base; the pinned behavior is to refuse 400 naming the affected
  vault. The sibling pins that a delete with another effective admin
  remaining stays allowed (GREEN at base and after the fix).

Acting superadmin is user id 1 (role superadmin); individual tests switch the
acting principal by mutating the harness's acting-user dict.
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


class _InvariantHarness:
    """Full-app harness (d07 pattern): own temp DB, get_db override, csrf
    override, users.get_pool patch, settings save and restore. Multi-user
    mode with a MUTABLE acting-user dict so each test can act as a different
    principal (the override returns the same dict object; tests mutate it in
    place)."""

    def __init__(self):
        self.temp_dir = tempfile.mkdtemp(prefix="t1025inv_")
        self.db_path = os.path.join(self.temp_dir, "t1025inv.db")
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
        app.dependency_overrides[csrf_protect] = lambda: "t1025inv-csrf"

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

    def seed_org_group_member(self, tag, user_id):
        """Org + group in that org + the user in BOTH the org and the group —
        the exact shape authz_policy requires for group-derived access."""
        org_id = self.insert(
            "INSERT INTO organizations (name) VALUES (?)", ("T1025 Org " + tag,)
        )
        group_id = self.insert(
            "INSERT INTO groups (org_id, name) VALUES (?, ?)",
            (org_id, "T1025 Group " + tag),
        )
        self.insert(
            "INSERT INTO org_members (org_id, user_id, role) VALUES (?, ?, 'member')",
            (org_id, user_id),
        )
        self.insert(
            "INSERT INTO group_members (group_id, user_id) VALUES (?, ?)",
            (group_id, user_id),
        )
        return org_id, group_id

    def seed_vault(self, name):
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
def inv():
    harness = _InvariantHarness()
    yield harness
    harness.close()


# AC3 ---------------------------------------------------------------------------


def test_group_grant_downgrade_refused_when_no_direct_admin(inv):
    inv.seed_user(2, "t1025coveredadmin")
    inv.act_as(2, "member")
    org_id, group_id = inv.seed_org_group_member("downgrade", 2)
    vault_id = inv.seed_vault("T1025 Downgrade Vault")
    inv.insert(
        "INSERT INTO vault_group_access (vault_id, group_id, permission, granted_by)"
        " VALUES (?, ?, 'admin', 1)",
        (vault_id, group_id),
    )
    # The group grant is the vault's ONLY admin coverage: no direct admins.
    assert (
        inv.count(
            "SELECT COUNT(*) FROM vault_members"
            " WHERE vault_id = ? AND permission = 'admin'",
            (vault_id,),
        )
        == 0
    )

    client = inv.no_raise()
    try:
        resp = client.patch(
            "/api/vaults/%d/group-access/%d" % (vault_id, group_id),
            json={"permission": "read"},
        )
    finally:
        client.close()
    assert resp.status_code == 409

    row = inv.fetch_one(
        "SELECT permission FROM vault_group_access"
        " WHERE vault_id = ? AND group_id = ?",
        (vault_id, group_id),
    )
    assert row is not None
    assert row[0] == "admin"


def test_group_grant_revoke_refused_when_no_direct_admin(inv):
    inv.seed_user(2, "t1025coveredadmin2")
    inv.act_as(2, "member")
    org_id, group_id = inv.seed_org_group_member("revoke", 2)
    vault_id = inv.seed_vault("T1025 Revoke Vault")
    inv.insert(
        "INSERT INTO vault_group_access (vault_id, group_id, permission, granted_by)"
        " VALUES (?, ?, 'admin', 1)",
        (vault_id, group_id),
    )
    assert (
        inv.count(
            "SELECT COUNT(*) FROM vault_members"
            " WHERE vault_id = ? AND permission = 'admin'",
            (vault_id,),
        )
        == 0
    )

    client = inv.no_raise()
    try:
        resp = client.delete(
            "/api/vaults/%d/group-access/%d" % (vault_id, group_id)
        )
    finally:
        client.close()
    assert resp.status_code == 409

    assert (
        inv.count(
            "SELECT COUNT(*) FROM vault_group_access"
            " WHERE vault_id = ? AND group_id = ?",
            (vault_id, group_id),
        )
        == 1
    )


# AC4 ---------------------------------------------------------------------------


def test_admin_inventory_counts_group_derived_admins(inv):
    inv.seed_user(2, "t1025directadmin")
    inv.seed_user(3, "t1025groupadmin")
    vault_id = inv.seed_vault("T1025 Inventory Vault")
    inv.insert(
        "INSERT INTO vault_members (vault_id, user_id, permission, granted_by)"
        " VALUES (?, 2, 'admin', 1)",
        (vault_id,),
    )
    org_id, group_id = inv.seed_org_group_member("inventory", 3)
    inv.insert(
        "INSERT INTO vault_group_access (vault_id, group_id, permission, granted_by)"
        " VALUES (?, ?, 'admin', 1)",
        (vault_id, group_id),
    )
    # Acting superadmin (user 1) by default.

    client = inv.no_raise()
    try:
        resp = client.patch(
            "/api/vaults/%d/members/2" % vault_id,
            json={"permission": "read"},
        )
    finally:
        client.close()
    assert resp.status_code == 200


# AC5 ---------------------------------------------------------------------------


def test_user_delete_last_vault_admin_behavior_is_pinned(inv):
    inv.seed_user(2, "t1025lastadmin")
    vault_id = inv.seed_vault("T1025 Last-Admin Vault")
    inv.insert(
        "INSERT INTO vault_members (vault_id, user_id, permission, granted_by)"
        " VALUES (?, 2, 'admin', 1)",
        (vault_id,),
    )
    # No group coverage anywhere: user 2 is the sole effective admin.
    assert (
        inv.count(
            "SELECT COUNT(*) FROM vault_group_access WHERE vault_id = ?",
            (vault_id,),
        )
        == 0
    )

    client = inv.no_raise()
    try:
        resp = client.delete("/api/users/2")
    finally:
        client.close()
    assert resp.status_code == 400
    assert str(vault_id) in resp.json()["detail"]


def test_user_delete_allowed_when_other_effective_admin_remains(inv):
    inv.seed_user(2, "t1025adminone")
    inv.seed_user(3, "t1025admintwo")
    vault_id = inv.seed_vault("T1025 Shared-Admin Vault")
    inv.insert(
        "INSERT INTO vault_members (vault_id, user_id, permission, granted_by)"
        " VALUES (?, 2, 'admin', 1)",
        (vault_id,),
    )
    inv.insert(
        "INSERT INTO vault_members (vault_id, user_id, permission, granted_by)"
        " VALUES (?, 3, 'admin', 1)",
        (vault_id,),
    )

    client = inv.no_raise()
    try:
        resp = client.delete("/api/users/2")
    finally:
        client.close()
    assert resp.status_code == 200
    assert resp.json() == {"message": "User deleted", "user_id": 2}
