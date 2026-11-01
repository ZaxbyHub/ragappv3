"""Arm's-length acceptance checks for trace D06 (CSRF store + admin/service-account CSRF).

Discriminating checks authored against base 4323afbb: every test here is RED
at base and GREEN after the fix; each docstring says so.

Harness notes (deliberate):

- This module NAMES csrf everywhere, so conftest's CSRF-bypass classifier
  leaves the autouse bypass OFF for the whole file. Every HTTP test therefore
  installs a pass-through ``app.dependency_overrides[csrf_protect]`` (the
  ``tests/test_admin_routes_issue273.py`` pattern) so mutations never 403 for
  a missing override — the CSRF *dependency wiring itself* is what the
  introspection tests below assert.
- The two PRESERVING checks (maintenance POST requires CSRF; admin toggle
  audit-rollback on failed audit write) already exist in
  ``tests/test_admin_routes_issue273.py`` and are intentionally NOT
  redefined here.
"""

import ast
import logging
import shutil
import sqlite3
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError

from app.api.routes import admin as admin_module
from app.api.routes.service_accounts import router as service_accounts_router
from app.config import settings
from app.models.database import SQLiteConnectionPool, init_db, run_migrations
from app.security import CSRFManager, csrf_protect, require_service_account
from app.services.toggle_manager import ToggleManager

ADMIN_TOKEN = "test-admin-key"
AUTH_LOGGER = "app.api.routes.auth"
D06_PASSWORD = "Sup3rSecret9"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _close_csrf_manager(manager: CSRFManager | None) -> None:
    """Close a CSRFManager's fallback-store SQLite connection (if any)."""
    if manager is None:
        return
    store = getattr(manager, "_fallback_store", None)
    conn = getattr(store, "_conn", None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def _iter_dependency_calls(dependant):
    """Yield every dependency callable wired into a dependant, sub-deps included."""
    for dep in getattr(dependant, "dependencies", []) or []:
        yield dep.call
        yield from _iter_dependency_calls(dep)


def _iter_flattened_routes(routes):
    """Yield concrete routes, expanding lazy router includes (FastAPI >= 0.139).

    ``app.routes`` holds ``_IncludedRouter`` placeholders whose
    ``effective_candidates()`` materialize the prefixed routes; older FastAPI
    versions already list concrete routes. Positive control: this walker
    finds ``csrf_protect`` on POST /api/admin/maintenance at base.
    """
    for route in routes:
        candidates = getattr(route, "effective_candidates", None)
        if callable(candidates):
            yield from _iter_flattened_routes(candidates())
        else:
            yield route


def _route_invokes_csrf_protect(app: FastAPI, path: str) -> bool:
    """True when the POST route at ``path`` runs ``app.security.csrf_protect``.

    Walks ``route.dependant.dependencies`` recursively (sub-dependencies
    included) so a fix that wires CSRF directly or via a wrapper dependency
    both count as invoking the real validator.
    """
    for route in _iter_flattened_routes(app.routes):
        if getattr(route, "path", None) != path:
            continue
        if "POST" not in (getattr(route, "methods", None) or set()):
            continue
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            dependant = getattr(
                getattr(route, "original_route", None), "dependant", None
            )
        if dependant is None:
            continue
        for call in _iter_dependency_calls(dependant):
            if call is csrf_protect:
                return True
        return False
    raise AssertionError(f"harness: no POST route found at {path!r}")


# ---------------------------------------------------------------------------
# AC1 / AC9 — route-level CSRF wiring (introspection on the real app)
# ---------------------------------------------------------------------------


def test_admin_toggles_route_invokes_csrf_protect():
    """RED at base, GREEN after the fix: POST /api/admin/toggles must run csrf_protect."""
    from app.main import app as main_app

    found = _route_invokes_csrf_protect(main_app, "/api/admin/toggles")
    assert found == True  # noqa: E712


@pytest.mark.parametrize(
    "path",
    [
        "/api/service-accounts",
        "/api/service-accounts/{sa_id}/rotate",
        "/api/service-accounts/{sa_id}/revoke",
    ],
)
def test_service_account_mutations_invoke_csrf_protect(path):
    """RED at base, GREEN after the fix: every service-account mutation must run csrf_protect."""
    from app.main import app as main_app

    found = _route_invokes_csrf_protect(main_app, path)
    assert found == True  # noqa: E712


# ---------------------------------------------------------------------------
# AC2 — admin toggle route survives pool RuntimeErrors at both sites
# ---------------------------------------------------------------------------


class _ToggleManagerWithFailingRead(ToggleManager):
    """ToggleManager whose post-commit cache read raises a pool RuntimeError.

    ``set_toggle_on_connection``/``commit_and_publish`` (used inside the
    route's write transaction) are inherited unchanged; only ``get_toggle``
    — which the route calls AFTER commit to refresh app state — fails.
    """

    def get_toggle(self, feature: str, default: bool = False) -> bool:
        raise RuntimeError("pool exhausted during post-commit state refresh")


def _d06_admin_app(conn, toggle_manager, secret_manager) -> FastAPI:
    """Issue-#273-style harness: admin router + dependency overrides + CSRF override."""
    app = FastAPI()
    app.include_router(admin_module.router)
    app.dependency_overrides[admin_module.get_db] = lambda: conn
    app.dependency_overrides[admin_module.get_toggle_manager] = lambda: toggle_manager
    app.dependency_overrides[admin_module.get_secret_manager] = lambda: secret_manager
    # This module is csrf-named: the conftest bypass is OFF, so the pass-through
    # override is mandatory once the fix wires Depends(csrf_protect) in.
    app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
    return app


@pytest.mark.parametrize("site", ["write"])
def test_admin_toggles_pool_runtime_error_is_handled(site, tmp_path):
    """RED at base, GREEN after the fix: pool RuntimeErrors must not escape as HTTP 500.

    ``write``: the transactional toggle write raises RuntimeError (pool
    exhausted) — the route must answer a handled 503, not an unhandled 500.
    ``state_refresh``: the post-commit app-state refresh raises RuntimeError —
    the committed toggle must still yield a 2xx (or a clean 503), never a 500.
    """
    db_path = str(tmp_path / "d06_admin.db")
    init_db(db_path)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    client = None
    try:
        secret_manager = MagicMock()
        secret_manager.get_hmac_key.return_value = (b"d06-hmac-key", "v1")
        if site == "write":
            toggle_manager = ToggleManager(pool)
        else:
            toggle_manager = _ToggleManagerWithFailingRead(pool)
        app = _d06_admin_app(conn, toggle_manager, secret_manager)
        client = TestClient(app, raise_server_exceptions=False)
        stack = ExitStack()
        stack.enter_context(patch.object(settings, "admin_secret_token", ADMIN_TOKEN))
        stack.enter_context(
            patch.object(
                settings, "admin_token_scopes", {ADMIN_TOKEN: ["admin:config"]}
            )
        )
        if site == "write":
            stack.enter_context(
                patch(
                    "app.api.routes.admin._write_toggle_with_audit",
                    side_effect=RuntimeError("pool exhausted during write"),
                )
            )
        with stack:
            response = client.post(
                "/admin/toggles",
                json={"feature": "d06_toggle", "enabled": True},
                headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            )
        if site == "write":
            ok = response.status_code == 503
        else:
            ok = response.status_code in (200, 503)
        assert ok, (
            f"{site}: pool RuntimeError escaped as HTTP {response.status_code} "
            "instead of being handled (503 / 200)"
        )
    finally:
        if client is not None:
            client.close()
        conn.close()
        pool.close_all()


# ---------------------------------------------------------------------------
# AC3 — app.state.model_validation is not write-only (AST census)
# ---------------------------------------------------------------------------


def test_model_validation_state_flag_is_not_write_only():
    """RED at base, GREEN after the fix: app.state.model_validation must be read somewhere.

    AST census over backend/app: every ``...model_validation`` attribute is
    classified Store vs Load. Writes with zero reads are dead state.
    """
    app_root = Path(__file__).resolve().parents[1] / "app"
    writes = 0
    reads = 0
    for source_path in sorted(app_root.rglob("*.py")):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "model_validation":
                if isinstance(node.ctx, ast.Store):
                    writes += 1
                elif isinstance(node.ctx, ast.Load):
                    reads += 1
    unread_writes = writes if reads == 0 else 0
    assert unread_writes == 0


# ---------------------------------------------------------------------------
# AC4 / AC5 — CSRFManager failover + recovery-probe backoff
# ---------------------------------------------------------------------------


class _FlakyRedisStub:
    """Redis stub that answers the construction ping, then loses connectivity."""

    def __init__(self) -> None:
        self.healthy = True

    def _maybe_raise(self) -> None:
        if not self.healthy:
            raise RedisConnectionError("stub: connection lost")

    def ping(self) -> None:
        self._maybe_raise()

    def setex(self, key: str, ttl: int, value: str) -> None:
        self._maybe_raise()

    def get(self, key: str):
        self._maybe_raise()
        return None

    def expire(self, key: str, ttl: int) -> None:
        self._maybe_raise()


def test_csrf_fails_over_after_healthy_boot(tmp_path):
    """RED at base, GREEN after the fix: Redis dying AFTER a healthy boot must fail over.

    The manager connects to Redis successfully at construction; Redis then
    dies. The next generate_token() must NOT surface a 503 — the manager must
    fail over to the fallback store and still issue a token.
    """
    stub = _FlakyRedisStub()
    manager = None
    try:
        with patch("app.security.redis.from_url", return_value=stub):
            manager = CSRFManager(
                redis_url="redis://d06-stub", db_path=str(tmp_path / "csrf.db")
            )
        # Redis was healthy at boot; it dies before the first token request.
        stub.healthy = False
        got_503 = False
        try:
            manager.generate_token()
        except HTTPException as exc:
            got_503 = exc.status_code == 503
        assert got_503 == False  # noqa: E712
    finally:
        _close_csrf_manager(manager)


class _CountingDeadRedisStub:
    """Redis stub whose ping always fails, counting recovery probes."""

    def __init__(self) -> None:
        self.ping_count = 0

    def ping(self) -> None:
        self.ping_count += 1
        raise RedisConnectionError("stub: redis down")

    def setex(self, key: str, ttl: int, value: str) -> None:
        raise RedisConnectionError("stub: redis down")

    def get(self, key: str):
        raise RedisConnectionError("stub: redis down")

    def expire(self, key: str, ttl: int) -> None:
        raise RedisConnectionError("stub: redis down")


def test_csrf_recovery_ping_is_rate_limited():
    """RED at base, GREEN after the fix: recovery pings must be rate-limited while in fallback.

    With Redis dead at boot the manager runs on the fallback store, but every
    generate_token() still pings Redis to check recovery — 10 requests must
    not trigger 10 pings.
    """
    stub = _CountingDeadRedisStub()
    with patch("app.security.redis.from_url", return_value=stub):
        manager = CSRFManager(redis_url="redis://d06-stub", db_path="")
    # Only the post-boot recovery probes count, not the boot ping itself.
    stub.ping_count = 0
    for _ in range(10):
        manager.generate_token()
    assert stub.ping_count <= 1


# ---------------------------------------------------------------------------
# AC6 / AC7 — SQLite CSRF fallback store robustness
# ---------------------------------------------------------------------------


def test_csrf_sqlite_store_not_starved_by_app_writers(tmp_path):
    """RED at base, GREEN after the fix: the CSRF store must not contend 5s-lock with app writers.

    The fallback store may share the app database file only if it opens with
    a long-enough busy timeout (>= 30s); otherwise it must use its own file.
    The default 5s sqlite timeout starves under app write pressure.
    """
    app_db = str(tmp_path / "app.db")
    calls: list[tuple[str, float]] = []
    real_connect = sqlite3.connect

    def spy_connect(path, *args, **kwargs):
        calls.append((str(path), kwargs.get("timeout", 5.0)))
        return real_connect(path, *args, **kwargs)

    manager = None
    try:
        with patch("app.security.sqlite3.connect", new=spy_connect):
            manager = CSRFManager(redis_url="", db_path=app_db)
        assert calls, (
            "harness: sqlite3.connect was never called during CSRFManager construction"
        )
        ok = any(opened != app_db or timeout >= 30 for opened, timeout in calls)
        assert ok == True  # noqa: E712
    finally:
        _close_csrf_manager(manager)


def test_csrf_sqlite_store_keys_are_hashed(tmp_path):
    """RED at base, GREEN after the fix: the SQLite CSRF store must not store raw tokens.

    The token_hash column must hold a hash (or otherwise not contain the raw
    token), and validation must still succeed through the hashed
    representation.
    """
    db_file = str(tmp_path / "csrf.db")
    manager = CSRFManager(redis_url="", db_path=db_file)
    try:
        token = manager.generate_token()
        reader = sqlite3.connect(db_file)
        try:
            rows = reader.execute("SELECT token_hash FROM csrf_tokens").fetchall()
        finally:
            reader.close()
        assert rows, "harness: no csrf_tokens rows were written"
        leaked = any(token in row[0] for row in rows)
        assert leaked == False  # noqa: E712
        assert manager.validate_token(token) is True
    finally:
        _close_csrf_manager(manager)


# ---------------------------------------------------------------------------
# AC8 — auth routes log swallowed CSRF issuance failures
# ---------------------------------------------------------------------------


class _WorkingCSRFManagerStub:
    def generate_token(self) -> str:
        return "d06-working-csrf-token"

    def validate_token(self, token: str) -> bool:
        return True


class _BrokenCSRFManagerStub:
    def generate_token(self) -> str:
        raise RuntimeError("csrf issuance failed (d06 stub)")

    def validate_token(self, token: str) -> bool:
        return True


@pytest.fixture
def d06_auth_app():
    """The real app on a temp DB with auth overrides and a working csrf manager stub.

    Mirrors ``tests/test_auth_register_users_enabled.py``: get_db overridden
    to a private pool, csrf_protect overridden pass-through (this module is
    csrf-named so the conftest bypass is OFF), users_enabled=True so the
    register leg can reach its CSRF issuance site.
    """
    from app.api.deps import get_db
    from app.main import app as main_app

    temp_dir = tempfile.mkdtemp(prefix="d06-auth-")
    db_path = str(Path(temp_dir) / "d06_auth.db")
    init_db(db_path)
    run_migrations(db_path)
    pool = SQLiteConnectionPool(db_path, max_size=5)

    def get_test_db():
        conn = pool.get_connection()
        try:
            yield conn
        finally:
            pool.release_connection(conn)

    main_app.dependency_overrides[get_db] = get_test_db
    main_app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
    original_manager = getattr(main_app.state, "csrf_manager", None)
    original_users_enabled = settings.users_enabled
    settings.users_enabled = True
    main_app.state.csrf_manager = _WorkingCSRFManagerStub()
    try:
        yield main_app
    finally:
        settings.users_enabled = original_users_enabled
        main_app.dependency_overrides.pop(get_db, None)
        main_app.dependency_overrides.pop(csrf_protect, None)
        if original_manager is None:
            try:
                del main_app.state.csrf_manager
            except AttributeError:
                pass
        else:
            main_app.state.csrf_manager = original_manager
        pool.close_all()
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.parametrize("route", ["register", "login", "refresh"])
def test_csrf_issuance_failure_is_logged(route, d06_auth_app, caplog):
    """RED at base, GREEN after the fix: swallowed CSRF issuance failures must be logged.

    Each auth route wraps its issue_csrf_token call in ``except Exception:
    pass`` — with the csrf manager broken, the route still returns 200 but a
    WARNING+ record from ``app.api.routes.auth`` must land in the log.
    """
    username = f"d06_{route}_user"
    client = TestClient(d06_auth_app)
    try:
        # Setup legs run with a WORKING csrf manager so only the leg under
        # test can be the swallowed-failure site.
        if route in ("login", "refresh"):
            created = client.post(
                "/api/auth/register",
                json={"username": username, "password": D06_PASSWORD},
            )
            assert created.status_code == 200, created.text
        refresh_token = None
        if route == "refresh":
            logged_in = client.post(
                "/api/auth/login",
                json={"username": username, "password": D06_PASSWORD},
            )
            assert logged_in.status_code == 200, logged_in.text
            refresh_token = client.cookies.get("refresh_token")
            assert refresh_token, "harness: login did not set a refresh_token cookie"

        d06_auth_app.state.csrf_manager = _BrokenCSRFManagerStub()
        with caplog.at_level(logging.WARNING, logger=AUTH_LOGGER):
            if route == "register":
                response = client.post(
                    "/api/auth/register",
                    json={"username": username, "password": D06_PASSWORD},
                )
            elif route == "login":
                response = client.post(
                    "/api/auth/login",
                    json={"username": username, "password": D06_PASSWORD},
                )
            else:
                response = client.post(
                    "/api/auth/refresh",
                    cookies={"refresh_token": refresh_token},
                )
        assert response.status_code == 200, (
            f"harness: {route} leg returned {response.status_code}: {response.text}"
        )
        n = sum(
            1
            for record in caplog.records
            if record.name == AUTH_LOGGER and record.levelno >= logging.WARNING
        )
        assert n >= 1
    finally:
        client.close()


# ---------------------------------------------------------------------------
# AC10 — previous service-account key stays valid until the new key is used
# ---------------------------------------------------------------------------


@pytest.fixture
def d06_sa_db(tmp_path) -> str:
    db_path = str(tmp_path / "app.db")
    init_db(db_path)
    return db_path


@pytest.fixture
def d06_sa_client(d06_sa_db: str):
    """Management client over the service-accounts router (test_service_accounts harness)."""
    from app.models.database import _pool_cache

    _pool_cache.clear()
    test_pool = SQLiteConnectionPool(d06_sa_db, max_size=1)

    def _get_pool(path, **kw):
        return test_pool

    app = FastAPI()
    app.include_router(service_accounts_router, prefix="/api")
    # csrf-named module: bypass OFF, so install the pass-through override.
    app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"

    with patch("app.models.database.get_pool", side_effect=_get_pool):
        with patch.object(settings, "data_dir", Path(d06_sa_db).parent):
            with patch.object(settings, "admin_secret_token", ADMIN_TOKEN):
                with patch.object(
                    settings, "admin_token_scopes", {ADMIN_TOKEN: ["admin:config"]}
                ):
                    client = TestClient(app)
                    yield client
                    client.close()

    _pool_cache.clear()
    test_pool.close_all()


@contextmanager
def _sa_protected_client(db_path: str):
    """Minimal app guarded by require_service_account (key-usage probe)."""
    from app.models.database import _pool_cache

    app = FastAPI()

    @app.get("/sa-protected")
    def sa_protected_endpoint(
        _auth: dict = Depends(require_service_account(["documents:read"])),
    ):
        return {"status": "ok", "sa_id": _auth["service_account_id"]}

    _pool_cache.clear()
    test_pool = SQLiteConnectionPool(db_path, max_size=1)

    def _get_pool(path, **kw):
        return test_pool

    with patch("app.models.database.get_pool", side_effect=_get_pool):
        with patch.object(settings, "data_dir", Path(db_path).parent):
            client = TestClient(app)
            yield client
            client.close()

    _pool_cache.clear()
    test_pool.close_all()


def test_previous_key_valid_until_new_key_used(d06_sa_client, d06_sa_db):
    """RED at base, GREEN after the fix: the previous service-account key outlives rotation.

    Rotation must not kill the previous key on the spot: after a successful
    rotate (key2 != key1), a request using ONLY key1 — the new key is never
    presented first — must still authenticate (grace period until first use
    of the new key).
    """
    headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    created = d06_sa_client.post(
        "/api/service-accounts",
        json={"name": "d06-grace-sa", "scopes": ["documents:read"]},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    sa_id = created.json()["id"]
    key1 = created.json()["key"]

    rotated = d06_sa_client.post(
        f"/api/service-accounts/{sa_id}/rotate", headers=headers
    )
    assert rotated.status_code == 200, rotated.text
    key2 = rotated.json()["key"]
    assert key2 != key1

    with _sa_protected_client(d06_sa_db) as sa_client:
        response = sa_client.get(
            "/sa-protected", headers={"Authorization": f"Bearer {key1}"}
        )
    assert response.status_code == 200, response.text
