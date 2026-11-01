"""Support matrix for issue #1028 (plan-critic R1/R2 items); frozen spec stays
byte-untouched.

The frozen acceptance file (tests/test_1027_followup_mutation_graph.py) pins
the refusal core of the six-route fix: each membership/organization mutation
refuses 409 when it would take a vault's effective admins from non-empty to
empty, and pre-broken (zero-admin) vaults stay repairable. This file pins the
support matrix AROUND that core:

- s01: positive non-empty -> non-empty transitions — every family keeps its
  normal success status when a second effective admin survives, and the
  mutation is actually COMMITTED (rows change, not just status codes).
- s02-s06: mixed multi-vault refusals — when one mutation touches a covered
  vault A and an only-group-covered vault B at once, the 409 names ONLY B
  (A's surviving admin never masks B's orphaning), the whole replacement
  rolls back, and no success security event is recorded on refusal.
- s07: the scoped non-superadmin branches of users.py refuse the same way and
  leave memberships outside the caller's org scope untouched, AND a committing
  scoped replace strips only the caller-org scope (foreign-org membership
  survives the scoped DELETE — scope-predicate positive control).
- s08: genuine repairs (zero -> non-empty) stay allowed on the three writer
  surfaces that can ADD coverage.
- s09: candidate-bearing pre-broken floors — a discovered vault that is
  already at zero effective admins (empty group or stale org premise) never
  triggers a refusal and never blocks the mutation, for ALL SIX families
  (AC7 precision; the two later legs discriminate a refuse-on-post-state-only
  mutant for the group-members and user-orgs families).
- s10: SEQUENTIAL writers (the second request observes the first writer's
  committed state) and arriving-grant visibility for every candidate-discovery
  shape (group, org, user): a grant committed BEFORE the route's BEGIN
  IMMEDIATE is discovered inside the lock. Lock MODE is pinned by the #1025
  race suites, not here.
- s11: EVERY candidate-discovery call site (7 of 7 across the three route
  modules) runs its discovery read inside an open write transaction.
- s12: the unscoped (superadmin) non-empty replace branch refuses and rolls
  back — the one clear/replace x scoped/unscoped refusal cell that no other
  leg drives.
- s13: audit positive control — a SUCCESSFUL membership replace records its
  security event (the refusal-silence asserts in s04/s05 need their other
  half; a no-op audit sink used to pass the whole suite).

The harness is a private duplicate of the frozen file's _MutationHarness
(that file is frozen; importing from it is not the convention) with small
builders added on top. Acting principal is superadmin user id 1 except in
s07, which flips the mutable dict to a scoped platform admin. No
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


class _SupportHarness:
    """Private copy of the frozen file's _MutationHarness pattern: full-app
    harness (own temp DB, get_db override, csrf override, users/organizations
    module get_pool patches, settings save/restore, mutable acting-user dict)
    plus the builders the support matrix needs."""

    def __init__(self):
        # Snapshot-restore (not pop) for app.dependency_overrides: a nested
        # harness closed while an outer harness is still live must hand the
        # PREVIOUS harness's overrides back, not leave the app bare
        # (implementation-review finding L5-C6).
        self._overrides_snapshot = dict(app.dependency_overrides)
        self.temp_dir = tempfile.mkdtemp(prefix="t1028s_")
        self.db_path = os.path.join(self.temp_dir, "t1028s.db")
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

        self._user_seq = 0

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

    # --- builders -----------------------------------------------------------

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

    def add_user(self, role="member"):
        self._user_seq += 1
        return self.insert(
            "INSERT INTO users (username, hashed_password, role, is_active)"
            " VALUES (?, 'x', ?, 1)",
            ("t1028s-u%d" % self._user_seq, role),
        )

    def add_org(self, name):
        return self.insert(
            "INSERT INTO organizations (name, description) VALUES (?, '')",
            (name,),
        )

    def add_group(self, org_id, name):
        return self.insert(
            "INSERT INTO groups (name, description, org_id) VALUES (?, '', ?)",
            (name, org_id),
        )

    def add_vault(self, name):
        return self.insert(
            "INSERT INTO vaults (name, org_id, visibility)"
            " VALUES (?, NULL, 'private')",
            (name,),
        )

    def add_org_member(self, org_id, user_id, role="member"):
        return self.insert(
            "INSERT INTO org_members (org_id, user_id, role) VALUES (?, ?, ?)",
            (org_id, user_id, role),
        )

    def add_group_member(self, group_id, user_id):
        return self.insert(
            "INSERT INTO group_members (group_id, user_id) VALUES (?, ?)",
            (group_id, user_id),
        )

    def grant_group_admin(self, vault_id, group_id):
        return self.insert(
            "INSERT INTO vault_group_access"
            " (vault_id, group_id, permission, granted_by)"
            " VALUES (?, ?, 'admin', NULL)",
            (vault_id, group_id),
        )

    def add_direct_vault_admin(self, vault_id):
        """A second, DIRECT effective admin (fresh user + vault_members
        'admin' row) — s01's surviving admin."""
        uid = self.add_user()
        self.insert(
            "INSERT INTO vault_members (vault_id, user_id, permission)"
            " VALUES (?, ?, 'admin')",
            (vault_id, uid),
        )
        return uid

    def build_two_vault_mixed(self, direct_admin_on_a=True):
        """The s02-s06 mixed shape: ONE org + ONE group with member U (org +
        group rows); vault A holds the group's admin grant PLUS a direct
        admin, vault B holds ONLY the group grant. Any mutation stripping the
        group coverage orphans B but never A."""
        org_id = self.add_org("T1028M O")
        group_id = self.add_group(org_id, "T1028M G")
        uid = self.add_user()
        self.add_org_member(org_id, uid)
        self.add_group_member(group_id, uid)
        vault_a = self.add_vault("T1028M VA")
        vault_b = self.add_vault("T1028M VB")
        self.grant_group_admin(vault_a, group_id)
        self.grant_group_admin(vault_b, group_id)
        direct_uid = None
        if direct_admin_on_a:
            direct_uid = self.add_direct_vault_admin(vault_a)
        return org_id, group_id, uid, vault_a, vault_b, direct_uid

    def close(self):
        app.dependency_overrides.clear()
        app.dependency_overrides.update(self._overrides_snapshot)
        for m, orig in zip(self._mods, self._orig_get_pools):
            m.get_pool = orig
        for key, value in self._saved.items():
            setattr(self._settings, key, value)
        self.pool.close_all()
        _pool_cache.clear()
        shutil.rmtree(self.temp_dir, ignore_errors=True)


@pytest.fixture
def sup():
    harness = _SupportHarness()
    yield harness
    harness.close()


def _assert_names_only(detail, named_vault_id, *silent_vault_ids):
    """The 409 detail carries the production wording with exactly the
    orphaned vault's id in brackets, and no co-covered vault's id."""
    assert "would leave vault(s)" in detail
    assert "without an effective admin" in detail
    assert "[%d]" % named_vault_id in detail
    for vid in silent_vault_ids:
        assert "[%d]" % vid not in detail


# ---------------------------------------------------------------------------
# s01 — positive non-empty -> non-empty, all six families, fresh harness each
# ---------------------------------------------------------------------------


def _s01_leg(route):
    """One family per fresh harness: the frozen builder's vault PLUS a second
    (direct) effective admin, so clearing the group-derived coverage is a
    non-empty -> non-empty transition. The mutation must keep its normal
    success status, actually commit, and leave the surviving admin."""
    harness = _SupportHarness()
    try:
        org_id, group_id, uid, vault_id = harness.build_group_derived_only_vault()
        direct_uid = harness.add_direct_vault_admin(vault_id)
        assert harness.effective_admin_ids(vault_id) == {uid, direct_uid}

        if route == "group-members-replace":
            resp = harness.request(
                "PUT", "/api/groups/%d/members" % group_id, json={"user_ids": []}
            )
            assert resp.status_code == 200
            assert harness.count(
                "SELECT COUNT(*) FROM group_members"
                " WHERE group_id = ? AND user_id = ?",
                (group_id, uid),
            ) == 0
        elif route == "group-delete":
            resp = harness.request("DELETE", "/api/groups/%d" % group_id)
            assert resp.status_code == 204
            assert harness.count(
                "SELECT COUNT(*) FROM groups WHERE id = ?", (group_id,)
            ) == 0
            # cascade ran: the grant rows went with the group
            assert harness.count(
                "SELECT COUNT(*) FROM vault_group_access WHERE group_id = ?",
                (group_id,),
            ) == 0
        elif route == "user-groups-replace":
            resp = harness.request(
                "PUT", "/api/users/%d/groups" % uid, json={"group_ids": []}
            )
            assert resp.status_code == 200
            assert harness.count(
                "SELECT COUNT(*) FROM group_members"
                " WHERE group_id = ? AND user_id = ?",
                (group_id, uid),
            ) == 0
        elif route == "user-orgs-replace":
            resp = harness.request(
                "PUT", "/api/users/%d/organizations" % uid, json={"memberships": []}
            )
            assert resp.status_code == 200
            assert harness.count(
                "SELECT COUNT(*) FROM org_members WHERE org_id = ? AND user_id = ?",
                (org_id, uid),
            ) == 0
            # dropped-org cleanup committed with the replace
            assert harness.count(
                "SELECT COUNT(*) FROM group_members"
                " WHERE group_id = ? AND user_id = ?",
                (group_id, uid),
            ) == 0
        elif route == "org-member-remove":
            # remove_org_member's caller check does not honor platform
            # superadmin: seed an org owner row for the acting user.
            harness.add_org_member(org_id, 1, role="owner")
            resp = harness.request(
                "DELETE", "/api/organizations/%d/members/%d" % (org_id, uid)
            )
            assert resp.status_code == 200
            assert harness.count(
                "SELECT COUNT(*) FROM org_members WHERE org_id = ? AND user_id = ?",
                (org_id, uid),
            ) == 0
            assert harness.count(
                "SELECT COUNT(*) FROM group_members"
                " WHERE group_id = ? AND user_id = ?",
                (group_id, uid),
            ) == 0
        else:  # org-delete
            resp = harness.request("DELETE", "/api/organizations/%d" % org_id)
            assert resp.status_code == 200
            assert harness.count(
                "SELECT COUNT(*) FROM organizations WHERE id = ?", (org_id,)
            ) == 0

        assert harness.effective_admin_ids(vault_id) == {direct_uid}
    finally:
        harness.close()


def test_s01_positive_nonempty_to_nonempty_all_families():
    legs = (
        "group-members-replace",
        "group-delete",
        "user-groups-replace",
        "user-orgs-replace",
        "org-member-remove",
        "org-delete",
    )
    for route in legs:
        _s01_leg(route)


# ---------------------------------------------------------------------------
# s02-s06 — mixed multi-vault refusals name only the truly orphaned vault
# ---------------------------------------------------------------------------


def test_s02_group_members_replace_mixed_refusal(sup):
    _org_id, group_id, uid, vault_a, vault_b, direct_uid = (
        sup.build_two_vault_mixed()
    )
    other_uid = sup.add_user()
    sup.add_org_member(_org_id, other_uid)
    assert sup.effective_admin_ids(vault_a) == {uid, direct_uid}
    assert sup.effective_admin_ids(vault_b) == {uid}

    # Non-empty replacement keeping a DIFFERENT user: the route's own
    # validation (every listed user must be a member of the group's
    # organization) guarantees that user becomes a group-derived admin on
    # every admin-granted vault of the group, so B is re-covered, not
    # orphaned — 200 with the replacement committed. A non-empty valid
    # replacement can therefore NEVER produce this family's 409 (observed
    # against the fixed code; the guard would be wrong to refuse here).
    resp = sup.request(
        "PUT", "/api/groups/%d/members" % group_id, json={"user_ids": [other_uid]}
    )
    assert resp.status_code == 200
    assert sup.count(
        "SELECT COUNT(*) FROM group_members WHERE group_id = ? AND user_id = ?",
        (group_id, uid),
    ) == 0
    assert sup.count(
        "SELECT COUNT(*) FROM group_members WHERE group_id = ? AND user_id = ?",
        (group_id, other_uid),
    ) == 1
    assert sup.effective_admin_ids(vault_a) == {direct_uid, other_uid}
    assert sup.effective_admin_ids(vault_b) == {other_uid}

    # The mixed multi-vault refusal itself (fresh state): the empty
    # replacement drops the only covering member. 409 naming exactly B, the
    # DELETE+INSERT rolled back (original member row survives, nothing was
    # inserted), both grants intact, A still covered, B still covered by the
    # group grant.
    harness = _SupportHarness()
    try:
        org_id, group_id, uid, vault_a, vault_b, direct_uid = (
            harness.build_two_vault_mixed()
        )
        assert harness.effective_admin_ids(vault_a) == {uid, direct_uid}
        assert harness.effective_admin_ids(vault_b) == {uid}

        resp = harness.request(
            "PUT", "/api/groups/%d/members" % group_id, json={"user_ids": []}
        )
        assert resp.status_code == 409
        _assert_names_only(resp.json()["detail"], vault_b, vault_a)
        assert harness.count(
            "SELECT COUNT(*) FROM group_members WHERE group_id = ? AND user_id = ?",
            (group_id, uid),
        ) == 1
        assert harness.count(
            "SELECT COUNT(*) FROM vault_group_access"
            " WHERE group_id = ? AND permission = 'admin'",
            (group_id,),
        ) == 2
        assert harness.effective_admin_ids(vault_a) == {uid, direct_uid}
        assert harness.effective_admin_ids(vault_b) == {uid}
    finally:
        harness.close()


def test_s03_group_delete_mixed_refusal(sup):
    _org_id, group_id, uid, vault_a, vault_b, direct_uid = (
        sup.build_two_vault_mixed()
    )
    resp = sup.request("DELETE", "/api/groups/%d" % group_id)
    assert resp.status_code == 409
    _assert_names_only(resp.json()["detail"], vault_b, vault_a)
    assert sup.count("SELECT COUNT(*) FROM groups WHERE id = ?", (group_id,)) == 1
    assert sup.count(
        "SELECT COUNT(*) FROM vault_group_access"
        " WHERE group_id = ? AND permission = 'admin'",
        (group_id,),
    ) == 2
    assert sup.effective_admin_ids(vault_a) == {uid, direct_uid}
    assert sup.effective_admin_ids(vault_b) == {uid}


def test_s04_user_groups_replace_mixed_refusal(sup):
    _org_id, group_id, uid, vault_a, vault_b, _direct_uid = (
        sup.build_two_vault_mixed()
    )
    resp = sup.request(
        "PUT", "/api/users/%d/groups" % uid, json={"group_ids": []}
    )
    assert resp.status_code == 409
    _assert_names_only(resp.json()["detail"], vault_b, vault_a)
    assert sup.count(
        "SELECT COUNT(*) FROM group_members WHERE group_id = ? AND user_id = ?",
        (group_id, uid),
    ) == 1
    assert sup.count(
        "SELECT COUNT(*) FROM vault_group_access"
        " WHERE group_id = ? AND permission = 'admin'",
        (group_id,),
    ) == 2
    assert sup.effective_admin_ids(vault_b) == {uid}
    # refusal records NO success security event for THIS route either (the
    # event at users.py runs only after the handler returns; the 409 skips
    # it) — implementation-review finding 2: both auditing user routes carry
    # the zero-row assertion.
    assert sup.count(
        "SELECT COUNT(*) FROM security_audit_log"
        " WHERE event_type = 'membership.groups_replaced'"
        " AND target_user_id = ?",
        (uid,),
    ) == 0


def test_s05_user_orgs_replace_mixed_refusal_no_success_event(sup):
    org_id, group_id, uid, vault_a, vault_b, _direct_uid = (
        sup.build_two_vault_mixed()
    )
    resp = sup.request(
        "PUT", "/api/users/%d/organizations" % uid, json={"memberships": []}
    )
    assert resp.status_code == 409
    _assert_names_only(resp.json()["detail"], vault_b, vault_a)
    assert sup.count(
        "SELECT COUNT(*) FROM org_members WHERE org_id = ? AND user_id = ?",
        (org_id, uid),
    ) == 1
    # the dropped-org group cleanup rolled back with the org row
    assert sup.count(
        "SELECT COUNT(*) FROM group_members WHERE group_id = ? AND user_id = ?",
        (group_id, uid),
    ) == 1
    # refusal records NO success security event (safe_record_security_event
    # runs only after the mutation handler returns; the 409 skips it)
    assert sup.count(
        "SELECT COUNT(*) FROM security_audit_log"
        " WHERE event_type = 'membership.organizations_replaced'"
        " AND target_user_id = ?",
        (uid,),
    ) == 0


def test_s06_org_member_remove_and_org_delete_mixed_refusals():
    # family 5: remove_org_member
    harness = _SupportHarness()
    try:
        org_id, group_id, uid, vault_a, vault_b, _direct_uid = (
            harness.build_two_vault_mixed()
        )
        harness.add_org_member(org_id, 1, role="owner")
        resp = harness.request(
            "DELETE", "/api/organizations/%d/members/%d" % (org_id, uid)
        )
        assert resp.status_code == 409
        _assert_names_only(resp.json()["detail"], vault_b, vault_a)
        assert harness.count(
            "SELECT COUNT(*) FROM org_members WHERE org_id = ? AND user_id = ?",
            (org_id, uid),
        ) == 1
        assert harness.count(
            "SELECT COUNT(*) FROM group_members WHERE group_id = ? AND user_id = ?",
            (group_id, uid),
        ) == 1
        assert harness.effective_admin_ids(vault_b) == {uid}
    finally:
        harness.close()

    # family 6: delete_organization
    harness = _SupportHarness()
    try:
        org_id, _group_id, _uid, vault_a, vault_b, _direct_uid = (
            harness.build_two_vault_mixed()
        )
        resp = harness.request("DELETE", "/api/organizations/%d" % org_id)
        assert resp.status_code == 409
        _assert_names_only(resp.json()["detail"], vault_b, vault_a)
        assert harness.count(
            "SELECT COUNT(*) FROM organizations WHERE id = ?", (org_id,)
        ) == 1
        assert harness.effective_admin_ids(vault_b) == {_uid}
    finally:
        harness.close()


# ---------------------------------------------------------------------------
# s07 — scoped non-superadmin branches (users.py)
# ---------------------------------------------------------------------------


def _s07_setup(harness):
    """Org O1 (group G1 -> vault V1, the target's only coverage) and an
    unrelated org O2 with group G2; caller = platform admin user id 1, member
    of O1 only; target member-role user in O1+O2 and both groups."""
    o1 = harness.add_org("T1028s O1")
    o2 = harness.add_org("T1028s O2")
    g1 = harness.add_group(o1, "T1028s G1")
    g2 = harness.add_group(o2, "T1028s G2")
    uid = harness.add_user(role="member")
    harness.add_org_member(o1, uid)
    harness.add_org_member(o2, uid)
    harness.add_group_member(g1, uid)
    harness.add_group_member(g2, uid)
    vault_id = harness.add_vault("T1028s V1")
    harness.grant_group_admin(vault_id, g1)
    harness.add_org_member(o1, 1, role="admin")
    harness.acting_user["role"] = "admin"
    assert harness.effective_admin_ids(vault_id) == {uid}
    return o1, o2, g1, g2, uid, vault_id


def _s07_leg(leg):
    harness = _SupportHarness()
    try:
        o1, o2, g1, g2, uid, vault_id = _s07_setup(harness)
        expected = {uid}
        if leg == "clear-groups":
            resp = harness.request(
                "PUT", "/api/users/%d/groups" % uid, json={"group_ids": []}
            )
            assert resp.status_code == 409
            _assert_names_only(resp.json()["detail"], vault_id)
            # scoped clear refused AND rolled back; O2 membership untouched
            assert harness.count(
                "SELECT COUNT(*) FROM group_members"
                " WHERE group_id = ? AND user_id = ?",
                (g1, uid),
            ) == 1
            assert harness.count(
                "SELECT COUNT(*) FROM group_members"
                " WHERE group_id = ? AND user_id = ?",
                (g2, uid),
            ) == 1
        elif leg == "scoped-replace-drops-coverage":
            gx = harness.add_group(o1, "T1028s GX")
            resp = harness.request(
                "PUT", "/api/users/%d/groups" % uid, json={"group_ids": [gx]}
            )
            assert resp.status_code == 409
            _assert_names_only(resp.json()["detail"], vault_id)
            # replacement rolled back: covering membership survives, the
            # requested new one was never committed
            assert harness.count(
                "SELECT COUNT(*) FROM group_members"
                " WHERE group_id = ? AND user_id = ?",
                (g1, uid),
            ) == 1
            assert harness.count(
                "SELECT COUNT(*) FROM group_members"
                " WHERE group_id = ? AND user_id = ?",
                (gx, uid),
            ) == 0
        elif leg == "scoped-replace-commits-foreign-survives":
            # scope-predicate positive control (out-of-band review F-04): a
            # COMMITTING scoped replace must remove only the caller-org
            # scope's memberships; deleting the caller-org predicate from the
            # scoped DELETE would also strip the foreign O2 row and fail the
            # G2-survival assert below.
            direct_uid = harness.add_direct_vault_admin(vault_id)
            expected = {direct_uid}
            gy = harness.add_group(o1, "T1028s GY")
            resp = harness.request(
                "PUT", "/api/users/%d/groups" % uid, json={"group_ids": [gy]}
            )
            assert resp.status_code == 200
            assert harness.count(
                "SELECT COUNT(*) FROM group_members"
                " WHERE group_id = ? AND user_id = ?",
                (g1, uid),
            ) == 0  # in-scope membership actually replaced
            assert harness.count(
                "SELECT COUNT(*) FROM group_members"
                " WHERE group_id = ? AND user_id = ?",
                (gy, uid),
            ) == 1  # requested membership committed
            assert harness.count(
                "SELECT COUNT(*) FROM group_members"
                " WHERE group_id = ? AND user_id = ?",
                (g2, uid),
            ) == 1  # FOREIGN-org membership survives the scoped DELETE
        else:  # clear-organizations
            resp = harness.request(
                "PUT", "/api/users/%d/organizations" % uid, json={"memberships": []}
            )
            assert resp.status_code == 409
            _assert_names_only(resp.json()["detail"], vault_id)
            # scoped org clear rolled back; both org rows survive
            assert harness.count(
                "SELECT COUNT(*) FROM org_members WHERE org_id = ? AND user_id = ?",
                (o1, uid),
            ) == 1
            assert harness.count(
                "SELECT COUNT(*) FROM org_members WHERE org_id = ? AND user_id = ?",
                (o2, uid),
            ) == 1
            expected = {uid}
        assert harness.effective_admin_ids(vault_id) == expected
    finally:
        harness.close()


def test_s07_scoped_nonsuperadmin_refusals():
    for leg in (
        "clear-groups",
        "scoped-replace-drops-coverage",
        "clear-organizations",
        "scoped-replace-commits-foreign-survives",
    ):
        _s07_leg(leg)


# ---------------------------------------------------------------------------
# s08 — genuine repairs (zero -> non-empty) stay allowed
# ---------------------------------------------------------------------------


def _s08_leg(leg):
    harness = _SupportHarness()
    try:
        org_id = harness.add_org("T1028r O")
        group_id = harness.add_group(org_id, "T1028r G")
        uid = harness.add_user()
        vault_id = harness.add_vault("T1028r V")
        harness.grant_group_admin(vault_id, group_id)

        if leg == "group-members-add":
            harness.add_org_member(org_id, uid)
            assert harness.effective_admin_ids(vault_id) == set()
            resp = harness.request(
                "PUT",
                "/api/groups/%d/members" % group_id,
                json={"user_ids": [uid]},
            )
            assert resp.status_code == 200
            assert harness.count(
                "SELECT COUNT(*) FROM group_members"
                " WHERE group_id = ? AND user_id = ?",
                (group_id, uid),
            ) == 1
        elif leg == "user-groups-add":
            harness.add_org_member(org_id, uid)
            assert harness.effective_admin_ids(vault_id) == set()
            resp = harness.request(
                "PUT", "/api/users/%d/groups" % uid, json={"group_ids": [group_id]}
            )
            assert resp.status_code == 200
            assert harness.count(
                "SELECT COUNT(*) FROM group_members"
                " WHERE group_id = ? AND user_id = ?",
                (group_id, uid),
            ) == 1
        else:  # user-orgs-add (org premise repair)
            harness.add_group_member(group_id, uid)
            # stale shape: group membership without the org premise -> zero
            assert harness.effective_admin_ids(vault_id) == set()
            resp = harness.request(
                "PUT",
                "/api/users/%d/organizations" % uid,
                json={"memberships": [{"org_id": org_id, "role": "member"}]},
            )
            assert resp.status_code == 200
            assert harness.count(
                "SELECT COUNT(*) FROM org_members WHERE org_id = ? AND user_id = ?",
                (org_id, uid),
            ) == 1

        assert harness.effective_admin_ids(vault_id) == {uid}
    finally:
        harness.close()


def test_s08_genuine_repairs_zero_to_nonempty():
    for leg in ("group-members-add", "user-groups-add", "user-orgs-add"):
        _s08_leg(leg)


# ---------------------------------------------------------------------------
# s09 — candidate-bearing pre-broken floors (AC7 precision)
# ---------------------------------------------------------------------------


def test_s09_candidate_bearing_prebroken_floor():
    # (a) family 2 — group delete: vault B's covering group grant exists but
    # the group has NO members (B already at zero); vault A covered via the
    # same group plus a direct admin. No refusal for B; the cascade runs.
    harness = _SupportHarness()
    try:
        org_id = harness.add_org("T1028f O")
        group_id = harness.add_group(org_id, "T1028f G")
        vault_a = harness.add_vault("T1028f VA")
        vault_b = harness.add_vault("T1028f VB")
        harness.grant_group_admin(vault_a, group_id)
        harness.grant_group_admin(vault_b, group_id)
        direct_uid = harness.add_direct_vault_admin(vault_a)
        assert harness.effective_admin_ids(vault_a) == {direct_uid}
        assert harness.effective_admin_ids(vault_b) == set()

        resp = harness.request("DELETE", "/api/groups/%d" % group_id)
        assert resp.status_code == 204
        assert harness.count(
            "SELECT COUNT(*) FROM groups WHERE id = ?", (group_id,)
        ) == 0
        assert harness.count(
            "SELECT COUNT(*) FROM vault_group_access WHERE group_id = ?",
            (group_id,),
        ) == 0
        assert harness.effective_admin_ids(vault_a) == {direct_uid}
        assert harness.effective_admin_ids(vault_b) == set()
    finally:
        harness.close()

    # (b) family 6 — org delete, same shape: B pre-broken, A covered.
    harness = _SupportHarness()
    try:
        org_id = harness.add_org("T1028g O")
        group_id = harness.add_group(org_id, "T1028g G")
        vault_a = harness.add_vault("T1028g VA")
        vault_b = harness.add_vault("T1028g VB")
        harness.grant_group_admin(vault_a, group_id)
        harness.grant_group_admin(vault_b, group_id)
        direct_uid = harness.add_direct_vault_admin(vault_a)

        resp = harness.request("DELETE", "/api/organizations/%d" % org_id)
        assert resp.status_code == 200
        assert harness.count(
            "SELECT COUNT(*) FROM organizations WHERE id = ?", (org_id,)
        ) == 0
        assert harness.count(
            "SELECT COUNT(*) FROM groups WHERE id = ?", (group_id,)
        ) == 0
        assert harness.effective_admin_ids(vault_a) == {direct_uid}
        assert harness.effective_admin_ids(vault_b) == set()
    finally:
        harness.close()

    # (c) family 3 stale shape — user-groups clear: user u holds a
    # group_members row for a group whose grant covers vault B, but u has NO
    # org_members row (B already empty). Clear succeeds without naming B.
    harness = _SupportHarness()
    try:
        org_id = harness.add_org("T1028h O")
        group_id = harness.add_group(org_id, "T1028h G")
        uid = harness.add_user()
        harness.add_group_member(group_id, uid)
        vault_b = harness.add_vault("T1028h VB")
        harness.grant_group_admin(vault_b, group_id)
        assert harness.effective_admin_ids(vault_b) == set()

        resp = harness.request(
            "PUT", "/api/users/%d/groups" % uid, json={"group_ids": []}
        )
        assert resp.status_code == 200
        assert "[%d]" % vault_b not in resp.text
        assert harness.count(
            "SELECT COUNT(*) FROM group_members WHERE group_id = ? AND user_id = ?",
            (group_id, uid),
        ) == 0
        assert harness.effective_admin_ids(vault_b) == set()
    finally:
        harness.close()

    # (d) family 5 stale shape — org-member remove: org O member u also sits
    # in a group of an UNRELATED org O2 without an O2 org_members row; the
    # group's grant to the stale vault B contributes no effective admin, so
    # removing u from O succeeds without naming B.
    harness = _SupportHarness()
    try:
        org_id = harness.add_org("T1028i O")
        other_org_id = harness.add_org("T1028i O2")
        stale_group_id = harness.add_group(other_org_id, "T1028i G2")
        uid = harness.add_user()
        harness.add_org_member(org_id, uid)
        harness.add_group_member(stale_group_id, uid)
        vault_b = harness.add_vault("T1028i VB")
        harness.grant_group_admin(vault_b, stale_group_id)
        assert harness.effective_admin_ids(vault_b) == set()
        harness.add_org_member(org_id, 1, role="owner")

        resp = harness.request(
            "DELETE", "/api/organizations/%d/members/%d" % (org_id, uid)
        )
        assert resp.status_code == 200
        assert "[%d]" % vault_b not in resp.text
        assert harness.count(
            "SELECT COUNT(*) FROM org_members WHERE org_id = ? AND user_id = ?",
            (org_id, uid),
        ) == 0
        # cleanup only touches O's groups: the stale O2 group row survives
        assert harness.count(
            "SELECT COUNT(*) FROM group_members WHERE group_id = ? AND user_id = ?",
            (stale_group_id, uid),
        ) == 1
        assert harness.effective_admin_ids(vault_b) == set()
    finally:
        harness.close()

    # (e) family 1 — group-members replace: B pre-broken (grant with no
    # members), A covered via the same grant plus a direct admin. A mutant
    # that refuses on post-state coverage alone (ignoring covered_before)
    # would 409 naming B here; the real guard's pre-state-non-empty rule
    # keeps the clear at 200 (implementation-review finding F-001).
    harness = _SupportHarness()
    try:
        org_id = harness.add_org("T1028j O")
        group_id = harness.add_group(org_id, "T1028j G")
        vault_a = harness.add_vault("T1028j VA")
        vault_b = harness.add_vault("T1028j VB")
        harness.grant_group_admin(vault_a, group_id)
        harness.grant_group_admin(vault_b, group_id)
        # A covered via a DIRECT admin only; the group is memberless, so B is
        # pre-broken (its grant realizes no admin). A shared-member shape
        # cannot work here: any group member covers BOTH grants at once.
        direct_uid = harness.add_direct_vault_admin(vault_a)
        assert harness.effective_admin_ids(vault_a) == {direct_uid}
        assert harness.effective_admin_ids(vault_b) == set()

        resp = harness.request(
            "PUT", "/api/groups/%d/members" % group_id, json={"user_ids": []}
        )
        assert resp.status_code == 200
        assert "[%d]" % vault_b not in resp.text
        # the clear committed (the memberless table stays empty) and B stays
        # at zero; a refuse-on-post-state-only mutant would 409 naming B
        assert harness.count(
            "SELECT COUNT(*) FROM group_members WHERE group_id = ?",
            (group_id,),
        ) == 0
        assert harness.effective_admin_ids(vault_a) == {direct_uid}
        assert harness.effective_admin_ids(vault_b) == set()
    finally:
        harness.close()

    # (f) family 4 — user-orgs clear: u holds a stale group_members row on a
    # group whose grant covers B (no org premise, B already empty) plus a
    # real O2 membership to drop. A post-state-only mutant would refuse
    # naming B; the real guard keeps the clear at 200.
    harness = _SupportHarness()
    try:
        grant_org_id = harness.add_org("T1028k GO")
        grant_group_id = harness.add_group(grant_org_id, "T1028k G")
        other_org_id = harness.add_org("T1028k O2")
        uid = harness.add_user()
        harness.add_org_member(other_org_id, uid)
        harness.add_group_member(grant_group_id, uid)
        vault_b = harness.add_vault("T1028k VB")
        harness.grant_group_admin(vault_b, grant_group_id)
        assert harness.effective_admin_ids(vault_b) == set()

        resp = harness.request(
            "PUT", "/api/users/%d/organizations" % uid, json={"memberships": []}
        )
        assert resp.status_code == 200
        assert "[%d]" % vault_b not in resp.text
        assert harness.count(
            "SELECT COUNT(*) FROM org_members WHERE user_id = ?",
            (uid,),
        ) == 0
        # the dropped-org cleanup only removes group rows of DROPPED orgs:
        # the stale row belongs to the never-joined grant org and survives,
        # while B stays at zero either way (no org premise anywhere)
        assert harness.count(
            "SELECT COUNT(*) FROM group_members WHERE user_id = ?",
            (uid,),
        ) == 1
        assert harness.effective_admin_ids(vault_b) == set()
    finally:
        harness.close()


# ---------------------------------------------------------------------------
# s10 — sequential writers (the second request observes the first writer's
# committed state). Lock MODE is deliberately NOT pinned here: a BEGIN
# DEFERRED mutant is caught fail-closed by the #1025 race suites, and
# update_user_organizations' manual BEGIN IMMEDIATE is pre-existing (#721),
# unpinned by any test.
# ---------------------------------------------------------------------------


def _s10a_sequential_writers():
    """Vault V's ONLY coverage is user u via group G. Writer 1 (groups.py,
    get_db connection style) same-content-replaces G's members keeping u — a
    non-empty -> non-empty transition, must succeed. Writer 2 (users.py,
    pooled connection style) then clears u's org memberships; by then writer
    1's state is committed, and stripping the org premise (which cascades the
    group cleanup) is a non-empty -> empty transition -> 409."""
    harness = _SupportHarness()
    try:
        org_id = harness.add_org("T1028c O")
        group_id = harness.add_group(org_id, "T1028c G")
        uid = harness.add_user()
        harness.add_org_member(org_id, uid)
        harness.add_group_member(group_id, uid)
        vault_id = harness.add_vault("T1028c V")
        harness.grant_group_admin(vault_id, group_id)
        assert harness.effective_admin_ids(vault_id) == {uid}

        first = harness.request(
            "PUT", "/api/groups/%d/members" % group_id, json={"user_ids": [uid]}
        )
        assert first.status_code == 200

        second = harness.request(
            "PUT", "/api/users/%d/organizations" % uid, json={"memberships": []}
        )
        assert second.status_code == 409
        _assert_names_only(second.json()["detail"], vault_id)
        # refusal rolled back both the org row and the group cleanup
        assert harness.effective_admin_ids(vault_id) == {uid}
        assert harness.count(
            "SELECT COUNT(*) FROM group_members WHERE group_id = ? AND user_id = ?",
            (group_id, uid),
        ) == 1
    finally:
        harness.close()


def _s10b_group_discovery_leg():
    """Group-route candidate discovery: an admin grant V2 -> G committed on a
    separate pool connection BEFORE the route's BEGIN IMMEDIATE must be seen
    as a candidate. Keeping u in the replace succeeds; dropping u 409s
    naming V2."""
    harness = _SupportHarness()
    try:
        org_id = harness.add_org("T1028c2 O")
        group_id = harness.add_group(org_id, "T1028c2 G")
        uid = harness.add_user()
        harness.add_org_member(org_id, uid)
        harness.add_group_member(group_id, uid)
        vault_id = harness.add_vault("T1028c2 V")
        # the arriving grant, committed before the route call
        harness.grant_group_admin(vault_id, group_id)
        assert harness.effective_admin_ids(vault_id) == {uid}

        keep = harness.request(
            "PUT", "/api/groups/%d/members" % group_id, json={"user_ids": [uid]}
        )
        assert keep.status_code == 200

        drop = harness.request(
            "PUT", "/api/groups/%d/members" % group_id, json={"user_ids": []}
        )
        assert drop.status_code == 409
        _assert_names_only(drop.json()["detail"], vault_id)
        assert harness.effective_admin_ids(vault_id) == {uid}
    finally:
        harness.close()


def _s10b_org_discovery_leg():
    """Org-route candidate discovery: a grant to a NEW group of the org,
    committed before the org-delete route's BEGIN IMMEDIATE, is discovered
    and refuses the delete naming that vault."""
    harness = _SupportHarness()
    try:
        org_id = harness.add_org("T1028c3 O")
        uid = harness.add_user()
        harness.add_org_member(org_id, uid)
        group_id = harness.add_group(org_id, "T1028c3 G")
        harness.add_group_member(group_id, uid)
        vault_id = harness.add_vault("T1028c3 V")
        harness.grant_group_admin(vault_id, group_id)
        assert harness.effective_admin_ids(vault_id) == {uid}

        resp = harness.request("DELETE", "/api/organizations/%d" % org_id)
        assert resp.status_code == 409
        _assert_names_only(resp.json()["detail"], vault_id)
        assert harness.count(
            "SELECT COUNT(*) FROM organizations WHERE id = ?", (org_id,)
        ) == 1
    finally:
        harness.close()


def _s10b_user_discovery_leg():
    """User-route candidate discovery: a grant on a group u belongs to,
    committed before the user-groups clear route's BEGIN IMMEDIATE, is
    discovered and refuses the clear naming that vault."""
    harness = _SupportHarness()
    try:
        org_id = harness.add_org("T1028c4 O")
        uid = harness.add_user()
        harness.add_org_member(org_id, uid)
        group_id = harness.add_group(org_id, "T1028c4 G")
        harness.add_group_member(group_id, uid)
        vault_id = harness.add_vault("T1028c4 V")
        harness.grant_group_admin(vault_id, group_id)
        assert harness.effective_admin_ids(vault_id) == {uid}

        resp = harness.request(
            "PUT", "/api/users/%d/groups" % uid, json={"group_ids": []}
        )
        assert resp.status_code == 409
        _assert_names_only(resp.json()["detail"], vault_id)
        assert harness.count(
            "SELECT COUNT(*) FROM group_members WHERE group_id = ? AND user_id = ?",
            (group_id, uid),
        ) == 1
    finally:
        harness.close()


def test_s10_serialized_writers_and_arriving_grant_visibility():
    _s10a_sequential_writers()
    _s10b_group_discovery_leg()
    _s10b_org_discovery_leg()
    _s10b_user_discovery_leg()


# ---------------------------------------------------------------------------
# s11 — candidate discovery runs INSIDE the write transaction (the
# discriminating arriving-grant proof the implementation review required:
# a spy records conn.in_transaction at discovery time during a real route
# call; a pre-BEGIN discovery implementation would record False and fail)
# ---------------------------------------------------------------------------


def _install_discovery_spy(module, helper_name):
    """Wrap a route module's candidate-discovery helper so each call records
    whether the connection is inside an open transaction. Observation only —
    the wrapped helper still runs the real SQL. Returns (restore, records)."""
    original = getattr(module, helper_name)
    records = []

    def spy(conn, key):
        records.append(bool(conn.in_transaction))
        return original(conn, key)

    setattr(module, helper_name, spy)

    def restore():
        setattr(module, helper_name, original)

    return restore, records


def _s11_leg(module, helper_name, mutate):
    """Build a covered vault, spy the module's discovery helper, fire the
    mutation route, and require: the helper actually ran during the route
    call, and EVERY discovery read saw an open transaction."""
    harness = _SupportHarness()
    try:
        _org_id, group_id, uid, vault_id = harness.build_group_derived_only_vault()
        assert harness.effective_admin_ids(vault_id) == {uid}
        restore, records = _install_discovery_spy(module, helper_name)
        try:
            resp = mutate(harness, group_id, uid)
        finally:
            restore()
        assert resp.status_code == 409, resp.status_code
        _assert_names_only(resp.json()["detail"], vault_id)
        assert records, "discovery helper never ran during the route call"
        assert all(records), (
            "candidate discovery ran outside an open transaction: %r" % records
        )
    finally:
        harness.close()


def test_s11_discovery_runs_inside_write_transaction():
    from app.api.routes import groups as groups_module
    from app.api.routes import organizations as orgs_module
    from app.api.routes import users as users_module

    # group shape (F1/F2): groups.py routes discover via
    # _group_admin_grant_vault_ids
    _s11_leg(
        groups_module,
        "_group_admin_grant_vault_ids",
        lambda h, g, u: h.request(
            "PUT", "/api/groups/%d/members" % g, json={"user_ids": []}
        ),
    )
    # organization shape (F6): organizations.py delete_organization discovers
    # via _org_group_admin_grant_vault_ids. The org-delete cascade needs the
    # group's org to be the vault's covering org, so build a fresh covered
    # vault whose org we then delete.
    harness = _SupportHarness()
    try:
        org_id, group_id, uid, vault_id = harness.build_group_derived_only_vault()
        assert harness.effective_admin_ids(vault_id) == {uid}
        # the org here owns the covering group, so org deletion hits the same
        # vault transition through the org candidate shape
        restore, records = _install_discovery_spy(
            orgs_module, "_org_group_admin_grant_vault_ids"
        )
        try:
            resp = harness.request("DELETE", "/api/organizations/%d" % org_id)
        finally:
            restore()
        assert resp.status_code == 409, resp.status_code
        _assert_names_only(resp.json()["detail"], vault_id)
        assert records, "org discovery helper never ran during the route call"
        assert all(records), (
            "org candidate discovery ran outside an open transaction: %r"
            % records
        )
    finally:
        harness.close()
    # user shape (F3/F4): users.py routes discover via the users module's
    # own _user_derived_admin_vault_ids. F5 (remove_org_member) is NOT
    # covered by a users_module spy — it resolves the ORGANIZATIONS module's
    # separate copy of the helper (organizations.py), spied in its own leg
    # below. (Comment corrected per the out-of-band review's F-01.)
    _s11_leg(
        users_module,
        "_user_derived_admin_vault_ids",
        lambda h, g, u: h.request(
            "PUT", "/api/users/%d/groups" % u, json={"group_ids": []}
        ),
    )
    # F2 second site — groups.delete_group (groups.py:658) shares the same
    # groups-module helper as the replace route above.
    _s11_leg(
        groups_module,
        "_group_admin_grant_vault_ids",
        lambda h, g, u: h.request("DELETE", "/api/groups/%d" % g),
    )
    # F4 — update_user_organizations' manual BEGIN IMMEDIATE site
    # (users.py:1171): same users-module helper, orgs-clear route.
    _s11_leg(
        users_module,
        "_user_derived_admin_vault_ids",
        lambda h, g, u: h.request(
            "PUT", "/api/users/%d/organizations" % u, json={"memberships": []}
        ),
    )
    # F3 replace branch (users.py:1496): a non-empty body routes to the
    # replace transaction; the replacement strips the sole coverage so the
    # route refuses, and the spy still sees every discovery read in-txn.
    harness = _SupportHarness()
    try:
        org_id, group_id, uid, vault_id = harness.build_group_derived_only_vault()
        assert harness.effective_admin_ids(vault_id) == {uid}
        other_group_id = harness.add_group(org_id, "T1028s11 H")
        restore, records = _install_discovery_spy(
            users_module, "_user_derived_admin_vault_ids"
        )
        try:
            resp = harness.request(
                "PUT",
                "/api/users/%d/groups" % uid,
                json={"group_ids": [other_group_id]},
            )
        finally:
            restore()
        assert resp.status_code == 409, resp.status_code
        _assert_names_only(resp.json()["detail"], vault_id)
        assert records, "replace-branch discovery helper never ran"
        assert all(records), (
            "replace-branch candidate discovery ran outside an open"
            " transaction: %r" % records
        )
    finally:
        harness.close()
    # F5 — remove_org_member (organizations.py:1227) resolves the
    # ORGANIZATIONS module's own copy of _user_derived_admin_vault_ids; the
    # caller needs an org owner row (the check ignores platform superadmin).
    harness = _SupportHarness()
    try:
        org_id, group_id, uid, vault_id = harness.build_group_derived_only_vault()
        assert harness.effective_admin_ids(vault_id) == {uid}
        harness.insert(
            "INSERT INTO org_members (org_id, user_id, role) VALUES (?, 1, 'owner')",
            (org_id,),
        )
        restore, records = _install_discovery_spy(
            orgs_module, "_user_derived_admin_vault_ids"
        )
        try:
            resp = harness.request(
                "DELETE", "/api/organizations/%d/members/%d" % (org_id, uid)
            )
        finally:
            restore()
        assert resp.status_code == 409, resp.status_code
        _assert_names_only(resp.json()["detail"], vault_id)
        assert records, "org-member-remove discovery helper never ran"
        assert all(records), (
            "org-member-remove candidate discovery ran outside an open"
            " transaction: %r" % records
        )
    finally:
        harness.close()


# ---------------------------------------------------------------------------
# s12 — the unscoped (superadmin) non-empty replace refusal (implementation
# review F-002 / out-of-band review F-05: the only clear/replace x
# scoped/unscoped refusal cell no other leg drives)
# ---------------------------------------------------------------------------


def test_s12_unscoped_replace_refusal():
    harness = _SupportHarness()
    try:
        org_id = harness.add_org("T1028u O")
        group_id = harness.add_group(org_id, "T1028u G")
        other_group_id = harness.add_group(org_id, "T1028u H")
        uid = harness.add_user()
        harness.add_org_member(org_id, uid)
        harness.add_group_member(group_id, uid)
        vault_id = harness.add_vault("T1028u V")
        harness.grant_group_admin(vault_id, group_id)
        assert harness.effective_admin_ids(vault_id) == {uid}
        # default acting user is the superadmin: caller_orgs is None, so the
        # replace branch takes the UNSCOPED DELETE form
        resp = harness.request(
            "PUT",
            "/api/users/%d/groups" % uid,
            json={"group_ids": [other_group_id]},
        )
        assert resp.status_code == 409, resp.status_code
        _assert_names_only(resp.json()["detail"], vault_id)
        # whole replacement rolled back: covering membership survives, the
        # requested new one was never committed
        assert harness.count(
            "SELECT COUNT(*) FROM group_members"
            " WHERE group_id = ? AND user_id = ?",
            (group_id, uid),
        ) == 1
        assert harness.count(
            "SELECT COUNT(*) FROM group_members"
            " WHERE group_id = ? AND user_id = ?",
            (other_group_id, uid),
        ) == 0
        assert harness.effective_admin_ids(vault_id) == {uid}
    finally:
        harness.close()


# ---------------------------------------------------------------------------
# s13 — audit positive control (out-of-band review F-03): the refusal-silence
# asserts in s04/s05 need their other half — a SUCCESSFUL membership replace
# must record its security event, so a no-op audit sink no longer passes.
# ---------------------------------------------------------------------------


def test_s13_audit_success_positive_control():
    harness = _SupportHarness()
    try:
        org_id = harness.add_org("T1028a O")
        group_id = harness.add_group(org_id, "T1028a G")
        uid = harness.add_user()
        harness.add_org_member(org_id, uid)
        harness.add_group_member(group_id, uid)
        vault_id = harness.add_vault("T1028a V")
        harness.grant_group_admin(vault_id, group_id)
        harness.add_direct_vault_admin(vault_id)  # surviving admin -> 200

        resp = harness.request(
            "PUT", "/api/users/%d/groups" % uid, json={"group_ids": []}
        )
        assert resp.status_code == 200
        assert harness.count(
            "SELECT COUNT(*) FROM security_audit_log"
            " WHERE event_type = 'membership.groups_replaced'"
            " AND target_user_id = ?",
            (uid,),
        ) == 1

        uid2 = harness.add_user()
        harness.add_org_member(org_id, uid2)
        resp = harness.request(
            "PUT",
            "/api/users/%d/organizations" % uid2,
            json={"memberships": []},
        )
        assert resp.status_code == 200
        assert harness.count(
            "SELECT COUNT(*) FROM security_audit_log"
            " WHERE event_type = 'membership.organizations_replaced'"
            " AND target_user_id = ?",
            (uid2,),
        ) == 1
    finally:
        harness.close()
