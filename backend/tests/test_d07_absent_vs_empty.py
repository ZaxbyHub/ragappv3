"""Acceptance checks for issue #725 (trace 725-726-d07-d08).

RED at base for C1-C6, C8, C9; C7 and C10 are upstream-fixed pins (GREEN at
base, stay GREEN after the fix).

C1  test_single_admin_accessible_vaults_lists_all
C2  test_single_admin_can_create_vault
C3  test_default_vault_identity_survives_rename
C4  test_replace_set_with_omitted_fields_does_not_clear
C5  test_list_users_rejects_negative_limit
C6  test_vault_list_never_truncates_silently
C7  test_update_user_rejects_short_username (pin, fixed upstream by #997)
C8  test_replace_set_with_duplicate_ids_dedupes
C9  test_create_vault_failure_is_logged
C10 test_delete_vault_error_does_not_echo_exception_text (pin, fixed by #723)
"""

import logging
import os
import shutil
import sqlite3
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.api.deps import (
    assign_user_to_default_vault,
    get_current_active_user,
    get_db,
    get_vector_store,
)
from app.main import app
from app.models.database import SQLiteConnectionPool, _pool_cache, init_db
from app.security import csrf_protect

_SINGLE_ADMIN_TOKEN = "d07-admin-secret-" + "x" * 48


class _FailingExecuteProxy:
    """Delegate everything to the real connection except the needle SQL.

    The first execute whose SQL contains the needle raises the injected
    exception (C9) instead of running it, mirroring the issue513_checks
    connection-factory technique.
    """

    def __init__(self, real, needle, exc):
        self._real = real
        self._needle = needle
        self._exc = exc

    def execute(self, sql, params=()):
        if self._needle in sql:
            raise self._exc
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


class _D07Harness:
    """Full-app harness (phase2-probe pattern): own temp DB, get_db override,
    csrf override, users.get_pool patch, settings save and restore."""

    def __init__(self, users_enabled: bool):
        self.temp_dir = tempfile.mkdtemp(prefix="d07_")
        self.db_path = os.path.join(self.temp_dir, "d07.db")
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

        # Optional per-request connection factory (C9 and C10 fault injection).
        self._conn_factory = None

        def override_get_db():
            if self._conn_factory is not None:
                raw = sqlite3.connect(self.db_path, check_same_thread=False)
                raw.execute("PRAGMA foreign_keys = ON")
                wrapped = self._conn_factory(raw)
                try:
                    yield wrapped
                finally:
                    try:
                        raw.rollback()
                    except sqlite3.Error:
                        pass
                    raw.close()
                return
            c = self.pool.get_connection()
            try:
                yield c
            finally:
                self.pool.release_connection(c)

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[csrf_protect] = lambda: "d07-csrf"

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

        self.client = TestClient(app)
        self.client.headers["user-agent"] = ""
        if users_enabled:
            settings.users_enabled = True
            self.auth = {}
            app.dependency_overrides[get_current_active_user] = lambda: {
                "id": 1,
                "username": "superadmin",
                "full_name": "S",
                "role": "superadmin",
                "is_active": True,
                "must_change_password": False,
            }
        else:
            settings.users_enabled = False
            settings.admin_secret_token = _SINGLE_ADMIN_TOKEN
            self.auth = {"Authorization": "Bearer " + _SINGLE_ADMIN_TOKEN}

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

    def seed_org_and_group(self, suffix):
        org_id = self.insert(
            "INSERT INTO organizations (name) VALUES (?)", ("D07 Org " + suffix,)
        )
        group_id = self.insert(
            "INSERT INTO groups (org_id, name) VALUES (?, ?)",
            (org_id, "D07 Group " + suffix),
        )
        return org_id, group_id

    def close(self):
        self._conn_factory = None
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_active_user, None)
        app.dependency_overrides.pop(csrf_protect, None)
        app.dependency_overrides.pop(get_vector_store, None)
        self._users_module.get_pool = self._orig_get_pool
        for key, value in self._saved.items():
            setattr(self._settings, key, value)
        self.client.close()
        self.pool.close_all()
        _pool_cache.clear()
        shutil.rmtree(self.temp_dir, ignore_errors=True)


@pytest.fixture
def multi_user():
    harness = _D07Harness(users_enabled=True)
    yield harness
    harness.close()


@pytest.fixture
def single_admin():
    harness = _D07Harness(users_enabled=False)
    yield harness
    harness.close()


# C1 ---------------------------------------------------------------------------


def test_single_admin_accessible_vaults_lists_all(single_admin):
    single_admin.insert(
        "INSERT INTO vaults (name, description) VALUES (?, ?)",
        ("D07 Only Vault", "d"),
    )
    resp = single_admin.client.get("/api/vaults/accessible", headers=single_admin.auth)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["vaults"]) == 1


# C2 ---------------------------------------------------------------------------


def test_single_admin_can_create_vault(single_admin):
    client = single_admin.no_raise()
    try:
        resp = client.post(
            "/api/vaults",
            json={"name": "D07 New Vault", "description": "d"},
            headers=single_admin.auth,
        )
    finally:
        client.close()
    assert resp.status_code == 201


# C3 ---------------------------------------------------------------------------


def test_default_vault_identity_survives_rename(single_admin):
    vault_id = single_admin.insert(
        "INSERT INTO vaults (name, description) VALUES (?, ?)",
        ("Default", "seed default vault"),
    )
    single_admin.insert(
        "INSERT INTO users (id, username, hashed_password, role, is_active)"
        " VALUES (9, 'd07newuser', 'x', 'member', 1)"
    )
    client = single_admin.no_raise()
    try:
        resp = client.put(
            "/api/vaults/%d" % vault_id,
            json={"name": "D07 Renamed"},
            headers=single_admin.auth,
        )
        rename_status = resp.status_code
    finally:
        client.close()

    conn = single_admin.pool.get_connection()
    try:
        assign_user_to_default_vault(conn, 9)
        conn.commit()
        vault_count = conn.execute("SELECT COUNT(*) FROM vaults").fetchone()[0]
    finally:
        single_admin.pool.release_connection(conn)

    ok = rename_status == 409 or vault_count == 1
    assert ok is True


# C4 ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ["vault-groups", "user-organizations"])
def test_replace_set_with_omitted_fields_does_not_clear(multi_user, route):
    org_id, group_id = multi_user.seed_org_and_group(route)
    if route == "vault-groups":
        vault_id = multi_user.insert(
            "INSERT INTO vaults (name) VALUES (?)", ("D07 Vault " + route,)
        )
        multi_user.insert(
            "INSERT INTO vault_group_access (vault_id, group_id, permission)"
            " VALUES (?, ?, 'read')",
            (vault_id, group_id),
        )
        resp = multi_user.client.put("/api/vaults/%d/groups" % vault_id, json={})
        rows_after = multi_user.count(
            "SELECT COUNT(*) FROM vault_group_access WHERE vault_id = ?",
            (vault_id,),
        )
    else:
        multi_user.insert(
            "INSERT INTO users (id, username, hashed_password, role, is_active)"
            " VALUES (9, 'd07orguser', 'x', 'member', 1)"
        )
        multi_user.insert(
            "INSERT INTO org_members (org_id, user_id, role) VALUES (?, 9, 'member')",
            (org_id,),
        )
        resp = multi_user.client.put("/api/users/9/organizations", json={})
        rows_after = multi_user.count(
            "SELECT COUNT(*) FROM org_members WHERE user_id = 9"
        )
    assert resp.status_code in (200, 422)
    assert rows_after == 1


# C5 ---------------------------------------------------------------------------


def test_list_users_rejects_negative_limit(multi_user):
    resp = multi_user.client.get("/api/users", params={"limit": -1})
    assert resp.status_code == 422


# C6 ---------------------------------------------------------------------------


def test_vault_list_never_truncates_silently(multi_user):
    conn = multi_user.pool.get_connection()
    try:
        conn.executemany(
            "INSERT INTO vaults (name, description) VALUES (?, ?)",
            [("D07 Vault %04d" % i, "d") for i in range(1001)],
        )
        conn.commit()
    finally:
        multi_user.pool.release_connection(conn)

    resp = multi_user.client.get("/api/vaults")
    assert resp.status_code == 200
    body = resp.json()
    ok = (
        len(body["vaults"]) == 1001
        or body.get("truncated") is True
        or body.get("total") == 1001
    )
    assert ok is True


# C7 (GREEN pin at base, fixed upstream by #997) -------------------------------


@pytest.mark.parametrize("username", ["", "  ", "ab"])
def test_update_user_rejects_short_username(multi_user, username):
    resp = multi_user.client.patch("/api/users/1", json={"username": username})
    assert resp.status_code == 422


# C8 ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ["vault-groups", "user-groups"])
def test_replace_set_with_duplicate_ids_dedupes(multi_user, route):
    org_id, group_id = multi_user.seed_org_and_group(route)
    client = multi_user.no_raise()
    try:
        if route == "vault-groups":
            vault_id = multi_user.insert(
                "INSERT INTO vaults (name) VALUES (?)", ("D07 Vault " + route,)
            )
            resp = client.put(
                "/api/vaults/%d/groups" % vault_id,
                json={"group_ids": [group_id, group_id]},
            )
        else:
            multi_user.insert(
                "INSERT INTO users (id, username, hashed_password, role, is_active)"
                " VALUES (9, 'd07groupuser', 'x', 'member', 1)"
            )
            multi_user.insert(
                "INSERT INTO org_members (org_id, user_id, role)"
                " VALUES (?, 9, 'member')",
                (org_id,),
            )
            resp = client.put(
                "/api/users/9/groups",
                json={"group_ids": [group_id, group_id]},
            )
    finally:
        client.close()
    assert resp.status_code == 200
    if route == "vault-groups":
        rows = multi_user.count(
            "SELECT COUNT(*) FROM vault_group_access WHERE vault_id = ?",
            (vault_id,),
        )
    else:
        rows = multi_user.count(
            "SELECT COUNT(*) FROM group_members WHERE user_id = 9"
            " AND group_id = ?",
            (group_id,),
        )
    assert rows == 1


# C9 ---------------------------------------------------------------------------


def test_create_vault_failure_is_logged(multi_user, caplog):
    multi_user._conn_factory = lambda raw: _FailingExecuteProxy(
        raw,
        needle="INSERT INTO vaults",
        exc=sqlite3.OperationalError("d07 create-vault injected failure"),
    )
    client = multi_user.no_raise()
    try:
        with caplog.at_level(logging.ERROR):
            resp = client.post(
                "/api/vaults", json={"name": "D07 Boom", "description": "d"}
            )
    finally:
        client.close()
        multi_user._conn_factory = None
    assert resp.status_code == 500
    n = sum(
        1
        for record in caplog.records
        if record.levelno >= logging.ERROR
        and record.name.startswith("app.api.routes.vaults")
        and record.exc_info is not None
    )
    assert n >= 1


# C10 (GREEN pin at base, echo already removed by the #723 delete rewrite) ----


def test_delete_vault_error_does_not_echo_exception_text(multi_user):
    mock_vector_store = MagicMock()
    mock_vector_store.delete_by_vault = AsyncMock(return_value=0)
    app.dependency_overrides[get_vector_store] = lambda: mock_vector_store
    vault_id = multi_user.insert(
        "INSERT INTO vaults (name) VALUES (?)", ("D07 Delete Victim",)
    )
    multi_user._conn_factory = lambda raw: _FailingExecuteProxy(
        raw,
        needle="DELETE FROM vaults",
        exc=sqlite3.OperationalError("d07-sentinel-cause"),
    )
    client = multi_user.no_raise()
    try:
        resp = client.delete("/api/vaults/%d" % vault_id)
    finally:
        client.close()
        multi_user._conn_factory = None
    echoed = "d07-sentinel-cause" in resp.text
    assert echoed is False
