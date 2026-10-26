"""Hardening tests for the collated trace 1025-1003 fix (non-frozen).

Covers the plan's A5 inventory beyond the frozen checks:

* same-user dual-admin demote (direct admin demoted while the user's group
  grant keeps them effective admin -> allowed);
* group-derived-only admin user deletion refused 400 naming the vault;
* adoption success/failure matrix (non-superadmin org member, org->org,
  foreign-grant conflict);
* bulk explicit-empty refusal 409 on the vault side;
* group-side replace guard incl. the multi-vault rollback (one vault keeps
  an admin, a second would lose its last -> 409 + complete rollback);
* single-admin (sentinel principal) update_vault_member and
  update_group_vaults 200s with granted_by IS NULL;
* the two concurrency interleavings (demote-vs-bulk-clear,
  group-revoke-vs-member-demote) pinned deterministically: the first writer
  holds its BEGIN IMMEDIATE (its first write executed, uncommitted) on a raw
  connection while the second writer's request runs in a worker thread and
  must block on the write lock, then observe the guarded post-commit state;
* the three adoption-vs-grant races (POST group-access, vault-side bulk
  replace, group-side replace), serialized the same way — the grant writer
  re-reads the vault org inside its own BEGIN IMMEDIATE, so a committed
  adoption always flips the outcome and the post-state is never cross-org;
* audit-event pins: exactly one ``vault.org_id_updated`` after a successful
  adoption with key-by-key metadata, the public-vault narrowing case, and
  zero events on every refused adoption.

Feedback-round additions (PR #1027):

* the ALLOWED directions of the shared effective-admin guard (group-access
  demote/revoke, member removal, user deletion, bulk replace) — each with a
  second coverage layer surviving the mutation, plus the sole-coverage
  refusal direction for member removal;
* single-admin PATCH group-access granted_by NULL (the sixth sharing site);
* the bulk-clear serialization discriminator (F-003): a racing grant DELETE
  commits while the clear waits on BEGIN IMMEDIATE, so the clear observes
  the committed admin-less pre-state and answers 200, not a stale-snapshot
  409.

Harness mirrors the frozen files' d07 pattern (own temp DB, get_db override,
csrf override, users.get_pool patch, settings save/restore) and supports both
multi-user mode (mutable acting-user dict) and single-admin mode (real
admin-token auth so the id-0 sentinel principal is minted).
"""

import json
import os
import shutil
import sqlite3
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_current_active_user, get_db
from app.main import app
from app.models.database import SQLiteConnectionPool, _pool_cache, init_db
from app.security import csrf_protect

_SINGLE_ADMIN_TOKEN = "t1025h-admin-secret-" + "x" * 48


class _HardeningHarness:
    """Full-app harness (d07 pattern) with selectable auth mode."""

    def __init__(self, users_enabled: bool = True):
        self.temp_dir = tempfile.mkdtemp(prefix="t1025h_")
        self.db_path = os.path.join(self.temp_dir, "t1025h.db")
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
        app.dependency_overrides[csrf_protect] = lambda: "t1025h-csrf"

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
            self.acting_user = {
                "id": 1,
                "username": "superadmin",
                "full_name": "S",
                "role": "superadmin",
                "is_active": True,
                "must_change_password": False,
            }
            app.dependency_overrides[get_current_active_user] = lambda: self.acting_user
        else:
            settings.users_enabled = False
            settings.admin_secret_token = _SINGLE_ADMIN_TOKEN
            self.auth = {"Authorization": "Bearer " + _SINGLE_ADMIN_TOKEN}

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

    def seed_group(self, org_id, name):
        return self.insert(
            "INSERT INTO groups (org_id, name) VALUES (?, ?)", (org_id, name)
        )

    def seed_org_group_member(self, tag, user_id):
        """Org + group in that org + the user in BOTH the org and the group —
        the exact shape authz_policy requires for group-derived access."""
        org_id = self.seed_org("T1025H Org " + tag)
        group_id = self.seed_group(org_id, "T1025H Group " + tag)
        self.insert(
            "INSERT INTO org_members (org_id, user_id, role) VALUES (?, ?, 'member')",
            (org_id, user_id),
        )
        self.insert(
            "INSERT INTO group_members (group_id, user_id) VALUES (?, ?)",
            (group_id, user_id),
        )
        return org_id, group_id

    def seed_vault(self, name, org_id=None, visibility="private"):
        return self.insert(
            "INSERT INTO vaults (name, org_id, visibility) VALUES (?, ?, ?)",
            (name, org_id, visibility),
        )

    def run_request_while_locked(self, held_writes, request_callable):
        """Run ``request_callable`` while a first writer holds its transaction.

        The holder is a RAW connection executing BEGIN IMMEDIATE plus the
        first writer's writes (uncommitted). The second writer — the HTTP
        request — necessarily serializes after the holder's commit: its
        transaction_context BEGIN IMMEDIATE blocks on (or starts after) the
        held write lock, so it always observes the post-commit state. That is
        the deterministic pin for the interleaving.

        The executor is created EXPLICITLY (never a ``with`` block) so a
        result timeout cannot wedge teardown on ``shutdown(wait=True)``
        (ML-T-1): on TimeoutError the executor is abandoned with
        ``shutdown(wait=False, cancel_futures=True)`` and the timeout
        propagates; the normal path shuts down without waiting once the
        future has already produced its result.
        """
        holder = sqlite3.connect(self.db_path, timeout=30)
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            holder.execute("BEGIN IMMEDIATE")
            for sql, params in held_writes:
                holder.execute(sql, params)
            future = executor.submit(request_callable)
            # Give the second writer time to reach (and block on) its
            # BEGIN IMMEDIATE; if it has not started yet the outcome is
            # identical — it then begins after the commit.
            time.sleep(0.5)
            holder.commit()
            try:
                return future.result(timeout=30)
            except TimeoutError:
                # Do NOT let a wedged request block teardown: abandon the
                # executor without waiting for the worker thread and let the
                # timeout propagate to the caller (ML-T-1).
                executor.shutdown(wait=False, cancel_futures=True)
                executor = None
                raise
        finally:
            holder.close()
            if executor is not None:
                executor.shutdown(wait=False)

    def audit_events(self, event_type):
        rows = []
        conn = self.pool.get_connection()
        try:
            for row in conn.execute(
                "SELECT metadata_json FROM security_audit_log WHERE event_type = ?"
                " ORDER BY id",
                (event_type,),
            ).fetchall():
                rows.append(json.loads(row[0]))
            return rows
        finally:
            self.pool.release_connection(conn)

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
def hard():
    harness = _HardeningHarness(users_enabled=True)
    yield harness
    harness.close()


@pytest.fixture
def hard_single_admin():
    harness = _HardeningHarness(users_enabled=False)
    yield harness
    harness.close()


# Effective-admin inventory ----------------------------------------------------


def test_same_user_dual_admin_demote_allowed(hard):
    """A member who is BOTH a direct admin and a group-derived admin may be
    demoted on the direct row — their group grant keeps them effective admin
    (the direct-only count refused this at base)."""
    hard.seed_user(2, "t1025hdualadmin")
    org_id, group_id = hard.seed_org_group_member("dual", 2)
    vault_id = hard.seed_vault("T1025H Dual-Admin Vault")
    hard.insert(
        "INSERT INTO vault_members (vault_id, user_id, permission, granted_by)"
        " VALUES (?, 2, 'admin', 1)",
        (vault_id,),
    )
    hard.insert(
        "INSERT INTO vault_group_access (vault_id, group_id, permission, granted_by)"
        " VALUES (?, ?, 'admin', 1)",
        (vault_id, group_id),
    )

    client = hard.no_raise()
    try:
        resp = client.patch(
            "/api/vaults/%d/members/2" % vault_id, json={"permission": "read"}
        )
    finally:
        client.close()
    assert resp.status_code == 200

    row = hard.fetch_one(
        "SELECT permission FROM vault_members WHERE vault_id = ? AND user_id = 2",
        (vault_id,),
    )
    assert row[0] == "read"


def test_delete_user_refused_when_group_derived_admin_is_sole_coverage(hard):
    """Deleting a user whose ONLY admin coverage is a group grant must be
    refused 400 naming the vault (the cascade deletes group_members rows too,
    zeroing the vault)."""
    hard.seed_user(2, "t1025hgroupadmin")
    org_id, group_id = hard.seed_org_group_member("delete", 2)
    vault_id = hard.seed_vault("T1025H Delete-Guard Vault")
    hard.insert(
        "INSERT INTO vault_group_access (vault_id, group_id, permission, granted_by)"
        " VALUES (?, ?, 'admin', 1)",
        (vault_id, group_id),
    )
    assert (
        hard.count(
            "SELECT COUNT(*) FROM vault_members WHERE vault_id = ?", (vault_id,)
        )
        == 0
    )

    client = hard.no_raise()
    try:
        resp = client.delete("/api/users/2")
    finally:
        client.close()
    assert resp.status_code == 400
    assert str(vault_id) in resp.json()["detail"]
    assert hard.count("SELECT COUNT(*) FROM users WHERE id = 2") == 1


# Org adoption -----------------------------------------------------------------


def test_non_superadmin_org_member_can_adopt(hard):
    """A non-superadmin vault admin who IS a member of the target org may
    adopt their org-less vault into it (the create flow's rule)."""
    hard.seed_user(2, "t1025hadoptingadmin")
    hard.act_as(2, "member")
    org_id = hard.seed_org("T1025H Adoption Org")
    hard.insert(
        "INSERT INTO org_members (org_id, user_id, role) VALUES (?, ?, 'member')",
        (org_id, 2),
    )
    vault_id = hard.seed_vault("T1025H Member-Adoption Vault")
    hard.insert(
        "INSERT INTO vault_members (vault_id, user_id, permission, granted_by)"
        " VALUES (?, 2, 'admin', 1)",
        (vault_id,),
    )

    client = hard.no_raise()
    try:
        resp = client.put("/api/vaults/%d" % vault_id, json={"org_id": org_id})
    finally:
        client.close()
    assert resp.status_code == 200
    row = hard.fetch_one("SELECT org_id FROM vaults WHERE id = ?", (vault_id,))
    assert row[0] == org_id


def test_org_to_org_reassignment_refused_400(hard):
    org_a = hard.seed_org("T1025H Org A")
    org_b = hard.seed_org("T1025H Org B")
    vault_id = hard.seed_vault("T1025H Reassign Vault", org_id=org_a)

    client = hard.no_raise()
    try:
        resp = client.put("/api/vaults/%d" % vault_id, json={"org_id": org_b})
    finally:
        client.close()
    assert resp.status_code == 400
    row = hard.fetch_one("SELECT org_id FROM vaults WHERE id = ?", (vault_id,))
    assert row[0] == org_a


def test_adoption_refused_on_foreign_group_grant_conflict(hard):
    """A cross-org group grant (state the grant writers never produce) blocks
    adoption: silently keeping it would violate the same-org invariant."""
    org_a = hard.seed_org("T1025H Adopt Target")
    org_b = hard.seed_org("T1025H Foreign")
    group_b = hard.seed_group(org_b, "T1025H Foreign Group")
    vault_id = hard.seed_vault("T1025H Conflict Vault")
    hard.insert(
        "INSERT INTO vault_group_access (vault_id, group_id, permission, granted_by)"
        " VALUES (?, ?, 'read', 1)",
        (vault_id, group_b),
    )

    client = hard.no_raise()
    try:
        resp = client.put("/api/vaults/%d" % vault_id, json={"org_id": org_a})
    finally:
        client.close()
    assert resp.status_code == 400
    assert str(group_b) in resp.json()["detail"]
    row = hard.fetch_one("SELECT org_id FROM vaults WHERE id = ?", (vault_id,))
    assert row[0] is None
    assert (
        hard.count(
            "SELECT COUNT(*) FROM vault_group_access"
            " WHERE vault_id = ? AND group_id = ?",
            (vault_id, group_b),
        )
        == 1
    )


# Bulk replace guards ----------------------------------------------------------


def test_bulk_explicit_empty_refused_when_group_grant_is_sole_admin(hard):
    """PUT /vaults/{id}/groups with an explicit empty list cannot clear the
    vault's only effective-admin coverage."""
    hard.seed_user(2, "t1025hsoleadmin")
    org_id, group_id = hard.seed_org_group_member("bulkclear", 2)
    vault_id = hard.seed_vault("T1025H Bulk-Clear Vault")
    hard.insert(
        "INSERT INTO vault_group_access (vault_id, group_id, permission, granted_by)"
        " VALUES (?, ?, 'admin', 1)",
        (vault_id, group_id),
    )

    client = hard.no_raise()
    try:
        resp = client.put(
            "/api/vaults/%d/groups" % vault_id, json={"group_ids": []}
        )
    finally:
        client.close()
    assert resp.status_code == 409
    assert (
        hard.count(
            "SELECT COUNT(*) FROM vault_group_access WHERE vault_id = ?", (vault_id,)
        )
        == 1
    )


def test_group_side_replace_multi_vault_refusal_rolls_back(hard):
    """Group-side replace over two vaults: one vault retains an admin, a
    second would lose its last -> 409 naming the vaults and the WHOLE
    replacement rolls back (one vault's preserved admin must not mask
    another's zeroing)."""
    hard.seed_user(2, "t1025hkeeper")
    hard.seed_user(3, "t1025hloser")
    org_id = hard.seed_org("T1025H Replace Org")
    group_id = hard.seed_group(org_id, "T1025H Replace Group")
    for user_id in (2, 3):
        hard.insert(
            "INSERT INTO org_members (org_id, user_id, role)"
            " VALUES (?, ?, 'member')",
            (org_id, user_id),
        )
        hard.insert(
            "INSERT INTO group_members (group_id, user_id) VALUES (?, ?)",
            (group_id, user_id),
        )
    kept_vault = hard.seed_vault("T1025H Kept Vault", org_id=org_id)
    lost_vault = hard.seed_vault("T1025H Lost Vault", org_id=org_id)
    # Kept vault: direct admin user 2 PLUS the group grant.
    hard.insert(
        "INSERT INTO vault_members (vault_id, user_id, permission, granted_by)"
        " VALUES (?, 2, 'admin', 1)",
        (kept_vault,),
    )
    # Lost vault: the group grant is its ONLY admin coverage.
    for vault_id in (kept_vault, lost_vault):
        hard.insert(
            "INSERT INTO vault_group_access (vault_id, group_id, permission,"
            " granted_by) VALUES (?, ?, 'admin', 1)",
            (vault_id, group_id),
        )

    # Replace the group's vault set with just the kept vault at 'read'.
    client = hard.no_raise()
    try:
        resp = client.put(
            "/api/groups/%d/vaults" % group_id,
            json={"vault_access": [{"vault_id": kept_vault, "permission": "read"}]},
        )
    finally:
        client.close()
    assert resp.status_code == 409
    assert str(lost_vault) in resp.json()["detail"]

    # Complete rollback asserted via DB re-read: both original admin grants
    # survive untouched.
    for vault_id in (kept_vault, lost_vault):
        grant_row = hard.fetch_one(
            "SELECT permission FROM vault_group_access"
            " WHERE vault_id = ? AND group_id = ?",
            (vault_id, group_id),
        )
        assert grant_row is not None
        assert grant_row[0] == "admin"


# Effective-admin guard: allowed directions (feedback round) ------------------


def _seed_direct_admin_and_group_admin(hard, tag):
    """Vault with a DIRECT admin (user 2) plus an org-validated group grant
    whose admin coverage is user 3 — the two-layer shape the shared
    effective-admin inventory counts."""
    hard.seed_user(2, "t1025hadirect" + tag)
    hard.seed_user(3, "t1025hgrouped" + tag)
    org_id, group_id = hard.seed_org_group_member(tag, 3)
    vault_id = hard.seed_vault("T1025H " + tag.title() + " Vault")
    hard.insert(
        "INSERT INTO vault_members (vault_id, user_id, permission, granted_by)"
        " VALUES (?, 2, 'admin', 1)",
        (vault_id,),
    )
    hard.insert(
        "INSERT INTO vault_group_access (vault_id, group_id, permission, granted_by)"
        " VALUES (?, ?, 'admin', 1)",
        (vault_id, group_id),
    )
    return vault_id, group_id


def test_group_access_demote_allowed_when_other_admin_remains(hard):
    """PATCH group-access admin->read is ALLOWED while a direct admin holds
    the vault (L5-02): the sole-coverage refusal must fire only when the
    demote would zero the effective-admin set, and here user 2 stays admin."""
    vault_id, group_id = _seed_direct_admin_and_group_admin(hard, "gademote")

    client = hard.no_raise()
    try:
        resp = client.patch(
            "/api/vaults/%d/group-access/%d" % (vault_id, group_id),
            json={"permission": "read"},
        )
    finally:
        client.close()
    assert resp.status_code == 200

    grant_row = hard.fetch_one(
        "SELECT permission FROM vault_group_access"
        " WHERE vault_id = ? AND group_id = ?",
        (vault_id, group_id),
    )
    assert grant_row is not None
    assert grant_row[0] == "read"
    member_row = hard.fetch_one(
        "SELECT permission FROM vault_members WHERE vault_id = ? AND user_id = 2",
        (vault_id,),
    )
    assert member_row is not None
    assert member_row[0] == "admin"


def test_group_access_revoke_allowed_when_other_admin_remains(hard):
    """DELETE group-access is ALLOWED while a direct admin holds the vault
    (L5-03): the grant row goes, the direct admin survives untouched."""
    vault_id, group_id = _seed_direct_admin_and_group_admin(hard, "garevoke")

    client = hard.no_raise()
    try:
        resp = client.delete("/api/vaults/%d/group-access/%d" % (vault_id, group_id))
    finally:
        client.close()
    assert resp.status_code == 200
    assert (
        hard.count(
            "SELECT COUNT(*) FROM vault_group_access"
            " WHERE vault_id = ? AND group_id = ?",
            (vault_id, group_id),
        )
        == 0
    )
    member_row = hard.fetch_one(
        "SELECT permission FROM vault_members WHERE vault_id = ? AND user_id = 2",
        (vault_id,),
    )
    assert member_row is not None
    assert member_row[0] == "admin"


def test_remove_member_sole_admin_refused(hard):
    """Removing the SOLE direct admin (no other coverage) is refused 409 and
    the membership row survives (L5-04, refusal direction)."""
    hard.seed_user(2, "t1025hsolemember")
    vault_id = hard.seed_vault("T1025H Sole-Member Vault")
    hard.insert(
        "INSERT INTO vault_members (vault_id, user_id, permission, granted_by)"
        " VALUES (?, 2, 'admin', 1)",
        (vault_id,),
    )

    client = hard.no_raise()
    try:
        resp = client.delete("/api/vaults/%d/members/2" % vault_id)
    finally:
        client.close()
    assert resp.status_code == 409
    assert (
        hard.count(
            "SELECT COUNT(*) FROM vault_members WHERE vault_id = ? AND user_id = 2",
            (vault_id,),
        )
        == 1
    )


def test_remove_member_allowed_when_group_admin_remains(hard):
    """Removing a direct admin is ALLOWED when a group grant still confers
    admin on someone else (L5-04, allowed direction): the member row goes,
    the group grant stays intact."""
    vault_id, group_id = _seed_direct_admin_and_group_admin(hard, "memberdel")

    client = hard.no_raise()
    try:
        resp = client.delete("/api/vaults/%d/members/2" % vault_id)
    finally:
        client.close()
    assert resp.status_code == 200
    assert (
        hard.count(
            "SELECT COUNT(*) FROM vault_members WHERE vault_id = ? AND user_id = 2",
            (vault_id,),
        )
        == 0
    )
    assert (
        hard.count(
            "SELECT COUNT(*) FROM vault_group_access"
            " WHERE vault_id = ? AND group_id = ?",
            (vault_id, group_id),
        )
        == 1
    )


def test_user_delete_allowed_when_group_derived_admin_remains(hard):
    """Deleting the sole DIRECT admin of a vault is ALLOWED when a group
    grant keeps it covered (L5-05): the orphan computation's group layer
    sees the surviving group-derived admin, so the 400 never fires and the
    vault retains its coverage."""
    hard.seed_user(2, "t1025hlastdirect")
    hard.seed_user(3, "t1025hsurviving")
    org_id, group_id = hard.seed_org_group_member("userdel", 3)
    vault_id = hard.seed_vault("T1025H User-Delete Vault")
    hard.insert(
        "INSERT INTO vault_members (vault_id, user_id, permission, granted_by)"
        " VALUES (?, 2, 'admin', 1)",
        (vault_id,),
    )
    hard.insert(
        "INSERT INTO vault_group_access (vault_id, group_id, permission, granted_by)"
        " VALUES (?, ?, 'admin', 1)",
        (vault_id, group_id),
    )

    client = hard.no_raise()
    try:
        resp = client.delete("/api/users/2")
    finally:
        client.close()
    assert resp.status_code == 200
    assert hard.count("SELECT COUNT(*) FROM users WHERE id = 2") == 0
    assert (
        hard.count(
            "SELECT COUNT(*) FROM vault_group_access"
            " WHERE vault_id = ? AND group_id = ?",
            (vault_id, group_id),
        )
        == 1
    )


def test_vault_bulk_replace_allowed_with_nonempty_prestate(hard):
    """PUT /vaults/{id}/groups replacing a NON-admin grant set over a direct
    admin is ALLOWED (L5-06): the guard refuses only when the POST state
    would be empty, and the direct admin (user 2) keeps the post state
    non-empty — so a non-empty pre-state alone must not refuse."""
    hard.seed_user(2, "t1025hbulkholder")
    hard.seed_user(3, "t1025hbulkcovered")
    org_id, group_id = hard.seed_org_group_member("bulkok", 3)
    vault_id = hard.seed_vault("T1025H Bulk-Replace OK Vault")
    hard.insert(
        "INSERT INTO vault_members (vault_id, user_id, permission, granted_by)"
        " VALUES (?, 2, 'admin', 1)",
        (vault_id,),
    )
    hard.insert(
        "INSERT INTO vault_group_access (vault_id, group_id, permission, granted_by)"
        " VALUES (?, ?, 'read', 1)",
        (vault_id, group_id),
    )

    client = hard.no_raise()
    try:
        resp = client.put(
            "/api/vaults/%d/groups" % vault_id, json={"group_ids": [group_id]}
        )
    finally:
        client.close()
    assert resp.status_code == 200
    grant_row = hard.fetch_one(
        "SELECT permission FROM vault_group_access"
        " WHERE vault_id = ? AND group_id = ?",
        (vault_id, group_id),
    )
    assert grant_row is not None
    assert grant_row[0] == "read"
    member_row = hard.fetch_one(
        "SELECT permission FROM vault_members WHERE vault_id = ? AND user_id = 2",
        (vault_id,),
    )
    assert member_row is not None
    assert member_row[0] == "admin"


# Single-admin granted_by ------------------------------------------------------


def test_single_admin_member_patch_succeeds_with_null_granted_by(hard_single_admin):
    """PATCH member re-binds granted_by: the sentinel principal must record
    NULL (id 0 would trip the FK and 500 on a single-admin demote)."""
    hard_single_admin.seed_user(2, "t1025hamember")
    hard_single_admin.seed_user(3, "t1025hbmember")
    vault_id = hard_single_admin.seed_vault("T1025H Single-Admin Member Vault")
    for user_id in (2, 3):
        hard_single_admin.insert(
            "INSERT INTO vault_members (vault_id, user_id, permission, granted_by)"
            " VALUES (?, ?, 'admin', 1)",
            (vault_id, user_id),
        )

    client = hard_single_admin.no_raise()
    try:
        resp = client.patch(
            "/api/vaults/%d/members/2" % vault_id,
            json={"permission": "read"},
            headers=hard_single_admin.auth,
        )
    finally:
        client.close()
    assert resp.status_code == 200

    row = hard_single_admin.fetch_one(
        "SELECT permission, granted_by FROM vault_members"
        " WHERE vault_id = ? AND user_id = 2",
        (vault_id,),
    )
    assert row[0] == "read"
    assert row[1] is None


def test_single_admin_group_vault_replace_succeeds_with_null_granted_by(
    hard_single_admin,
):
    """Group-side replace binds granted_by too: the sentinel records NULL and
    the replace answers 200 (500 at base)."""
    hard_single_admin.seed_user(2, "t1025hcmember")
    org_id = hard_single_admin.seed_org("T1025H Single-Admin Org")
    group_id = hard_single_admin.seed_group(org_id, "T1025H Single-Admin Group")
    vault_id = hard_single_admin.seed_vault("T1025H Single-Admin Group Vault")
    hard_single_admin.insert(
        "INSERT INTO vault_members (vault_id, user_id, permission, granted_by)"
        " VALUES (?, 2, 'admin', 1)",
        (vault_id,),
    )

    client = hard_single_admin.no_raise()
    try:
        resp = client.put(
            "/api/groups/%d/vaults" % group_id,
            json={"vault_access": [{"vault_id": vault_id, "permission": "read"}]},
            headers=hard_single_admin.auth,
        )
    finally:
        client.close()
    assert resp.status_code == 200

    row = hard_single_admin.fetch_one(
        "SELECT permission, granted_by FROM vault_group_access"
        " WHERE vault_id = ? AND group_id = ?",
        (vault_id, group_id),
    )
    assert row is not None
    assert row[0] == "read"
    assert row[1] is None


def test_single_admin_patch_group_access_granted_by_null(hard_single_admin):
    """PATCH group-access re-binds granted_by (the sixth sharing-write site,
    L5-07): the sentinel principal must record NULL — id 0 (or any non-int
    principal id) would trip the users(id) FK and 500 the permission change."""
    hard_single_admin.seed_user(2, "t1025hgamember")
    org_id = hard_single_admin.seed_org("T1025H Single-Admin GA Org")
    group_id = hard_single_admin.seed_group(org_id, "T1025H Single-Admin GA Group")
    vault_id = hard_single_admin.seed_vault("T1025H Single-Admin GA Vault")
    hard_single_admin.insert(
        "INSERT INTO vault_group_access (vault_id, group_id, permission, granted_by)"
        " VALUES (?, ?, 'read', NULL)",
        (vault_id, group_id),
    )

    client = hard_single_admin.no_raise()
    try:
        resp = client.patch(
            "/api/vaults/%d/group-access/%d" % (vault_id, group_id),
            json={"permission": "write"},
            headers=hard_single_admin.auth,
        )
    finally:
        client.close()
    assert resp.status_code == 200

    row = hard_single_admin.fetch_one(
        "SELECT permission, granted_by FROM vault_group_access"
        " WHERE vault_id = ? AND group_id = ?",
        (vault_id, group_id),
    )
    assert row is not None
    assert row[0] == "write"
    assert row[1] is None


# Concurrency interleavings (deterministic two-connection pins) ----------------


def test_demote_vs_bulk_clear_serialized_refusal(hard):
    """Serialized-outcome pin: writer 1 (member demote) holds its
    transaction; writer 2 (bulk clear) runs while the lock is held and must
    end up refused on the state writer 1 committed — the demote committed,
    so the group grant is the last effective admin and the clear is refused
    with its DELETE rolled back. This asserts the second writer's observed
    OUTCOME, not which lock mechanism produced the ordering."""
    hard.seed_user(2, "t1025hdemoted")
    hard.seed_user(3, "t1025hgroupcovered")
    org_id, group_id = hard.seed_org_group_member("race1", 3)
    vault_id = hard.seed_vault("T1025H Race-Demote Vault")
    hard.insert(
        "INSERT INTO vault_members (vault_id, user_id, permission, granted_by)"
        " VALUES (?, 2, 'admin', 1)",
        (vault_id,),
    )
    hard.insert(
        "INSERT INTO vault_group_access (vault_id, group_id, permission, granted_by)"
        " VALUES (?, ?, 'admin', 1)",
        (vault_id, group_id),
    )

    client = hard.no_raise()

    def second_writer():
        return client.put(
            "/api/vaults/%d/groups" % vault_id, json={"group_ids": []}
        )

    resp = hard.run_request_while_locked(
        [
            (
                "UPDATE vault_members SET permission = 'read'"
                " WHERE vault_id = ? AND user_id = 2",
                (vault_id,),
            )
        ],
        second_writer,
    )
    client.close()
    assert resp.status_code == 409
    # Writer 1 committed the demote; writer 2's DELETE rolled back.
    member_row = hard.fetch_one(
        "SELECT permission FROM vault_members WHERE vault_id = ? AND user_id = 2",
        (vault_id,),
    )
    assert member_row[0] == "read"
    assert (
        hard.count(
            "SELECT COUNT(*) FROM vault_group_access WHERE vault_id = ?", (vault_id,)
        )
        == 1
    )


def test_group_revoke_vs_member_demote_serialized_refusal(hard):
    """Serialized-outcome pin: writer 1 (group revoke) holds its transaction;
    writer 2 (member demote) runs while the lock is held and ends up refused
    on the state writer 1 committed — the revoke committed, so the direct
    member is the last effective admin and the demote is refused (row
    unchanged). Asserts the observed outcome, not the lock mechanism."""
    hard.seed_user(2, "t1025hlaststanding")
    hard.seed_user(3, "t1025hgroupmember2")
    org_id, group_id = hard.seed_org_group_member("race2", 3)
    vault_id = hard.seed_vault("T1025H Race-Revoke Vault")
    hard.insert(
        "INSERT INTO vault_members (vault_id, user_id, permission, granted_by)"
        " VALUES (?, 2, 'admin', 1)",
        (vault_id,),
    )
    hard.insert(
        "INSERT INTO vault_group_access (vault_id, group_id, permission, granted_by)"
        " VALUES (?, ?, 'admin', 1)",
        (vault_id, group_id),
    )

    client = hard.no_raise()

    def second_writer():
        return client.patch(
            "/api/vaults/%d/members/2" % vault_id, json={"permission": "read"}
        )

    resp = hard.run_request_while_locked(
        [
            (
                "DELETE FROM vault_group_access WHERE vault_id = ? AND group_id = ?",
                (vault_id, group_id),
            )
        ],
        second_writer,
    )
    client.close()
    assert resp.status_code == 409
    member_row = hard.fetch_one(
        "SELECT permission FROM vault_members WHERE vault_id = ? AND user_id = 2",
        (vault_id,),
    )
    assert member_row[0] == "admin"
    assert (
        hard.count(
            "SELECT COUNT(*) FROM vault_group_access WHERE vault_id = ?", (vault_id,)
        )
        == 0
    )


def test_bulk_clear_after_holder_deletes_grant_observes_committed_state(hard):
    """THE serialization discriminator (F-003): the bulk-clear guard must
    observe the COMMITTED pre-state, not a snapshot taken while a racing
    delete is still uncommitted.

    The vault is org-less/global and its ONLY admin coverage is a group
    grant. The holder's transaction DELETEs that grant (uncommitted); the
    bulk-clear request runs while the write lock is held and must block on
    its BEGIN IMMEDIATE until the holder commits, then read the pre-state
    EMPTY — an already-admin-less vault stays repairable, so the clear
    answers 200 and the table ends empty. Discriminating: if the route's
    pre-read were NOT inside the BEGIN IMMEDIATE (e.g. a plain deferred
    snapshot before the lock), it would see the still-present grant
    (pre_nonempty True) and, reading an empty post-state, refuse 409.
    """
    hard.seed_user(2, "t1025hraceclear")
    org_id, group_id = hard.seed_org_group_member("raceclear", 2)
    vault_id = hard.seed_vault("T1025H Race-Clear Vault")
    hard.insert(
        "INSERT INTO vault_group_access (vault_id, group_id, permission, granted_by)"
        " VALUES (?, ?, 'admin', 1)",
        (vault_id, group_id),
    )

    client = hard.no_raise()

    def second_writer():
        return client.put(
            "/api/vaults/%d/groups" % vault_id, json={"group_ids": []}
        )

    resp = hard.run_request_while_locked(
        [
            (
                "DELETE FROM vault_group_access WHERE vault_id = ? AND group_id = ?",
                (vault_id, group_id),
            )
        ],
        second_writer,
    )
    client.close()
    # Serialized after the holder's commit: the pre-state read found NO
    # effective admins (the grant's DELETE committed first), so the
    # repairable-vault branch applies — 200, not the stale-snapshot 409.
    assert resp.status_code == 200
    assert (
        hard.count(
            "SELECT COUNT(*) FROM vault_group_access WHERE vault_id = ?", (vault_id,)
        )
        == 0
    )


# Adoption-vs-grant races (serialized under BEGIN IMMEDIATE) -------------------


def _seed_adoption_race(hard):
    """Org-less vault + org B group whose grant WOULD be legal pre-adoption
    (issue #721 AC9 pins org-less vaults accepting any-org groups)."""
    org_a = hard.seed_org("T1025H Adopt A")
    org_b = hard.seed_org("T1025H Grant B")
    group_b = hard.seed_group(org_b, "T1025H Grant-B Group")
    vault_id = hard.seed_vault("T1025H Race-Grant Vault")
    return org_a, org_b, group_b, vault_id


def _assert_never_cross_org(hard, vault_id, group_b):
    row = hard.fetch_one("SELECT org_id FROM vaults WHERE id = ?", (vault_id,))
    assert row[0] is not None
    assert (
        hard.count(
            "SELECT COUNT(*) FROM vault_group_access"
            " WHERE vault_id = ? AND group_id = ?",
            (vault_id, group_b),
        )
        == 0
    )


def test_adoption_vs_post_group_access_race(hard):
    org_a, org_b, group_b, vault_id = _seed_adoption_race(hard)

    client = hard.no_raise()

    def second_writer():
        return client.post(
            "/api/vaults/%d/group-access" % vault_id,
            json={"group_id": group_b, "permission": "read"},
        )

    resp = hard.run_request_while_locked(
        [("UPDATE vaults SET org_id = ? WHERE id = ?", (org_a, vault_id))],
        second_writer,
    )
    client.close()
    assert resp.status_code == 400
    _assert_never_cross_org(hard, vault_id, group_b)


def test_adoption_vs_vault_bulk_replace_race(hard):
    org_a, org_b, group_b, vault_id = _seed_adoption_race(hard)

    client = hard.no_raise()

    def second_writer():
        return client.put(
            "/api/vaults/%d/groups" % vault_id, json={"group_ids": [group_b]}
        )

    resp = hard.run_request_while_locked(
        [("UPDATE vaults SET org_id = ? WHERE id = ?", (org_a, vault_id))],
        second_writer,
    )
    client.close()
    assert resp.status_code == 400
    _assert_never_cross_org(hard, vault_id, group_b)


def test_adoption_vs_group_side_replace_race(hard):
    org_a, org_b, group_b, vault_id = _seed_adoption_race(hard)

    client = hard.no_raise()

    def second_writer():
        return client.put(
            "/api/groups/%d/vaults" % group_b,
            json={"vault_access": [{"vault_id": vault_id, "permission": "read"}]},
        )

    resp = hard.run_request_while_locked(
        [("UPDATE vaults SET org_id = ? WHERE id = ?", (org_a, vault_id))],
        second_writer,
    )
    client.close()
    assert resp.status_code == 400
    _assert_never_cross_org(hard, vault_id, group_b)


# Audit events -----------------------------------------------------------------


def test_adoption_records_exactly_one_org_id_updated_event(hard):
    org_id = hard.seed_org("T1025H Audit Org")
    vault_id = hard.seed_vault("T1025H Audit Vault")

    client = hard.no_raise()
    try:
        resp = client.put("/api/vaults/%d" % vault_id, json={"org_id": org_id})
    finally:
        client.close()
    assert resp.status_code == 200

    events = hard.audit_events("vault.org_id_updated")
    assert len(events) == 1
    metadata = events[0]
    assert metadata["vault_id"] == vault_id
    assert metadata["old_org_id"] is None
    assert metadata["new_org_id"] == org_id
    assert metadata["old_visibility"] == "private"
    # new_visibility records the visibility this same request persisted
    # (ML-P-1): an adoption-only PUT leaves it at the in-txn current value.
    assert metadata["new_visibility"] == "private"


def test_public_vault_adoption_metadata_carries_old_visibility(hard):
    """Adopting a PUBLIC org-less vault NARROWS global reads to org members —
    the metadata must record the pre-change visibility."""
    org_id = hard.seed_org("T1025H Narrowing Org")
    vault_id = hard.seed_vault("T1025H Narrowing Vault", visibility="public")

    client = hard.no_raise()
    try:
        resp = client.put("/api/vaults/%d" % vault_id, json={"org_id": org_id})
    finally:
        client.close()
    assert resp.status_code == 200

    events = hard.audit_events("vault.org_id_updated")
    assert len(events) == 1
    assert events[0]["old_visibility"] == "public"
    assert events[0]["new_visibility"] == "public"


@pytest.mark.parametrize(
    "refusal_case",
    ["org_to_org", "foreign_grant", "not_org_member", "missing_org"],
)
def test_refused_adoption_emits_no_event_and_leaves_org_unset(hard, refusal_case):
    org_a = hard.seed_org("T1025H Refusal Org A")
    org_b = hard.seed_org("T1025H Refusal Org B")

    if refusal_case == "org_to_org":
        vault_id = hard.seed_vault("T1025H Refusal Vault", org_id=org_a)
        payload_org = org_b
    elif refusal_case == "foreign_grant":
        group_b = hard.seed_group(org_b, "T1025H Refusal Foreign Group")
        vault_id = hard.seed_vault("T1025H Refusal Vault")
        hard.insert(
            "INSERT INTO vault_group_access (vault_id, group_id, permission,"
            " granted_by) VALUES (?, ?, 'read', 1)",
            (vault_id, group_b),
        )
        payload_org = org_a
    elif refusal_case == "not_org_member":
        hard.seed_user(2, "t1025houtsider")
        hard.act_as(2, "member")
        vault_id = hard.seed_vault("T1025H Refusal Vault")
        hard.insert(
            "INSERT INTO vault_members (vault_id, user_id, permission, granted_by)"
            " VALUES (?, 2, 'admin', 1)",
            (vault_id,),
        )
        payload_org = org_a
    else:  # missing_org
        vault_id = hard.seed_vault("T1025H Refusal Vault")
        payload_org = 999999

    expected_org = hard.fetch_one(
        "SELECT org_id FROM vaults WHERE id = ?", (vault_id,)
    )[0]

    client = hard.no_raise()
    try:
        resp = client.put("/api/vaults/%d" % vault_id, json={"org_id": payload_org})
    finally:
        client.close()
    assert resp.status_code in (400, 403)
    assert hard.audit_events("vault.org_id_updated") == []
    row = hard.fetch_one("SELECT org_id FROM vaults WHERE id = ?", (vault_id,))
    assert row[0] == expected_org
