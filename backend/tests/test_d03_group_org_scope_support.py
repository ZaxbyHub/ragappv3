"""Support tests for the #721 group-membership org-scoping fix (non-frozen).

Covers the plan's item-13 pins that the frozen d03 file does not: the
journaled orphan purge migration, the AC3/AC5 scoped-replace edge computation,
AC8 create/resend redeemability parity, AC7's two rename directions, and the
vault-creation granted_by provenance pin (vaults.py's creation writer).
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
    "d03s-support-admin-secret-token-0123456789abcdef0123456789abcdef",
)

import pytest  # noqa: E402
from backend.tests.user_route_helpers import (  # noqa: E402
    create_user,
    get_token,
    setup_test_db,
)
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class _Env:
    """Per-test namespace: seed connection, client, and standard users."""


@pytest.fixture()
def env(monkeypatch):
    """Fresh temp DB + mounted users/organizations/groups/vaults routers."""
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "app.db")

    from app.models.database import _pool_cache, _pool_cache_lock

    with _pool_cache_lock:
        for _path, pool in list(_pool_cache.items()):
            pool.close_all()
        _pool_cache.clear()

    conn = setup_test_db(db_path)
    # The vaults router's counts query joins memories/chat_sessions, which
    # TEST_SCHEMA does not carry.
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT, vault_id INTEGER);
        CREATE TABLE IF NOT EXISTS chat_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, vault_id INTEGER);
        """
    )
    conn.commit()

    monkeypatch.setattr("app.config.settings.data_dir", Path(tmpdir))
    monkeypatch.setattr(
        "app.config.settings.jwt_secret_key", os.environ["JWT_SECRET_KEY"]
    )
    monkeypatch.setattr("app.config.settings.users_enabled", True)

    e = _Env()
    e.conn = conn
    e.db_path = db_path
    e.superadmin_id = create_user(
        conn, "superadmin", "pass123", "superadmin", "Super Admin"
    )
    e.admin_id = create_user(conn, "d03sadmin", "pass123", "admin", "D03S Admin")
    e.member_id = create_user(conn, "d03smember", "pass123", "member", "D03S Member")

    from app.api.routes.groups import router as groups_router
    from app.api.routes.organizations import router as organizations_router
    from app.api.routes.users import router as users_router
    from app.api.routes.vaults import router as vaults_router
    from app.security import csrf_protect

    app = FastAPI()
    app.include_router(users_router)
    app.include_router(organizations_router, prefix="/api")
    app.include_router(groups_router)
    app.include_router(vaults_router, prefix="/api")
    app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"

    client = TestClient(app)
    client.headers["user-agent"] = ""
    e.client = client

    yield e

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


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _groups_in_org(conn, user_id, org_id):
    cursor = conn.execute(
        """SELECT COUNT(*) FROM group_members gm JOIN groups g ON g.id = gm.group_id
            WHERE gm.user_id = ? AND g.org_id = ?""",
        (user_id, org_id),
    )
    return cursor.fetchone()[0]


# ---------------------------------------------------------------------------
# Journaled orphan-purge migration (plan item 11/13)
# ---------------------------------------------------------------------------

def _seed_migration_db(path):
    """Minimal schema for migrate_purge_orphaned_group_members."""
    from app.models.migration_journal import MIGRATION_JOURNAL_DDL

    conn = sqlite3.connect(path)
    conn.executescript(
        MIGRATION_JOURNAL_DDL
        + """
        CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT);
        CREATE TABLE groups (id INTEGER PRIMARY KEY, org_id INTEGER NOT NULL, name TEXT);
        CREATE TABLE org_members (org_id INTEGER, user_id INTEGER, role TEXT);
        CREATE TABLE group_members (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER, user_id INTEGER);
        """
    )
    conn.execute("INSERT INTO users (id, username) VALUES (1, 'u1')")
    conn.execute("INSERT INTO users (id, username) VALUES (2, 'u2')")
    conn.execute("INSERT INTO groups (id, org_id, name) VALUES (10, 100, 'g1')")
    conn.execute("INSERT INTO groups (id, org_id, name) VALUES (11, 101, 'g2')")
    # user 1 is an org-100 member (their row in group 10 is legitimate).
    conn.execute("INSERT INTO org_members (org_id, user_id, role) VALUES (100, 1, 'member')")
    conn.execute("INSERT INTO group_members (group_id, user_id) VALUES (10, 1)")
    # user 2 has no org membership anywhere: both rows are orphaned.
    conn.execute("INSERT INTO group_members (group_id, user_id) VALUES (10, 2)")
    conn.execute("INSERT INTO group_members (group_id, user_id) VALUES (11, 2)")
    # user 1's row in group 11 (org 101) is orphaned too.
    conn.execute("INSERT INTO group_members (group_id, user_id) VALUES (11, 1)")
    conn.commit()
    return conn


def test_purge_migration_deletes_only_orphaned_rows():
    from app.models.database import migrate_purge_orphaned_group_members

    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, "purge.db")
    conn = _seed_migration_db(path)
    try:
        migrate_purge_orphaned_group_members(path)
        rows = conn.execute(
            "SELECT group_id, user_id FROM group_members ORDER BY group_id, user_id"
        ).fetchall()
        # Only (10, 1) survives: u1 is an org-100 member.
        assert rows == [(10, 1)]
        journal = conn.execute(
            "SELECT detail FROM migration_journal WHERE migration_name = 'migrate_purge_orphaned_group_members'"
        ).fetchall()
        assert len(journal) == 1
        assert "3" in journal[0][0]
    finally:
        conn.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_purge_migration_is_idempotent_and_journal_silent_on_second_run():
    from app.models.database import migrate_purge_orphaned_group_members

    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, "purge.db")
    conn = _seed_migration_db(path)
    try:
        migrate_purge_orphaned_group_members(path)
        migrate_purge_orphaned_group_members(path)
        journal = conn.execute(
            "SELECT COUNT(*) FROM migration_journal WHERE migration_name = 'migrate_purge_orphaned_group_members'"
        ).fetchone()[0]
        assert journal == 1  # converged boot journals nothing new
        rows = conn.execute("SELECT COUNT(*) FROM group_members").fetchone()[0]
        assert rows == 1
    finally:
        conn.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# AC3/AC5 edge computation (plan item 13)
# ---------------------------------------------------------------------------

def test_scoped_admin_in_scope_drop_cascades_group_cleanup(env):
    """Dropping an org the scoped caller DOES manage removes its group rows."""
    org1, org2 = _seed_org(env.conn, "Edge Org One"), _seed_org(env.conn, "Edge Org Two")
    g1, g2 = _seed_group(env.conn, org1, "edge-g1"), _seed_group(env.conn, org2, "edge-g2")
    _seed_org_member(env.conn, org1, env.admin_id, "admin")
    _seed_org_member(env.conn, org2, env.admin_id, "admin")
    _seed_org_member(env.conn, org1, env.member_id, "member")
    _seed_org_member(env.conn, org2, env.member_id, "member")
    _seed_group_member(env.conn, g1, env.member_id)
    _seed_group_member(env.conn, g2, env.member_id)

    token = get_token(env.admin_id, "d03sadmin", "admin")
    resp = env.client.put(
        f"/users/{env.member_id}/organizations",
        json={"memberships": [{"org_id": org2, "role": "member"}]},
        headers=_bearer(token),
    )
    assert resp.status_code == 200, resp.text
    assert _groups_in_org(env.conn, env.member_id, org1) == 0
    assert _groups_in_org(env.conn, env.member_id, org2) == 1


def test_owner_protected_org_keeps_group_members(env):
    """An org led by an owner-protected row survives the replace (with its groups)."""
    org1, org2 = _seed_org(env.conn, "Owner Org One"), _seed_org(env.conn, "Owner Org Two")
    g2 = _seed_group(env.conn, org2, "owner-g2")
    _seed_org_member(env.conn, org1, env.admin_id, "admin")
    _seed_org_member(env.conn, org1, env.member_id, "member")
    _seed_org_member(env.conn, org2, env.member_id, "owner")  # protected
    _seed_group_member(env.conn, g2, env.member_id)

    token = get_token(env.superadmin_id, "superadmin", "superadmin")
    resp = env.client.put(
        f"/users/{env.member_id}/organizations",
        json={"memberships": [{"org_id": org1, "role": "member"}]},
        headers=_bearer(token),
    )
    assert resp.status_code == 200, resp.text
    # org2's owner row survived the DELETE; the after-set contains org2, so
    # no group cleanup ran for it.
    assert _groups_in_org(env.conn, env.member_id, org2) == 1


def test_out_of_scope_org_group_members_survive_scoped_replace(env):
    """A scoped caller must not touch orgs they do not belong to (groups included)."""
    org1, org2 = _seed_org(env.conn, "Scope Org One"), _seed_org(env.conn, "Scope Org Two")
    g2 = _seed_group(env.conn, org2, "scope-g2")
    _seed_org_member(env.conn, org1, env.admin_id, "admin")
    _seed_org_member(env.conn, org1, env.member_id, "member")
    _seed_org_member(env.conn, org2, env.member_id, "member")
    _seed_group_member(env.conn, g2, env.member_id)

    token = get_token(env.admin_id, "d03sadmin", "admin")
    resp = env.client.put(
        f"/users/{env.member_id}/organizations",
        json={"memberships": [{"org_id": org1, "role": "member"}]},
        headers=_bearer(token),
    )
    assert resp.status_code == 200, resp.text
    cursor = env.conn.execute(
        "SELECT COUNT(*) FROM org_members WHERE user_id = ? AND org_id = ?",
        (env.member_id, org2),
    )
    assert cursor.fetchone()[0] == 1
    assert _groups_in_org(env.conn, env.member_id, org2) == 1


# ---------------------------------------------------------------------------
# AC8 create/resend redeemability parity (plan item 13)
# ---------------------------------------------------------------------------

def _create_invite(env, org_id, email, actor_token):
    return env.client.post(
        f"/api/organizations/{org_id}/invites",
        json={"email": email, "role": "member"},
        headers=_bearer(actor_token),
    )


def _resend(env, org_id, invite_id, actor_token):
    return env.client.post(
        f"/api/organizations/{org_id}/invites/{invite_id}/resend",
        headers=_bearer(actor_token),
    )


def _make_org_admin(env, org_id):
    _seed_org_member(env.conn, org_id, env.admin_id, "admin")
    return get_token(env.admin_id, "d03sadmin", "admin")


def test_resend_not_yet_provisioned_username_invite_stays_allowed(env):
    """#300 parity: username-form invites to nonexistent users are creatable and resendable."""
    org_id = _seed_org(env.conn, "Parity Org A")
    token = _make_org_admin(env, org_id)
    created = _create_invite(env, org_id, "futureuser", token)
    assert created.status_code == 201, created.text
    resend = _resend(env, org_id, created.json()["invite_id"], token)
    assert resend.status_code == 201, resend.text


def test_resend_email_form_invite_whose_user_vanished_is_400(env):
    """Email-form invite bound to a user who was deleted cannot be re-minted."""
    org_id = _seed_org(env.conn, "Parity Org B")
    token = _make_org_admin(env, org_id)
    doomed_id = create_user(env.conn, "vanishing@example.com", "pass123", "member", "V")
    created = _create_invite(env, org_id, "vanishing@example.com", token)
    assert created.status_code == 201, created.text
    # Remove the invitee entirely.
    env.conn.execute("DELETE FROM group_members WHERE user_id = ?", (doomed_id,))
    env.conn.execute("DELETE FROM org_members WHERE user_id = ?", (doomed_id,))
    env.conn.execute("DELETE FROM users WHERE id = ?", (doomed_id,))
    env.conn.commit()
    resend = _resend(env, org_id, created.json()["invite_id"], token)
    assert resend.status_code == 400
    assert "No user found" in resend.json()["detail"]


# ---------------------------------------------------------------------------
# AC7 both rename directions (plan item 13)
# ---------------------------------------------------------------------------

def _accept_invite(env, org_id, invite_id, token, payload):
    # The accept route is token-keyed and org-less in its path:
    # POST /organizations/invites/accept
    return env.client.post(
        "/api/organizations/invites/accept",
        json=payload,
        headers=_bearer(token),
    )


def test_rename_revokes_pending_invite_addressed_to_new_name(env):
    """alice→bob must revoke a pending invite addressed to a (future) bob."""
    org_id = _seed_org(env.conn, "Hijack Org")
    token = _make_org_admin(env, org_id)
    invite = _create_invite(env, org_id, "bob", token)
    assert invite.status_code == 201, invite.text

    rename = env.client.patch(
        f"/users/{env.member_id}",
        json={"username": "bob"},
        headers=_bearer(get_token(env.superadmin_id, "superadmin", "superadmin")),
    )
    assert rename.status_code == 200, rename.text
    row = env.conn.execute(
        "SELECT revoked_at FROM org_invites WHERE id = ?",
        (invite.json()["invite_id"],),
    ).fetchone()
    assert row[0] is not None


def test_rename_keeps_own_pending_invite_acceptable(env):
    """The renamer's own pending invite is re-bound and still acceptable (200)."""
    org_id = _seed_org(env.conn, "Rebind Org")
    token = _make_org_admin(env, org_id)
    invite = _create_invite(env, org_id, "d03smember", token)
    assert invite.status_code == 201, invite.text

    rename = env.client.patch(
        f"/users/{env.member_id}",
        json={"username": "renamedmember"},
        headers=_bearer(get_token(env.superadmin_id, "superadmin", "superadmin")),
    )
    assert rename.status_code == 200, rename.text
    row = env.conn.execute(
        "SELECT email, revoked_at FROM org_invites WHERE id = ?",
        (invite.json()["invite_id"],),
    ).fetchone()
    assert row[0] == "renamedmember"
    assert row[1] is None
    # The accept-200 arm: the renamed user can still redeem their own invite.
    accepted = _accept_invite(
        env,
        org_id,
        invite.json()["invite_id"],
        get_token(env.member_id, "renamedmember", "member"),
        {"token": invite.json()["token"]},
    )
    assert accepted.status_code == 200, accepted.text
    member_row = env.conn.execute(
        "SELECT COUNT(*) FROM org_members WHERE org_id = ? AND user_id = ?",
        (org_id, env.member_id),
    ).fetchone()
    assert member_row[0] == 1


# ---------------------------------------------------------------------------
# vaults.py creation-writer granted_by pin (plan item 6/13)
# ---------------------------------------------------------------------------

def test_vault_creation_records_granted_by(env):
    org_id = _seed_org(env.conn, "Vault Provenance Org")
    _seed_org_member(env.conn, org_id, env.member_id, "member")
    token = get_token(env.member_id, "d03smember", "member")
    created = env.client.post(
        "/api/vaults",
        json={"name": "Provenance Vault", "description": "", "org_id": org_id},
        headers=_bearer(token),
    )
    assert created.status_code in (200, 201), created.text
    vault_id = created.json()["id"]
    row = env.conn.execute(
        "SELECT user_id, permission, granted_by FROM vault_members WHERE vault_id = ?",
        (vault_id,),
    ).fetchone()
    assert row == (env.member_id, "admin", env.member_id)
