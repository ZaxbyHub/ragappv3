"""Issue #1068 AC1: the eight auth.py worker-commit tails keep their
security-audit row across a cancellation delivered exactly at the commit
boundary.

At base d00bc160 each of the mutating auth.py tails commits inside a
``to_thread`` worker (``_register_db`` / ``_login_create_session`` /
``_record_failed_attempt_db`` / ``_logout_db`` — twice: with and without a
Bearer access-token denial — / ``_change_password_db`` /
``_revoke_session_db`` / ``_revoke_all_sessions_db``) and only AFTER the
commit await resumes does the handler create the mandatory
``safe_record_security_event(...)`` coroutine for ``drain_audit``. A
cancellation delivered exactly at the commit await — after the commit work
completed in the worker — therefore unwinds the handler before the audit
coroutine exists, losing the tamper-evident row for an already-applied
mutation (the residual window PR #1067's ``drain_commit_audit`` /
drained-unit mechanism closes at the converted users.py/vaults.py sites).

Each leg here drives the REAL handler coroutine with a deterministic
commit-boundary schedule: the driven connection is a proxy whose FIRST
transaction end (``conn.commit()`` or an inline ``execute("COMMIT")``) runs
the REAL commit and, once it has completed, schedules the driving task's
cancellation onto the event loop from the worker thread — queued before the
``to_thread`` completion callback can resume the route's commit-bearing
await, so the CancelledError lands exactly there. sqlite3.Connection forbids
attribute assignment, hence the delegating proxy. The Bearer-logout leg uses
a worker-commit-only proxy variant: with a Bearer header the handler's FIRST
two transaction ends are the synchronous loop-thread denial commits
(``deny_access_token`` / ``purge_expired_denied_tokens``,
auth_service.py:215-247) that precede the session-delete worker, so the
cancellation must be armed at the session-delete commit — the boundary after
those denial mutations — not at the first denial commit.

Contract asserted at BOTH ends (RED at base, GREEN after the fix — the
commit and the audit tail become one drained unit, so the cancellation is
absorbed until the audit row is written and only then re-raised):

- non-vacuity: the commit hook fired AND the mutation is durable;
- the handler task ends cancelled (the delivered cancellation is re-raised
  after the drained unit completes, per the #1067 outcome contract);
- the mutation's security-audit row exists in ``security_audit_log``
  (``user.register`` / ``auth.login_success`` / ``auth.login_failed`` /
  ``auth.logout`` (both logout legs) / ``auth.password_changed`` /
  ``auth.session_revoked`` / ``auth.sessions_revoked``).
"""

import asyncio
import os

os.environ["JWT_SECRET_KEY"] = (
    "test-jwt-secret-key-for-testing-only-12345678901234567890"
)
os.environ["USERS_ENABLED"] = "true"
os.environ["REDIS_URL"] = ""
os.environ["ADMIN_SECRET_TOKEN"] = (
    "test-admin-secret-0123456789abcdef0123456789abcdef0123456789abcdef"
)

import hashlib  # noqa: E402
import sqlite3  # noqa: E402
import threading  # noqa: E402
from pathlib import Path  # noqa: E402

from starlette.requests import Request  # noqa: E402
from starlette.responses import Response  # noqa: E402

from app.api.routes import auth as auth_mod  # noqa: E402
from app.api.routes.auth import (  # noqa: E402
    ChangePasswordRequest,
    LoginRequest,
    RegisterRequest,
    change_password,
    login,
    logout,
    register,
    revoke_all_sessions,
    revoke_session,
)
from app.config import settings  # noqa: E402
from app.models.database import (  # noqa: E402
    SQLiteConnectionPool,
    _pool_cache,
    init_db,
    run_migrations,
)
from app.services.auth_service import (  # noqa: E402
    async_hash_password,
    create_access_token,
)

OWNER_ID = 1
TARGET_USER_ID = 2

_GOOD_PASSWORD = "Password123"
_GOOD_PASSWORD_HASH = None  # lazily computed argon2 hash (see _hash())

_SUPERADMIN = {
    "id": OWNER_ID,
    "username": "root",
    "full_name": "Root",
    "role": "superadmin",
    "is_active": True,
    "must_change_password": False,
}


def _hash(plain: str) -> str:
    return asyncio.run(async_hash_password(plain))


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
        # Inline COMMIT statements (BEGIN EXCLUSIVE workers) are transaction
        # ends too: run the real statement, then arm.
        if str(sql).strip().rstrip(";").upper() == "COMMIT":
            cursor = self._real.execute(sql, *args, **kwargs)
            self._arm()
            return cursor
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):  # noqa: ANN001
        return getattr(self._real, name)


class _CancelAtWorkerCommitConn(_CancelAtCommitConn):
    """Bearer-logout variant: ``deny_access_token`` /
    ``purge_expired_denied_tokens`` commit SYNCHRONOUSLY on the loop thread
    before the session-delete worker runs (auth_service.py:215-247), so the
    FIRST transaction end is a denial commit, not the boundary under test.
    Arm only at the first transaction end made from a NON-loop thread — the
    committing ``to_thread`` worker — so the cancellation lands exactly at
    the session-delete commit boundary, after the denial mutations are
    durable. Only fires once (the security-audit write's own commit must not
    re-arm)."""

    def __init__(self, real: sqlite3.Connection, arm) -> None:  # noqa: ANN001
        super().__init__(real, arm)
        self._loop_thread = threading.get_ident()

    def _in_worker(self) -> bool:
        return threading.get_ident() != self._loop_thread

    def commit(self) -> None:
        in_worker = self._in_worker()
        self._real.commit()
        if in_worker:
            self._arm()

    def execute(self, sql, *args, **kwargs):  # noqa: ANN001, ANN202, ANN003
        if str(sql).strip().rstrip(";").upper() == "COMMIT":
            in_worker = self._in_worker()
            cursor = self._real.execute(sql, *args, **kwargs)
            if in_worker:
                self._arm()
            return cursor
        return self._real.execute(sql, *args, **kwargs)


class _AuthWorld:
    """Per-test world: temp DB, real pool, settings toggles, seeding."""

    def __init__(self, tmp_path: Path, db_name: str) -> None:
        _pool_cache.clear()
        self._saved_users_enabled = settings.users_enabled
        settings.users_enabled = True
        self.db_path = str(tmp_path / db_name)
        init_db(self.db_path)
        # password_changed_at (and the rest of the migration journal) does
        # not exist in the base SCHEMA — mirror the users-family harness and
        # run the real migrations so _change_password_db's UPDATE can execute
        # at all (its failure at base must be the AUDIT-LOSS assert, not an
        # OperationalError on a missing column).
        run_migrations(self.db_path)
        self.pool = SQLiteConnectionPool(self.db_path, max_size=2)
        self.real_conn = self.pool.get_connection()

    def seed_users(self, *rows: tuple) -> None:
        for uid, username in rows:
            self.real_conn.execute(
                "INSERT INTO users (id, username, hashed_password, role, is_active) "
                "VALUES (?, ?, ?, 'member', 1)",
                (uid, username, _hash(_GOOD_PASSWORD)),
            )
        self.real_conn.commit()

    def seed_session(
        self, user_id: int, refresh_token: str, session_id: int | None = None
    ) -> None:
        token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
        if session_id is not None:
            self.real_conn.execute(
                "INSERT INTO user_sessions (id, user_id, refresh_token_hash, expires_at) "
                "VALUES (?, ?, ?, '2030-01-01T00:00:00+00:00')",
                (session_id, user_id, token_hash),
            )
        else:
            self.real_conn.execute(
                "INSERT INTO user_sessions (user_id, refresh_token_hash, expires_at) "
                "VALUES (?, ?, '2030-01-01T00:00:00+00:00')",
                (user_id, token_hash),
            )
        self.real_conn.commit()

    def scalar(self, sql: str, params: tuple = ()) -> object:
        check = sqlite3.connect(self.db_path)
        try:
            row = check.execute(sql, params).fetchone()
            return None if row is None else row[0]
        finally:
            check.close()

    def close(self) -> None:
        settings.users_enabled = self._saved_users_enabled
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


def _request_for(method: str, path: str, cookies: dict | None = None) -> Request:
    headers = []
    if cookies:
        for name, value in cookies.items():
            headers.append((b"cookie", f"{name}={value}".encode()))
    return Request(
        {
            "type": "http",
            "method": method,
            "headers": headers,
            "path": path,
            "query_string": b"",
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


def _drive(world: _AuthWorld, make_call, proxy_cls=_CancelAtCommitConn) -> tuple[str, dict]:  # noqa: ANN001
    """Deterministic commit-boundary schedule against ``world``'s connection."""

    async def drive() -> str:
        loop = asyncio.get_running_loop()
        state = {"fired": False}
        holder: dict = {"task": None}

        def _arm() -> None:
            if not state["fired"]:
                state["fired"] = True
                loop.call_soon_threadsafe(holder["task"].cancel)

        conn = proxy_cls(world.real_conn, _arm)
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


def _audit_rows(world: _AuthWorld, event_type: str) -> int:
    return int(
        world.scalar(
            "SELECT COUNT(*) FROM security_audit_log WHERE event_type = ?",
            (event_type,),
        )
    )


# named-mutation: delete the audit arm of the drained unit at the auth register
# site (resume after the commit without draining the user.register row inside
# the unit) -> test_register_cancel_at_commit_still_audits regresses.


def test_register_cancel_at_commit_still_audits(tmp_path: Path) -> None:
    """RED at base, GREEN after the fix. The registration transaction commits
    in ``_register_db``; a cancellation landing at that commit await must not
    lose the ``user.register`` row."""
    world = _AuthWorld(tmp_path, "auth-register.db")
    try:
        outcome, state = _drive(
            world,
            lambda conn: register(
                _request_for("POST", "/api/auth/register"),
                Response(),
                RegisterRequest(username="newuser", password=_GOOD_PASSWORD),
                db=conn,
                _csrf_token="t",
            ),
        )
        assert state["fired"], "non-vacuity: the commit hook must have fired"
        assert outcome == "cancelled", f"route must unwind cancelled (got {outcome!r})"
        assert (
            world.scalar("SELECT COUNT(*) FROM users WHERE username = 'newuser'") == 1
        ), "precondition: the registration commit completed before the cancel landed"
        assert _audit_rows(world, "user.register") == 1, (
            "commit-boundary audit loss: the registration was applied (the "
            "commit completed) but its user.register security-audit row was "
            "never recorded — the cancellation delivered exactly at the "
            "commit await skipped the audit tail"
        )
    finally:
        world.close()


def test_login_success_cancel_at_commit_still_audits(tmp_path: Path) -> None:
    """RED at base, GREEN after the fix. The session-create transaction
    commits in ``_login_create_session``; a cancellation landing at that
    commit await must not lose the ``auth.login_success`` row."""
    world = _AuthWorld(tmp_path, "auth-login-success.db")
    world.seed_users((OWNER_ID, "root"))
    try:
        outcome, state = _drive(
            world,
            lambda conn: login(
                _request_for("POST", "/api/auth/login"),
                Response(),
                LoginRequest(username="root", password=_GOOD_PASSWORD),
                db=conn,
                _csrf_token="t",
            ),
        )
        assert state["fired"], "non-vacuity: the commit hook must have fired"
        assert outcome == "cancelled", f"route must unwind cancelled (got {outcome!r})"
        assert (
            world.scalar(
                "SELECT COUNT(*) FROM user_sessions WHERE user_id = ?", (OWNER_ID,)
            )
            >= 1
        ), "precondition: the session-create commit completed"
        assert _audit_rows(world, "auth.login_success") == 1, (
            "commit-boundary audit loss: the login session was applied (the "
            "commit completed) but its auth.login_success security-audit row "
            "was never recorded"
        )
    finally:
        world.close()


def test_login_failed_cancel_at_commit_still_audits(tmp_path: Path) -> None:
    """RED at base, GREEN after the fix. The failed-attempt counter increments
    and commits in ``_record_failed_attempt_db``; a cancellation landing at
    that commit await must not lose the ``auth.login_failed`` row (the
    lockout-counter audit)."""
    world = _AuthWorld(tmp_path, "auth-login-failed.db")
    world.seed_users((OWNER_ID, "root"))
    try:
        outcome, state = _drive(
            world,
            lambda conn: login(
                _request_for("POST", "/api/auth/login"),
                Response(),
                LoginRequest(username="root", password="WrongPassword9"),
                db=conn,
                _csrf_token="t",
            ),
        )
        assert state["fired"], "non-vacuity: the commit hook must have fired"
        assert outcome == "cancelled", f"route must unwind cancelled (got {outcome!r})"
        assert (
            world.scalar(
                "SELECT failed_attempts FROM users WHERE id = ?", (OWNER_ID,)
            )
            == 1
        ), "precondition: the failed-attempt counter commit completed"
        assert _audit_rows(world, "auth.login_failed") == 1, (
            "commit-boundary audit loss: the failed-attempt counter was "
            "applied (the commit completed) but its auth.login_failed "
            "security-audit row was never recorded"
        )
    finally:
        world.close()


def test_logout_cancel_at_commit_still_audits(tmp_path: Path) -> None:
    """RED at base, GREEN after the fix. The session delete commits in
    ``_logout_db``; a cancellation landing at that commit await must not lose
    the ``auth.logout`` row."""
    token = "logout-refresh-token-probe"
    world = _AuthWorld(tmp_path, "auth-logout.db")
    world.seed_users((OWNER_ID, "root"))
    world.seed_session(OWNER_ID, token)
    try:
        outcome, state = _drive(
            world,
            lambda conn: logout(
                _request_for("POST", "/api/auth/logout"),
                Response(),
                authorization=None,
                refresh_token=token,
                db=conn,
                _csrf_token="t",
            ),
        )
        assert state["fired"], "non-vacuity: the commit hook must have fired"
        assert outcome == "cancelled", f"route must unwind cancelled (got {outcome!r})"
        assert (
            world.scalar(
                "SELECT COUNT(*) FROM user_sessions WHERE refresh_token_hash = ?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            )
            == 0
        ), "precondition: the session-delete commit completed (0 rows left)"
        assert _audit_rows(world, "auth.logout") == 1, (
            "commit-boundary audit loss: the session delete was applied (the "
            "commit completed) but its auth.logout security-audit row was "
            "never recorded"
        )
    finally:
        world.close()


def test_logout_with_bearer_cancel_at_commit_still_audits(tmp_path: Path) -> None:
    """RED at base, GREEN after the fix. With a Bearer authorization header
    the handler first denies the access token — ``deny_access_token`` and
    ``purge_expired_denied_tokens`` commit SYNCHRONOUSLY on the loop thread
    (auth_service.py:215-247) — and only then the session delete commits in
    the ``_logout_db`` worker. A cancellation landing at THAT commit await —
    after the denial mutations are already durable — must not lose the
    ``auth.logout`` row."""
    token = "logout-bearer-refresh-token-probe"
    world = _AuthWorld(tmp_path, "auth-logout-bearer.db")
    world.seed_users((OWNER_ID, "root"))
    world.seed_session(OWNER_ID, token)
    access_token = create_access_token(OWNER_ID, "root", "superadmin")
    try:
        outcome, state = _drive(
            world,
            lambda conn: logout(
                _request_for("POST", "/api/auth/logout"),
                Response(),
                authorization=f"Bearer {access_token}",
                refresh_token=token,
                db=conn,
                _csrf_token="t",
            ),
            proxy_cls=_CancelAtWorkerCommitConn,
        )
        assert state["fired"], "non-vacuity: the commit hook must have fired"
        assert outcome == "cancelled", f"route must unwind cancelled (got {outcome!r})"
        assert (
            world.scalar("SELECT COUNT(*) FROM access_token_denylist") == 1
        ), "precondition: the access-token denial committed before the cancel landed"
        assert (
            world.scalar(
                "SELECT COUNT(*) FROM user_sessions WHERE refresh_token_hash = ?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            )
            == 0
        ), "precondition: the session-delete commit completed (0 rows left)"
        assert _audit_rows(world, "auth.logout") == 1, (
            "commit-boundary audit loss: the denial mutations and the session "
            "delete were both applied (their commits completed) but the "
            "auth.logout security-audit row was never recorded — the "
            "cancellation delivered exactly at the session-delete commit "
            "await unwound the handler before the audit coroutine existed"
        )
    finally:
        world.close()


def test_change_password_cancel_at_commit_still_audits(tmp_path: Path) -> None:
    """RED at base, GREEN after the fix. The password rotation commits in
    ``_change_password_db``; a cancellation landing at that commit await must
    not lose the ``auth.password_changed`` row, and the post-commit cache
    invalidation must still run (it sits between commit and audit today)."""
    world = _AuthWorld(tmp_path, "auth-password.db")
    world.seed_users((OWNER_ID, "root"))
    invalidations: list[int] = []
    saved_invalidate = auth_mod.invalidate_active_user_cache
    auth_mod.invalidate_active_user_cache = lambda uid: invalidations.append(uid)
    try:
        outcome, state = _drive(
            world,
            lambda conn: change_password(
                _request_for("POST", "/api/auth/change-password"),
                ChangePasswordRequest(
                    current_password=_GOOD_PASSWORD, new_password="NewPassword456"
                ),
                Response(),
                user=dict(_SUPERADMIN),
                db=conn,
                _csrf_token="t",
            ),
        )
        assert state["fired"], "non-vacuity: the commit hook must have fired"
        assert outcome == "cancelled", f"route must unwind cancelled (got {outcome!r})"
        assert (
            world.scalar(
                "SELECT COUNT(*) FROM user_sessions WHERE user_id = ?", (OWNER_ID,)
            )
            == 1
        ), "precondition: the password-rotation commit completed (new session row)"
        assert _audit_rows(world, "auth.password_changed") == 1, (
            "commit-boundary audit loss: the password rotation was applied "
            "(the commit completed) but its auth.password_changed "
            "security-audit row was never recorded"
        )
        assert invalidations == [OWNER_ID], (
            "the cache invalidation must run inside the drained unit, between "
            "the commit and the audit row"
        )
    finally:
        auth_mod.invalidate_active_user_cache = saved_invalidate
        world.close()


def test_revoke_session_cancel_at_commit_still_audits(tmp_path: Path) -> None:
    """RED at base, GREEN after the fix. The targeted session delete commits
    in ``_revoke_session_db``; a cancellation landing at that commit await
    must not lose the ``auth.session_revoked`` row."""
    token = "revoke-one-refresh-token-probe"
    world = _AuthWorld(tmp_path, "auth-revoke-one.db")
    world.seed_users((OWNER_ID, "root"))
    world.seed_session(OWNER_ID, token, session_id=41)
    try:
        outcome, state = _drive(
            world,
            lambda conn: revoke_session(
                41,
                _request_for("DELETE", "/api/auth/sessions/41"),
                user=dict(_SUPERADMIN),
                db=conn,
                _csrf_token="t",
            ),
        )
        assert state["fired"], "non-vacuity: the commit hook must have fired"
        assert outcome == "cancelled", f"route must unwind cancelled (got {outcome!r})"
        assert (
            world.scalar("SELECT COUNT(*) FROM user_sessions WHERE id = 41") == 0
        ), "precondition: the session-revoke commit completed"
        assert _audit_rows(world, "auth.session_revoked") == 1, (
            "commit-boundary audit loss: the session revoke was applied (the "
            "commit completed) but its auth.session_revoked security-audit "
            "row was never recorded"
        )
    finally:
        world.close()


def test_revoke_all_sessions_cancel_at_commit_still_audits(tmp_path: Path) -> None:
    """RED at base, GREEN after the fix. The bulk revoke + current-session
    rotation commits in ``_revoke_all_sessions_db``; a cancellation landing
    at that commit await must not lose the ``auth.sessions_revoked`` row."""
    current_token = "revoke-all-current-refresh-probe"
    other_token = "revoke-all-other-refresh-probe"
    world = _AuthWorld(tmp_path, "auth-revoke-all.db")
    world.seed_users((OWNER_ID, "root"))
    world.seed_session(OWNER_ID, current_token, session_id=51)
    world.seed_session(OWNER_ID, other_token, session_id=52)
    try:
        outcome, state = _drive(
            world,
            lambda conn: revoke_all_sessions(
                Response(),
                _request_for(
                    "DELETE",
                    "/api/auth/sessions",
                    cookies={"refresh_token": current_token},
                ),
                user=dict(_SUPERADMIN),
                db=conn,
                _csrf_token="t",
            ),
        )
        assert state["fired"], "non-vacuity: the commit hook must have fired"
        assert outcome == "cancelled", f"route must unwind cancelled (got {outcome!r})"
        assert (
            world.scalar("SELECT COUNT(*) FROM user_sessions WHERE id = 52") == 0
        ), "precondition: the bulk-revoke commit completed (other session gone)"
        assert _audit_rows(world, "auth.sessions_revoked") == 1, (
            "commit-boundary audit loss: the bulk session revoke was applied "
            "(the commit completed) but its auth.sessions_revoked "
            "security-audit row was never recorded"
        )
    finally:
        world.close()
