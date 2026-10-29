"""Issue #1036 C3: transfer_ownership's pre-check windows and the same-family
remove_org_member caller window must never authorize a stale writer.

``transfer_ownership`` reads its caller-owner row and its new-owner membership
row on autocommit BEFORE its write transaction (organizations.py), the demote
UPDATE is unconditional and the promote UPDATE is unpredicated with no
rowcount check. Three probe-verified interleavings escape at the base tree:

  Window A — the caller is demoted between the caller-owner pre-check and
  the write transaction (a racing superadmin transfer commits first): the
  demoted caller's transfer still executes and returns 200.

  Window B — a ``remove_org_member`` commits between the pre-checks and the
  write transaction (deleting the new owner's member row): the demote still
  runs, the promote matches 0 rows, and the organization is left with ZERO
  owners (200).

  Zero-row promote — even after every pre-check passes, a promote UPDATE
  that matches zero rows (a concurrent writer won the row between
  validation and the write) is not detected: the demote commits alongside
  the no-op promote and the handler returns 200 on a transfer that never
  landed.

  Same family — ``remove_org_member``'s caller-privilege check
  (``_is_org_admin_or_owner``) runs OUTSIDE its write transaction: a caller
  demoted to plain member between that check and ``_remove_member`` — an
  org admin, or a GLOBAL superadmin whose only org privilege is the admin
  role — still has their removal execute.

Interleaving control is deliberately fix-agnostic (plan-critic F6): no hook
matches a ``BEGIN`` or UPDATE statement text, since the fix moves the writes
into a transaction worker and predicates the statements. Instead a stateful
wrapper ARMS at the to_thread dispatch of the route's new-owner membership
SELECT and fires the racing handler AFTER that read's ``fetchone`` completes
(run the real fetchone, await the racing handler to completion on its own
real pool connection, then return the real, now-stale result) — the racing
commit lands between the completed pre-check read and the write transaction
under both the base shape and the fixed shape. The same-family leg fires at
the ``_remove_member`` to_thread dispatch (a dispatch the #1033-shaped fix
keeps). The zero-row-promote leg is pure fault injection through the
connection: the promote UPDATE runs for real but its cursor reports
rowcount 0.

Contract asserted at BOTH ends of the fix (RED at base, GREEN after the
fix): the stale writer is REFUSED and the organization never loses its
owner / the target's membership.
"""

import asyncio
import os

os.environ["JWT_SECRET_KEY"] = (
    "test-jwt-secret-key-for-testing-only-12345678901234567890"
)
os.environ["USERS_ENABLED"] = "false"
os.environ["REDIS_URL"] = ""
os.environ["ADMIN_SECRET_TOKEN"] = (
    "test-admin-secret-0123456789abcdef0123456789abcdef0123456789abcdef"
)

import sqlite3  # noqa: E402
from pathlib import Path  # noqa: E402

from fastapi import HTTPException  # noqa: E402

from app.api.routes import organizations as org_mod  # noqa: E402
from app.api.routes.organizations import (  # noqa: E402
    TransferOwnershipRequest,
    remove_org_member,
    transfer_ownership,
)
from app.models.database import SQLiteConnectionPool, _pool_cache, init_db  # noqa: E402

ORG_ID = 1

OWNER = {"id": 1, "username": "owner1", "role": "admin"}
SUPERADMIN = {"id": 99, "username": "root", "role": "superadmin"}

# Normalized (whitespace-collapsed) SQL of transfer_ownership's new-owner
# membership pre-check read — the arming statement for the fix-agnostic
# interleavings.
_NEW_OWNER_READ_SQL = (
    "SELECT u.id FROM users u JOIN org_members om ON u.id = om.user_id "
    "WHERE u.id = ? AND om.org_id = ? AND u.is_active = 1"
)


def _norm_sql(sql) -> str:
    return " ".join(str(sql).split())


class _MultiConnPool:
    """Pool facade handing every caller its own REAL pool connection.

    The driven route and the racing handler must run on separate
    connections, exactly as two concurrent requests would — a shared
    connection would collapse the race into one transaction.
    """

    def __init__(self, real_pool: SQLiteConnectionPool) -> None:
        self._real_pool = real_pool
        self._out: list = []

    async def get_connection_async(self, *args, **kwargs):  # noqa: ANN002, ANN003
        conn = self._real_pool.get_connection()
        self._out.append(conn)
        return conn

    def release_connection(self, conn) -> None:
        try:
            if conn.in_transaction:
                conn.rollback()
        except sqlite3.Error:
            pass
        self._real_pool.release_connection(conn)


class _SingleConnFacade:
    """Pool facade handing the driven route one wrapped connection (used by
    the pure fault-injection leg, which has no racing handler)."""

    def __init__(self, real_pool: SQLiteConnectionPool, conn) -> None:
        self._real_pool = real_pool
        self._conn = conn

    async def get_connection_async(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return self._conn

    def release_connection(self, _conn) -> None:
        try:
            if self._conn.in_transaction:
                self._conn.rollback()
        except sqlite3.Error:
            pass
        self._real_pool.release_connection(self._conn._inner)


class _RowcountShimCursor:
    """Delegates everything to the real cursor but reports rowcount 0."""

    def __init__(self, inner, rowcount: int) -> None:  # noqa: ANN001
        self._inner = inner
        self._rowcount = rowcount

    @property
    def rowcount(self) -> int:
        return self._rowcount

    def __getattr__(self, name):  # noqa: ANN001
        return getattr(self._inner, name)


class _ZeroRowConn:
    """Runs the REAL matched statement but hands back a cursor whose
    rowcount reports 0 — models a concurrent writer winning the row between
    the route's validation and its UPDATE. Matched by normalized-SQL PREFIX
    (``UPDATE org_members SET role = 'owner'``) so it fires for the base
    unpredicated promote and any fixed predicated form alike."""

    _MATCH_PREFIX = "UPDATE org_members SET role = 'owner'"

    def __init__(self, inner: sqlite3.Connection) -> None:
        self._inner = inner
        self.shim_fired = False

    def execute(self, sql, params=()):  # noqa: ANN001
        if _norm_sql(sql).startswith(self._MATCH_PREFIX):
            self.shim_fired = True
            return _RowcountShimCursor(self._inner.execute(sql, params), 0)
        return self._inner.execute(sql, params)

    def __getattr__(self, name):  # noqa: ANN001
        return getattr(self._inner, name)


class _RaceEnv:
    """Per-test world: fresh db, real pool, patched org-route pool binding."""

    def __init__(self, tmp_path: Path) -> None:
        _pool_cache.clear()
        self.db_path = str(tmp_path / "transfer-race.db")
        init_db(self.db_path)
        self.pool = SQLiteConnectionPool(self.db_path, max_size=6)
        self.facade: object = _MultiConnPool(self.pool)
        self._saved_get_pool = org_mod.get_pool
        org_mod.get_pool = lambda _p: self.facade

    def seed_org(self, second_member_role: str = "member") -> None:
        """Users 1 (org owner), 2 (member), 3 (member), 99 (superadmin);
        org 1 with user 2 at ``second_member_role`` and user 3 a member."""
        seed = self.pool.get_connection()
        try:
            for uid, username, role in (
                (1, "owner1", "admin"),
                (2, "member2", "member"),
                (3, "member3", "member"),
                (99, "root", "superadmin"),
            ):
                seed.execute(
                    "INSERT INTO users (id, username, hashed_password, "
                    "full_name, role, is_active) VALUES (?, ?, 'abc123', ?, ?, 1)",
                    (uid, username, username.title(), role),
                )
            seed.execute(
                "INSERT INTO organizations (id, name, created_by) "
                "VALUES (1, 'TransferRaceOrg', 1)"
            )
            seed.execute(
                "INSERT INTO org_members (org_id, user_id, role) "
                "VALUES (1, 1, 'owner')"
            )
            seed.execute(
                "INSERT INTO org_members (org_id, user_id, role) VALUES (1, 2, ?)",
                (second_member_role,),
            )
            seed.execute(
                "INSERT INTO org_members (org_id, user_id, role) "
                "VALUES (1, 3, 'member')"
            )
            seed.commit()
        finally:
            self.pool.release_connection(seed)

    def seed_org_with_superadmin_admin(self) -> None:
        """Users 1 (org owner), 3 (member), 99 (superadmin who is ALSO an
        org admin — the only org-role shape a global superadmin caller of
        remove_org_member can hold); org 1."""
        seed = self.pool.get_connection()
        try:
            for uid, username, role in (
                (1, "owner1", "admin"),
                (3, "member3", "member"),
                (99, "root", "superadmin"),
            ):
                seed.execute(
                    "INSERT INTO users (id, username, hashed_password, "
                    "full_name, role, is_active) VALUES (?, ?, 'abc123', ?, ?, 1)",
                    (uid, username, username.title(), role),
                )
            seed.execute(
                "INSERT INTO organizations (id, name, created_by) "
                "VALUES (1, 'TransferRaceOrg', 1)"
            )
            seed.execute(
                "INSERT INTO org_members (org_id, user_id, role) "
                "VALUES (1, 1, 'owner')"
            )
            seed.execute(
                "INSERT INTO org_members (org_id, user_id, role) "
                "VALUES (1, 3, 'member')"
            )
            seed.execute(
                "INSERT INTO org_members (org_id, user_id, role) "
                "VALUES (1, 99, 'admin')"
            )
            seed.commit()
        finally:
            self.pool.release_connection(seed)

    def member_role(self, user_id: int) -> str | None:
        # Raw connection read: usable both before and after pool teardown.
        check = sqlite3.connect(self.db_path)
        try:
            row = check.execute(
                "SELECT role FROM org_members WHERE org_id = ? AND user_id = ?",
                (ORG_ID, user_id),
            ).fetchone()
            return None if row is None else str(row[0])
        finally:
            check.close()

    def owner_ids(self) -> list[int]:
        check = sqlite3.connect(self.db_path)
        try:
            rows = check.execute(
                "SELECT user_id FROM org_members "
                "WHERE org_id = ? AND role = 'owner' ORDER BY user_id",
                (ORG_ID,),
            ).fetchall()
        finally:
            check.close()
        return [row[0] for row in rows]

    def close(self) -> None:
        org_mod.get_pool = self._saved_get_pool
        for conn in list(getattr(self.facade, "_out", [])):
            try:
                self.pool.release_connection(conn)
            except Exception:
                pass
        self.pool.close_all()
        _pool_cache.clear()


def _after_read_interleave(match_sql_norm: str, interleave):
    """Stateful, fix-agnostic interleaving builder: ARM when the to_thread
    dispatch carries the matched SELECT; at the NEXT to_thread(cursor.
    fetchone) run the REAL fetchone first, then await the racing handler to
    completion, then return the real (now-stale) result."""

    state = {"armed": False, "fired": False}

    def build(real_to_thread):  # noqa: ANN001, ANN202
        async def scripted(fn, *args, **kwargs):  # noqa: ANN002, ANN003
            if (
                not state["armed"]
                and getattr(fn, "__name__", "") == "execute"
                and args
                and _norm_sql(args[0]) == match_sql_norm
            ):
                state["armed"] = True
            elif (
                state["armed"]
                and not state["fired"]
                and getattr(fn, "__name__", "") == "fetchone"
            ):
                state["fired"] = True
                result = await real_to_thread(fn, *args, **kwargs)
                await interleave()
                return result
            return await real_to_thread(fn, *args, **kwargs)

        return scripted

    return build, state


def _at_dispatch_interleave(fn_name: str, interleave):
    """Fires the racing handler at the to_thread dispatch of the named
    inner function (before the original call runs)."""

    state = {"fired": False}

    def build(real_to_thread):  # noqa: ANN001, ANN202
        async def scripted(fn, *args, **kwargs):  # noqa: ANN002, ANN003
            if not state["fired"] and getattr(fn, "__name__", "") == fn_name:
                state["fired"] = True
                await interleave()
            return await real_to_thread(fn, *args, **kwargs)

        return scripted

    return build, state


def _drive_interleaved(build, drive_factory) -> dict:  # noqa: ANN001
    """Run the driven handler with the wrapped asyncio.to_thread."""
    real_to_thread = asyncio.to_thread
    scripted = build(real_to_thread)
    outcome: dict = {}

    async def drive() -> None:
        asyncio.to_thread = scripted
        try:
            result = await drive_factory()
            outcome["ok"] = str(result)
        except HTTPException as exc:
            outcome["refused_status"] = exc.status_code
        finally:
            asyncio.to_thread = real_to_thread

    asyncio.run(drive())
    return outcome


def _drive(drive_factory) -> dict:  # noqa: ANN001
    """Run the driven handler without any to_thread patching."""
    outcome: dict = {}

    async def drive() -> None:
        try:
            result = await drive_factory()
            outcome["ok"] = str(result)
        except HTTPException as exc:
            outcome["refused_status"] = exc.status_code

    asyncio.run(drive())
    return outcome


def _assert_refused(outcome: dict, defect: str, expected: int) -> None:
    # Statuses pinned per leg to the documented contract (release note +
    # Multi-Stage review): demoted-privilege refusals are 403, a removed
    # new owner is 400, a zero-row write backstop is 409.
    assert "refused_status" in outcome, (
        f"{defect}: the handler returned success "
        f"({outcome.get('ok')}) instead of refusing"
    )
    assert outcome["refused_status"] == expected, (
        f"a refusal must be HTTP {expected}, not {outcome['refused_status']}"
    )


def test_window_a_caller_demoted_before_transaction_is_refused(tmp_path: Path) -> None:
    """RED at base, GREEN after the fix: a transfer whose caller was demoted
    between the owner pre-check and the write transaction must be refused,
    never executed on the stale authorization."""
    env = _RaceEnv(tmp_path)
    try:
        env.seed_org()

        async def racing_superadmin_transfer() -> None:
            # Demotes user 1 (the driven caller) off the owner role after
            # the caller's own pre-check read completed.
            await transfer_ownership(
                ORG_ID,
                TransferOwnershipRequest(new_owner_user_id=3),
                user=SUPERADMIN,
                _csrf_token="t",
            )

        build, state = _after_read_interleave(
            _NEW_OWNER_READ_SQL, racing_superadmin_transfer
        )
        outcome = _drive_interleaved(
            build,
            lambda: transfer_ownership(
                ORG_ID,
                TransferOwnershipRequest(new_owner_user_id=2),
                user=OWNER,
                _csrf_token="t",
            ),
        )

        assert state["fired"], (
            "the racing transfer must have committed after the new-owner "
            "pre-check read completed — otherwise this run proved nothing"
        )
        assert env.member_role(1) != "owner", (
            "non-vacuity: the racing transfer must have demoted user 1 "
            "before the caller's transaction ran"
        )
        _assert_refused(
            outcome,
            "authorization window: the caller was demoted between the "
            "owner pre-check and the write transaction, but the stale "
            "transfer still executed",
            expected=403,
        )
        assert env.owner_ids() != [], (
            "organization left with zero owners after the caller-demoted "
            "transfer window"
        )
    finally:
        env.close()


def test_window_b_new_owner_removed_before_transaction_is_refused(
    tmp_path: Path,
) -> None:
    """RED at base, GREEN after the fix: a remove_org_member committing
    between the pre-checks and the write transaction (deleting the new
    owner's member row) must leave the transfer refused and the org never
    with zero owners."""
    env = _RaceEnv(tmp_path)
    try:
        env.seed_org()

        async def racing_member_removal() -> None:
            await remove_org_member(ORG_ID, 2, user=OWNER, _csrf_token="t")

        build, state = _after_read_interleave(
            _NEW_OWNER_READ_SQL, racing_member_removal
        )
        outcome = _drive_interleaved(
            build,
            lambda: transfer_ownership(
                ORG_ID,
                TransferOwnershipRequest(new_owner_user_id=2),
                user=OWNER,
                _csrf_token="t",
            ),
        )

        assert state["fired"], (
            "the racing removal must have committed after the new-owner "
            "pre-check read completed — otherwise this run proved nothing"
        )
        assert env.member_role(2) is None, (
            "non-vacuity: the racing removal must have deleted the new "
            "owner's member row before the transfer's transaction ran"
        )
        _assert_refused(
            outcome,
            "organization left with zero owners: the new owner's membership "
            "row was removed between the pre-check and the transaction, the "
            "unconditional demote still ran and the unpredicated promote "
            "matched zero rows",
            expected=400,
        )
        assert env.owner_ids() == [1], (
            "organization left with zero owners: the refused transfer must "
            "leave user 1 as the org's owner"
        )
    finally:
        env.close()


def test_zero_row_promote_refuses_and_retains_old_owner(tmp_path: Path) -> None:
    """RED at base, GREEN after the fix (fault injection): a promote UPDATE
    that runs but matches zero rows after every pre-check passed must be
    detected — the handler refuses (403/409) and the transaction rolls back,
    retaining the old owner. At base there is no rowcount check: the demote
    commits alongside the no-op promote and the handler returns 200 on a
    transfer that never landed."""
    env = _RaceEnv(tmp_path)
    try:
        env.seed_org()
        wrapped = _ZeroRowConn(env.pool.get_connection())
        env.facade = _SingleConnFacade(env.pool, wrapped)

        outcome = _drive(
            lambda: transfer_ownership(
                ORG_ID,
                TransferOwnershipRequest(new_owner_user_id=2),
                user=OWNER,
                _csrf_token="t",
            )
        )

        assert wrapped.shim_fired, (
            "the zero-row promote shim must actually have fired — "
            "otherwise this run proved nothing"
        )
        _assert_refused(
            outcome,
            "zero-row promote: the promote UPDATE matched zero rows after "
            "validation, but the handler committed the unconditional demote "
            "anyway and returned success on a transfer that never landed",
            expected=409,
        )
        assert env.owner_ids() == [1], (
            "the refused transfer must roll back and retain the old owner "
            f"(owner rows: {env.owner_ids()})"
        )
    finally:
        env.close()


def test_same_family_remove_org_member_demoted_caller_is_refused(
    tmp_path: Path,
) -> None:
    """RED at base, GREEN after the fix: a caller demoted to plain member
    between remove_org_member's ``_is_org_admin_or_owner`` check and its
    write transaction must have the removal refused, never executed."""
    env = _RaceEnv(tmp_path)
    try:
        env.seed_org(second_member_role="admin")

        async def racing_caller_demotion() -> None:
            # Demote user 2 (the driven caller) from org admin to plain
            # member after their privilege check has already passed.
            await org_mod.update_org_member_role(
                ORG_ID,
                2,
                org_mod.OrgMemberUpdateRequest(role="member"),
                user=OWNER,
                _csrf_token="t",
            )

        build, state = _at_dispatch_interleave("_remove_member", racing_caller_demotion)
        outcome = _drive_interleaved(
            build,
            lambda: remove_org_member(
                ORG_ID, 3, user={"id": 2, "username": "admin2", "role": "member"},
                _csrf_token="t",
            ),
        )

        assert state["fired"], (
            "the racing demotion must have committed before the inner "
            "_remove_member ran — otherwise this run proved nothing"
        )
        assert env.member_role(2) == "member", (
            "non-vacuity: the racing update_org_member_role must have "
            "demoted user 2 to plain member before the removal transaction"
        )
        _assert_refused(
            outcome,
            "authorization window: the caller was demoted between the "
            "_is_org_admin_or_owner check and the removal transaction, but "
            "the stale removal still executed",
            expected=403,
        )
        assert env.member_role(3) is not None, (
            "the refused removal must leave the target's membership intact"
        )
    finally:
        env.close()


def test_same_family_superadmin_org_admin_demoted_caller_is_refused(
    tmp_path: Path,
) -> None:
    """RED at base, GREEN after the fix (plan-critic R2-F5): a caller who is
    a GLOBAL superadmin but holds only org-admin privilege in this org,
    demoted to plain member between ``_is_org_admin_or_owner`` and
    ``_remove_member``'s transaction, must have the removal refused — the
    org-role check may not run on the stale pre-transaction read."""
    env = _RaceEnv(tmp_path)
    try:
        env.seed_org_with_superadmin_admin()

        async def racing_caller_demotion() -> None:
            # Demote user 99 (the driven caller, a superadmin who holds the
            # org-admin role) to plain org member after their privilege
            # check has already passed.
            await org_mod.update_org_member_role(
                ORG_ID,
                99,
                org_mod.OrgMemberUpdateRequest(role="member"),
                user=OWNER,
                _csrf_token="t",
            )

        build, state = _at_dispatch_interleave("_remove_member", racing_caller_demotion)
        outcome = _drive_interleaved(
            build,
            lambda: remove_org_member(
                ORG_ID, 3, user=SUPERADMIN, _csrf_token="t"
            ),
        )

        assert state["fired"], (
            "the racing demotion must have committed before the inner "
            "_remove_member ran — otherwise this run proved nothing"
        )
        assert env.member_role(99) == "member", (
            "non-vacuity: the racing update_org_member_role must have "
            "demoted user 99 from org admin to plain member before the "
            "removal transaction"
        )
        assert outcome.get("refused_status") == 403, (
            "authorization window: the caller (a global superadmin holding "
            "only org-admin privilege) was demoted between the "
            "_is_org_admin_or_owner check and the removal transaction, but "
            "the stale removal still ran (outcome: "
            f"{outcome.get('refused_status', outcome)})"
        )
        assert env.member_role(3) is not None, (
            "the refused removal must leave the target's membership intact"
        )
    finally:
        env.close()
