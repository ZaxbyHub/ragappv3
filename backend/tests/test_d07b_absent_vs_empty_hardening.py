"""Plan-critic hardening addendum for issues #725/#726 (trace 725-726-d07-d08).

The full-contract username check is the amended frozen check C7 (RED at base,
GREEN after the fix); the remaining tests are additonal validation-suite
hardening, not frozen.

Amended C7  test_update_user_rejects_short_username_full_contract
H1         test_create_user_rejects_short_username_after_strip
H2         test_single_admin_create_vault_is_durable
H3         test_vault_list_total_truncated_consistent
H4         test_delete_vault_error_is_500_fixed_detail (pin of the #723 contract)
H5         test_update_vault_member_last_admin_refusal_status
H6         test_membership_mutation_racing_delete_reports_status
H7         test_docs_name_both_seam_forms
H8         test_group_fields_exact_ui_matrix
"""

import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from test_d07_absent_vs_empty import _D07Harness, _FailingExecuteProxy
from test_d08_ops_hygiene import (
    _build_db_fixture,
    _exec,
    _patch_settings,
    _RaceConnection,
    _seed_users,
    _superadmin_token,
    _teardown_db,
)

from app.api.routes.auth import router as auth_router
from app.api.routes.groups import router as groups_router
from app.api.routes.vault_members import (
    group_access_router,
)
from app.api.routes.vault_members import (
    router as vault_members_router,
)
from app.main import app
from app.security import csrf_protect
from app.services.auth_service import (
    compute_client_fingerprint,
    create_access_token,
    hash_password,
)


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
    app.dependency_overrides[csrf_protect] = lambda: "d07b-csrf"
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
    app.dependency_overrides[csrf_protect] = lambda: "d07b-csrf"
    client = TestClient(app, raise_server_exceptions=False)
    client.headers["user-agent"] = ""
    yield client, groups_db
    client.close()


# Amended C7: the create-side raw 3-char floor already rejects raw-short
# values; the trimmed contract (validate-only strip, then a 3-char
# after-strip floor) is what is missing at base. The raw-length-3 params
# "ab " and "  ab" (trimmed length 2) distinguish a trimmed-min-2
# implementation from the required trimmed-min-3.


@pytest.mark.parametrize(
    "username", ["", "  ", "ab", "   ", " a ", "x  ", "ab ", "  ab"]
)
def test_update_user_rejects_short_username_full_contract(multi_user, username):
    resp = multi_user.client.patch("/api/users/1", json={"username": username})
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "username", ["", "  ", "ab", "   ", " a ", "x  ", "ab ", "  ab"]
)
def test_create_user_rejects_short_username_after_strip(multi_user, username):
    resp = multi_user.client.post(
        "/api/users",
        json={
            "username": username,
            "password": "SecurePass123!",
            "full_name": "D07B User",
            "role": "member",
        },
    )
    assert resp.status_code == 422


# GREEN pin at base AND after the fix: the padded boundary value is already
# long enough raw, validators must never rewrite it, and a duplicate create
# is refused on the collision path. This test pins boundary plus
# no-normalization; it is expected to PASS at base by design.
def test_username_validation_boundary_and_persistence(multi_user):
    padded = "  abc  "
    resp = multi_user.client.patch("/api/users/1", json={"username": padded})
    assert resp.status_code == 200

    check = sqlite3.connect(multi_user.db_path)
    try:
        stored = check.execute(
            "SELECT username FROM users WHERE id = 1"
        ).fetchone()[0]
    finally:
        check.close()
    assert stored == padded

    dup = multi_user.client.post(
        "/api/users",
        json={
            "username": padded,
            "password": "SecurePass123!",
            "full_name": "D07B Dup",
            "role": "member",
        },
    )
    # The admin create path answers the collision with a clean 400 at base
    # (manual duplicate check, users.py); 409 and 422 are the sibling
    # collision codes a fix may consolidate on.
    assert dup.status_code in (400, 409, 422)


# RED at base via the reject leg: the public register path enforces only a
# raw-length floor (clean 400), so "  ab  " (trimmed length 2) registers
# today. The accept leg also pins that registration stores the username raw.
def test_registration_username_after_strip(multi_user):
    reject = multi_user.client.post(
        "/api/auth/register",
        json={
            "username": "  ab  ",
            "password": "SecurePass123!",
            "full_name": "D07B Reg Reject",
        },
    )
    # Registration's intentional manual contract: a clean 400 with the
    # 3-characters message (test_auth_routes.py test_register_short_username
    # precedent), not a FastAPI 422 envelope.
    assert reject.status_code == 400
    assert "3" in reject.json()["detail"]

    accept = multi_user.client.post(
        "/api/auth/register",
        json={
            "username": "  abc  ",
            "password": "SecurePass123!",
            "full_name": "D07B Reg Accept",
        },
    )
    assert accept.status_code in (200, 201)
    check = sqlite3.connect(multi_user.db_path)
    try:
        names = [
            row[0] for row in check.execute("SELECT username FROM users").fetchall()
        ]
    finally:
        check.close()
    assert "  abc  " in names


def test_single_admin_create_vault_is_durable(single_admin):
    client = single_admin.no_raise()
    try:
        resp = client.post(
            "/api/vaults",
            json={"name": "D07B Durable Vault", "description": "d"},
            headers=single_admin.auth,
        )
    finally:
        client.close()
    assert resp.status_code == 201

    check = sqlite3.connect(single_admin.db_path)
    try:
        check.execute("PRAGMA foreign_keys = ON")
        row = check.execute(
            "SELECT COUNT(*) FROM vaults WHERE name = ?", ("D07B Durable Vault",)
        ).fetchone()
        stored = row[0]
    finally:
        check.close()
    assert stored == 1

    listing = single_admin.client.get("/api/vaults", headers=single_admin.auth)
    assert listing.status_code == 200
    assert len(listing.json()["vaults"]) == 1


def test_vault_list_total_truncated_consistent(multi_user):
    for i in range(5):
        multi_user.insert(
            "INSERT INTO vaults (name) VALUES (?)", ("D07B Vault %d" % i,)
        )
    resp = multi_user.client.get("/api/vaults")
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("total") == 5
    assert body.get("truncated") is False
    assert len(body["vaults"]) == 5


def test_delete_vault_error_is_500_fixed_detail(multi_user):
    from unittest.mock import AsyncMock, MagicMock

    from app.api.deps import get_vector_store

    mock_vector_store = MagicMock()
    mock_vector_store.delete_by_vault = AsyncMock(return_value=0)
    app.dependency_overrides[get_vector_store] = lambda: mock_vector_store
    vault_id = multi_user.insert(
        "INSERT INTO vaults (name) VALUES (?)", ("D07B Delete Victim",)
    )
    multi_user._conn_factory = lambda raw: _FailingExecuteProxy(
        raw,
        needle="DELETE FROM vaults",
        exc=sqlite3.OperationalError("d07b-sentinel-cause"),
    )
    client = multi_user.no_raise()
    try:
        resp = client.delete("/api/vaults/%d" % vault_id)
    finally:
        client.close()
        multi_user._conn_factory = None
    assert resp.status_code == 500
    assert resp.json()["detail"] == "Failed to delete vault"
    assert "d07b-sentinel-cause" not in resp.text


def test_update_vault_member_last_admin_refusal_status(members_app):
    client, db_path = members_app
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, full_name, role, is_active)"
        " VALUES (5, 'd07bsoleadmin', ?, 'Sole Admin', 'member', 1)",
        (hash_password("d07bpass"),),
    )
    # The caller holds the vault's ONLY admin grant.
    conn.execute("DELETE FROM vault_members WHERE vault_id = 1")
    conn.execute(
        "INSERT INTO vault_members (vault_id, user_id, permission, granted_by)"
        " VALUES (1, 5, 'admin', 1)"
    )
    conn.commit()
    conn.close()

    token = create_access_token(
        5, "d07bsoleadmin", "member", client_fingerprint=compute_client_fingerprint("")
    )
    resp = client.patch(
        "/api/vaults/1/members/5",
        json={"permission": "read"},
        headers={"Authorization": "Bearer " + token},
    )
    assert resp.status_code == 409
    assert "admin" in resp.json()["detail"]

    conn = sqlite3.connect(db_path)
    try:
        admin_count = conn.execute(
            "SELECT COUNT(*) FROM vault_members WHERE vault_id = 1"
            " AND permission = 'admin'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert admin_count == 1


@pytest.mark.parametrize(
    "case",
    [
        "update_vault_member",
        "update_vault_group_access",
        "add_vault_member",
        "grant_vault_group_access",
    ],
)
def test_membership_mutation_racing_delete_reports_status(members_db, case):
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
        org_id = _exec(
            db_path, "INSERT INTO organizations (name) VALUES ('D07B Org UGA')"
        )
        group_id = _exec(
            db_path,
            "INSERT INTO groups (org_id, name) VALUES (?, 'D07B Group UGA')",
            (org_id,),
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
        org_id = _exec(db_path, "INSERT INTO organizations (name) VALUES ('D07B Org GA')")
        group_id = _exec(
            db_path,
            "INSERT INTO groups (org_id, name) VALUES (?, 'D07B Group GA')",
            (org_id,),
        )
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

    race_app = FastAPI()
    race_app.include_router(auth_router, prefix="/api")
    race_app.include_router(vault_members_router, prefix="/api")
    race_app.include_router(group_access_router, prefix="/api")
    race_app.dependency_overrides[csrf_protect] = lambda: "d07b-csrf"

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

    race_app.dependency_overrides[get_db] = override_get_db
    client = TestClient(race_app, raise_server_exceptions=False)
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
    assert ok is True, "handler %s answered %s, allowed %s" % (
        case,
        resp.status_code,
        allowed,
    )


def test_docs_name_both_seam_forms():
    testing_md = Path(__file__).resolve().parents[2] / "docs" / "engineering" / "testing.md"
    text = testing_md.read_text(encoding="utf-8")
    assert text.count("deps._SQLITE_SERIALIZED") == 0
    assert text.count("authz_policy._SQLITE_SERIALIZED") >= 1


_GROUP_MATRIX = [
    pytest.param("create-name-x", True, id="reject-create-name-x"),
    pytest.param("create-name-trimmed-one", True, id="reject-create-name-trimmed-one"),
    pytest.param("create-name-101", True, id="reject-create-name-101"),
    pytest.param("create-name-raw-101", True, id="reject-create-name-raw-101"),
    pytest.param("create-description-501", True, id="reject-create-description-501"),
    pytest.param("update-name-empty", True, id="reject-update-name-empty"),
    pytest.param("update-name-blank", True, id="reject-update-name-blank"),
    pytest.param(
        "update-description-501", True, id="reject-update-description-501"
    ),
    pytest.param("create-name-two", False, id="accept-create-name-two"),
    pytest.param("create-name-100", False, id="accept-create-name-100"),
    pytest.param(
        "create-description-500", False, id="accept-create-description-500"
    ),
]


@pytest.mark.parametrize(("case", "must_be_422"), _GROUP_MATRIX)
def test_group_fields_exact_ui_matrix(groups_app, case, must_be_422):
    client, db_path = groups_app
    org_id = _exec(
        db_path, "INSERT INTO organizations (name) VALUES ('D07B Groups Org')"
    )
    headers = {"Authorization": "Bearer " + _superadmin_token()}
    if case.startswith("create-"):
        name = "D07B Default Name"
        description = "d"
        if case == "create-name-x":
            name = "x"
        elif case == "create-name-trimmed-one":
            name = "  y"
        elif case == "create-name-101":
            name = "y" * 101
        elif case == "create-name-raw-101":
            name = "y" * 100 + " "
        elif case == "create-description-501":
            description = "d" * 501
        elif case == "create-name-two":
            name = "ab"
        elif case == "create-name-100":
            name = "y" * 100
        elif case == "create-description-500":
            description = "d" * 500
        resp = client.post(
            "/api/groups",
            json={"name": name, "description": description, "org_id": org_id},
            headers=headers,
        )
    else:
        group_id = _exec(
            db_path,
            "INSERT INTO groups (org_id, name) VALUES (?, 'D07B Rename Target')",
            (org_id,),
        )
        if case == "update-name-empty":
            payload = {"name": ""}
        elif case == "update-name-blank":
            payload = {"name": "  "}
        else:  # update-description-501
            payload = {"description": "d" * 501}
        resp = client.put(
            "/api/groups/%d" % group_id, json=payload, headers=headers
        )
    if must_be_422:
        assert resp.status_code == 422
        return
    assert resp.status_code in (200, 201)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT name, description FROM groups").fetchone()
    finally:
        conn.close()
    # Accept pins persist exactly what was submitted, raw and un-normalized
    # (validators validate-only, never rewrite).
    assert row[0] == name
    assert row[1] == description
