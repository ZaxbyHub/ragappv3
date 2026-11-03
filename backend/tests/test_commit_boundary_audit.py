"""Issue #1043 residual 3: a cancellation landing exactly at the commit
boundary loses the security-audit row for an applied mutation.

users.py's post-commit bookkeeping pattern —
``await asyncio.to_thread(conn.commit)`` followed by
``await drain_audit(safe_record_security_event(...))``
(update_user_role commits at :629 then audits at :632; same shape at the
other commit sites) — has a window the #1035 drain cannot close: if the
COMMIT work has already completed in the to_thread worker and a client
disconnect cancels the route task BEFORE the commit await resumes, the
CancelledError is raised at that await and everything after it — including
the drained audit — is skipped. The mutation is durable (the commit
happened) but its ``user.role_updated`` security-audit row is never
recorded.

This check drives the REAL ``update_user_role`` with a deterministic
schedule (no timing races): the scripted ``to_thread`` wrapper runs the
REAL ``conn.commit`` in the worker thread and, once it has COMPLETED, uses
``loop.call_soon_threadsafe(task.cancel)`` to cancel the driving ROUTE task
before the commit await can resume. The route task must unwind
CancelledError — and the security-audit row for the applied mutation must
still exist in ``security_audit_log`` afterward (the real table
``safe_record_security_event`` writes via
``record_security_event`` in app/services/security_audit.py).

Contract asserted at BOTH ends of the fix (RED at base, GREEN after the
fix — the commit+audit boundary is deferred as one drained sequence, so
the cancellation is absorbed until the audit row is written and only then
re-raised).
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
from app.api.routes.users import UpdateRoleRequest, update_user_role  # noqa: E402
from app.models.database import SQLiteConnectionPool, _pool_cache, init_db  # noqa: E402

TARGET_USER_ID = 2

_SUPERADMIN = {
    "id": 1,
    "username": "root",
    "full_name": "Root",
    "role": "superadmin",
    "is_active": True,
    "must_change_password": False,
}


class _SingleConnPoolFacade:
    """Pool facade handing the driven route one real pool connection."""

    def __init__(self, real_pool: SQLiteConnectionPool) -> None:
        self._real_pool = real_pool
        self._conn: sqlite3.Connection | None = None

    async def get_connection_async(self, *args, **kwargs):  # noqa: ANN002, ANN003
        if self._conn is None:
            self._conn = self._real_pool.get_connection()
        return self._conn

    def release_connection(self, conn) -> None:
        try:
            if conn.in_transaction:
                conn.rollback()
        except sqlite3.Error:
            pass
        # The connection is released back to the real pool in env.close().


class _AuditEnv:
    """Per-test world: temp DB, real pool, patched users-route pool binding."""

    def __init__(self, tmp_path: Path) -> None:
        _pool_cache.clear()
        self.db_path = str(tmp_path / "commit-boundary-audit.db")
        init_db(self.db_path)
        self.pool = SQLiteConnectionPool(self.db_path, max_size=2)
        self.facade = _SingleConnPoolFacade(self.pool)
        self._saved_get_pool = users_mod.get_pool
        users_mod.get_pool = lambda _p: self.facade

        seed = self.pool.get_connection()
        try:
            seed.execute(
                "INSERT INTO users (id, username, hashed_password, role, is_active) "
                "VALUES (?, ?, ?, ?, ?)",
                (1, "root", "abc123", "superadmin", 1),
            )
            seed.execute(
                "INSERT INTO users (id, username, hashed_password, role, is_active) "
                "VALUES (?, ?, ?, ?, ?)",
                (TARGET_USER_ID, "target", "abc123", "member", 1),
            )
            seed.commit()
        finally:
            self.pool.release_connection(seed)

    def scalar(self, sql: str, params: tuple = ()) -> object:
        check = sqlite3.connect(self.db_path)
        try:
            row = check.execute(sql, params).fetchone()
            return None if row is None else row[0]
        finally:
            check.close()

    def close(self) -> None:
        users_mod.get_pool = self._saved_get_pool
        if self.facade._conn is not None:
            try:
                self.pool.release_connection(self.facade._conn)
            except Exception:
                pass
        self.pool.close_all()
        _pool_cache.clear()


def _request_for(user_id: int) -> Request:
    return Request(
        {
            "type": "http",
            "method": "PATCH",
            "headers": [],
            "path": f"/api/users/{user_id}/role",
            "query_string": b"",
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


def test_commit_boundary_cancellation_still_records_audit(tmp_path: Path) -> None:
    """RED at base, GREEN after the fix.

    The REAL commit executes in the to_thread worker; once it has completed,
    the driving task is cancelled (loop.call_soon_threadsafe(task.cancel))
    BEFORE the commit await resumes. The route task must unwind
    CancelledError AND the ``user.role_updated`` security-audit row must
    exist in the real security_audit_log table afterward.
    """
    env = _AuditEnv(tmp_path)
    real_to_thread = asyncio.to_thread
    state = {"commit_cancel_fired": False}
    route_holder: dict = {"task": None}

    async def scripted(fn, *args, **kwargs):  # noqa: ANN002, ANN003
        if (
            not state["commit_cancel_fired"]
            and getattr(fn, "__name__", "") == "commit"
        ):
            state["commit_cancel_fired"] = True
            loop = asyncio.get_running_loop()

            def _commit_then_cancel() -> None:
                # Runs on the worker thread: the REAL commit completes
                # FIRST, then the cancellation is scheduled onto the loop —
                # queued before the future-completion callback that would
                # resume the route's commit await, so the CancelledError is
                # delivered exactly at that await, after the commit work.
                fn(*args, **kwargs)
                loop.call_soon_threadsafe(route_holder["task"].cancel)

            return await real_to_thread(_commit_then_cancel)
        return await real_to_thread(fn, *args, **kwargs)

    async def drive() -> str:
        asyncio.to_thread = scripted
        try:
            route_holder["task"] = asyncio.ensure_future(
                update_user_role(
                    TARGET_USER_ID,
                    _request_for(TARGET_USER_ID),
                    UpdateRoleRequest(role="admin"),
                    user=dict(_SUPERADMIN),
                    _csrf_token="t",
                )
            )
            try:
                await route_holder["task"]
                return "returned"
            except asyncio.CancelledError:
                return "cancelled"
            except Exception as exc:  # noqa: BLE001
                return f"error:{type(exc).__name__}"
        finally:
            asyncio.to_thread = real_to_thread

    try:
        outcome = asyncio.run(drive())
    finally:
        env.close()

    assert state["commit_cancel_fired"], (
        "non-vacuity: the cancellation must have been scheduled right after "
        "the real commit completed (the commit hook never fired) — otherwise "
        "this run proved nothing"
    )
    assert outcome == "cancelled", (
        "the route task must unwind CancelledError: the delivered "
        f"cancellation propagates after the boundary completes (got {outcome!r})"
    )
    applied_role = env.scalar(
        "SELECT role FROM users WHERE id = ?", (TARGET_USER_ID,)
    )
    assert applied_role == "admin", (
        "precondition: the commit boundary was actually CROSSED — the REAL "
        "commit completed before the cancellation landed, so the mutation "
        f"is durable (role now {applied_role!r})"
    )
    audit_rows = env.scalar(
        "SELECT COUNT(*) FROM security_audit_log "
        "WHERE event_type = 'user.role_updated' AND target_user_id = ?",
        (TARGET_USER_ID,),
    )
    assert audit_rows == 1, (
        "commit-boundary audit loss: the role mutation was applied (the "
        "commit completed) but its user.role_updated security-audit row was "
        f"never recorded (found {audit_rows} rows) — the cancellation "
        "delivered exactly at the commit await skipped the drained audit"
    )
