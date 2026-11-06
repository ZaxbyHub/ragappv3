"""Issue #1068 AC2: the three users.py worker-commit tails keep their
security-audit row (and the post-commit callback) across a cancellation
delivered exactly at the commit boundary.

At base d00bc160 each of the three mutating users.py tails still named by
the #1068 census commits inside a worker and only after the commit await
resumes does the handler create the mandatory ``safe_record_security_event``
coroutine:

- ``admin_reset_password`` — inline ``db.execute("COMMIT")`` inside
  ``_admin_reset_password_db`` (BEGIN EXCLUSIVE), then
  ``invalidate_active_user_cache`` then the ``user.password_reset`` tail;
- ``update_user_organizations`` — ``conn.commit()`` inside ``_update_orgs``,
  then the ``membership.organizations_replaced`` tail;
- ``update_user_groups`` — helper commit inside
  ``with transaction_context(conn):`` in ``_update_groups``, then the
  ``membership.groups_replaced`` tail.

A cancellation delivered exactly at the commit await — after the commit work
completed in the worker — unwinds the handler before the audit coroutine
exists (and before the password-reset cache invalidation runs), losing the
tamper-evident row for an already-applied mutation. PR #1067 already
converted this file's create/update/role/active tails to
``drain_commit_audit``; these three legs pin the same contract for the
remaining family members.

Each leg drives the REAL handler coroutine with a deterministic
commit-boundary schedule: the driven connection is a proxy whose FIRST
transaction end (``conn.commit()`` or the inline ``execute("COMMIT")``) runs
the REAL commit and, once it has completed, schedules the driving task's
cancellation onto the event loop from the worker thread — queued before the
``to_thread`` completion callback can resume the route's commit-bearing
await, so the CancelledError lands exactly there. The orgs/groups routes
resolve their connection through the module-level ``get_pool`` binding,
which is patched to a single-connection facade handing out the hooked
proxy.

Contract asserted at BOTH ends (RED at base, GREEN after the fix — the
commit, the post-commit callback where present, and the audit tail become
one drained unit, so the cancellation is absorbed until the audit row is
written and only then re-raised):

- non-vacuity: the commit hook fired AND the mutation is durable;
- the handler task ends cancelled (the delivered cancellation is re-raised
  after the drained unit completes, per the #1067 outcome contract);
- the mutation's security-audit row exists in ``security_audit_log``;
- for the password reset, ``invalidate_active_user_cache`` still ran.
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

from starlette.requests import Request  # noqa: E402

from app.api.routes import users as users_mod  # noqa: E402
from app.api.routes.users import (  # noqa: E402
    AdminResetPasswordRequest,
    UserGroupsUpdateRequest,
    UserOrgsUpdateRequest,
    admin_reset_password,
    update_user_groups,
    update_user_organizations,
)
from app.models.database import (  # noqa: E402
    SQLiteConnectionPool,
    _pool_cache,
    init_db,
    run_migrations,
)

TARGET_USER_ID = 2

_SUPERADMIN = {
    "id": 1,
    "username": "root",
    "full_name": "Root",
    "role": "superadmin",
    "is_active": True,
    "must_change_password": False,
}


class _CancelAtCommitConn:
    """Delegating sqlite3 connection proxy that arms the deterministic
    cancellation at the FIRST transaction end on the driven connection.

    The real commit completes FIRST, then the driving route task's
    cancellation is scheduled onto the loop from the worker thread via
    ``loop.call_soon_threadsafe`` — deterministically ahead of the
    to_thread-future completion callback, so the CancelledError is delivered
    exactly at the commit-bearing await, after the commit work completed.
    Only fires once (the security-audit write's own commit must not re-arm).
    """

    def __init__(self, real: sqlite3.Connection, arm) -> None:  # noqa: ANN001
        self._real = real
        self._arm = arm

    def commit(self) -> None:
        self._real.commit()
        self._arm()

    def execute(self, sql, *args, **kwargs):  # noqa: ANN001, ANN202, ANN003
        # admin_reset_password's worker commits via an inline "COMMIT"
        # statement under BEGIN EXCLUSIVE: run the real statement, then arm.
        if str(sql).strip().rstrip(";").upper() == "COMMIT":
            cursor = self._real.execute(sql, *args, **kwargs)
            self._arm()
            return cursor
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):  # noqa: ANN001
        return getattr(self._real, name)


class _PoolFacade:
    """Single-connection pool facade for the orgs/groups routes: hands the
    driven route the hooked proxy (set by the drive helper just before
    dispatch) and swallows the handler's release (the underlying real
    connection is returned to the real pool by the world's close)."""

    def __init__(self) -> None:
        self.hooked: _CancelAtCommitConn | None = None

    async def get_connection_async(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return self.hooked

    def release_connection(self, conn) -> None:  # noqa: ANN001
        try:
            if getattr(conn, "in_transaction", False):
                conn.rollback()
        except sqlite3.Error:
            pass


class _UsersWorld:
    """Per-test world: temp DB, real pool, patched users-route pool binding."""

    def __init__(self, tmp_path: Path, db_name: str) -> None:
        _pool_cache.clear()
        self.db_path = str(tmp_path / db_name)
        init_db(self.db_path)
        # password_changed_at / organizations / groups columns and tables
        # arrive through the migration journal, not the base schema.
        run_migrations(self.db_path)
        self.pool = SQLiteConnectionPool(self.db_path, max_size=2)
        self.real_conn = self.pool.get_connection()
        self.facade = _PoolFacade()
        self._saved_get_pool = users_mod.get_pool
        users_mod.get_pool = lambda _p: self.facade
        seed = self.pool.get_connection()
        try:
            for uid, username, role in (
                (1, "root", "superadmin"),
                (TARGET_USER_ID, "target", "member"),
            ):
                seed.execute(
                    "INSERT INTO users (id, username, hashed_password, role, is_active) "
                    "VALUES (?, ?, 'abc123', ?, 1)",
                    (uid, username, role),
                )
            seed.execute(
                "INSERT INTO user_sessions (user_id, refresh_token_hash, expires_at) "
                "VALUES (?, 'seed-hash', '2030-01-01T00:00:00+00:00')",
                (TARGET_USER_ID,),
            )
            seed.commit()
        finally:
            self.pool.release_connection(seed)
        self.invalidations: list[int] = []
        self._saved_invalidate = users_mod.invalidate_active_user_cache
        users_mod.invalidate_active_user_cache = lambda uid: self.invalidations.append(
            uid
        )

    def seed_org_world(self) -> None:
        conn = self.pool.get_connection()
        try:
            conn.execute(
                "INSERT INTO organizations (id, name) VALUES (1, 'Org One')"
            )
            conn.execute(
                "INSERT INTO org_members (org_id, user_id, role) "
                "VALUES (1, ?, 'member')",
                (TARGET_USER_ID,),
            )
            conn.execute(
                "INSERT INTO groups (id, org_id, name) VALUES (1, 1, 'Group One')"
            )
            conn.commit()
        finally:
            self.pool.release_connection(conn)

    def scalar(self, sql: str, params: tuple = ()) -> object:
        check = sqlite3.connect(self.db_path)
        try:
            row = check.execute(sql, params).fetchone()
            return None if row is None else row[0]
        finally:
            check.close()

    def close(self) -> None:
        users_mod.get_pool = self._saved_get_pool
        users_mod.invalidate_active_user_cache = self._saved_invalidate
        try:
            if self.real_conn.in_transaction:
                self.real_conn.rollback()
        except sqlite3.Error:
            pass
        try:
            self.pool.release_connection(self.real_conn)
        except Exception:  # noqa: BLE001
            pass
        self.pool.close_all()
        _pool_cache.clear()


def _request_for(method: str, path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "headers": [],
            "path": path,
            "query_string": b"",
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


def _drive(world: _UsersWorld, make_call) -> tuple[str, dict]:  # noqa: ANN001
    """Deterministic commit-boundary schedule: the first transaction end on
    the world's driven connection arms the driving task's cancellation."""

    async def drive() -> str:
        loop = asyncio.get_running_loop()
        state = {"fired": False}
        holder: dict = {"task": None}

        def _arm() -> None:
            if not state["fired"]:
                state["fired"] = True
                loop.call_soon_threadsafe(holder["task"].cancel)

        conn = _CancelAtCommitConn(world.real_conn, _arm)
        world.facade.hooked = conn
        holder["task"] = asyncio.ensure_future(make_call(conn))
        try:
            await holder["task"]
            return "returned"
        except asyncio.CancelledError:
            return "cancelled"
        except Exception as exc:  # noqa: BLE001
            return f"error:{type(exc).__name__}"
        finally:
            state_holder["state"] = state

    state_holder: dict = {}
    outcome = asyncio.run(drive())
    return outcome, state_holder["state"]


def _audit_rows(world: _UsersWorld, event_type: str) -> int:
    return int(
        world.scalar(
            "SELECT COUNT(*) FROM security_audit_log WHERE event_type = ?",
            (event_type,),
        )
    )


# named-mutation: delete the after_commit arm of the drained unit at the
# admin_reset_password site (drop invalidate_active_user_cache from inside the
# unit) -> test_admin_reset_password_cancel_at_commit_still_audits regresses.


def test_admin_reset_password_cancel_at_commit_still_audits(
    tmp_path: Path,
) -> None:
    """RED at base, GREEN after the fix. The BEGIN-EXCLUSIVE worker commits
    via an inline ``execute("COMMIT")``; a cancellation landing at that commit
    await must not lose the ``user.password_reset`` row, and the post-commit
    ``invalidate_active_user_cache`` callback must still run."""
    world = _UsersWorld(tmp_path, "users-password-reset.db")
    try:
        outcome, state = _drive(
            world,
            lambda conn: admin_reset_password(
                TARGET_USER_ID,
                _request_for("PATCH", f"/api/users/{TARGET_USER_ID}/password"),
                AdminResetPasswordRequest(new_password="NewPassword456"),
                user=dict(_SUPERADMIN),
                db=conn,
                _csrf_token="t",
            ),
        )
        assert state["fired"], "non-vacuity: the commit hook must have fired"
        assert outcome == "cancelled", f"route must unwind cancelled (got {outcome!r})"
        assert (
            world.scalar(
                "SELECT must_change_password FROM users WHERE id = ?",
                (TARGET_USER_ID,),
            )
            == 1
        ), "precondition: the password-reset commit completed"
        assert (
            world.scalar(
                "SELECT COUNT(*) FROM user_sessions WHERE user_id = ?",
                (TARGET_USER_ID,),
            )
            == 0
        ), "precondition: the session purge inside the same commit completed"
        assert world.invalidations == [TARGET_USER_ID], (
            "the post-commit cache invalidation must run inside the drained "
            "unit, between the commit and the audit row"
        )
        assert _audit_rows(world, "user.password_reset") == 1, (
            "commit-boundary audit loss: the password reset was applied (the "
            "commit completed) but its user.password_reset security-audit row "
            "was never recorded"
        )
    finally:
        world.close()


def test_update_user_organizations_cancel_at_commit_still_audits(
    tmp_path: Path,
) -> None:
    """RED at base, GREEN after the fix. ``_update_orgs`` commits the
    membership replacement; a cancellation landing at that commit await must
    not lose the ``membership.organizations_replaced`` row."""
    world = _UsersWorld(tmp_path, "users-orgs.db")
    world.seed_org_world()
    try:
        outcome, state = _drive(
            world,
            lambda conn: update_user_organizations(
                TARGET_USER_ID,
                _request_for("PUT", f"/api/users/{TARGET_USER_ID}/organizations"),
                UserOrgsUpdateRequest(org_ids=[1]),
                user=dict(_SUPERADMIN),
                _csrf_token="t",
            ),
        )
        assert state["fired"], "non-vacuity: the commit hook must have fired"
        assert outcome == "cancelled", f"route must unwind cancelled (got {outcome!r})"
        assert (
            world.scalar(
                "SELECT COUNT(*) FROM org_members WHERE user_id = ? AND org_id = 1",
                (TARGET_USER_ID,),
            )
            == 1
        ), "precondition: the membership replacement commit completed"
        assert _audit_rows(world, "membership.organizations_replaced") == 1, (
            "commit-boundary audit loss: the organization membership "
            "replacement was applied (the commit completed) but its "
            "membership.organizations_replaced security-audit row was never "
            "recorded"
        )
    finally:
        world.close()


def test_update_user_groups_cancel_at_commit_still_audits(tmp_path: Path) -> None:
    """RED at base, GREEN after the fix. ``_update_groups`` commits inside
    ``transaction_context``; a cancellation landing at that commit await must
    not lose the ``membership.groups_replaced`` row."""
    world = _UsersWorld(tmp_path, "users-groups.db")
    world.seed_org_world()
    try:
        outcome, state = _drive(
            world,
            lambda conn: update_user_groups(
                TARGET_USER_ID,
                _request_for("PUT", f"/api/users/{TARGET_USER_ID}/groups"),
                UserGroupsUpdateRequest(group_ids=[1]),
                user=dict(_SUPERADMIN),
                _csrf_token="t",
            ),
        )
        assert state["fired"], "non-vacuity: the commit hook must have fired"
        assert outcome == "cancelled", f"route must unwind cancelled (got {outcome!r})"
        assert (
            world.scalar(
                "SELECT COUNT(*) FROM group_members WHERE user_id = ? AND group_id = 1",
                (TARGET_USER_ID,),
            )
            == 1
        ), "precondition: the group membership replacement commit completed"
        assert _audit_rows(world, "membership.groups_replaced") == 1, (
            "commit-boundary audit loss: the group membership replacement "
            "was applied (the commit completed) but its "
            "membership.groups_replaced security-audit row was never recorded"
        )
    finally:
        world.close()
