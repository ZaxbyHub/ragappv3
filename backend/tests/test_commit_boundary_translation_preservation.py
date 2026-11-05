"""Issue #1068 normal-path preservation: the moved try/except boundaries at
the drained-unit conversion sites keep their exact current semantics.

The 21-site conversion wraps each site's existing commit-to-audit sequence
verbatim inside one ``drained_unit`` closure. These route-level legs pin the
exception-translation shapes that verbatim relocation must NOT alter (critic
r3 F1 — the named mutation probes: widen a route's except clause to cover
the post-commit steps and these tests go RED):

- register/login WORKER exceptions keep their current translation (the
  in-worker 409, the route-level 500) while POST-commit token/cookie/cache
  failures retain their ORIGINAL propagation (not swallowed by a widened
  route except) and SKIP the audit — drain_commit_audit parity;
- login CSRF issuance stays best-effort (failure does not fail the login
  response);
- change_password + admin password_reset invalidate_active_user_cache
  failures propagate with their original exception type and skip the audit
  (the unit's post-commit step, not a commit failure);
- logout's commit-failure path STILL writes its audit (the one
  site-specific exception, preserved by its own try/except INSIDE the
  unit);
- the #1068 site census stays pinned: 21 converted sites (20 drained-unit
  handler sites + update_vault's single unit carrying BOTH conditional
  census tails), 2 not-converted (compile replay rollback path;
  promotion-cleanup-failure error path).
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

import ast  # noqa: E402
import sqlite3  # noqa: E402
from pathlib import Path  # noqa: E402

from starlette.requests import Request  # noqa: E402
from starlette.responses import Response  # noqa: E402

from app.api.routes import auth as auth_mod  # noqa: E402
from app.api.routes import users as users_mod  # noqa: E402
from app.api.routes.auth import (  # noqa: E402
    ChangePasswordRequest,
    LoginRequest,
    RegisterRequest,
    change_password,
    login,
    logout,
    register,
)
from app.api.routes.users import (  # noqa: E402
    AdminResetPasswordRequest,
    admin_reset_password,
)
from app.config import settings  # noqa: E402
from app.models.database import (  # noqa: E402
    SQLiteConnectionPool,
    _pool_cache,
    init_db,
    run_migrations,
)
from app.services.auth_service import async_hash_password  # noqa: E402

OWNER_ID = 1
TARGET_USER_ID = 2

_GOOD_PASSWORD = "Password123"


def _hash(plain: str) -> str:
    return asyncio.run(async_hash_password(plain))


_SUPERADMIN = {
    "id": OWNER_ID,
    "username": "root",
    "full_name": "Root",
    "role": "superadmin",
    "is_active": True,
    "must_change_password": False,
}


class _World:
    """Per-test world: temp DB, real pool, settings toggle, seeding."""

    def __init__(self, tmp_path: Path, db_name: str, *, users_enabled: bool = False):
        _pool_cache.clear()
        self._saved_users_enabled = settings.users_enabled
        settings.users_enabled = users_enabled
        self.db_path = str(tmp_path / db_name)
        init_db(self.db_path)
        # password_changed_at (and the rest of the migration journal) does
        # not exist in the base SCHEMA — run the real migrations so the
        # password routes' UPDATEs execute.
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

    def seed_session(self, user_id: int, refresh_token: str) -> None:
        import hashlib

        self.real_conn.execute(
            "INSERT INTO user_sessions (user_id, refresh_token_hash, expires_at) "
            "VALUES (?, ?, '2030-01-01T00:00:00+00:00')",
            (user_id, hashlib.sha256(refresh_token.encode()).hexdigest()),
        )
        self.real_conn.commit()

    def scalar(self, sql: str, params: tuple = ()) -> object:
        check = sqlite3.connect(self.db_path)
        try:
            row = check.execute(sql, params).fetchone()
            return None if row is None else row[0]
        finally:
            check.close()

    def audit_rows(self, event_type: str) -> int:
        return int(
            self.scalar(
                "SELECT COUNT(*) FROM security_audit_log WHERE event_type = ?",
                (event_type,),
            )
        )

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


def _drive(make_call):  # noqa: ANN001
    """Run one handler coroutine to completion, returning (kind, value)."""
    return asyncio.run(_drive_async(make_call))


async def _drive_async(make_call):  # noqa: ANN001
    try:
        return ("returned", await make_call())
    except BaseException as exc:  # noqa: BLE001
        return ("raised", exc)


# ── 1. worker-exception translation preserved; post-commit failures keep
#    their ORIGINAL propagation and skip the audit ────────────────────────────


def test_register_worker_409_translation_preserved(tmp_path: Path) -> None:
    """The in-worker uniqueness refusal (F-2.1) still answers 409 verbatim
    after the conversion — and skips the audit (nothing was applied)."""
    world = _World(tmp_path, "reg-409.db", users_enabled=True)
    world.seed_users((OWNER_ID, "dup"))
    try:
        kind, value = _drive(
            lambda: register(
                _request_for("POST", "/api/auth/register"),
                Response(),
                RegisterRequest(username="dup", password=_GOOD_PASSWORD),
                db=world.real_conn,
                _csrf_token="t",
            )
        )
        assert kind == "raised" and isinstance(value, auth_mod.HTTPException), (
            f"the worker's 409 must surface as HTTPException (got {kind} {value!r})"
        )
        assert value.status_code == 409, f"expected 409 (got {value.status_code})"
        assert value.detail == "Username already exists"
        assert world.audit_rows("user.register") == 0, (
            "no audit may be written for a refused registration"
        )
    finally:
        world.close()


def test_register_worker_500_translation_preserved(tmp_path: Path) -> None:
    """A raw worker failure (BEGIN IMMEDIATE refused) still answers the
    route-level 500 translation — and skips the audit."""
    world = _World(tmp_path, "reg-500.db", users_enabled=True)
    world.seed_users((OWNER_ID, "root"))

    class _BeginFailsConn:
        def __init__(self, real: sqlite3.Connection) -> None:
            self._real = real

        def execute(self, sql, *args, **kwargs):  # noqa: ANN001, ANN202, ANN003
            if str(sql).strip().upper().startswith("BEGIN"):
                raise sqlite3.OperationalError("database is locked")
            return self._real.execute(sql, *args, **kwargs)

        def __getattr__(self, name):  # noqa: ANN001
            return getattr(self._real, name)

    try:
        kind, value = _drive(
            lambda: register(
                _request_for("POST", "/api/auth/register"),
                Response(),
                RegisterRequest(username="newuser", password=_GOOD_PASSWORD),
                db=_BeginFailsConn(world.real_conn),
                _csrf_token="t",
            )
        )
        assert kind == "raised" and isinstance(value, auth_mod.HTTPException), (
            f"the route-level translation must answer HTTPException (got {kind} {value!r})"
        )
        assert value.status_code == 500, f"expected 500 (got {value.status_code})"
        assert value.detail == "An internal error occurred. Please try again later."
        assert world.scalar("SELECT COUNT(*) FROM users WHERE username = 'newuser'") == 0
        assert world.audit_rows("user.register") == 0, (
            "no audit may be written when the registration transaction failed"
        )
    finally:
        world.close()


def test_register_post_commit_token_failure_original_propagation(
    tmp_path: Path,
) -> None:
    """Named mutation probe: a POST-commit access-token failure keeps its
    ORIGINAL exception type (not the route's worker 500 translation — a
    widened except clause over the post-commit steps makes this RED) and
    SKIPS the audit, even though the registration commit is durable."""

    class _TokenDown(RuntimeError):
        pass

    def _token_down(*args, **kwargs):  # noqa: ANN002, ANN003
        raise _TokenDown("token signing unavailable")

    saved = auth_mod.create_access_token
    auth_mod.create_access_token = _token_down
    world = _World(tmp_path, "reg-token.db", users_enabled=True)
    try:
        kind, value = _drive(
            lambda: register(
                _request_for("POST", "/api/auth/register"),
                Response(),
                RegisterRequest(username="newuser", password=_GOOD_PASSWORD),
                db=world.real_conn,
                _csrf_token="t",
            )
        )
        assert kind == "raised" and isinstance(value, _TokenDown), (
            "the post-commit token failure must propagate with its ORIGINAL "
            f"type, unswallowed by any route except (got {kind} {value!r})"
        )
        assert world.scalar("SELECT COUNT(*) FROM users WHERE username = 'newuser'") == 1, (
            "precondition: the registration commit completed before the failure"
        )
        assert world.audit_rows("user.register") == 0, (
            "an ordinary post-commit failure must SKIP the audit "
            "(drain_commit_audit parity)"
        )
    finally:
        auth_mod.create_access_token = saved
        world.close()


def test_login_worker_500_translation_preserved(tmp_path: Path) -> None:
    """A raw session-create worker failure still answers the route-level 500
    translation — and skips the auth.login_success audit."""
    world = _World(tmp_path, "login-500.db")
    world.seed_users((OWNER_ID, "root"))

    class _SessionInsertFailsConn:
        def __init__(self, real: sqlite3.Connection) -> None:
            self._real = real

        def execute(self, sql, *args, **kwargs):  # noqa: ANN001, ANN202, ANN003
            if "INSERT INTO user_sessions" in str(sql):
                raise sqlite3.OperationalError("disk i/o error")
            return self._real.execute(sql, *args, **kwargs)

        def __getattr__(self, name):  # noqa: ANN001
            return getattr(self._real, name)

    try:
        kind, value = _drive(
            lambda: login(
                _request_for("POST", "/api/auth/login"),
                Response(),
                LoginRequest(username="root", password=_GOOD_PASSWORD),
                db=_SessionInsertFailsConn(world.real_conn),
                _csrf_token="t",
            )
        )
        assert kind == "raised" and isinstance(value, auth_mod.HTTPException), (
            f"the route-level translation must answer HTTPException (got {kind} {value!r})"
        )
        assert value.status_code == 500, f"expected 500 (got {value.status_code})"
        assert world.audit_rows("auth.login_success") == 0, (
            "no login_success audit may be written when the session create failed"
        )
    finally:
        world.close()


# ── 2. login CSRF issuance stays best-effort ─────────────────────────────────


def test_login_csrf_issuance_failure_is_best_effort(tmp_path: Path) -> None:
    """A CSRF-issuance failure must not fail the login response (issue #724
    semantics preserved through the conversion) and must not skip the
    auth.login_success audit."""
    def _csrf_down(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("csrf manager unavailable")

    saved = auth_mod.issue_csrf_token
    auth_mod.issue_csrf_token = _csrf_down
    world = _World(tmp_path, "login-csrf.db")
    world.seed_users((OWNER_ID, "root"))
    try:
        kind, value = _drive(
            lambda: login(
                _request_for("POST", "/api/auth/login"),
                Response(),
                LoginRequest(username="root", password=_GOOD_PASSWORD),
                db=world.real_conn,
                _csrf_token="t",
            )
        )
        assert kind == "returned" and isinstance(value, dict), (
            f"the login response must succeed despite CSRF issuance failure "
            f"(got {kind} {value!r})"
        )
        assert "access_token" in value
        assert world.audit_rows("auth.login_success") == 1, (
            "the auth.login_success audit must be written even when CSRF "
            "issuance failed"
        )
    finally:
        auth_mod.issue_csrf_token = saved
        world.close()


# ── 3. cache-invalidation failure: original type, audit skipped ──────────────


def test_change_password_cache_invalidation_failure_propagates(tmp_path: Path) -> None:
    """Named mutation probe: invalidate_active_user_cache sits BETWEEN the
    commit and the audit inside the drained unit — its failure must keep its
    original exception type (a widened worker except would convert it to the
    500 translation and make this RED) and skip the auth.password_changed
    audit, even though the password rotation is durable."""

    class _CacheDown(RuntimeError):
        pass

    def _cache_down(uid):  # noqa: ANN001
        raise _CacheDown("cache invalidation blew up")

    saved = auth_mod.invalidate_active_user_cache
    auth_mod.invalidate_active_user_cache = _cache_down
    world = _World(tmp_path, "pw-cache.db")
    world.seed_users((OWNER_ID, "root"))
    try:
        kind, value = _drive(
            lambda: change_password(
                _request_for("POST", "/api/auth/change-password"),
                ChangePasswordRequest(
                    current_password=_GOOD_PASSWORD, new_password="NewPassword456"
                ),
                Response(),
                user=dict(_SUPERADMIN),
                db=world.real_conn,
                _csrf_token="t",
            )
        )
        assert kind == "raised" and isinstance(value, _CacheDown), (
            "the cache-invalidation failure must propagate with its ORIGINAL "
            f"exception type (got {kind} {value!r})"
        )
        assert (
            world.scalar(
                "SELECT COUNT(*) FROM user_sessions WHERE user_id = ?", (OWNER_ID,)
            )
            == 1
        ), "precondition: the password-rotation commit completed (new session row)"
        assert world.audit_rows("auth.password_changed") == 0, (
            "the audit must be skipped when the post-commit cache "
            "invalidation failed (the unit's post-commit step, not a commit failure)"
        )
    finally:
        auth_mod.invalidate_active_user_cache = saved
        world.close()


def test_admin_password_reset_cache_invalidation_failure_propagates(
    tmp_path: Path,
) -> None:
    """Same contract on the admin reset route: the invalidate failure keeps
    its original type and skips the user.password_reset audit while the
    reset itself is durable."""
    world = _World(tmp_path, "admin-pw-cache.db")
    world.seed_users((OWNER_ID, "root"), (TARGET_USER_ID, "target"))

    class _CacheDown(RuntimeError):
        pass

    def _cache_down(uid):  # noqa: ANN001
        raise _CacheDown("cache invalidation blew up")

    saved = users_mod.invalidate_active_user_cache
    users_mod.invalidate_active_user_cache = _cache_down
    try:
        kind, value = _drive(
            lambda: admin_reset_password(
                TARGET_USER_ID,
                _request_for("PATCH", f"/api/users/{TARGET_USER_ID}/password"),
                AdminResetPasswordRequest(new_password="NewPassword456"),
                user=dict(_SUPERADMIN),
                db=world.real_conn,
                _csrf_token="t",
            )
        )
        assert kind == "raised" and isinstance(value, _CacheDown), (
            "the cache-invalidation failure must propagate with its ORIGINAL "
            f"exception type (got {kind} {value!r})"
        )
        assert (
            world.scalar(
                "SELECT must_change_password FROM users WHERE id = ?",
                (TARGET_USER_ID,),
            )
            == 1
        ), "precondition: the password-reset commit completed"
        assert world.audit_rows("user.password_reset") == 0, (
            "the audit must be skipped when the post-commit cache "
            "invalidation failed (the unit's post-commit step, not a commit failure)"
        )
    finally:
        users_mod.invalidate_active_user_cache = saved
        world.close()


# ── 4. logout's commit-failure path STILL writes its audit ───────────────────


def test_logout_commit_failure_still_writes_audit(tmp_path: Path) -> None:
    """The one site-specific exception: _logout_db's commit failing is
    logged and the route continues — the auth.logout audit row must still
    be written (the try/except preserved INSIDE the drained unit), and the
    route must still complete with its success response."""
    token = "logout-commit-failure-probe"
    world = _World(tmp_path, "logout-commit.db")
    world.seed_users((OWNER_ID, "root"))
    world.seed_session(OWNER_ID, token)

    class _FirstCommitFailsConn:
        def __init__(self, real: sqlite3.Connection) -> None:
            self._real = real
            self._failed_once = False

        def commit(self) -> None:
            if not self._failed_once:
                self._failed_once = True
                raise sqlite3.OperationalError("commit failed")
            self._real.commit()

        def __getattr__(self, name):  # noqa: ANN001
            return getattr(self._real, name)

    try:
        kind, value = _drive(
            lambda: logout(
                _request_for("POST", "/api/auth/logout"),
                Response(),
                authorization=None,
                refresh_token=token,
                db=_FirstCommitFailsConn(world.real_conn),
                _csrf_token="t",
            )
        )
        assert kind == "returned" and value == {"message": "Logged out successfully"}, (
            f"logout must still complete with its success response (got {kind} {value!r})"
        )
        assert world.audit_rows("auth.logout") == 1, (
            "logout's commit-failure path must STILL write its auth.logout "
            "audit row (the site-specific try/except inside the drained unit)"
        )
        assert (
            world.scalar(
                "SELECT COUNT(*) FROM user_sessions WHERE user_id = ?", (OWNER_ID,)
            )
            == 1
        ), "precondition mirror: the session-delete commit itself failed"
    finally:
        world.close()


# ── 5. the #1068 site census pin: 21 converted sites, 2 NAP ──────────────────

_ROUTES_DIR = Path(auth_mod.__file__).parent

_EXPECTED_UNITS: dict[str, dict[str, int]] = {
    "auth.py": {
        "register": 1,
        "login": 2,  # login_failed arm + login_success arm
        "logout": 1,
        "change_password": 1,
        "revoke_session": 1,
        "revoke_all_sessions": 1,
    },
    "users.py": {
        "admin_reset_password": 1,
        "update_user_organizations": 1,
        "update_user_groups": 1,
    },
    "vaults.py": {
        "update_vault": 1,
        "update_vault_groups": 1,
    },
    "draft_room.py": {
        "delete_draft": 1,
        "cancel_draft_job": 1,
        "retry_draft_job": 1,
        "compile_draft": 1,
        "mark_revision_ready": 1,
        "export_draft_revision": 1,
        "promote_draft_source": 1,
        "dispose_draft_finding": 1,
    },
}


def _fn_by_name(tree: ast.AST, name: str):  # noqa: ANN202
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _event_constants(fn: ast.AST) -> set[str]:
    return {
        kw.value.value
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "event_type" and isinstance(kw.value, ast.Constant)
    }


def _drained_unit_calls(fn: ast.AST) -> int:
    return sum(
        1
        for sub in ast.walk(fn)
        if isinstance(sub, ast.Call)
        and isinstance(sub.func, ast.Name)
        and sub.func.id == "drained_unit"
    )


def test_commit_boundary_census_21_converted_2_nap() -> None:
    """The #1068 census pin (mirroring how the frozen family files classify):
    21 converted sites — 20 drained-unit handler sites plus update_vault's
    single unit carrying BOTH conditional census tails — and 2
    not-converted sites (compile replay rollback path;
    promotion-cleanup-failure error path). A reverted site, an added unit,
    or an unconverted tail can no longer hide behind the total."""
    total_units = 0
    for fname, expected_handlers in _EXPECTED_UNITS.items():
        tree = ast.parse((_ROUTES_DIR / fname).read_text(encoding="utf-8"))
        found: dict[str, int] = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = _drained_unit_calls(node)
            if calls:
                found[node.name] = found.get(node.name, 0) + calls
        assert found == expected_handlers, (
            f"{fname} drained_unit call-site binding drifted: expected "
            f"{expected_handlers}, found {found} — a site was reverted, "
            "renamed, or an unbound unit appeared"
        )
        total_units += sum(found.values())

    # update_vault's ONE unit owes BOTH conditional census tails: the 21st
    # converted site is its second tail inside the same unit.
    vaults_tree = ast.parse((_ROUTES_DIR / "vaults.py").read_text(encoding="utf-8"))
    update_vault_fn = _fn_by_name(vaults_tree, "update_vault")
    assert update_vault_fn is not None, "census drift: update_vault not found"
    both_tails_in_one_unit = any(
        isinstance(n, ast.AsyncFunctionDef)
        and {"vault.org_id_updated", "vault.visibility_updated"} <= _event_constants(n)
        for n in ast.walk(update_vault_fn)
    )
    assert both_tails_in_one_unit, (
        "census drift: update_vault's drained unit must carry BOTH conditional "
        "audit tails (vault.org_id_updated AND vault.visibility_updated)"
    )
    converted_sites = total_units + 1
    assert converted_sites == 21, (
        f"census drift: expected 21 converted sites (20 handler units + "
        f"update_vault's second tail), counted {converted_sites}"
    )

    # NAP 1 — promotion-cleanup-failure: the error-path helper keeps NO
    # drained unit of its own (it is reached only from _run_promotion's
    # except arms; the frozen draft_room family pins that classification).
    draft_tree = ast.parse((_ROUTES_DIR / "draft_room.py").read_text(encoding="utf-8"))
    cleanup_fn = _fn_by_name(draft_tree, "_record_promotion_cleanup_failure")
    assert cleanup_fn is not None, (
        "census drift: _record_promotion_cleanup_failure not found"
    )
    assert _drained_unit_calls(cleanup_fn) == 0, (
        "census drift: the promotion-cleanup-failure error path must stay "
        "outside the drained-unit conversion (NAP)"
    )

    # NAP 2 — compile replay: compile_draft carries exactly ONE drained unit
    # (the created branch); the replay audit rides the shared `if created:`
    # gate whose created=False exits are rollback-preceded (no applied
    # mutation — the frozen draft_room family pins the two-audits-one-gate
    # shape).
    compile_fn = _fn_by_name(draft_tree, "compile_draft")
    assert compile_fn is not None, "census drift: compile_draft not found"
    assert _drained_unit_calls(compile_fn) == 1, (
        "census drift: compile_draft must carry exactly the created-branch "
        "drained unit (the replay branch is the rollback-path NAP)"
    )
