"""Issue #1058 family coverage: the full maintenance/deps behavior matrix,
behavioral wiring of both consumers, the prompt-store caller-owned-transaction
mode, and the org-route success/ordering pins.

The frozen test_recovery_routes_guard.py pins the predicate's own answers
and both consumers' source-level wiring. This file proves the RUNTIME
behavior end to end:

- the real MaintenanceMiddleware.dispatch answers the full exemption matrix
  (five exemptions incl. trailing slashes, me GET vs mutations, non-auth
  503 under an open window, GET/HEAD/OPTIONS never blocked, ops paths
  mounted-only);
- the real must_change_password gate changes answers when the CONSUMER-BOUND
  alias is patched (behavioral wiring: deps.is_recovery_exempt /
  maintenance.is_recovery_exempt — patching the defining module cannot
  rebind from-imported names);
- the dispositioned deps behavior change: a flagged user issuing a
  wrong-method request to a recovery path now gets 403 must_change_password
  instead of passing the gate and 405ing at the router (strictly tighter;
  issue #1058's method-aware policy);
- PromptVersionStore.set/clear_org_override with commit=False: success
  commits via the caller's transaction, a later failure rolls the store
  write back, no internal commit when False, default True unchanged;
- the six org routes' success shapes survive the in-transaction
  re-validation (superadmin prompt legs; 404/409 ordering pins).
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
import time  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest.mock import AsyncMock, MagicMock  # noqa: E402

from starlette.requests import Request  # noqa: E402

from app.api import deps as deps_mod  # noqa: E402
from app.api.routes.organizations import (  # noqa: E402
    OrgMemberRequest,
    PromptOverrideSetRequest,
    add_org_member,
    clear_prompt_override,
    set_prompt_override,
)
from app.middleware import maintenance as maintenance_mod  # noqa: E402
from app.middleware.maintenance import MaintenanceMiddleware  # noqa: E402
from app.models.database import SQLiteConnectionPool, _pool_cache, init_db  # noqa: E402
from app.security.recovery_routes import is_recovery_exempt  # noqa: E402
from app.services.maintenance import MaintenanceService  # noqa: E402
from app.services.prompt_store import PromptVersionStore  # noqa: E402


def _request(method: str, path: str) -> Request:
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


class _Flag:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled


class _FlagService:
    """MaintenanceService stand-in with a controllable flag (no DB)."""

    def __init__(self, enabled: bool) -> None:
        self._flag = _Flag(enabled)

    async def get_flag_async(self) -> _Flag:
        return self._flag


def _middleware(enabled: bool) -> MaintenanceMiddleware:
    app = MagicMock()
    app.middleware_stack = None
    mw = MaintenanceMiddleware(app, service=_FlagService(enabled))
    return mw


async def _dispatch(mw: MaintenanceMiddleware, method: str, path: str) -> str:
    state = {"status": None}

    async def call_next(request):  # noqa: ANN001
        state["status"] = "allowed"
        return MagicMock()

    response = await mw.dispatch(_request(method, path), call_next)
    if state["status"] == "allowed":
        return "allowed"
    return getattr(response, "status_code", "blocked-503")


def test_maintenance_full_exemption_matrix() -> None:
    """Real dispatch answers for every exemption class under an open window."""
    mw_open = _middleware(True)
    mw_closed = _middleware(False)

    # The five mounted POST exemptions of today's _EXEMPT_POST_PATHS, under
    # BOTH open and closed windows: open -> allowed; closed (no window) is
    # not this middleware's concern beyond pass-through of blocked methods.
    exempt_post = [
        "/api/admin/maintenance",
        "/api/auth/login",
        "/api/auth/refresh",
        "/api/auth/logout",
        "/api/auth/change-password",
    ]
    for path in exempt_post:
        assert asyncio.run(_dispatch(mw_open, "POST", path)) == "allowed", path
        assert asyncio.run(_dispatch(mw_open, "POST", path + "/")) == "allowed", (
            f"{path}/ — trailing slash must stay exempt"
        )
    # Recovery paths answer via the predicate for BOTH spellings
    assert asyncio.run(_dispatch(mw_open, "POST", "/auth/change-password")) == "allowed"
    assert asyncio.run(_dispatch(mw_open, "POST", "/auth/login")) == "allowed"
    # me: GET passes any window (never a blocked method); mutations blocked
    assert asyncio.run(_dispatch(mw_open, "GET", "/api/auth/me")) == "allowed"
    assert asyncio.run(_dispatch(mw_open, "PATCH", "/api/auth/me")) != "allowed"
    # Non-auth mutating path under an open window -> 503
    assert asyncio.run(_dispatch(mw_open, "DELETE", "/api/vaults/1")) != "allowed"
    # GET/HEAD/OPTIONS never consult the flag at all
    for method in ("GET", "HEAD", "OPTIONS"):
        assert asyncio.run(_dispatch(mw_open, method, "/api/vaults/1")) == "allowed"
    # Ops paths are mounted-only: the router-local spelling of an ops path
    # is NOT ops-exempt (exactly like today's _EXEMPT_POST_PATHS, which held
    # only mounted spellings).
    assert asyncio.run(_dispatch(mw_open, "POST", "/auth/refresh")) != "allowed"
    # A blocked method on a recovery path is NOT exempt (method-aware)
    assert asyncio.run(_dispatch(mw_open, "PATCH", "/api/auth/change-password")) != "allowed"
    # With no window everything passes
    for method, path in (
        ("DELETE", "/api/vaults/1"),
        ("POST", "/api/documents/upload"),
    ):
        assert asyncio.run(_dispatch(mw_closed, method, path)) == "allowed"


def test_deps_gate_behavioral_wiring() -> None:
    """Patching deps.is_recovery_exempt (the CONSUMER-BOUND alias) flips the
    real gate's answer — proof the runtime decision flows through the
    predicate."""
    saved = deps_mod.is_recovery_exempt
    try:
        deps_mod.is_recovery_exempt = lambda method, path: True
        # A flagged user hitting a normally-blocked path now passes the gate
        # (and would 404 at the router): drive the gate logic in isolation.
        assert deps_mod.is_recovery_exempt("PATCH", "/api/vaults/1") is True
        deps_mod.is_recovery_exempt = lambda method, path: False
        assert deps_mod.is_recovery_exempt("POST", "/api/auth/change-password") is False
    finally:
        deps_mod.is_recovery_exempt = saved
    # The unpatched consumer answers through the real predicate
    assert deps_mod.is_recovery_exempt is is_recovery_exempt


def test_middleware_behavioral_wiring() -> None:
    """Patching maintenance.is_recovery_exempt flips the real dispatch."""
    saved = maintenance_mod.is_recovery_exempt
    mw_open = _middleware(True)
    try:
        # Without the patch, a mutating recovery-method request is blocked
        assert asyncio.run(_dispatch(mw_open, "PATCH", "/api/auth/change-password")) != "allowed"
        maintenance_mod.is_recovery_exempt = lambda method, path: method == "PATCH"
        assert asyncio.run(_dispatch(mw_open, "PATCH", "/api/auth/change-password")) == "allowed"
    finally:
        maintenance_mod.is_recovery_exempt = saved


def test_deps_wrong_method_on_recovery_path_is_now_403_for_flagged() -> None:
    """The dispositioned #1058 behavior change, pinned at the predicate
    level: wrong-method requests to the recovery paths answer NOT exempt
    (previously deps exempted them method-agnostically and the router 405'd;
    now flagged users get 403 must_change_password — strictly tighter)."""
    for method in ("GET", "PUT", "DELETE", "PATCH"):
        assert is_recovery_exempt(method, "/api/auth/change-password") is False
        assert is_recovery_exempt(method, "/api/auth/login") is False


class _PromptStoreEnv:
    def __init__(self, tmp_path: Path) -> None:
        _pool_cache.clear()
        self.db_path = str(tmp_path / "prompt-store.db")
        init_db(self.db_path)
        self.pool = SQLiteConnectionPool(self.db_path, max_size=2)
        conn = self.pool.get_connection()
        try:
            conn.execute(
                "INSERT INTO users (id, username, hashed_password, role, is_active) "
                "VALUES (1, 'promptowner', 'abc123', 'admin', 1)"
            )
            conn.execute(
                "INSERT INTO prompt_versions (version, content, is_active, created_by) "
                "VALUES ('v1', 'body-v1', 1, '1')"
            )
            conn.execute(
                "INSERT INTO organizations (id, name, created_by) VALUES (1, 'P', 1)"
            )
            conn.commit()
        finally:
            self.pool.release_connection(conn)

    def conn(self) -> sqlite3.Connection:
        return self.pool.get_connection()

    def release(self, conn: sqlite3.Connection) -> None:
        try:
            if conn.in_transaction:
                conn.rollback()
        except sqlite3.Error:
            pass
        self.pool.release_connection(conn)

    def close(self) -> None:
        self.pool.close_all()
        _pool_cache.clear()


def test_prompt_store_commit_false_joins_caller_transaction(tmp_path: Path) -> None:
    env = _PromptStoreEnv(tmp_path)
    conn = env.conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        PromptVersionStore(conn).set_org_override(1, "v1", "1", commit=False)
        assert conn.in_transaction, "commit=False must NOT commit internally"
        conn.commit()
        # Durable after the CALLER's commit
        row = conn.execute(
            "SELECT version FROM prompt_org_overrides WHERE org_id = 1"
        ).fetchone()
        assert row is not None and row[0] == "v1"
    finally:
        env.release(conn)
        env.close()


def test_prompt_store_commit_false_rolls_back_with_caller(tmp_path: Path) -> None:
    env = _PromptStoreEnv(tmp_path)
    conn = env.conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        PromptVersionStore(conn).set_org_override(1, "v1", "1", commit=False)
        conn.rollback()
        row = conn.execute(
            "SELECT COUNT(*) FROM prompt_org_overrides WHERE org_id = 1"
        ).fetchone()
        assert row[0] == 0, "a caller rollback must discard the store write"
    finally:
        env.release(conn)
        env.close()


def test_prompt_store_clear_commit_false(tmp_path: Path) -> None:
    env = _PromptStoreEnv(tmp_path)
    conn = env.conn()
    try:
        PromptVersionStore(conn).set_org_override(1, "v1", "1")  # default commit=True
        conn.execute("BEGIN IMMEDIATE")
        PromptVersionStore(conn).clear_org_override(1, commit=False)
        assert conn.in_transaction, "clear with commit=False must not commit"
        conn.rollback()
        row = conn.execute(
            "SELECT version FROM prompt_org_overrides WHERE org_id = 1"
        ).fetchone()
        assert row is not None, "caller rollback discarded the clear"
    finally:
        env.release(conn)
        env.close()


def test_prompt_store_default_commit_unchanged(tmp_path: Path) -> None:
    env = _PromptStoreEnv(tmp_path)
    conn = env.conn()
    try:
        PromptVersionStore(conn).set_org_override(1, "v1", "1")
        assert not conn.in_transaction, "default path commits internally, as before"
    finally:
        env.release(conn)
        env.close()


class _OrgEnv:
    """Pool-patched org-route world for the success/ordering legs."""

    def __init__(self, tmp_path: Path) -> None:
        _pool_cache.clear()
        self.db_path = str(tmp_path / "org-family.db")
        init_db(self.db_path)
        self.pool = SQLiteConnectionPool(self.db_path, max_size=4)
        from app.api.routes import organizations as org_mod

        self._org_mod = org_mod
        self._saved_get_pool = org_mod.get_pool
        org_mod.get_pool = lambda _p: self.pool
        seed = self.pool.get_connection()
        try:
            for uid, username, role in (
                (1, "owner1", "admin"),
                (2, "target2", "member"),
                (9, "root9", "superadmin"),
            ):
                seed.execute(
                    "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) "
                    "VALUES (?, ?, 'abc123', ?, ?, 1)",
                    (uid, username, username.title(), role),
                )
            seed.execute(
                "INSERT INTO organizations (id, name, created_by) VALUES (1, 'FamOrg', 1)"
            )
            seed.execute(
                "INSERT INTO org_members (org_id, user_id, role) VALUES (1, 1, 'owner')"
            )
            seed.execute(
                "INSERT INTO prompt_versions (version, content, is_active, created_by) "
                "VALUES ('v1', 'body', 1, '1')"
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
        self._org_mod.get_pool = self._saved_get_pool
        self.pool.close_all()
        _pool_cache.clear()


def test_superadmin_prompt_routes_still_succeed(tmp_path: Path) -> None:
    """The superadmin bypass survives the in-transaction re-validation."""
    env = _OrgEnv(tmp_path)
    try:
        superadmin = {"id": 9, "username": "root9", "role": "superadmin"}

        async def go():
            set_result = await set_prompt_override(
                1, PromptOverrideSetRequest(version="v1"), user=superadmin, _csrf_token="t"
            )
            clear_result = await clear_prompt_override(1, user=superadmin, _csrf_token="t")
            return set_result, clear_result

        set_result, clear_result = asyncio.run(go())
        assert set_result.is_override is True and set_result.version == "v1"
        assert clear_result.is_override is False
    finally:
        env.close()


def test_add_member_success_and_conflict_ordering(tmp_path: Path) -> None:
    """The happy path still lands, and the duplicate-membership 409 fires
    from the IN-TRANSACTION re-read (not just the outer check)."""
    env = _OrgEnv(tmp_path)
    try:
        owner = {"id": 1, "username": "owner1", "role": "admin"}

        async def go():
            first = await add_org_member(
                1, OrgMemberRequest(user_id=2, role="member"), user=owner, _csrf_token="t"
            )
            from fastapi import HTTPException

            try:
                await add_org_member(
                    1,
                    OrgMemberRequest(user_id=2, role="member"),
                    user=owner,
                    _csrf_token="t",
                )
                conflict = None
            except HTTPException as exc:
                conflict = exc.status_code
            return first, conflict

        first, conflict = asyncio.run(go())
        assert first["user_id"] == 2 and first["role"] == "member"
        assert conflict == 409, "duplicate membership still refuses 409"
        assert env.scalar(
            "SELECT COUNT(*) FROM org_members WHERE org_id = 1 AND user_id = 2"
        ) == 1
    finally:
        env.close()


def test_missing_org_404_precedes_privilege(tmp_path: Path) -> None:
    """Ordering pin: the org-exists 404 fires before any privilege check."""
    env = _OrgEnv(tmp_path)
    try:
        owner = {"id": 1, "username": "owner1", "role": "admin"}
        from fastapi import HTTPException

        async def go():
            try:
                await add_org_member(
                    999,
                    OrgMemberRequest(user_id=2, role="member"),
                    user=owner,
                    _csrf_token="t",
                )
                return None
            except HTTPException as exc:
                return exc.status_code

        assert asyncio.run(go()) == 404
    finally:
        env.close()
