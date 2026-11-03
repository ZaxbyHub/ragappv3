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


def test_create_user_cancel_at_commit_still_audits(tmp_path: Path) -> None:
    """Review PRR-002: the create_user tail gets the same deterministic
    commit-completes-then-cancel schedule as its siblings — the user row is
    durable (the commit completed) and the user.created audit row exists
    even though the route unwinds cancelled."""
    import asyncio
    from datetime import datetime, timedelta, timezone
    from unittest.mock import MagicMock

    import jwt
    from starlette.requests import Request

    from app.api.routes.users import CreateUserRequest, create_user

    class _PoolFacade:
        def __init__(self, real_pool):
            self._real_pool = real_pool
            self._conn = None

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

    _pool_cache.clear()
    db_path = str(tmp_path / "create-user-boundary.db")
    init_db(db_path)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    facade = _PoolFacade(pool)
    saved_get_pool = users_mod.get_pool
    users_mod.get_pool = lambda _p: facade
    seed = pool.get_connection()
    try:
        seed.execute(
            "INSERT INTO users (id, username, hashed_password, role, is_active) "
            "VALUES (1, 'root', 'abc123', 'superadmin', 1)"
        )
        seed.commit()
    finally:
        pool.release_connection(seed)

    def _request() -> Request:
        return Request(
            {
                "type": "http",
                "method": "POST",
                "headers": [],
                "path": "/api/users",
                "query_string": b"",
                "client": ("127.0.0.1", 12345),
                "server": ("testserver", 80),
                "scheme": "http",
            }
        )

    secret, algorithm = "test-secret-key", "HS256"
    payload = {
        "sub": "1",
        "username": "root",
        "role": "superadmin",
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        "type": "access",
        "jti": "test-jti-create-user",
    }
    token = jwt.encode(payload, secret, algorithm=algorithm)

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
            holder["task"] = asyncio.ensure_future(
                create_user(
                    _request(),
                    CreateUserRequest(
                        username="newuser", password="Password123x", role="member"
                    ),
                    user={
                        "id": 1,
                        "username": "root",
                        "role": "superadmin",
                    },
                    _csrf_token="t",
                )
            )
            try:
                await holder["task"]
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
        users_mod.get_pool = saved_get_pool
        if facade._conn is not None:
            try:
                pool.release_connection(facade._conn)
            except Exception:
                pass
        pool.close_all()
        _pool_cache.clear()

    assert state["fired"], "non-vacuity: the commit hook must have fired"
    assert outcome == "cancelled", f"route must unwind cancelled (got {outcome!r})"
    check = sqlite3.connect(db_path)
    try:
        created = check.execute(
            "SELECT COUNT(*) FROM users WHERE username = 'newuser'"
        ).fetchone()[0]
        audited = check.execute(
            "SELECT COUNT(*) FROM security_audit_log WHERE event_type = 'user.created'"
        ).fetchone()[0]
    finally:
        check.close()
    assert created == 1, "precondition: the create commit completed"
    assert audited == 1, (
        "the user.created security-audit row was written before the cancel "
        "re-raised"
    )


def test_users_py_tails_census() -> None:
    """Review PRR-001: the census BINDS each converted tail to its NAMED
    route function — an AST walk that requires exactly one
    ``drain_commit_audit(...)`` call lexically inside each of
    create_user / update_user / update_user_role / update_user_active and
    NO call inside any other function in users.py. A count-preserving
    reversion (one tail reverted while an extra call appears elsewhere) or
    an omitted conversion can no longer hide behind the total. Also: no
    bare to_thread(conn.commit) remains in users.py, and delete_vault's
    commit goes through drained_commit."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(users_mod.__file__).read_text(encoding="utf-8"))
    expected = {
        "create_user",
        "update_user",
        "update_user_role",
        "update_user_active",
    }
    found: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            calls = sum(
                1
                for sub in ast.walk(node)
                if isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Name)
                and sub.func.id == "drain_commit_audit"
            )
            if calls:
                found[node.name] = calls
    assert set(found) == expected and all(v == 1 for v in found.values()), (
        f"users.py drain_commit_audit call-site binding drifted: expected "
        f"exactly one call inside each of {sorted(expected)}, found {found} "
        "— a tail was reverted, renamed, or an unbound call appeared"
    )
    source = Path(users_mod.__file__).read_text(encoding="utf-8")
    assert 'await asyncio.to_thread(conn.commit)' not in source and "await asyncio.to_thread(db.commit)" not in source, (
        "no bare commit await may remain in users.py (every one is an "
        "un-drained commit boundary)"
    )
    vaults_path = Path(users_mod.__file__).parent / "vaults.py"
    vaults_source = vaults_path.read_text(encoding="utf-8")
    assert "drained_commit(conn)" in vaults_source, (
        "delete_vault's commit must go through drained_commit"
    )


def test_org_worker_privilege_re_reads_are_inside_transaction_context() -> None:
    """Implementation-review R2 + review PRR-005: pin the ACTUAL
    transactionality AND the deciding-gate shape of the org routes'
    privilege re-reads — an AST census that, in each write worker, the FIRST
    statement of the ``with transaction_context(conn):`` body is an ``if
    not _is_org_admin_or_owner(conn, ...)`` refusal whose call node sits
    lexically inside that with-block. Catches BOTH partial-fix shapes the
    behavioral frozen check cannot distinguish: (a) the re-read present but
    OUTSIDE the transaction (a narrow race between that read and BEGIN
    IMMEDIATE survives), and (b) a decoy — a deciding check outside plus a
    result-ignored call inside the block. Covers all SEVEN org write
    workers (the six #1043 conversions plus update_organization's
    `_update_organization`, the review-PRR-003 sibling)."""
    import ast
    from pathlib import Path

    from app.api.routes import organizations as org_mod

    tree = ast.parse(Path(org_mod.__file__).read_text(encoding="utf-8"))
    route_workers = {
        "_add_member", "_create_invite", "_resend_invite",
        "_revoke_invite", "_set_override", "_clear_override",
        "_update_organization",
    }
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in route_workers:
            verdict = "no-transaction_context-with"
            for w in ast.walk(node):
                if not isinstance(w, ast.With):
                    continue
                opens_txn = any(
                    isinstance(item.context_expr, ast.Call)
                    and isinstance(item.context_expr.func, ast.Name)
                    and item.context_expr.func.id == "transaction_context"
                    for item in w.items
                )
                if not opens_txn:
                    continue
                body = w.body
                if not body or not isinstance(body[0], ast.If):
                    verdict = "txn-with-first-statement-not-a-guard-if"
                    continue
                test = body[0].test
                deciding = any(
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == "_is_org_admin_or_owner"
                    for call in ast.walk(test)
                )
                if not deciding:
                    verdict = "guard-if-does-not-call-_is_org_admin_or_owner"
                    continue
                verdict = "ok"
                break
            found[node.name] = verdict
        elif isinstance(node, ast.AsyncFunctionDef) and node.name in route_workers:
            found[node.name] = "worker-not-a-sync-function"
    assert set(found) == route_workers, (
        f"expected all seven org write workers, found {sorted(found)} — a "
        "worker was renamed, removed, or added without updating the census; "
        "update the census"
    )
    assert all(v == "ok" for v in found.values()), (
        "org workers whose privilege re-read is not the DECIDING first "
        f"statement inside transaction_context: {found}"
    )
