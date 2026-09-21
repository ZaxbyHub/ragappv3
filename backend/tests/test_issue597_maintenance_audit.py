"""Acceptance checks for issue #597: maintenance flag writes must be audited.

``POST /api/admin/maintenance`` flips the write-blocking maintenance flag via
``MaintenanceService.set_flag`` but — unlike every other admin toggle write
(``_write_toggle_with_audit`` in ``app/api/routes/admin.py``) — writes no
HMAC-signed ``audit_toggle_log`` row. The checks below pin the issue contract
against the route and service seams, independent of how a fix is shaped:

- AC1: one ``audit_toggle_log`` row per toggle (feature='maintenance', enabled
  value, acting user id, client IP), ground-truthed via a raw sqlite3
  connection to the test's temp DB.
- AC2: a failed audit INSERT fails the whole write (non-2xx response,
  ``system_flags`` unchanged, zero audit rows), with the failure injected by
  a duck-typed pool wrapper whose connections reject audit SQL.
- AC3: the optimistic-lock retry on ``system_flags`` survives (stale-version
  writer retries onto the fresh version, +1 exactly) and the exhausted-retry
  failure path leaves no partial audit row.
- AC4: existing behavior is preserved — route roundtrip reflects toggles
  immediately, and ``service.set_flag(enabled, reason)`` stays callable
  without any audit data while still invalidating the TTL cache.
- AC5: the audit row's ``hmac_sha256`` verifies against the secret manager's
  key over ``feature|int(enabled)|user_id|ip|timestamp`` and ``key_version``
  matches the secret manager's version.

This module does not exercise CSRF enforcement (the pytest-only conftest
bypass applies); the audit contract is the subject under test.
"""

import hashlib
import hmac
import inspect
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import admin as admin_module
from app.config import settings
from app.models.database import SQLiteConnectionPool, init_db
from app.services.maintenance import MaintenanceError, MaintenanceService

CSRF_TEST_POLICY = "naive"

ADMIN_TOKEN = "test-admin-key"
TEST_KEY = b"issue597-test-key"
TEST_KEY_VERSION = "v597"
# Starlette's TestClient always reports the client host as "testclient".
TESTCLIENT_HOST = "testclient"


@contextmanager
def _temp_maintenance_db():
    """Yield (db_path, pool) with the full schema; always close and unlink."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        init_db(db_path)
        yield db_path, pool
    finally:
        pool.close_all()
        Path(db_path).unlink(missing_ok=True)


def _secret_manager() -> MagicMock:
    manager = MagicMock()
    manager.get_hmac_key.return_value = (TEST_KEY, TEST_KEY_VERSION)
    return manager


def _audit_payload() -> SimpleNamespace:
    """Audit payload shaped like MaintenanceAudit without importing it.

    The pre-fix tree has no MaintenanceAudit symbol (importing it here would
    break every check on the base leg); the service reads only these five
    fields, and a foreign HMAC value is fine for the AC3 row-gate assertions.
    """
    return SimpleNamespace(
        user_id=ADMIN_TOKEN,
        ip=TESTCLIENT_HOST,
        key_version=TEST_KEY_VERSION,
        hmac_sha256=hashlib.sha256(b"issue597-ac3-payload").hexdigest(),
        timestamp="2026-01-01T00:00:00+00:00",
    )


def _issue597_app(service: MaintenanceService, db_conn=None) -> FastAPI:
    """App mounting the admin router with the service (and known audit key).

    The secret-manager override is inert on the current tree (the maintenance
    route never resolves it) and required after a fix, so the audit HMAC key
    is always the known test key. It is installed as a zero-arg callable
    because FastAPI re-inspects an override's signature — a bare MagicMock
    would present ``(*args, **kwargs)`` and turn every POST into a 422 once
    the route starts resolving the dependency. ``db_conn`` optionally
    overrides ``get_db`` for fix shapes that write through the
    request-scoped connection.
    """
    app = FastAPI()
    app.include_router(admin_module.router, prefix="/api")
    app.dependency_overrides[admin_module.get_maintenance_service] = lambda: service
    app.dependency_overrides[admin_module.get_secret_manager] = lambda: (
        _secret_manager()
    )
    if db_conn is not None:
        app.dependency_overrides[admin_module.get_db] = lambda: db_conn
    return app


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@contextmanager
def _admin_auth():
    with patch.object(settings, "admin_secret_token", ADMIN_TOKEN):
        with patch.object(
            settings, "admin_token_scopes", {ADMIN_TOKEN: ["admin:config"]}
        ):
            yield


def _flag_row(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT value, version FROM system_flags WHERE name = 'maintenance'"
    ).fetchone()


def _audit_rows(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT feature, enabled, user_id, ip, timestamp, key_version, hmac_sha256 "
        "FROM audit_toggle_log WHERE feature = 'maintenance' ORDER BY id"
    ).fetchall()


class AuditFailingConnection:
    """Duck-typed connection whose audit_toggle_log statements fail.

    Modeled on ``AuditFailingConnection`` in test_admin_routes_issue273.py:
    any SQL string containing ``audit_toggle_log`` raises
    ``sqlite3.OperationalError``; everything else passes through unchanged.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @property
    def in_transaction(self) -> bool:
        return self.conn.in_transaction

    def execute(self, sql: str, params=()):
        if "audit_toggle_log" in sql:
            raise sqlite3.OperationalError("audit failed")
        return self.conn.execute(sql, params)

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()


class AuditFailingPool:
    """Pool wrapper handing out AuditFailingConnection instances.

    ``release_connection`` unwraps so the real pool only ever sees its own
    sqlite3 connections (the pool probes/queues raw connections).
    """

    def __init__(self, pool: SQLiteConnectionPool) -> None:
        self.pool = pool

    def get_connection(self, max_wait_attempts: int = 3) -> AuditFailingConnection:
        return AuditFailingConnection(self.pool.get_connection(max_wait_attempts))

    def release_connection(self, conn) -> None:
        if isinstance(conn, AuditFailingConnection):
            self.pool.release_connection(conn.conn)
        else:
            self.pool.release_connection(conn)

    def close_all(self) -> None:
        self.pool.close_all()


def test_issue597_ac1_audit_row_written():
    """AC1: each POST writes exactly one audit row for feature='maintenance'."""
    with _temp_maintenance_db() as (db_path, pool):
        service = MaintenanceService(pool)
        # The get_db override is inert on the current tree; it keeps any
        # request-scoped writes on the temp DB if a fix routes them through
        # Depends(get_db) instead of the service pool.
        route_db = sqlite3.connect(db_path, check_same_thread=False)
        app = _issue597_app(service, db_conn=route_db)
        client = TestClient(app)
        verify = sqlite3.connect(db_path)
        try:
            with _admin_auth():
                enabled = client.post(
                    "/api/admin/maintenance",
                    json={"enabled": True, "reason": "ac1 enable"},
                    headers=_auth_headers(),
                )
                assert enabled.status_code == 200, (
                    f"issue597 AC1: enable POST expected 200, got {enabled.status_code}"
                )
                disabled = client.post(
                    "/api/admin/maintenance",
                    json={"enabled": False, "reason": "ac1 disable"},
                    headers=_auth_headers(),
                )
                assert disabled.status_code == 200, (
                    f"issue597 AC1: disable POST expected 200, got {disabled.status_code}"
                )

            rows = _audit_rows(verify)
            assert len(rows) == 2, (
                "issue597 AC1: expected exactly one audit_toggle_log row per "
                f"toggle (2 for enable+disable), found {len(rows)}"
            )
            expected = [
                ("maintenance", 1, ADMIN_TOKEN, TESTCLIENT_HOST),
                ("maintenance", 0, ADMIN_TOKEN, TESTCLIENT_HOST),
            ]
            actual = [(r[0], r[1], r[2], r[3]) for r in rows]
            assert actual == expected, (
                "issue597 AC1: audit rows must carry feature='maintenance', the "
                "enabled value, the acting user id and the client IP; got "
                f"{actual}, expected {expected}"
            )
        finally:
            verify.close()
            route_db.close()


def test_issue597_ac2_failed_audit_rolls_back_flag():
    """AC2: a failed audit INSERT rolls back the flag write entirely."""
    with _temp_maintenance_db() as (db_path, pool):
        failing_pool = AuditFailingPool(pool)
        service = MaintenanceService(failing_pool)
        direct = sqlite3.connect(db_path, check_same_thread=False)
        try:
            # Belt-and-braces for a fix that writes through Depends(get_db):
            # the injected connection also rejects audit SQL.
            app = _issue597_app(service, db_conn=AuditFailingConnection(direct))
            client = TestClient(app, raise_server_exceptions=False)
            # Ground truth is verified through this separate raw connection,
            # never through the wrapper.
            verify = sqlite3.connect(db_path)
            try:
                before = _flag_row(verify)
                with _admin_auth():
                    response = client.post(
                        "/api/admin/maintenance",
                        json={"enabled": True, "reason": "ac2"},
                        headers=_auth_headers(),
                    )
                assert not (200 <= response.status_code < 300), (
                    "issue597 AC2: a failed audit INSERT must fail the whole "
                    f"write, got a successful response ({response.status_code})"
                )
                after = _flag_row(verify)
                assert after == before, (
                    "issue597 AC2: system_flags must be unchanged when the "
                    f"audit write fails; before={tuple(before)} after={tuple(after)}"
                )
                audit_count = verify.execute(
                    "SELECT COUNT(*) FROM audit_toggle_log WHERE feature = 'maintenance'"
                ).fetchone()[0]
                assert audit_count == 0, (
                    "issue597 AC2: zero audit_toggle_log rows must exist when "
                    f"the audit write fails, found {audit_count}"
                )
            finally:
                verify.close()
        finally:
            direct.close()


def test_issue597_ac3_optimistic_lock_retry_no_partial_audit():
    """AC3: optimistic-lock retry survives; exhausted retries leave no audit."""
    with _temp_maintenance_db() as (db_path, pool):
        service = MaintenanceService(pool)
        verify = sqlite3.connect(db_path)
        try:
            # (a) A writer holding a stale snapshot retries onto the fresh
            # version written by a "foreign" writer and lands exactly +1.
            stale = service.get_flag()
            foreign_version = stale.version + 1
            foreign = sqlite3.connect(db_path)
            try:
                foreign.execute(
                    "UPDATE system_flags SET version = version + 1, "
                    "updated_at = CURRENT_TIMESTAMP WHERE name = 'maintenance'"
                )
                foreign.commit()
            finally:
                foreign.close()

            real_get_flag = service.get_flag
            reads = {"count": 0}

            def first_read_stale_then_fresh():
                reads["count"] += 1
                if reads["count"] == 1:
                    return stale
                return real_get_flag()

            # On the fixed tree the audit parameter exists: exercise the
            # AUDITED retry paths (the no-audit legacy shape cannot prove
            # anything about audit-row gating). The capability probe keeps
            # this check GREEN on the pre-fix tree, where it is a
            # PRESERVING row.
            audited = "audit" in inspect.signature(service.set_flag).parameters
            audit_payload = None
            if audited:
                audit_payload = _audit_payload()

            with patch.object(
                service, "get_flag", side_effect=first_read_stale_then_fresh
            ):
                if audited:
                    service.set_flag(True, "ac3 retry writer", audit_payload)
                else:
                    service.set_flag(True, "ac3 retry writer")

            row = _flag_row(verify)
            assert row == (1, foreign_version + 1), (
                "issue597 AC3: a stale-version writer must still succeed via "
                f"retry and increment the version by exactly 1 (expected "
                f"(1, {foreign_version + 1}), got {tuple(row)})"
            )

            # (b) Retries exhausted (permanently stale snapshot): the service
            # raises MaintenanceError and no partial audit row was written.
            current = service.get_flag()
            with patch.object(service, "get_flag", return_value=stale):
                with pytest.raises(MaintenanceError):
                    if audited:
                        service.set_flag(True, "ac3 never lands", _audit_payload())
                    else:
                        service.set_flag(True, "ac3 never lands")
            row_after = _flag_row(verify)
            assert row_after == (int(current.enabled), current.version), (
                "issue597 AC3: an exhausted retry must leave system_flags "
                f"unchanged; expected {(int(current.enabled), current.version)}, "
                f"got {tuple(row_after)}"
            )
            audit_count = verify.execute(
                "SELECT COUNT(*) FROM audit_toggle_log WHERE feature = 'maintenance'"
            ).fetchone()[0]
            if audited:
                # The audited retry must land exactly one audit row (an INSERT
                # attempted per loop iteration or per optimistic-lock miss
                # would over-insert), and the exhausted audited retries must
                # add none.
                assert audit_count == 1, (
                    "issue597 AC3: the successful audited retry must write "
                    "exactly one audit_toggle_log row (INSERT is gated on the "
                    f"version match), found {audit_count}"
                )
                audit_row = verify.execute(
                    "SELECT feature, enabled FROM audit_toggle_log "
                    "WHERE feature = 'maintenance'"
                ).fetchone()
                assert audit_row == ("maintenance", 1), (
                    "issue597 AC3: the audited retry's audit row must record "
                    f"feature='maintenance' enabled=1, got {audit_row}"
                )
            else:
                assert audit_count == 0, (
                    "issue597 AC3: no partial audit_toggle_log row may exist on the "
                    f"exhausted-retry failure path, found {audit_count}"
                )
        finally:
            verify.close()


def test_issue597_ac4_route_roundtrip_and_cache_invalidation():
    """AC4: route roundtrip and no-argument service calls keep working."""
    with _temp_maintenance_db() as (db_path, pool):
        service = MaintenanceService(pool)
        route_db = sqlite3.connect(db_path, check_same_thread=False)
        app = _issue597_app(service, db_conn=route_db)
        client = TestClient(app)
        verify = sqlite3.connect(db_path)
        try:
            with _admin_auth():
                on = client.post(
                    "/api/admin/maintenance",
                    json={"enabled": True, "reason": "ac4 on"},
                    headers=_auth_headers(),
                )
                assert on.status_code == 200, (
                    f"issue597 AC4: enable POST expected 200, got {on.status_code}"
                )
                assert on.json()["enabled"] is True, (
                    "issue597 AC4: enable POST response must report enabled"
                )
                got_on = client.get("/api/admin/maintenance", headers=_auth_headers())
                assert got_on.status_code == 200 and got_on.json()["enabled"] is True, (
                    "issue597 AC4: immediate GET after enable must reflect enabled"
                )

                off = client.post(
                    "/api/admin/maintenance",
                    json={"enabled": False, "reason": "ac4 off"},
                    headers=_auth_headers(),
                )
                assert off.status_code == 200, (
                    f"issue597 AC4: disable POST expected 200, got {off.status_code}"
                )
                got_off = client.get("/api/admin/maintenance", headers=_auth_headers())
                assert (
                    got_off.status_code == 200 and got_off.json()["enabled"] is False
                ), "issue597 AC4: immediate GET after disable must reflect disabled"

            # set_flag(enabled, reason) — no audit data — must keep working for
            # existing callers and still invalidate the TTL cache: prime the
            # cache with the disabled state, flip directly, and require the
            # very next cached read to see the new state (TTL is 5s, so a
            # missed invalidation would serve the stale cached value).
            primed = service.get_flag_cached()
            assert primed.enabled is False, (
                "issue597 AC4: cached read after route disable must be disabled"
            )
            service.set_flag(True, "ac4 direct call without audit data")
            immediate = service.get_flag_cached()
            assert immediate.enabled is True, (
                "issue597 AC4: set_flag(enabled, reason) must remain callable "
                "without audit data and invalidate the flag cache on commit"
            )
            row = _flag_row(verify)
            assert row[0] == 1, (
                f"issue597 AC4: direct set_flag must persist the flag, got "
                f"value={row[0]}"
            )
        finally:
            verify.close()
            route_db.close()


def test_issue597_ac5_audit_row_hmac_verifies():
    """AC5: the audit row's HMAC verifies against the secret manager's key."""
    with _temp_maintenance_db() as (db_path, pool):
        service = MaintenanceService(pool)
        route_db = sqlite3.connect(db_path, check_same_thread=False)
        app = _issue597_app(service, db_conn=route_db)
        client = TestClient(app)
        verify = sqlite3.connect(db_path)
        try:
            with _admin_auth():
                response = client.post(
                    "/api/admin/maintenance",
                    json={"enabled": True, "reason": "ac5"},
                    headers=_auth_headers(),
                )
            assert response.status_code == 200, (
                f"issue597 AC5: POST expected 200, got {response.status_code}"
            )

            rows = _audit_rows(verify)
            assert len(rows) == 1, (
                "issue597 AC5: expected exactly one audit_toggle_log row for "
                f"feature='maintenance', found {len(rows)}"
            )
            feature, enabled, user_id, ip, timestamp, key_version, digest = rows[0]
            message = f"{feature}|{int(enabled)}|{user_id or ''}|{ip or ''}|{timestamp}"
            expected = hmac.new(
                TEST_KEY, message.encode("utf-8"), hashlib.sha256
            ).hexdigest()
            assert digest == expected, (
                "issue597 AC5: audit row hmac_sha256 does not verify against "
                f"the secret-manager key over message {message!r}"
            )
            assert key_version == TEST_KEY_VERSION, (
                "issue597 AC5: audit row key_version must equal the secret "
                f"manager's version ({TEST_KEY_VERSION!r}), got {key_version!r}"
            )
        finally:
            verify.close()
            route_db.close()
