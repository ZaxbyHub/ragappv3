"""Acceptance checks for issue #726 (trace 725-726-d07-d08).

RED at base for C13-C19; C20-C24 are PRESERVING pins on existing suites (not
in this file).

C13 test_update_vault_member_invokes_last_admin_guard
C14 test_membership_mutation_racing_delete_is_handled
C15 test_admin_guide_documents_argon2_downgrade_hazard
C16 test_local_curator_opt_in_honoured_from_env_file
C17 test_docs_name_the_sqlite_serialized_seam_that_is_read
C18 test_sequential_permission_branch_has_a_forcing_test
C19 test_group_fields_validated_like_the_ui
"""

import re
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest
from backend.tests.schema_constants import build_test_schema
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.auth import router as auth_router
from app.api.routes.groups import router as groups_router
from app.api.routes.vault_members import (
    group_access_router,
)
from app.api.routes.vault_members import (
    router as vault_members_router,
)
from app.security import csrf_protect
from app.services.auth_service import (
    compute_client_fingerprint,
    create_access_token,
    hash_password,
)

_THIS_FILE = Path(__file__).resolve()
_JWT_SECRET = "test-secret-key-for-testing-only-min-32-chars!!"


def _superadmin_token():
    return create_access_token(
        1, "superadmin", "superadmin", client_fingerprint=compute_client_fingerprint("")
    )


def _exec(db_path, statement, params=()):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.execute(statement, params)
    row_id = cur.lastrowid
    conn.commit()
    conn.close()
    return row_id


def _build_db_fixture():
    """Shared body for the members and groups DB fixtures."""
    temp_dir = tempfile.mkdtemp(prefix="d08_")
    db_path = str(Path(temp_dir) / "app.db")

    from app.models.database import _pool_cache, _pool_cache_lock

    with _pool_cache_lock:
        for pool in list(_pool_cache.values()):
            pool.close_all()
        _pool_cache.clear()

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    build_test_schema(conn)
    conn.commit()
    conn.close()
    return temp_dir, db_path


def _teardown_db(temp_dir, db_path):
    from app.models.database import _pool_cache, _pool_cache_lock

    with _pool_cache_lock:
        if db_path in _pool_cache:
            _pool_cache[db_path].close_all()
            del _pool_cache[db_path]
    shutil.rmtree(temp_dir, ignore_errors=True)


def _patch_settings(monkeypatch, temp_dir):
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", Path(temp_dir))
    monkeypatch.setattr(settings, "jwt_secret_key", _JWT_SECRET)
    monkeypatch.setattr(settings, "users_enabled", True)


def _seed_users(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    pw = hash_password("testpass")
    for user_id, username, role in (
        (1, "superadmin", "superadmin"),
        (2, "admin1", "admin"),
        (3, "member1", "member"),
        (4, "member2", "member"),
    ):
        conn.execute(
            "INSERT INTO users (id, username, hashed_password, full_name, role, is_active)"
            " VALUES (?, ?, ?, ?, ?, 1)",
            (user_id, username, pw, username.title(), role),
        )
    conn.execute("INSERT INTO vaults (id, name) VALUES (1, 'D08 Test Vault')")
    conn.execute(
        "INSERT INTO vault_members (vault_id, user_id, permission, granted_by)"
        " VALUES (1, 2, 'admin', 1)"
    )
    conn.commit()
    conn.close()


@pytest.fixture
def members_db(monkeypatch):
    temp_dir, db_path = _build_db_fixture()
    _patch_settings(monkeypatch, temp_dir)
    _seed_users(db_path)
    yield db_path
    _teardown_db(temp_dir, db_path)


@pytest.fixture
def members_app(members_db):
    app = FastAPI()
    app.include_router(auth_router, prefix="/api")
    app.include_router(vault_members_router, prefix="/api")
    app.include_router(group_access_router, prefix="/api")
    app.dependency_overrides[csrf_protect] = lambda: "d08-csrf"
    client = TestClient(app, raise_server_exceptions=False)
    client.headers["user-agent"] = ""
    yield client, members_db
    client.close()


@pytest.fixture
def groups_db(monkeypatch):
    temp_dir, db_path = _build_db_fixture()
    _patch_settings(monkeypatch, temp_dir)
    _seed_users(db_path)
    yield db_path
    _teardown_db(temp_dir, db_path)


@pytest.fixture
def groups_app(groups_db):
    app = FastAPI()
    app.include_router(auth_router, prefix="/api")
    app.include_router(groups_router, prefix="/api")
    app.dependency_overrides[csrf_protect] = lambda: "d08-csrf"
    client = TestClient(app, raise_server_exceptions=False)
    client.headers["user-agent"] = ""
    yield client, groups_db
    client.close()


# C13 --------------------------------------------------------------------------


def test_update_vault_member_invokes_last_admin_guard(members_app):
    client, db_path = members_app
    pw = hash_password("d08pass")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, full_name, role, is_active)"
        " VALUES (5, 'soleadmin', ?, 'Sole Admin', 'member', 1)",
        (pw,),
    )
    # Drop the seeded user-2 grant so the caller's direct admin row below is
    # the vault's ONLY admin member (no other grant can mask the invariant).
    conn.execute("DELETE FROM vault_members WHERE vault_id = 1")
    conn.execute(
        "INSERT INTO vault_members (vault_id, user_id, permission, granted_by)"
        " VALUES (1, 5, 'admin', 1)"
    )
    conn.commit()
    conn.close()

    token = create_access_token(
        5, "soleadmin", "member", client_fingerprint=compute_client_fingerprint("")
    )
    resp = client.patch(
        "/api/vaults/1/members/5",
        json={"permission": "read"},
        headers={"Authorization": "Bearer " + token},
    )
    refused = resp.status_code >= 400

    conn = sqlite3.connect(db_path)
    admin_count = conn.execute(
        "SELECT COUNT(*) FROM vault_members WHERE vault_id = 1"
        " AND permission = 'admin'"
    ).fetchone()[0]
    conn.close()
    assert admin_count == 1, "self-demotion refused=%s" % refused


# C14 --------------------------------------------------------------------------


class _RaceCursor:
    """Cursor wrapper that lets the connection proxy observe every statement."""

    def __init__(self, real_cursor, proxy):
        self._real_cursor = real_cursor
        self._proxy = proxy

    def execute(self, sql, params=()):
        self._proxy.maybe_fire(sql)
        return self._real_cursor.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real_cursor, name)


class _RaceConnection:
    """Connection wrapper making the delete race deterministic.

    Right before the needle statement first executes, the racing DELETE runs
    and commits on the underlying real connection, so the handler's own
    statement then hits a row that a concurrent remove already deleted.
    """

    def __init__(self, real, needle, delete_sql, delete_params):
        self._real = real
        self._needle = needle
        self._delete_sql = delete_sql
        self._delete_params = delete_params
        self._fired = False

    def maybe_fire(self, sql):
        if not self._fired and self._needle in sql:
            self._fired = True
            self._real.execute(self._delete_sql, self._delete_params)
            self._real.commit()

    def cursor(self, *args, **kwargs):
        return _RaceCursor(self._real.cursor(*args, **kwargs), self)

    def execute(self, sql, params=()):
        self.maybe_fire(sql)
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.mark.parametrize(
    "case",
    [
        "update_vault_member",
        "update_vault_group_access",
        "add_vault_member",
        "grant_vault_group_access",
    ],
)
def test_membership_mutation_racing_delete_is_handled(members_db, case):
    db_path = members_db
    if case == "update_vault_member":
        _exec(
            db_path,
            "INSERT INTO vault_members (vault_id, user_id, permission, granted_by)"
            " VALUES (1, 3, 'read', 1)",
        )
        needle = "UPDATE vault_members SET permission"
        delete_sql = "DELETE FROM vault_members WHERE vault_id = ? AND user_id = ?"
        delete_params = (1, 3)
        method, path, payload = (
            "PATCH",
            "/api/vaults/1/members/3",
            {"permission": "write"},
        )
        allowed = (404,)
    elif case == "update_vault_group_access":
        group_id = _exec(
            db_path,
            "INSERT INTO organizations (name) VALUES ('D08 Org UGA')",
        )
        group_id = _exec(
            db_path,
            "INSERT INTO groups (org_id, name) VALUES (?, 'D08 Group UGA')",
            (group_id,),
        )
        _exec(
            db_path,
            "INSERT INTO vault_group_access (vault_id, group_id, permission, granted_by)"
            " VALUES (1, ?, 'read', 1)",
            (group_id,),
        )
        needle = "UPDATE vault_group_access SET permission"
        delete_sql = "DELETE FROM vault_group_access WHERE vault_id = ? AND group_id = ?"
        delete_params = (1, group_id)
        method, path, payload = (
            "PATCH",
            "/api/vaults/1/group-access/%d" % group_id,
            {"permission": "write"},
        )
        allowed = (404,)
    elif case == "add_vault_member":
        # Fires just before the post-commit re-read of the inserted row.
        needle = "AND vm.user_id = ?"
        delete_sql = "DELETE FROM vault_members WHERE vault_id = ? AND user_id = ?"
        delete_params = (1, 3)
        method, path, payload = (
            "POST",
            "/api/vaults/1/members",
            {"member_user_id": 3, "permission": "read"},
        )
        allowed = (200, 404)
    else:  # grant_vault_group_access
        org_id = _exec(db_path, "INSERT INTO organizations (name) VALUES ('D08 Org GA')")
        group_id = _exec(
            db_path,
            "INSERT INTO groups (org_id, name) VALUES (?, 'D08 Group GA')",
            (org_id,),
        )
        # Fires just before the post-commit re-read of the granted row.
        needle = "AND vga.group_id = ?"
        delete_sql = "DELETE FROM vault_group_access WHERE vault_id = ? AND group_id = ?"
        delete_params = (1, group_id)
        method, path, payload = (
            "POST",
            "/api/vaults/1/group-access",
            {"group_id": group_id, "permission": "read"},
        )
        allowed = (200, 404)

    from app.api.deps import get_db

    app = FastAPI()
    app.include_router(auth_router, prefix="/api")
    app.include_router(vault_members_router, prefix="/api")
    app.include_router(group_access_router, prefix="/api")
    app.dependency_overrides[csrf_protect] = lambda: "d08-csrf"

    def override_get_db():
        raw = sqlite3.connect(db_path, check_same_thread=False)
        raw.execute("PRAGMA foreign_keys = ON")
        proxy = _RaceConnection(raw, needle, delete_sql, delete_params)
        try:
            yield proxy
        finally:
            try:
                raw.rollback()
            except sqlite3.Error:
                pass
            raw.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app, raise_server_exceptions=False)
    client.headers["user-agent"] = ""
    headers = {"Authorization": "Bearer " + _superadmin_token()}
    try:
        if method == "PATCH":
            resp = client.patch(path, json=payload, headers=headers)
        else:
            resp = client.post(path, json=payload, headers=headers)
    finally:
        client.close()
    ok = resp.status_code in allowed
    assert ok is True


# C15 --------------------------------------------------------------------------


def test_admin_guide_documents_argon2_downgrade_hazard():
    guide = _THIS_FILE.parents[2] / "docs" / "admin-guide.md"
    lines = guide.read_text(encoding="utf-8").splitlines()
    idx = next(
        (i for i, line in enumerate(lines) if "upgraded to Argon2id" in line), None
    )
    found = False
    if idx is not None:
        window = lines[idx + 1 : idx + 16]
        found = any(
            re.search(r"(?i)(downgrad|roll ?back|one-way|older release)", line)
            for line in window
        )
    assert found is True


# C16 --------------------------------------------------------------------------


def test_local_curator_opt_in_honoured_from_env_file(tmp_path, monkeypatch):
    from app.config import Settings
    from app.services import curator_ssrf
    from app.services.curator_ssrf import CuratorURLBlocked, assert_curator_url_safe

    env_file = tmp_path / "d08-curator.env"
    env_file.write_text("ALLOW_LOCAL_CURATOR=1\n", encoding="utf-8")

    monkeypatch.delenv("ALLOW_LOCAL_CURATOR", raising=False)
    monkeypatch.setenv("JWT_SECRET_KEY", "d08-curator-secret-key-at-least-32-characters")
    monkeypatch.setenv("ADMIN_SECRET_TOKEN", "d08-curator-test-token")
    instance = Settings(_env_file=str(env_file))
    monkeypatch.setattr("app.config.settings", instance)
    monkeypatch.setattr(curator_ssrf, "settings", instance, raising=False)

    allowed = True
    try:
        assert_curator_url_safe("http://127.0.0.1:11434/v1")
    except CuratorURLBlocked:
        allowed = False
    assert allowed is True


# C17 --------------------------------------------------------------------------


def test_docs_name_the_sqlite_serialized_seam_that_is_read():
    repo = _THIS_FILE.parents[2]
    count = 0
    for rel in ("docs/engineering/testing.md", "docs/engineering/conventions.md"):
        text = (repo / rel).read_text(encoding="utf-8")
        count += text.count("deps._SQLITE_SERIALIZED")
    assert count == 0


# C18 --------------------------------------------------------------------------


def test_sequential_permission_branch_has_a_forcing_test():
    tests_dir = _THIS_FILE.parent
    # Executable forcing only: an actual monkeypatch of the module attribute
    # that the branch reads, or a direct dotted assignment to it. A textual
    # read of the flag, or a grep-pattern string literal in another test's
    # source, must not count.
    pattern = re.compile(
        r"setattr\(\s*authz_policy\s*,\s*[\"']_SQLITE_SERIALIZED"
        r"|authz_policy\s*\.\s*_SQLITE_SERIALIZED\s*="
    )
    matched = []
    for path in sorted(tests_dir.rglob("*.py")):
        if path.resolve() == _THIS_FILE:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if pattern.search(text):
            matched.append(path.name)
    n = len(matched)
    assert n >= 1, (
        "executable forcing files: %s; the docs-string literal in "
        "test_d07b_absent_vs_empty_hardening.py must never match: %s"
        % (
            matched,
            "test_d07b_absent_vs_empty_hardening.py" in matched,
        )
    )


# C19 --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case",
    [
        "create-short-name",
        "create-long-name",
        "create-long-description",
        "update-empty-name",
    ],
)
def test_group_fields_validated_like_the_ui(groups_app, case):
    client, db_path = groups_app
    org_id = _exec(
        db_path, "INSERT INTO organizations (name) VALUES ('D08 Groups Org')"
    )
    headers = {"Authorization": "Bearer " + _superadmin_token()}
    if case.startswith("create-"):
        if case == "create-short-name":
            payload = {"name": "x", "description": "d", "org_id": org_id}
        elif case == "create-long-name":
            payload = {"name": "a" * 101, "description": "d", "org_id": org_id}
        else:
            payload = {"name": "D08 Group", "description": "d" * 501, "org_id": org_id}
        resp = client.post("/api/groups", json=payload, headers=headers)
    else:
        group_id = _exec(
            db_path,
            "INSERT INTO groups (org_id, name) VALUES (?, 'D08 Rename Target')",
            (org_id,),
        )
        resp = client.put(
            "/api/groups/%d" % group_id, json={"name": ""}, headers=headers
        )
    assert resp.status_code == 422
