"""Frozen acceptance checks for group-membership / org-scoping defects (D03).

RED at base, GREEN after the fix: each test below pins one verified defect in
the group-membership / org-scoping surface (PUT /users/{id}/groups target-org
check for superadmins, org-membership removal cascades, scoped org replace,
orphaned group_members rows, group-side vault grants missing granted_by, and
invite redeemability). Every test in this module is intentionally failing at
the pre-fix tree and must turn green once the corresponding production fix
lands.

Harness: per-test temp SQLite DB (TEST_SCHEMA via backend test helpers) with
``settings.data_dir`` pointed at the temp dir so every router (users,
organizations, groups) resolves its pool to the temp DB, plus the repo's
standard csrf_protect dependency override and ""-fingerprint tokens.
"""

import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

# Set up test environment BEFORE importing app modules.
os.environ["JWT_SECRET_KEY"] = (
    "test-jwt-secret-key-for-testing-only-12345678901234567890"
)
os.environ["USERS_ENABLED"] = "true"
os.environ.setdefault(
    "ADMIN_SECRET_TOKEN",
    "d03-acceptance-admin-secret-token-0123456789abcdef0123456789abcdef",
)

import pytest  # noqa: E402
from backend.tests.user_route_helpers import (  # noqa: E402
    create_user,
    get_token,
    setup_test_db,
)
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class _D03Env:
    """Per-test namespace: seed connection, client, and standard users."""


@pytest.fixture(autouse=True)
def d03_env(monkeypatch):
    """Fresh temp DB + mounted users/organizations/groups routers."""
    tmpdir = tempfile.mkdtemp()
    # settings.sqlite_path == data_dir / "app.db" — the pool key every router
    # resolves, so the temp file MUST be named app.db.
    db_path = os.path.join(tmpdir, "app.db")

    from app.models.database import _pool_cache, _pool_cache_lock

    with _pool_cache_lock:
        for _path, pool in list(_pool_cache.items()):
            pool.close_all()
        _pool_cache.clear()

    conn = setup_test_db(db_path)

    monkeypatch.setattr("app.config.settings.data_dir", Path(tmpdir))
    monkeypatch.setattr(
        "app.config.settings.jwt_secret_key", os.environ["JWT_SECRET_KEY"]
    )
    monkeypatch.setattr("app.config.settings.users_enabled", True)

    env = _D03Env()
    env.conn = conn
    env.db_path = db_path
    env.superadmin_id = create_user(
        conn, "superadmin", "pass123", "superadmin", "Super Admin"
    )
    env.admin_id = create_user(conn, "d03admin", "pass123", "admin", "D03 Admin")
    env.member_id = create_user(conn, "d03member", "pass123", "member", "D03 Member")

    from app.api.routes.groups import router as groups_router
    from app.api.routes.organizations import router as organizations_router
    from app.api.routes.users import router as users_router
    from app.security import csrf_protect

    app = FastAPI()
    app.include_router(users_router)
    app.include_router(organizations_router, prefix="/api")
    app.include_router(groups_router)
    # Mutating routes declare csrf_protect; this standalone app has no csrf
    # manager on state, so override the dependency (repo-standard pattern).
    app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"

    client = TestClient(app)
    # Tokens are minted with the fingerprint of "" (user_route_helpers.get_token).
    client.headers["user-agent"] = ""
    env.client = client

    yield env

    client.close()
    conn.close()
    with _pool_cache_lock:
        for _path, pool in list(_pool_cache.items()):
            pool.close_all()
        _pool_cache.clear()
    shutil.rmtree(tmpdir, ignore_errors=True)


def _insert(conn, sql, params=()):
    cursor = conn.execute(sql, params)
    conn.commit()
    return cursor.lastrowid


def _seed_org(conn, name):
    return _insert(
        conn,
        "INSERT INTO organizations (name, slug) VALUES (?, ?)",
        (name, name.lower().replace(" ", "-")),
    )


def _seed_group(conn, org_id, name):
    return _insert(
        conn, "INSERT INTO groups (org_id, name) VALUES (?, ?)", (org_id, name)
    )


def _seed_org_member(conn, org_id, user_id, role="member"):
    return _insert(
        conn,
        "INSERT INTO org_members (org_id, user_id, role) VALUES (?, ?, ?)",
        (org_id, user_id, role),
    )


def _seed_group_member(conn, group_id, user_id):
    return _insert(
        conn,
        "INSERT INTO group_members (group_id, user_id) VALUES (?, ?)",
        (group_id, user_id),
    )


def _seed_vault(conn, name, org_id, visibility="private"):
    return _insert(
        conn,
        "INSERT INTO vaults (name, org_id, visibility) VALUES (?, ?, ?)",
        (name, org_id, visibility),
    )


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_superadmin_group_assignment_invokes_target_org_check(d03_env):
    """Superadmin PUT /users/{id}/groups must still enforce the target-org check.

    RED at base (200 — superadmin skips the target-org membership validation),
    GREEN after the fix (400: user is not a member of the group's org)."""
    org_id = _seed_org(d03_env.conn, "D03 Scope Org")
    group_id = _seed_group(d03_env.conn, org_id, "D03 Scope Group")
    # Target member exists but is NOT a member of the org owning the group.

    token = get_token(d03_env.superadmin_id, "superadmin", "superadmin")
    response = d03_env.client.put(
        f"/users/{d03_env.member_id}/groups",
        json={"group_ids": [group_id]},
        headers=_bearer(token),
    )

    assert response.status_code == 400


def test_org_member_removal_clears_org_group_memberships(d03_env):
    """DELETE /organizations/{org}/members/{user} must cascade to group_members.

    RED at base (org_members row removed, group_members row survives -> count 1),
    GREEN after the fix (count 0)."""
    org_id = _seed_org(d03_env.conn, "D03 Removal Org")
    group_id = _seed_group(d03_env.conn, org_id, "D03 Removal Group")
    _seed_org_member(d03_env.conn, org_id, d03_env.member_id, "member")
    _seed_group_member(d03_env.conn, group_id, d03_env.member_id)
    # Caller must be org admin/owner for remove_org_member.
    _seed_org_member(d03_env.conn, org_id, d03_env.admin_id, "admin")

    token = get_token(d03_env.admin_id, "d03admin", "admin")
    response = d03_env.client.delete(
        f"/api/organizations/{org_id}/members/{d03_env.member_id}",
        headers=_bearer(token),
    )

    assert response.status_code == 200
    count = d03_env.conn.execute(
        "SELECT COUNT(*) FROM group_members gm "
        "JOIN groups g ON g.id = gm.group_id "
        "WHERE gm.user_id = ? AND g.org_id = ?",
        (d03_env.member_id, org_id),
    ).fetchone()[0]
    assert count == 0


def test_user_org_replace_clears_dropped_org_group_memberships(d03_env):
    """PUT /users/{id}/organizations dropping an org must drop its group_members.

    RED at base (org membership replaced, group_members row survives -> count 1),
    GREEN after the fix (count 0)."""
    org_a = _seed_org(d03_env.conn, "D03 Replace Org A")
    org_b = _seed_org(d03_env.conn, "D03 Replace Org B")
    group_id = _seed_group(d03_env.conn, org_a, "D03 Replace Group")
    _seed_org_member(d03_env.conn, org_a, d03_env.member_id, "member")
    _seed_group_member(d03_env.conn, group_id, d03_env.member_id)

    token = get_token(d03_env.superadmin_id, "superadmin", "superadmin")
    # Replacement list omits org_a (keeps only org_b), so org_a is dropped.
    response = d03_env.client.put(
        f"/users/{d03_env.member_id}/organizations",
        json={"memberships": [{"org_id": org_b, "role": "member"}]},
        headers=_bearer(token),
    )

    assert response.status_code == 200
    count = d03_env.conn.execute(
        "SELECT COUNT(*) FROM group_members gm "
        "JOIN groups g ON g.id = gm.group_id "
        "WHERE gm.user_id = ? AND g.org_id = ?",
        (d03_env.member_id, org_a),
    ).fetchone()[0]
    assert count == 0


@pytest.mark.asyncio
async def test_orphaned_group_membership_confers_no_vault_permission(d03_env):
    """A group_members row without org membership must grant no vault permission.

    RED at base (get_effective_vault_permission returns 'write'), GREEN after
    the fix (None)."""
    from app.services.authz_policy import get_effective_vault_permission

    org_id = _seed_org(d03_env.conn, "D03 Orphan Org")
    vault_id = _seed_vault(d03_env.conn, "D03 Orphan Vault", org_id, "private")
    group_id = _seed_group(d03_env.conn, org_id, "D03 Orphan Group")
    _insert(
        d03_env.conn,
        "INSERT INTO vault_group_access (vault_id, group_id, permission) "
        "VALUES (?, ?, ?)",
        (vault_id, group_id, "write"),
    )
    # group membership WITHOUT the org_members row -> orphaned membership.
    _seed_group_member(d03_env.conn, group_id, d03_env.member_id)

    # get_effective_vault_permissions queries via asyncio.to_thread, so the
    # connection must tolerate cross-thread use (test_org_group_vault_chain
    # pattern).
    conn = sqlite3.connect(d03_env.db_path, check_same_thread=False)
    try:
        permission = await get_effective_vault_permission(
            conn, {"id": d03_env.member_id, "role": "member"}, vault_id
        )
    finally:
        conn.close()

    assert permission is None


def test_org_replace_by_scoped_admin_keeps_out_of_scope_membership(d03_env):
    """A scoped admin's org replace must not touch orgs outside their scope.

    RED at base (caller-scoped replace deletes the org2 membership -> count 0),
    GREEN after the fix (org2 membership survives -> count 1)."""
    org1 = _seed_org(d03_env.conn, "D03 Scoped Org One")
    org2 = _seed_org(d03_env.conn, "D03 Scoped Org Two")
    _seed_org_member(d03_env.conn, org1, d03_env.member_id, "member")
    _seed_org_member(d03_env.conn, org2, d03_env.member_id, "member")
    # Caller is admin of org1 only (member of org1, not of org2).
    _seed_org_member(d03_env.conn, org1, d03_env.admin_id, "admin")

    token = get_token(d03_env.admin_id, "d03admin", "admin")
    response = d03_env.client.put(
        f"/users/{d03_env.member_id}/organizations",
        json={"memberships": [{"org_id": org1, "role": "member"}]},
        headers=_bearer(token),
    )

    assert response.status_code == 200
    count = d03_env.conn.execute(
        "SELECT COUNT(*) FROM org_members WHERE user_id = ? AND org_id = ?",
        (d03_env.member_id, org2),
    ).fetchone()[0]
    assert count == 1


def test_group_side_grant_records_granted_by(d03_env):
    """PUT /groups/{id}/vaults must record who granted the vault access.

    RED at base (granted_by stays NULL), GREEN after the fix (granted_by is
    the calling user's id)."""
    org_id = _seed_org(d03_env.conn, "D03 Grant Org")
    group_id = _seed_group(d03_env.conn, org_id, "D03 Grant Group")
    vault_id = _seed_vault(d03_env.conn, "D03 Grant Vault", org_id, "private")
    # Caller may manage the group: global admin role (evaluate_policy group
    # update) AND org member (the route's org-membership requirement).
    _seed_org_member(d03_env.conn, org_id, d03_env.admin_id, "admin")

    token = get_token(d03_env.admin_id, "d03admin", "admin")
    response = d03_env.client.put(
        f"/groups/{group_id}/vaults",
        json={"vault_access": [{"vault_id": vault_id, "permission": "write"}]},
        headers=_bearer(token),
    )

    assert response.status_code == 200
    granted_by = d03_env.conn.execute(
        "SELECT granted_by FROM vault_group_access WHERE vault_id = ? AND group_id = ?",
        (vault_id, group_id),
    ).fetchone()[0]
    assert granted_by == d03_env.admin_id


def test_invite_survives_or_revokes_on_invitee_rename(d03_env):
    """Renaming an invited user must keep the invite redeemable or revoke it.

    RED at base (accept 403 because the invite still binds the old username,
    and the invite is not revoked), GREEN after the fix (accept succeeds or
    the invite is revoked)."""
    inviter_id = create_user(
        d03_env.conn, "d03inviter", "pass123", "member", "D03 Inviter"
    )
    org_id = _seed_org(d03_env.conn, "D03 Invite Org")
    _seed_org_member(d03_env.conn, org_id, inviter_id, "owner")

    inviter_token = get_token(inviter_id, "d03inviter", "member")
    create = d03_env.client.post(
        f"/api/organizations/{org_id}/invites",
        json={"email": "d03member", "role": "member"},
        headers=_bearer(inviter_token),
    )
    assert create.status_code == 201
    invite_id = create.json()["invite_id"]
    raw_token = create.json()["token"]

    superadmin_token = get_token(d03_env.superadmin_id, "superadmin", "superadmin")
    rename = d03_env.client.patch(
        f"/users/{d03_env.member_id}",
        json={"username": "renamed-user"},
        headers=_bearer(superadmin_token),
    )
    assert rename.status_code == 200

    # The invitee (same user id; the token's username claim is not re-checked)
    # attempts to accept the invite after the rename.
    member_token = get_token(d03_env.member_id, "d03member", "member")
    accept = d03_env.client.post(
        "/api/organizations/invites/accept",
        json={"token": raw_token},
        headers=_bearer(member_token),
    )
    revoked_at = d03_env.conn.execute(
        "SELECT revoked_at FROM org_invites WHERE id = ?", (invite_id,)
    ).fetchone()[0]

    resolved = (accept.status_code == 200) or (revoked_at is not None)
    assert resolved is True


def test_resend_invokes_redeemability_checks(d03_env):
    """Resending an invite must re-run the redeemability checks.

    RED at base (resend succeeds with 201 even though the invitee's global
    role was downgraded below member), GREEN after the fix (400)."""
    inviter_id = create_user(
        d03_env.conn, "d03inviter", "pass123", "member", "D03 Inviter"
    )
    org_id = _seed_org(d03_env.conn, "D03 Resend Org")
    _seed_org_member(d03_env.conn, org_id, inviter_id, "owner")

    inviter_token = get_token(inviter_id, "d03inviter", "member")
    create = d03_env.client.post(
        f"/api/organizations/{org_id}/invites",
        json={"email": "d03member", "role": "member"},
        headers=_bearer(inviter_token),
    )
    assert create.status_code == 201
    invite_id = create.json()["invite_id"]

    # Downgrade the invitee below member in SQL: the invite is no longer
    # redeemable (accept requires global role >= member).
    d03_env.conn.execute(
        "UPDATE users SET role = 'viewer' WHERE id = ?", (d03_env.member_id,)
    )
    d03_env.conn.commit()

    response = d03_env.client.post(
        f"/api/organizations/{org_id}/invites/{invite_id}/resend",
        headers=_bearer(inviter_token),
    )

    assert response.status_code == 400
