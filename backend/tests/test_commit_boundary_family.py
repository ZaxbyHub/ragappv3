"""Issue #1043 residual 3 family coverage: every converted users.py
commit->audit tail, plus a source census pinning the conversion set.

The frozen ``test_commit_boundary_audit.py`` drives only
``update_user_role``. This file closes the remaining named conversions and
catches silent reversion of ANY of them:

- ``update_user`` (PATCH /users/{id}) driven through the deterministic
  commit-completes-then-cancel schedule: the audit row AND the cache
  invalidation must both happen before the CancelledError propagates.
- the activation tail (PATCH /users/{id}/active): same schedule, same
  contract.
- a source census over users.py asserting EXACTLY the four adjacent-pair
  tails route through ``drain_commit_audit`` (create_user, update_user,
  update_user_role, activation) and that no bare ``to_thread(conn.commit)``
  remains ahead of a ``drain_audit`` in that file; the deliberately
  unconverted worker-owned commit sites are enumerated in the trace's 4.2
  sweep with explicit dispositions, not asserted here.
- delete_vault's commit goes through ``drained_commit`` (census leg).
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
    UpdateActiveRequest,
    UpdateUserRequest,
    update_user,
    update_user_active,
)
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
    def __init__(self, real_pool: SQLiteConnectionPool) -> None:
        self._real_pool = real_pool
        self._conn: sqlite3.Connection | None = None

    async def get_connection_async(self, *args, **kwargs):  # noqa: ANN002, ANN003
        if self._conn is None:
            self._conn = self._real_pool.get_connection()
        return self._conn

    def release_connection(self, conn) -> None:  # noqa: ANN001
        try:
            if self._conn.in_transaction:
                self._conn.rollback()
        except sqlite3.Error:
            pass


class _AuditEnv:
    def __init__(self, tmp_path: Path) -> None:
        _pool_cache.clear()
        self.db_path = str(tmp_path / "commit-family.db")
        init_db(self.db_path)
        self.pool = SQLiteConnectionPool(self.db_path, max_size=2)
        self.facade = _SingleConnPoolFacade(self.pool)
        self.facade._conn = self.pool.get_connection()
        self._saved_get_pool = users_mod.get_pool
        users_mod.get_pool = lambda _p: self.facade
        seed = self.pool.get_connection()
        try:
            for uid, username, role in ((1, "root", "superadmin"), (2, "target", "member")):
                seed.execute(
                    "INSERT INTO users (id, username, hashed_password, role, is_active) "
                    "VALUES (?, ?, 'abc123', ?, 1)",
                    (uid, username, role),
                )
            seed.commit()
        finally:
            self.pool.release_connection(seed)
        self.invalidations: list[int] = []
        self._saved_invalidate = users_mod.invalidate_active_user_cache
        users_mod.invalidate_active_user_cache = lambda uid: self.invalidations.append(uid)

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
        if self.facade._conn is not None:
            try:
                self.pool.release_connection(self.facade._conn)
            except Exception:
                pass
        self.pool.close_all()
        _pool_cache.clear()


def _request_for(method: str, suffix: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "headers": [],
            "path": f"/api/users/{TARGET_USER_ID}{suffix}",
            "query_string": b"",
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


def _drive_cancel_at_commit(route_call):  # noqa: ANN001
    """Deterministic schedule: the REAL commit completes in the worker, then
    the driving task is cancelled before the commit await resumes."""
    real_to_thread = asyncio.to_thread
    state = {"fired": False}
    holder: dict = {"task": None}

    async def scripted(fn, *args, **kwargs):  # noqa: ANN002, ANN003
        if not state["fired"] and getattr(fn, "__name__", "") == "commit":
            state["fired"] = True
            loop = asyncio.get_running_loop()

            def _commit_then_cancel() -> None:
                fn(*args, **kwargs)
                loop.call_soon_threadsafe(holder["task"].cancel)

            return await real_to_thread(_commit_then_cancel)
        return await real_to_thread(fn, *args, **kwargs)

    async def drive() -> str:
        asyncio.to_thread = scripted
        try:
            holder["task"] = asyncio.ensure_future(route_call())
            try:
                await holder["task"]
                return "returned"
            except asyncio.CancelledError:
                return "cancelled"
            except Exception as exc:  # noqa: BLE001
                return f"error:{type(exc).__name__}"
        finally:
            asyncio.to_thread = real_to_thread

    return asyncio.run(drive()), state


def test_update_user_cancel_at_commit_still_audits_and_invalidates(tmp_path: Path) -> None:
    env = _AuditEnv(tmp_path)
    try:
        outcome, state = _drive_cancel_at_commit(
            lambda: update_user(
                TARGET_USER_ID,
                _request_for("PATCH", ""),
                body=UpdateUserRequest(full_name="Renamed User"),
                user=dict(_SUPERADMIN),
                db=env.facade._conn,
                _csrf_token="t",
            )
        )
        assert state["fired"], "non-vacuity: the commit hook must have fired"
        assert outcome == "cancelled", f"route must unwind cancelled (got {outcome!r})"
        assert env.scalar("SELECT full_name FROM users WHERE id = ?", (TARGET_USER_ID,)) == "Renamed User", (
            "precondition: the commit completed before the cancellation landed"
        )
        assert env.invalidations == [TARGET_USER_ID], (
            "the cache invalidation ran inside the drained unit, between "
            "commit and audit"
        )
        assert env.scalar(
            "SELECT COUNT(*) FROM security_audit_log WHERE event_type = 'user.updated' AND target_user_id = ?",
            (TARGET_USER_ID,),
        ) == 1, "the user.updated audit row was written before the cancel re-raised"
    finally:
        env.close()


def test_update_user_active_cancel_at_commit_still_audits(tmp_path: Path) -> None:
    env = _AuditEnv(tmp_path)
    try:
        outcome, state = _drive_cancel_at_commit(
            lambda: update_user_active(
                TARGET_USER_ID,
                _request_for("PATCH", "/active"),
                body=UpdateActiveRequest(is_active=False),
                user=dict(_SUPERADMIN),
                _csrf_token="t",
            )
        )
        assert state["fired"], "non-vacuity: the commit hook must have fired"
        assert outcome == "cancelled", f"route must unwind cancelled (got {outcome!r})"
        assert env.scalar("SELECT is_active FROM users WHERE id = ?", (TARGET_USER_ID,)) == 0, (
            "precondition: the deactivation commit completed"
        )
        assert env.invalidations == [TARGET_USER_ID]
        assert env.scalar(
            "SELECT COUNT(*) FROM security_audit_log WHERE event_type = 'user.active_updated' AND target_user_id = ?",
            (TARGET_USER_ID,),
        ) == 1
    finally:
        env.close()


def test_users_py_tails_census() -> None:
    """EXACTLY the four converted adjacent-pair tails call drain_commit_audit
    in users.py, and no bare to_thread(conn.commit) remains there."""
    source = Path(users_mod.__file__).read_text(encoding="utf-8")
    assert source.count("drain_commit_audit(") == 4, (
        "users.py must route its four commit->audit tails through "
        "drain_commit_audit (exactly 4 call sites; the import line carries "
        "no call paren); a silent reversion of any tail changes this count"
    )
    assert 'await asyncio.to_thread(conn.commit)' not in source and "await asyncio.to_thread(db.commit)" not in source, (
        "no bare commit await may remain in users.py (every one is an "
        "un-drained commit boundary)"
    )
    vaults_path = Path(users_mod.__file__).parent / "vaults.py"
    vaults_source = vaults_path.read_text(encoding="utf-8")
    assert "drained_commit(conn)" in vaults_source, (
        "delete_vault's commit must go through drained_commit"
    )
