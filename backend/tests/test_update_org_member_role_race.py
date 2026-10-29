"""Issue #1037 item 2 (collated with #1036): update_org_member_role's
outer-read windows must never authorize a stale writer or report success on
a write that never landed.

``update_org_member_role`` reads the caller's privilege, the target's
current role on autocommit, and its UPDATE is unpredicated with no write
transaction and no rowcount check (organizations.py). Four defects escape
at the base tree:

  Target-read race — a ``transfer_ownership`` committing between the
  target-role read and the UPDATE promotes the target to owner; the stale
  UPDATE then demotes the freshly promoted owner and the organization is
  left with ZERO owners (base outcome: HTTP 200).

  Caller demotion — the CALLER is demoted from org admin to plain member
  between the outer ``_is_org_admin_or_owner`` check and the write: the
  stale update still executes (base outcome: HTTP 200).

  Vanished target — the target's ``org_members`` row is removed between the
  outer target read and the write: the UPDATE matches zero rows and the
  response read subscripts ``None`` — the base outcome is an unhandled
  TypeError, never a 404 (empirically confirmed at base).

  Zero-row UPDATE — the predicated UPDATE of the fixed shape must check its
  rowcount: an UPDATE that matches zero rows must refuse and roll back, not
  report success on a write that never landed (base outcome: HTTP 200).

  Owner-at-write — the UPDATE's ``AND role != 'owner'`` predicate itself: a
  stale 'admin' read for the OWNER target hides the row from both the outer
  owner-refusal and the fixed shape's in-transaction re-read, so the
  predicate is the only remaining guard that knows the row is an owner
  row. Without it the UPDATE demotes the real owner and the org is left
  with zero owners (base outcome: HTTP 200).

Interleaving control is deliberately fix-agnostic (plan-critic rounds 1-2):
no hook matches an UPDATE or BEGIN statement text, since the fix predicates
the statement and moves it into a transaction worker. The race, demotion
and vanished-target legs use a stateful wrapper that ARMS at the to_thread
dispatch of the route's target-role SELECT and fires the racing handler
AFTER that read's ``fetchone`` completes (run the real fetchone, await the
racing handler to completion on its own real pool connection, then return
the real, now-stale result). The zero-row leg is pure fault injection
through the connection: the UPDATE runs for real but its cursor reports
rowcount 0 — matched by normalized-SQL PREFIX (``UPDATE org_members SET
role =``) so it fires for both the base unpredicated form and any fixed
predicated form.

Contract asserted at BOTH ends of the fix (RED at base, GREEN after the
fix): the stale writer is REFUSED and the organization keeps its owner /
the target's membership and role.
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
    OrgMemberUpdateRequest,
    TransferOwnershipRequest,
    remove_org_member,
    transfer_ownership,
    update_org_member_role,
)
from app.models.database import SQLiteConnectionPool, _pool_cache, init_db  # noqa: E402

ORG_ID = 1
TARGET_USER_ID = 2
THIRD_USER_ID = 3

OWNER = {"id": 1, "username": "owner1", "role": "admin"}
ADMIN_CALLER = {"id": 2, "username": "admin2", "role": "member"}

# The route's target-role pre-check read (normalized) — the arming
# statement for the fix-agnostic interleavings.
_TARGET_ROLE_READ_SQL = (
    "SELECT role FROM org_members WHERE org_id = ? AND user_id = ?"
)

UPDATE_ROLE_SQL = "UPDATE org_members SET role = ? WHERE org_id = ? AND user_id = ?"


def _norm_sql(sql) -> str:
    return " ".join(str(sql).split())


class _MultiConnPool:
    """Pool facade handing every caller its own REAL pool connection.

    The driven route and the racing handler must run on separate
    connections, exactly as two concurrent requests would."""

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
    (``UPDATE org_members SET role =``) so it fires for the base
    unpredicated form and any fixed predicated form alike."""

    _MATCH_PREFIX = "UPDATE org_members SET role ="

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


class _StaleFetchoneCursor:
    """Delegates everything to the real cursor but serves a fixed stale row
    from ``fetchone``."""

    def __init__(self, inner, stale_row: tuple) -> None:  # noqa: ANN001
        self._inner = inner
        self._stale_row = stale_row

    def fetchone(self):  # noqa: ANN204
        return self._stale_row

    def __getattr__(self, name):  # noqa: ANN001
        return getattr(self._inner, name)


class _StaleRoleReadConn:
    """Serves a STALE non-owner role ('admin') for the OWNER TARGET's
    role-read statement — for BOTH the outer pre-check read and the fixed
    shape's in-transaction re-read — while every other statement runs real:
    the caller-privilege check (a different user id, so not intercepted)
    and, crucially, the UPDATE against the REAL owner row. This isolates
    the UPDATE's ``role != 'owner'`` predicate: it is the only remaining
    guard that knows the target row is an owner row. Matches by normalized
    SQL text AND bind params (org_id, target user id) so sibling reads for
    other members are untouched."""

    def __init__(
        self, inner: sqlite3.Connection, org_id: int, target_user_id: int
    ) -> None:
        self._inner = inner
        self._org_id = org_id
        self._target_user_id = target_user_id
        self.stale_reads = 0

    def execute(self, sql, params=()):  # noqa: ANN001
        if (
            _norm_sql(sql) == _TARGET_ROLE_READ_SQL
            and tuple(params) == (self._org_id, self._target_user_id)
        ):
            self.stale_reads += 1
            return _StaleFetchoneCursor(
                self._inner.execute(sql, params), ("admin",)
            )
        return self._inner.execute(sql, params)

    def __getattr__(self, name):  # noqa: ANN001
        return getattr(self._inner, name)


class _RaceEnv:
    """Per-test world: fresh db, real pool, patched org-route pool binding."""

    def __init__(self, tmp_path: Path) -> None:
        _pool_cache.clear()
        self.db_path = str(tmp_path / "update-role-race.db")
        init_db(self.db_path)
        self.pool = SQLiteConnectionPool(self.db_path, max_size=6)
        self.facade: object = _MultiConnPool(self.pool)
        self._saved_get_pool = org_mod.get_pool
        org_mod.get_pool = lambda _p: self.facade

    def seed_org(self, user2_org_role: str = "member") -> None:
        """Users 1 (org owner), 2 (at ``user2_org_role``), 3 (member), 99
        (superadmin); org 1."""
        seed = self.pool.get_connection()
        try:
            for uid, username, role in (
                (1, "owner1", "admin"),
                (TARGET_USER_ID, "admin2" if user2_org_role == "admin" else "member2", "member"),
                (THIRD_USER_ID, "member3", "member"),
                (99, "root", "superadmin"),
            ):
                seed.execute(
                    "INSERT INTO users (id, username, hashed_password, full_name, "
                    "role, is_active) VALUES (?, ?, 'abc123', ?, ?, 1)",
                    (uid, username, username.title(), role),
                )
            seed.execute(
                "INSERT INTO organizations (id, name, created_by) "
                "VALUES (1, 'UpdateRoleRaceOrg', 1)"
            )
            seed.execute(
                "INSERT INTO org_members (org_id, user_id, role) "
                "VALUES (1, 1, 'owner')"
            )
            seed.execute(
                "INSERT INTO org_members (org_id, user_id, role) VALUES (1, 2, ?)",
                (user2_org_role,),
            )
            seed.execute(
                "INSERT INTO org_members (org_id, user_id, role) "
                "VALUES (1, 3, 'member')"
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


def _after_target_read_interleave(interleave):
    """Stateful, fix-agnostic interleaving builder: ARM at the to_thread
    dispatch of the route's target-role SELECT; at the NEXT
    to_thread(cursor.fetchone) run the REAL fetchone first, then await the
    racing handler to completion, then return the real (now-stale)
    result."""

    state = {"armed": False, "fired": False}

    def build(real_to_thread):  # noqa: ANN001, ANN202
        async def scripted(fn, *args, **kwargs):  # noqa: ANN002, ANN003
            if (
                not state["armed"]
                and getattr(fn, "__name__", "") == "execute"
                and args
                and _norm_sql(args[0]) == _TARGET_ROLE_READ_SQL
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


def _drive_interleaved(build, drive_factory) -> dict:  # noqa: ANN001
    """Run the driven handler with the wrapped asyncio.to_thread. Non-HTTP
    exceptions (e.g. the base tree's TypeError on a vanished target) are
    captured in ``outcome['error']`` rather than escaping the test."""
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
        except Exception as exc:  # noqa: BLE001
            outcome["error"] = f"{type(exc).__name__}: {exc}"
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
        except Exception as exc:  # noqa: BLE001
            outcome["error"] = f"{type(exc).__name__}: {exc}"

    asyncio.run(drive())
    return outcome


def _assert_refused(outcome: dict, defect: str, statuses: tuple = ()) -> None:
    # Statuses pinned per leg to the documented contract (release note +
    # Multi-Stage review): demoted-privilege/owner refusals are 403, a
    # vanished member is 404, a zero-row write backstop is 409. Callers must
    # pass the expected set explicitly; the empty default fails any status so
    # an unparametrized leg cannot silently widen the contract.
    assert "refused_status" in outcome, (
        f"{defect}: the handler returned success "
        f"({outcome.get('ok')}) instead of refusing"
    )
    assert outcome["refused_status"] in statuses, (
        f"a refusal must be one of {statuses}, not {outcome['refused_status']}"
    )


def test_target_read_racing_ownership_transfer_is_refused(tmp_path: Path) -> None:
    """RED at base, GREEN after the fix.

    A transfer_ownership committing between update_org_member_role's
    target-role read and its unpredicated UPDATE promotes the target to
    owner; the update must REFUSE (400/403/409-class) instead of demoting
    the freshly promoted owner and leaving the org with zero owners."""
    env = _RaceEnv(tmp_path)
    try:
        env.seed_org(user2_org_role="member")

        assert env.member_role(TARGET_USER_ID) == "member", (
            "precondition: the target must start as a plain member, so the "
            "'Cannot change the role of the organization owner' refusal "
            "cannot fire on the pre-check read"
        )

        async def racing_transfer() -> None:
            await transfer_ownership(
                ORG_ID,
                TransferOwnershipRequest(new_owner_user_id=TARGET_USER_ID),
                user=OWNER,
                _csrf_token="t",
            )

        build, state = _after_target_read_interleave(racing_transfer)
        outcome = _drive_interleaved(
            build,
            lambda: update_org_member_role(
                ORG_ID,
                TARGET_USER_ID,
                OrgMemberUpdateRequest(role="member"),
                user=OWNER,
                _csrf_token="t",
            ),
        )

        assert state["fired"], (
            "the racing transfer must have committed after the target-role "
            "read completed — otherwise this run proved nothing"
        )
        assert env.member_role(1) != "owner", (
            "non-vacuity: the racing transfer must have demoted user 1 off "
            "the owner role before the stale UPDATE ran"
        )
        _assert_refused(
            outcome,
            "organization left with zero owners: a transfer_ownership "
            "committed between update_org_member_role's target read and its "
            "unpredicated UPDATE, promoting the target to owner, but the "
            "stale update still executed",
            statuses=(403,),
        )
        assert env.owner_ids() == [TARGET_USER_ID], (
            "organization left with zero owners: the refused update must "
            "keep the promoted target (user 2) as the org's owner"
        )
    finally:
        env.close()


def test_caller_demoted_before_transaction_is_refused(tmp_path: Path) -> None:
    """RED at base, GREEN after the fix (plan-critic R2-F5): the update
    CALLER is demoted from org admin to plain member between the outer
    ``_is_org_admin_or_owner`` check and the write — the stale update must
    be refused with 403 and the target's role left unchanged."""
    env = _RaceEnv(tmp_path)
    try:
        env.seed_org(user2_org_role="admin")

        async def racing_caller_demotion() -> None:
            # Demote user 2 (the driven caller) from org admin to plain
            # member after their privilege check has already passed.
            await update_org_member_role(
                ORG_ID,
                TARGET_USER_ID,
                OrgMemberUpdateRequest(role="member"),
                user=OWNER,
                _csrf_token="t",
            )

        build, state = _after_target_read_interleave(racing_caller_demotion)
        outcome = _drive_interleaved(
            build,
            lambda: update_org_member_role(
                ORG_ID,
                THIRD_USER_ID,
                OrgMemberUpdateRequest(role="member"),
                user=ADMIN_CALLER,
                _csrf_token="t",
            ),
        )

        assert state["fired"], (
            "the racing demotion must have committed after the caller's "
            "target-role read completed — otherwise this run proved nothing"
        )
        assert env.member_role(TARGET_USER_ID) == "member", (
            "non-vacuity: the racing update_org_member_role must have "
            "demoted user 2 (the caller) to plain member before the stale "
            "update's transaction"
        )
        assert outcome.get("refused_status") == 403, (
            "authorization window: the caller was demoted between the "
            "_is_org_admin_or_owner check and the write transaction, but "
            "the stale update still ran (outcome: "
            f"{outcome.get('refused_status', outcome)})"
        )
        assert env.member_role(THIRD_USER_ID) == "member", (
            "the refused update must leave the target's role unchanged"
        )
    finally:
        env.close()


def test_vanished_target_returns_404_not_an_unhandled_error(
    tmp_path: Path,
) -> None:
    """RED at base, GREEN after the fix (plan-critic R2-F6): the target's
    org_members row is removed between the outer target read and the write —
    the update must answer 404 'Member not found', never an unhandled
    TypeError/500. (Empirically at base the UPDATE matches zero rows, the
    commit succeeds, and the response read subscripts None -> TypeError.)"""
    env = _RaceEnv(tmp_path)
    try:
        env.seed_org(user2_org_role="member")

        async def racing_target_removal() -> None:
            await remove_org_member(ORG_ID, TARGET_USER_ID, user=OWNER, _csrf_token="t")

        build, state = _after_target_read_interleave(racing_target_removal)
        outcome = _drive_interleaved(
            build,
            lambda: update_org_member_role(
                ORG_ID,
                TARGET_USER_ID,
                OrgMemberUpdateRequest(role="admin"),
                user=OWNER,
                _csrf_token="t",
            ),
        )

        assert state["fired"], (
            "the racing removal must have committed after the target-role "
            "read completed — otherwise this run proved nothing"
        )
        assert env.member_role(TARGET_USER_ID) is None, (
            "non-vacuity: the racing removal must have deleted the target's "
            "member row before the stale update's write"
        )
        assert outcome.get("refused_status") == 404, (
            "vanished target: the member row was removed between the outer "
            "target read and the write, but the handler did not answer the "
            "404 'Member not found' refusal (outcome: "
            f"{outcome.get('refused_status') or outcome.get('error') or outcome.get('ok')})"
        )
    finally:
        env.close()


def test_zero_row_update_refuses_and_leaves_role_unchanged(tmp_path: Path) -> None:
    """RED at base, GREEN after the fix (fault injection): an UPDATE that
    runs but matches zero rows (rowcount shim) must be detected — the
    handler refuses (403/409-class) and the transaction rolls back, leaving
    the target's role unchanged. At base there is no rowcount check: the
    real UPDATE commits and the handler returns 200 on a write it believed
    landed."""
    env = _RaceEnv(tmp_path)
    try:
        env.seed_org(user2_org_role="admin")

        wrapped = _ZeroRowConn(env.pool.get_connection())
        env.facade = _SingleConnFacade(env.pool, wrapped)

        outcome = _drive(
            lambda: update_org_member_role(
                ORG_ID,
                TARGET_USER_ID,
                OrgMemberUpdateRequest(role="member"),
                user=OWNER,
                _csrf_token="t",
            )
        )

        assert wrapped.shim_fired, (
            "the zero-row UPDATE shim must actually have fired — "
            "otherwise this run proved nothing"
        )
        _assert_refused(
            outcome,
            "zero-row UPDATE: the role UPDATE matched zero rows, but the "
            "handler returned success on a write that never landed",
            statuses=(409,),
        )
        assert env.member_role(TARGET_USER_ID) == "admin", (
            "the refused update must roll back and leave the target's role "
            "unchanged (rollback proven)"
        )
        assert env.owner_ids() == [1], "the org keeps its owner"
    finally:
        env.close()


def test_owner_at_write_without_role_predicate_refuses_and_preserves_owner(
    tmp_path: Path,
) -> None:
    """RED at base, GREEN after the fix (plan-critic round 3): the UPDATE's
    ``AND role != 'owner'`` predicate is the only guard that still knows the
    target row is an OWNER row when both role reads are served a stale
    'admin' — an implementation with the in-transaction re-reads and the
    rowcount check but WITHOUT the predicate demotes the real owner here
    and leaves the org with zero owners. With the predicate the UPDATE
    matches zero rows against the real owner row, the route refuses
    (403/409-class), and the owner is preserved."""
    env = _RaceEnv(tmp_path)
    try:
        # Target is user 1 — the org's sole OWNER. The caller (user 2) is a
        # plain org admin, so the outer caller-privilege check passes
        # without touching the shimmed target read.
        env.seed_org(user2_org_role="admin")

        wrapped = _StaleRoleReadConn(
            env.pool.get_connection(), org_id=ORG_ID, target_user_id=1
        )
        env.facade = _SingleConnFacade(env.pool, wrapped)

        outcome = _drive(
            lambda: update_org_member_role(
                ORG_ID,
                1,
                OrgMemberUpdateRequest(role="member"),
                user=ADMIN_CALLER,
                _csrf_token="t",
            )
        )

        assert wrapped.stale_reads >= 1, (
            "the stale-role shim must have served the route's target-role "
            "read(s) — the outer pre-check read at base, plus the "
            "in-transaction re-read at the fixed head — otherwise this run "
            "proved nothing"
        )
        _assert_refused(
            outcome,
            "owner-at-write: the stale 'admin' read hid the owner target "
            "from the role refusals, and without the role != 'owner' "
            "predicate on the UPDATE the real owner row was demoted — the "
            "organization was left with zero owners",
            statuses=(409,),
        )
        assert env.member_role(1) == "owner", (
            "the refused update must preserve the target as the org's owner "
            f"(role now: {env.member_role(1)!r})"
        )
        assert env.owner_ids() == [1], (
            "organization left with zero owners: the owner-at-write UPDATE "
            "must never strip the org's only owner row"
        )
    finally:
        env.close()
