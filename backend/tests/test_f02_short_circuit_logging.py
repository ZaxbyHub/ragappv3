"""Acceptance checks: short-circuited responses are logged and identified.

Issue-tracer v3 trace 730-731 (GitHub issue #731, ACs 1-5): frozen
red-checkpoint checks for the middleware-stack ordering defect. At base,
``LoggingMiddleware`` is registered innermost, so the response
``MaintenanceMiddleware`` short-circuits without ``call_next`` (the
maintenance 503) never passes through it: no ``http_request`` access-log
line, no ``X-Request-ID`` header. ``SlowAPIMiddleware`` also sits outside
Logging, but its pre-check short-circuit only applies to application-level
limits this app does not configure; the LIVE login 429 comes from the route
decorator, raised inside the router and converted at the innermost exception
middleware — INSIDE Logging — so 429s already carry the header and line at
base (that check is PRESERVING; it guards the reorder keeping them logged).
A second defect: the maintenance POST exemption set omits
``/api/auth/change-password``, the one route the forced-password-change gate
leaves open.

Harnesses mirror the proven suites:

- ``backend/tests/test_maintenance.py::TestMaintenanceAgainstRealApp`` (real
  ``app.main.app``, temp DB + migrations + ``get_db`` override +
  ``state.maintenance_service``),
- ``backend/tests/test_maintenance.py::TestMaintenanceMiddlewareDispatch``
  (minimal FastAPI app + ``MaintenanceMiddleware`` via a lazy service
  getter),
- ``backend/tests/test_auth_route_rate_limit_runtime.py`` (10 unknown-user
  logins consume the 10/minute bucket; the 11th returns 429).

NOTE: this module must stay lexically clean of the double-submit token
mechanism's four-letter name. The tests/conftest.py module classifier keys
on that substring and, when present, withdraws the global mutation bypass —
which would make every HTTP mutation in these harnesses fail before the
behavior under test is reached. With the file clean, the bypass stays active
and the harnesses need no token plumbing at all.

No network access; no production code is modified by this module.
"""

import logging
import os
import sys
from types import SimpleNamespace

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies (mirrors test_auth_route_rate_limit_runtime.py)
try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow  # noqa: F401
except ImportError:
    import types

    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    import types

    _unstructured = types.ModuleType("unstructured")
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType("unstructured.partition")
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType("unstructured.partition.auto")
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    _unstructured.chunking = types.ModuleType("unstructured.chunking")
    _unstructured.chunking.__path__ = []
    _unstructured.chunking.title = types.ModuleType("unstructured.chunking.title")
    _unstructured.chunking.title.chunk_by_title = lambda *args, **kwargs: []
    _unstructured.documents = types.ModuleType("unstructured.documents")
    _unstructured.documents.__path__ = []
    _unstructured.documents.elements = types.ModuleType(
        "unstructured.documents.elements"
    )
    _unstructured.documents.elements.Element = type("Element", (), {})
    sys.modules["unstructured"] = _unstructured
    sys.modules["unstructured.partition"] = _unstructured.partition
    sys.modules["unstructured.partition.auto"] = _unstructured.partition.auto
    sys.modules["unstructured.chunking"] = _unstructured.chunking
    sys.modules["unstructured.chunking.title"] = _unstructured.chunking.title
    sys.modules["unstructured.documents"] = _unstructured.documents
    sys.modules["unstructured.documents.elements"] = _unstructured.documents.elements

from fastapi.testclient import TestClient  # noqa: E402


def _real_app_with_maintenance_service(tmp_path, stem: str) -> SimpleNamespace:
    """Real app.main.app + one temp-DB pool shared by get_db and a
    MaintenanceService whose flag is ENABLED.

    Mirrors test_maintenance.py::TestMaintenanceAgainstRealApp's wiring
    (temp DB + migrations + get_db override + the lazy service getter's
    ``state.maintenance_service`` attribute), with the settings overrides of
    test_auth_route_rate_limit_runtime.py restored on exit.
    """
    from app.api.deps import get_db
    from app.config import settings
    from app.main import app as main_app
    from app.models.database import (
        SQLiteConnectionPool,
        init_db,
        run_migrations,
    )
    from app.services.maintenance import MaintenanceService

    db_path = str(tmp_path / f"{stem}.db")
    init_db(db_path)
    run_migrations(db_path)

    saved = (settings.jwt_secret_key, settings.users_enabled)
    settings.jwt_secret_key = "test-secret-key-for-testing-at-least-32-chars-long"
    settings.users_enabled = True

    pool = SQLiteConnectionPool(db_path, max_size=5)
    service = MaintenanceService(pool)
    service.set_flag(True, "f02 acceptance window")

    def get_test_db():
        conn = pool.get_connection()
        try:
            yield conn
        finally:
            pool.release_connection(conn)

    main_app.dependency_overrides[get_db] = get_test_db
    # The middleware's lazy getter (main.py) reads this attribute.
    main_app.state.maintenance_service = service
    return SimpleNamespace(
        app=main_app,
        client=TestClient(main_app),
        service=service,
        _pool=pool,
        _get_db=get_db,
        _saved=saved,
    )


def _release_real_app(wiring: SimpleNamespace) -> None:
    from app.config import settings

    settings.jwt_secret_key, settings.users_enabled = wiring._saved
    wiring.app.dependency_overrides.pop(wiring._get_db, None)
    wiring.app.state.maintenance_service = None
    wiring._pool.close_all()


def test_maintenance_503_carries_request_id_header(tmp_path):
    """Issue #731 AC1 (S04-SK-04): the real app's maintenance 503 must carry
    an X-Request-ID response header.

    Harness like test_maintenance.py::TestMaintenanceAgainstRealApp:
    app.state.maintenance_service is a temp-DB MaintenanceService with the
    flag enabled; POST /api/documents/upload is a blocked mutating route.
    Does not depend on the request_id emit-before-reset fix (issue #730).
    """
    wiring = _real_app_with_maintenance_service(tmp_path, "f02-maintenance")
    try:
        resp = wiring.client.post("/api/documents/upload")
        assert resp.status_code == 503, (
            f"expected the maintenance 503, got {resp.status_code}: {resp.text}"
        )
        missing = resp.headers.get("x-request-id") is None
        assert not missing, "maintenance 503 must carry an X-Request-ID header"
    finally:
        _release_real_app(wiring)


def test_rate_limit_429_carries_request_id_header(tmp_path):
    """Issue #731 AC2 (S04-SK-04): the 429 from the 11th login inside the
    10/minute window must carry an X-Request-ID response header.

    PRESERVING (GREEN at base, re-executed during the trace): the login
    429 is produced by the ROUTE decorator (@limiter.limit), raised inside
    the router and converted at the innermost exception middleware — inside
    LoggingMiddleware — so it already carries the header at base. The check
    guards the reorder: the reordered stack must keep decorator 429s
    flowing through LoggingMiddleware.
    """
    from app.api.deps import get_db
    from app.config import settings
    from app.main import app as main_app
    from app.models.database import SQLiteConnectionPool, init_db, run_migrations

    db_path = str(tmp_path / "f02-rate-limit.db")
    init_db(db_path)
    run_migrations(db_path)

    saved = (settings.jwt_secret_key, settings.users_enabled)
    settings.jwt_secret_key = "test-secret-key-for-testing-at-least-32-chars-long"
    settings.users_enabled = True

    pool = SQLiteConnectionPool(db_path, max_size=5)

    def get_test_db():
        conn = pool.get_connection()
        try:
            yield conn
        finally:
            pool.release_connection(conn)

    main_app.dependency_overrides[get_db] = get_test_db
    client = TestClient(main_app)
    try:
        for i in range(10):
            resp = client.post(
                "/api/auth/login",
                json={
                    "username": f"f02-rate-probe-{i}",
                    "password": "WrongPass123",
                },
            )
            assert resp.status_code == 401, (
                f"call {i + 1}/10 should return 401 (unknown user), "
                f"got {resp.status_code}"
            )
        resp = client.post(
            "/api/auth/login",
            json={"username": "f02-rate-probe-final", "password": "WrongPass123"},
        )
        assert resp.status_code == 429, (
            f"11th call should return 429 (rate limited), got {resp.status_code}"
        )
        assert resp.headers.get("x-request-id") is not None, (
            "rate-limit 429 must carry an X-Request-ID header"
        )
    finally:
        settings.jwt_secret_key, settings.users_enabled = saved
        main_app.dependency_overrides.pop(get_db, None)
        pool.close_all()


def test_maintenance_503_is_access_logged(tmp_path, caplog):
    """Issue #731 AC3 (S04-SK-04): a maintenance short-circuit must emit an
    http_request record on the ``http`` logger with status_code 503.

    AC1's harness with caplog at INFO on logger ``http``; asserts only that
    the line exists (carrying request_id is issue #730's check).
    """
    wiring = _real_app_with_maintenance_service(tmp_path, "f02-logged")
    try:
        with caplog.at_level(logging.INFO, logger="http"):
            resp = wiring.client.post("/api/documents/upload")
        assert resp.status_code == 503, (
            f"expected the maintenance 503, got {resp.status_code}: {resp.text}"
        )
        n = sum(
            1
            for record in caplog.records
            if record.getMessage() == "http_request"
            and getattr(record, "status_code", None) == 503
        )
        assert n >= 1, "expected at least one http_request access-log record (503)"
    finally:
        _release_real_app(wiring)


def test_logging_middleware_wraps_short_circuiting_middleware():
    """Issue #731 AC4 (S04-SK-04): pin the middleware stack order.

    ``app.user_middleware`` is outermost-first. Precondition (held by the
    Constraints): CORSMiddleware sits outside the logging, maintenance and
    rate-limit middleware (conditional middlewares like the host-header
    validator or the OTel span middleware may sit outside CORS, so CORS is
    not assumed at index 0). Assertion: LoggingMiddleware wraps BOTH
    MaintenanceMiddleware and SlowAPIMiddleware so every short-circuited
    response passes through it.
    """
    from app.main import app as main_app

    names = []
    for entry in main_app.user_middleware:
        cls = getattr(entry, "cls", None) or entry
        names.append(getattr(cls, "__name__", str(cls)))

    def idx(name: str) -> int:
        assert name in names, f"{name} missing from app.user_middleware: {names}"
        return names.index(name)

    idx_cors = idx("CORSMiddleware")
    idx_logging = idx("LoggingMiddleware")
    idx_maintenance = idx("MaintenanceMiddleware")
    idx_slowapi = idx("SlowAPIMiddleware")

    assert idx_cors < min(idx_logging, idx_maintenance, idx_slowapi), names
    wraps = idx_logging < idx_maintenance and idx_logging < idx_slowapi
    assert wraps, (
        f"LoggingMiddleware must wrap the short-circuiting middleware: {names}"
    )


def test_change_password_is_maintenance_exempt(tmp_path):
    """Issue #731 AC5 (S02-SK-08): POST /api/auth/change-password must pass
    through the maintenance middleware instead of answering 503.

    It is the one POST the must_change_password gate leaves open (deps.py
    exempt paths) and the one the SPA calls; during a window a flagged user
    can authenticate but would have no exit route. Harness like
    test_maintenance.py::TestMaintenanceMiddlewareDispatch with a stub
    change-password route and the flag enabled.
    """
    from fastapi import FastAPI

    from app.middleware.maintenance import MaintenanceMiddleware
    from app.models.database import SQLiteConnectionPool, init_db
    from app.services.maintenance import MaintenanceService

    db_path = str(tmp_path / "f02-dispatch.db")
    init_db(db_path)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    service = MaintenanceService(pool)
    service.set_flag(True, "f02 dispatch window")

    app = FastAPI()
    app.state._maintenance_service_getter = lambda: service

    @app.post("/api/auth/change-password")
    def change_password():
        return {"ok": True}

    app.add_middleware(
        MaintenanceMiddleware,
        service_getter=app.state._maintenance_service_getter,
    )
    client = TestClient(app)
    try:
        resp = client.post("/api/auth/change-password")
        assert resp.status_code != 503, (
            "change-password must be exempt from the maintenance block, got 503"
        )
    finally:
        pool.close_all()
