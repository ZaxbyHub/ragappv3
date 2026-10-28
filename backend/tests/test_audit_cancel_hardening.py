"""Issue #1036 C1: a cancellation at a post-commit audit await keeps the row.

Every mutating route in users.py / auth.py / draft_room.py / vaults.py ends
its committed path with a plain ``await safe_record_security_event(...)``
(backend/app/services/security_audit.py:127 — a single un-shielded
``await asyncio.to_thread(record_security_event, ...)``). A task
cancellation delivered in the window between the mutation commit's await
completing and the audit submission unwinds the handler there, and the
tamper-evident ``security_audit_log`` row for an ALREADY-COMMITTED mutation
is never written.

This check drives the REAL handler coroutines (direct async calls over a
seeded SQLite db behind a pool facade), one representative mutation site per
route file, with a scripted audit wrapper that cancels the ROUTE task by
reference (captured before dispatch) and then yields (``await
asyncio.sleep(0)``) before calling the real audit — modelling the client
disconnect landing at the resume instant between commit-completion and
audit-start. Cancelling the route task by reference keeps the script aimed
at the route under ANY fixed design: at base the wrapper runs inline on the
route task, so the pending cancellation lands at its opening yield and the
real audit is never submitted; once the audit is wrapped in a drained child
task the wrapper runs on the child, the route's cancellation is absorbed by
the drain while the audit completes, and the route then ends cancelled with
the row written. Contract asserted at BOTH ends of the fix (RED at base,
GREEN after the fix): the hook fires exactly once, the route task ends
cancelled, and the mutation's audit row EXISTS in ``security_audit_log``
afterwards.

The un-cancelled control leg (users.py site, no scripted cancellation) pins
the audit machinery itself: the row lands exactly once there, so row
absence in the interleaved legs is the cancellation, not a seeding artifact.
That leg passes at the pre-fix base and must keep passing after the fix.
"""

import asyncio
import hashlib
import sqlite3
from pathlib import Path
from typing import Any, Callable

import pytest
from starlette.requests import Request
from starlette.responses import Response

from app.api.routes import auth as auth_mod
from app.api.routes import draft_room as draft_room_mod
from app.api.routes import users as users_mod
from app.api.routes import vaults as vaults_mod
from app.api.routes.auth import logout
from app.api.routes.draft_room import delete_draft
from app.api.routes.users import UpdateRoleRequest, update_user_role
from app.api.routes.vaults import VaultUpdateRequest, update_vault
from app.config import settings
from app.models.database import SQLiteConnectionPool, _pool_cache, init_db
from app.services.draft_input_storage import DraftInputStorage
from app.services.draft_store import DraftStore

_SUPERADMIN = {
    "id": 1,
    "username": "admin",
    "full_name": "Admin",
    "role": "superadmin",
    "is_active": True,
    "must_change_password": False,
}
_VICTIM = {
    "id": 2,
    "username": "victim",
    "full_name": "Victim",
    "role": "member",
    "is_active": True,
    "must_change_password": False,
}

_LOGOUT_REFRESH_TOKEN = "probe-refresh-token-for-the-logout-audit-site"
_LOGOUT_REFRESH_HASH = hashlib.sha256(_LOGOUT_REFRESH_TOKEN.encode()).hexdigest()


class _SingleConnPool:
    """Pool facade handing the driven route one REAL pool connection."""

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
        self._real_pool.release_connection(self._conn)


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


def _drive(coro_factory: Callable[[], Any], module: Any = None) -> tuple[str, int]:
    """Run the handler coroutine as the ROUTE task; a cancelled end is
    expected when ``module`` is given: its ``safe_record_security_event`` is
    scripted (inside the loop, so it can cancel the route task by
    reference — never ``current_task()``, which would aim at whatever child
    task a fixed design runs the audit on) to cancel the route at its
    opening yield before the real audit is submitted. Returns the route
    outcome and how many times the scripted hook fired."""

    async def drive() -> tuple[str, int]:
        fired = {"count": 0}
        real_audit = None
        route_task: asyncio.Future | None = None

        async def scripted_audit(*args, **kwargs):  # noqa: ANN002, ANN003
            fired["count"] += 1
            route_task.cancel()
            await asyncio.sleep(0)  # yield: the pending cancellation lands here
            return await real_audit(*args, **kwargs)

        route_task = asyncio.ensure_future(coro_factory())
        if module is not None:
            real_audit = module.safe_record_security_event
            module.safe_record_security_event = scripted_audit
        try:
            try:
                await route_task
                outcome = "returned"
            except asyncio.CancelledError:
                outcome = "cancelled"
        finally:
            if module is not None:
                module.safe_record_security_event = real_audit
        return outcome, fired["count"]

    return asyncio.run(drive())


def _scalar(db_path: str, sql: str, params: tuple = ()) -> Any:
    check = sqlite3.connect(db_path)
    try:
        return check.execute(sql, params).fetchone()[0]
    finally:
        check.close()


class _World:
    """Per-site world: fresh db + real pool. The pool binding is patched only
    for modules whose routes resolve their connection through the module-level
    ``get_pool`` (users.py); auth/draft_room/vaults routes take the connection
    via ``Depends(get_db)`` and are handed ``world.conn`` directly."""

    def __init__(self, tmp_path: Path, module: Any, db_name: str) -> None:
        _pool_cache.clear()
        self.db_path = str(tmp_path / db_name)
        init_db(self.db_path)
        self.pool = SQLiteConnectionPool(self.db_path, max_size=2)
        self.module = module
        self._saved_get_pool = None
        self.conn = None
        self.facade = None

    def bind(self) -> None:
        self.conn = self.pool.get_connection()
        self.facade = _SingleConnPool(self.pool, self.conn)
        if self.module is not None and hasattr(self.module, "get_pool"):
            self._saved_get_pool = self.module.get_pool
            self.module.get_pool = lambda _path: self.facade

    def close(self) -> None:
        if self._saved_get_pool is not None:
            self.module.get_pool = self._saved_get_pool
        if self.conn is not None:
            try:
                self.pool.release_connection(self.conn)
            except Exception:
                pass
        self.pool.close_all()
        _pool_cache.clear()


# ── site 1: users.py — update_user_role (role commit, then audit) ───────────


def _site_users_update_user_role(tmp_path: Path) -> dict:
    world = _World(tmp_path, users_mod, "audit-users.db")
    seed = world.pool.get_connection()
    try:
        seed.execute(
            "INSERT INTO users (id, username, hashed_password, role, is_active) "
            "VALUES (1, 'admin', 'abc123', 'superadmin', 1)"
        )
        seed.execute(
            "INSERT INTO users (id, username, hashed_password, role, is_active) "
            "VALUES (2, 'victim', 'abc123', 'member', 1)"
        )
        seed.commit()
    finally:
        world.pool.release_connection(seed)

    world.bind()
    try:
        outcome, hook_fires = _drive(
            lambda: update_user_role(
                2,
                _request_for("PATCH", "/api/users/2/role"),
                UpdateRoleRequest(role="admin"),
                user=_SUPERADMIN,
                _csrf_token="t",
            ),
            module=users_mod,
        )
    finally:
        world.close()

    return {
        "site": "users.update_user_role",
        "outcome": outcome,
        "hook_fires": hook_fires,
        "mutation_applied": (
            _scalar(world.db_path, "SELECT role FROM users WHERE id = 2") == "admin"
        ),
        "mutation_fact": "users.role(id=2) == 'admin'",
        "audit_event": "user.role_updated",
        "audit_rows": _scalar(
            world.db_path,
            "SELECT COUNT(*) FROM security_audit_log WHERE event_type = ?",
            ("user.role_updated",),
        ),
    }


# ── site 2: auth.py — logout (session delete commit, then audit) ────────────


def _site_auth_logout(tmp_path: Path) -> dict:
    world = _World(tmp_path, auth_mod, "audit-auth.db")
    seed = world.pool.get_connection()
    try:
        seed.execute(
            "INSERT INTO users (id, username, hashed_password, role, is_active) "
            "VALUES (1, 'admin', 'abc123', 'superadmin', 1)"
        )
        seed.execute(
            "INSERT INTO user_sessions "
            "(user_id, refresh_token_hash, expires_at, ip_address, user_agent) "
            "VALUES (1, ?, '2030-01-01T00:00:00+00:00', NULL, NULL)",
            (_LOGOUT_REFRESH_HASH,),
        )
        seed.commit()
    finally:
        world.pool.release_connection(seed)

    world.bind()
    try:
        outcome, hook_fires = _drive(
            lambda: logout(
                _request_for("POST", "/api/auth/logout"),
                Response(),
                authorization=None,
                refresh_token=_LOGOUT_REFRESH_TOKEN,
                db=world.conn,
                _csrf_token="t",
            ),
            module=auth_mod,
        )
    finally:
        world.close()

    return {
        "site": "auth.logout",
        "outcome": outcome,
        "hook_fires": hook_fires,
        "mutation_applied": (
            _scalar(
                world.db_path,
                "SELECT COUNT(*) FROM user_sessions "
                "WHERE refresh_token_hash = ?",
                (_LOGOUT_REFRESH_HASH,),
            )
            == 0
        ),
        "mutation_fact": "the logout session DELETE committed (0 rows left)",
        "audit_event": "auth.logout",
        "audit_rows": _scalar(
            world.db_path,
            "SELECT COUNT(*) FROM security_audit_log WHERE event_type = ?",
            ("auth.logout",),
        ),
    }


# ── site 3: draft_room.py — delete_draft (draft purge commit, then audit) ───


def _site_draft_room_delete_draft(tmp_path: Path) -> dict:
    saved_data_dir = settings.data_dir
    settings.data_dir = tmp_path
    world = _World(tmp_path, draft_room_mod, "audit-draft-room.db")
    storage = DraftInputStorage(tmp_path / "draft-room")
    try:
        seed = world.pool.get_connection()
        try:
            seed.execute(
                "INSERT INTO users (id, username, hashed_password, role, is_active) "
                "VALUES (2, 'victim', 'abc123', 'member', 1)"
            )
            seed.execute(
                "INSERT INTO vaults (id, name) VALUES (1, 'AuditSiteVault')"
            )
            seed.commit()
            draft = DraftStore(seed).create_draft(
                vault_id=1,
                created_by=2,
                title="audit site draft",
                mode="compose",
                tier="standard",
                brief_json="{}",
            )
            seed.commit()
            draft_id = draft.id
        finally:
            world.pool.release_connection(seed)

        world.bind()
        outcome, hook_fires = _drive(
            lambda: delete_draft(
                draft_id,
                _request_for("DELETE", f"/api/drafts/{draft_id}"),
                db=world.conn,
                user=_VICTIM,
                storage=storage,
                _csrf_token="t",
            ),
            module=draft_room_mod,
        )
    finally:
        world.close()
        settings.data_dir = saved_data_dir

    return {
        "site": "draft_room.delete_draft",
        "outcome": outcome,
        "hook_fires": hook_fires,
        "mutation_applied": (
            _scalar(
                world.db_path, "SELECT COUNT(*) FROM drafts WHERE id = ?", (draft_id,)
            )
            == 0
        ),
        "mutation_fact": "the draft DELETE committed (draft row gone)",
        "audit_event": "draft_deleted",
        "audit_rows": _scalar(
            world.db_path,
            "SELECT COUNT(*) FROM security_audit_log WHERE event_type = ?",
            ("draft_deleted",),
        ),
    }


# ── site 4: vaults.py — update_vault visibility (commit, then audit) ────────


def _site_vaults_update_vault(tmp_path: Path) -> dict:
    world = _World(tmp_path, vaults_mod, "audit-vaults.db")
    seed = world.pool.get_connection()
    try:
        seed.execute(
            "INSERT INTO users (id, username, hashed_password, role, is_active) "
            "VALUES (1, 'admin', 'abc123', 'superadmin', 1)"
        )
        seed.execute(
            "INSERT INTO vaults (id, name, description) "
            "VALUES (1, 'AuditVault', 'before')"
        )
        seed.commit()
    finally:
        world.pool.release_connection(seed)

    world.bind()
    try:
        outcome, hook_fires = _drive(
            lambda: update_vault(
                1,
                VaultUpdateRequest(visibility="public"),
                _request_for("PUT", "/api/vaults/1"),
                conn=world.conn,
                user=_SUPERADMIN,
                _csrf_token="t",
            ),
            module=vaults_mod,
        )
    finally:
        world.close()

    return {
        "site": "vaults.update_vault",
        "outcome": outcome,
        "hook_fires": hook_fires,
        "mutation_applied": (
            _scalar(world.db_path, "SELECT visibility FROM vaults WHERE id = 1")
            == "public"
        ),
        "mutation_fact": "vaults.visibility(id=1) == 'public'",
        "audit_event": "vault.visibility_updated",
        "audit_rows": _scalar(
            world.db_path,
            "SELECT COUNT(*) FROM security_audit_log WHERE event_type = ?",
            ("vault.visibility_updated",),
        ),
    }


@pytest.mark.parametrize(
    "facts_factory",
    [
        pytest.param(_site_users_update_user_role, id="users.update_user_role"),
        pytest.param(_site_auth_logout, id="auth.logout"),
        pytest.param(_site_draft_room_delete_draft, id="draft_room.delete_draft"),
        pytest.param(_site_vaults_update_vault, id="vaults.update_vault"),
    ],
)
def test_cancel_at_audit_await_still_records_the_event(
    tmp_path: Path, facts_factory: Callable[[Path], dict]
) -> None:
    """RED at base, GREEN after the fix.

    A cancellation delivered at the post-commit ``safe_record_security_event``
    await may end the handler task (that is expected), but it must never lose
    the audit row for the already-committed mutation.
    """
    facts = facts_factory(tmp_path)

    assert facts["mutation_applied"], (
        f"[{facts['site']}] non-vacuity precondition failed: the mutation must "
        f"have committed before the audit window ({facts['mutation_fact']}), "
        f"otherwise this leg proved nothing (handler outcome: {facts['outcome']})"
    )
    assert facts["hook_fires"] == 1, (
        f"[{facts['site']}] the scripted cancellation must fire exactly once "
        f"at the post-commit audit await (fired {facts['hook_fires']} times) "
        "— otherwise this leg proved nothing"
    )
    assert facts["outcome"] == "cancelled", (
        f"[{facts['site']}] the route task must end cancelled (the delivered "
        f"cancellation propagates); got {facts['outcome']!r}"
    )
    assert facts["audit_rows"] == 1, (
        f"[{facts['site']}] audit row missing for applied mutation: a "
        f"cancellation at the post-commit safe_record_security_event await "
        f"skipped the '{facts['audit_event']}' security_audit_log row for the "
        f"already-committed mutation ({facts['mutation_fact']}); the handler "
        f"task ended '{facts['outcome']}' with {facts['audit_rows']} "
        f"'{facts['audit_event']}' rows recorded"
    )


def test_control_leg_no_cancellation_records_audit_exactly_once(
    tmp_path: Path,
) -> None:
    """Control leg (passes at base and must keep passing): without the
    scripted cancellation the same users.py site records exactly one audit
    row — proving row absence in the interleaved legs is the cancellation,
    not a seeding artifact."""
    world = _World(tmp_path, users_mod, "audit-control.db")
    seed = world.pool.get_connection()
    try:
        seed.execute(
            "INSERT INTO users (id, username, hashed_password, role, is_active) "
            "VALUES (1, 'admin', 'abc123', 'superadmin', 1)"
        )
        seed.execute(
            "INSERT INTO users (id, username, hashed_password, role, is_active) "
            "VALUES (2, 'victim', 'abc123', 'member', 1)"
        )
        seed.commit()
    finally:
        world.pool.release_connection(seed)

    world.bind()
    try:
        outcome, _hook_fires = _drive(
            lambda: update_user_role(
                2,
                _request_for("PATCH", "/api/users/2/role"),
                UpdateRoleRequest(role="admin"),
                user=_SUPERADMIN,
                _csrf_token="t",
            )
        )
    finally:
        world.close()

    assert outcome == "returned"
    assert _scalar(world.db_path, "SELECT role FROM users WHERE id = 2") == "admin"
    assert _scalar(
        world.db_path,
        "SELECT COUNT(*) FROM security_audit_log WHERE event_type = ?",
        ("user.role_updated",),
    ) == 1
