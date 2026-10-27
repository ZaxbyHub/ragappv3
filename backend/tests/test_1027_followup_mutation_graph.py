"""Frozen acceptance checks for issue #1028 (spec file named per that issue).

AC1-AC6: the six membership/organization mutation routes PR #1027's scope
boundary excluded must not silently drive a vault to zero EFFECTIVE admins
(direct vault_members admins UNION group-derived admins under the shared
authz_policy inventory). Each test builds a vault whose ONLY effective admin
is a group-derived grant (org -> group -> group member -> vault_group_access
admin, no direct vault_members admin), fires the named mutation, and pins the
refusal: 409 naming the affected vault, whole replacement rolled back for the
replace routes. At the unfixed base each route answers 200 (204 for group
delete), so the status asserts there read ``assert 200 == 409`` /
``assert 204 == 409``.

AC7 regression floor: a vault already at zero effective admins BEFORE the
mutation stays repairable — all six routes keep their normal success status
(guards refuse only on pre-state-non-empty transitions). Acting principal is
superadmin user id 1; AC5 seeds an extra org owner row because
remove_org_member's caller check ignores platform superadmin. No
network/Redis/LanceDB: users_enabled=True in-harness, own temp DB.
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
from app.services.authz_policy import (
    direct_vault_admin_ids,
    group_derived_vault_admin_ids,
)


class _MutationHarness:
    """Full-app harness (test_1025 _InvariantHarness pattern): own temp DB,
    get_db override, csrf override, users/organizations module get_pool
    patches (those routes call module-level get_pool(settings.sqlite_path)),
    settings save/restore, mutable acting-superadmin dict (user id 1)."""

    def __init__(self):
        self.temp_dir = tempfile.mkdtemp(prefix="t1028_")
        self.db_path = os.path.join(self.temp_dir, "t1028.db")
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
        app.dependency_overrides[csrf_protect] = lambda: "t1028-csrf"

        from app.api.routes import organizations as orgs_module
        from app.api.routes import users as users_module

        self._mods = (users_module, orgs_module)
        self._orig_get_pools = [m.get_pool for m in self._mods]
        for m in self._mods:
            m.get_pool = lambda path: self.pool

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

    def request(self, method, path, json=None):
        # raise_server_exceptions=False; the client is closed after the call
        # (the returned Response is already fully read).
        client = TestClient(app, raise_server_exceptions=False)
        client.headers["user-agent"] = ""
        try:
            return client.request(method, path, json=json)
        finally:
            client.close()

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

    def effective_admin_ids(self, vault_id):
        conn = self.pool.get_connection()
        try:
            return (
                direct_vault_admin_ids(conn, vault_id)
                | group_derived_vault_admin_ids(conn, vault_id)
            )
        finally:
            self.pool.release_connection(conn)

    def _build(self, with_group_grant):
        """Org + group in that org + user U in BOTH the org and the group +
        vault V; with_group_grant adds the admin vault_group_access row that
        makes U the vault's ONLY (group-derived) effective admin."""
        org_id = self.insert(
            "INSERT INTO organizations (name, description) VALUES ('T1028 O', '')"
        )
        group_id = self.insert(
            "INSERT INTO groups (name, description, org_id)"
            " VALUES ('T1028 G', '', ?)",
            (org_id,),
        )
        uid = self.insert(
            "INSERT INTO users (username, hashed_password, role, is_active)"
            " VALUES ('t1028u', 'x', 'member', 1)"
        )
        self.insert(
            "INSERT INTO org_members (org_id, user_id, role)"
            " VALUES (?, ?, 'member')",
            (org_id, uid),
        )
        self.insert(
            "INSERT INTO group_members (group_id, user_id) VALUES (?, ?)",
            (group_id, uid),
        )
        vault_id = self.insert(
            "INSERT INTO vaults (name, org_id, visibility)"
            " VALUES ('T1028 V', NULL, 'private')"
        )
        if with_group_grant:
            self.insert(
                "INSERT INTO vault_group_access"
                " (vault_id, group_id, permission, granted_by)"
                " VALUES (?, ?, 'admin', NULL)",
                (vault_id, group_id),
            )
        return org_id, group_id, uid, vault_id

    def build_group_derived_only_vault(self):
        return self._build(with_group_grant=True)

    def build_adminless_vault(self):
        return self._build(with_group_grant=False)

    def close(self):
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_active_user, None)
        app.dependency_overrides.pop(csrf_protect, None)
        for m, orig in zip(self._mods, self._orig_get_pools):
            m.get_pool = orig
        for key, value in self._saved.items():
            setattr(self._settings, key, value)
        self.pool.close_all()
        _pool_cache.clear()
        shutil.rmtree(self.temp_dir, ignore_errors=True)


@pytest.fixture
def mut():
    harness = _MutationHarness()
    yield harness
    harness.close()


def test_group_members_replace_refused(mut):
    _org_id, group_id, uid, vault_id = mut.build_group_derived_only_vault()
    assert mut.effective_admin_ids(vault_id) == {uid}

    response = mut.request("PUT", "/api/groups/%d/members" % group_id, json={"user_ids": []})
    assert response.status_code == 409
    assert str(vault_id) in response.json()["detail"]
    assert mut.count(
        "SELECT COUNT(*) FROM group_members WHERE group_id = ? AND user_id = ?",
        (group_id, uid),
    ) == 1


def test_group_delete_refused(mut):
    _org_id, group_id, uid, vault_id = mut.build_group_derived_only_vault()
    assert mut.effective_admin_ids(vault_id) == {uid}

    response = mut.request("DELETE", "/api/groups/%d" % group_id)
    assert response.status_code == 409
    assert str(vault_id) in response.json()["detail"]
    assert mut.count("SELECT COUNT(*) FROM groups WHERE id = ?", (group_id,)) == 1


def test_user_groups_replace_refused(mut):
    _org_id, group_id, uid, vault_id = mut.build_group_derived_only_vault()
    assert mut.effective_admin_ids(vault_id) == {uid}

    response = mut.request("PUT", "/api/users/%d/groups" % uid, json={"group_ids": []})
    assert response.status_code == 409
    assert str(vault_id) in response.json()["detail"]
    assert mut.count(
        "SELECT COUNT(*) FROM group_members WHERE group_id = ? AND user_id = ?",
        (group_id, uid),
    ) == 1


def test_user_orgs_replace_refused(mut):
    org_id, _group_id, uid, vault_id = mut.build_group_derived_only_vault()
    assert mut.effective_admin_ids(vault_id) == {uid}

    response = mut.request(
        "PUT", "/api/users/%d/organizations" % uid, json={"memberships": []}
    )
    assert response.status_code == 409
    assert str(vault_id) in response.json()["detail"]
    assert mut.count(
        "SELECT COUNT(*) FROM org_members WHERE org_id = ? AND user_id = ?",
        (org_id, uid),
    ) == 1


def test_org_member_remove_refused(mut):
    org_id, _group_id, uid, vault_id = mut.build_group_derived_only_vault()
    # remove_org_member's caller check does not honor platform superadmin:
    # seed an org owner row for the acting user (probe pattern).
    mut.insert(
        "INSERT INTO org_members (org_id, user_id, role) VALUES (?, 1, 'owner')",
        (org_id,),
    )
    assert mut.effective_admin_ids(vault_id) == {uid}

    response = mut.request("DELETE", "/api/organizations/%d/members/%d" % (org_id, uid))
    assert response.status_code == 409
    assert str(vault_id) in response.json()["detail"]
    assert mut.count(
        "SELECT COUNT(*) FROM org_members WHERE org_id = ? AND user_id = ?",
        (org_id, uid),
    ) == 1


def test_org_delete_refused(mut):
    org_id, _group_id, uid, vault_id = mut.build_group_derived_only_vault()
    assert mut.effective_admin_ids(vault_id) == {uid}

    response = mut.request("DELETE", "/api/organizations/%d" % org_id)
    assert response.status_code == 409
    assert str(vault_id) in response.json()["detail"]
    assert mut.count(
        "SELECT COUNT(*) FROM organizations WHERE id = ?", (org_id,)
    ) == 1


def _adminless_leg_status(route):
    """Fresh harness per route so one mutation cannot interfere with the
    next; the vault has zero effective admins BEFORE the call, so every route
    must keep answering its normal success status."""
    harness = _MutationHarness()
    try:
        org_id, group_id, uid, vault_id = harness.build_adminless_vault()
        assert harness.effective_admin_ids(vault_id) == set()
        if route == "org-member-remove":
            # Caller check ignores platform superadmin: owner row for user 1.
            harness.insert(
                "INSERT INTO org_members (org_id, user_id, role)"
                " VALUES (?, 1, 'owner')",
                (org_id,),
            )
            return harness.request(
                "DELETE", "/api/organizations/%d/members/%d" % (org_id, uid)
            ).status_code
        if route == "group-members-clear":
            return harness.request(
                "PUT", "/api/groups/%d/members" % group_id, json={"user_ids": []}
            ).status_code
        if route == "group-delete":
            return harness.request("DELETE", "/api/groups/%d" % group_id).status_code
        if route == "user-groups-clear":
            return harness.request(
                "PUT", "/api/users/%d/groups" % uid, json={"group_ids": []}
            ).status_code
        if route == "user-orgs-clear":
            return harness.request(
                "PUT", "/api/users/%d/organizations" % uid, json={"memberships": []}
            ).status_code
        return harness.request("DELETE", "/api/organizations/%d" % org_id).status_code
    finally:
        harness.close()


def test_prebroken_vaults_stay_repairable():
    expected = {
        "group-members-clear": 200,
        "group-delete": 204,
        "user-groups-clear": 200,
        "user-orgs-clear": 200,
        "org-member-remove": 200,
        "org-delete": 200,
    }
    for route, wanted in expected.items():
        assert _adminless_leg_status(route) == wanted, route
