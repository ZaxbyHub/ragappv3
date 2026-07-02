"""Regression tests for issue #273 admin maintenance and toggle routes."""

import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import admin as admin_module
from app.config import settings
from app.middleware.maintenance import MaintenanceMiddleware
from app.models.database import SQLiteConnectionPool, init_db
from app.security import CSRFManager, csrf_protect
from app.services.maintenance import MaintenanceService
from app.services.toggle_manager import ToggleManager


@pytest.fixture
def maintenance_service():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        init_db(db_path)
        yield MaintenanceService(pool)
    finally:
        pool.close_all()
        Path(db_path).unlink(missing_ok=True)


def _auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-admin-key"}


def _maintenance_app(
    service: MaintenanceService, *, bypass_csrf: bool = True
) -> FastAPI:
    app = FastAPI()

    @app.post("/api/non-exempt-write")
    def non_exempt_write():
        return {"ok": True}

    app.include_router(admin_module.router, prefix="/api")
    app.add_middleware(MaintenanceMiddleware, service=service)
    app.state.csrf_manager = CSRFManager("")
    app.dependency_overrides[admin_module.get_maintenance_service] = lambda: service
    if bypass_csrf:
        app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
    return app


def test_maintenance_routes_get_enable_and_disable_while_enabled(maintenance_service):
    app = _maintenance_app(maintenance_service)
    client = TestClient(app)

    with patch.object(settings, "admin_secret_token", "test-admin-key"):
        with patch.object(
            settings, "admin_token_scopes", {"test-admin-key": ["admin:config"]}
        ):
            initial = client.get("/api/admin/maintenance", headers=_auth_headers())
            assert initial.status_code == 200
            assert initial.json()["enabled"] is False

            enabled = client.post(
                "/api/admin/maintenance",
                json={"enabled": True, "reason": "deploy"},
                headers=_auth_headers(),
            )
            assert enabled.status_code == 200
            assert enabled.json()["enabled"] is True
            assert enabled.json()["reason"] == "deploy"

            blocked = client.post("/api/non-exempt-write", headers=_auth_headers())
            assert blocked.status_code == 503

            disabled = client.post(
                "/api/admin/maintenance",
                json={"enabled": False, "reason": "done"},
                headers=_auth_headers(),
            )
            assert disabled.status_code == 200
            assert disabled.json()["enabled"] is False


def test_maintenance_post_requires_csrf(maintenance_service):
    app = _maintenance_app(maintenance_service, bypass_csrf=False)
    client = TestClient(app)

    with patch.object(settings, "admin_secret_token", "test-admin-key"):
        with patch.object(
            settings, "admin_token_scopes", {"test-admin-key": ["admin:config"]}
        ):
            response = client.post(
                "/api/admin/maintenance",
                json={"enabled": True},
                headers=_auth_headers(),
            )

    assert response.status_code == 403


def test_maintenance_routes_require_admin_scope(maintenance_service):
    app = _maintenance_app(maintenance_service)
    client = TestClient(app)

    with patch.object(settings, "admin_secret_token", "test-admin-key"):
        with patch.object(settings, "admin_token_scopes", {"test-admin-key": []}):
            response = client.get("/api/admin/maintenance", headers=_auth_headers())

    assert response.status_code == 403


def test_maintenance_middleware_exemption_matches_exact_internal_path(
    maintenance_service,
):
    maintenance_service.set_flag(True, "active")
    app = _maintenance_app(maintenance_service)
    client = TestClient(app)

    with patch.object(settings, "admin_secret_token", "test-admin-key"):
        with patch.object(
            settings, "admin_token_scopes", {"test-admin-key": ["admin:config"]}
        ):
            exact = client.post(
                "/api/admin/maintenance",
                json={"enabled": False},
                headers=_auth_headers(),
            )
            maintenance_service.set_flag(True, "active")
            trailing = client.post(
                "/api/admin/maintenance/extra",
                json={"enabled": False},
                headers=_auth_headers(),
            )

    assert exact.status_code == 200
    assert trailing.status_code == 503


class AuditFailingConnection:
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


def test_admin_toggle_rolls_back_when_audit_write_fails():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    real_conn = sqlite3.connect(db_path, check_same_thread=False)
    try:
        init_db(db_path)
        failing_conn = AuditFailingConnection(real_conn)
        toggle_manager = ToggleManager(pool)
        secret_manager = MagicMock()
        secret_manager.get_hmac_key.return_value = (b"key", "v1")

        app = FastAPI()
        app.include_router(admin_module.router)
        app.dependency_overrides[admin_module.get_db] = lambda: failing_conn
        app.dependency_overrides[admin_module.get_toggle_manager] = (
            lambda: toggle_manager
        )
        app.dependency_overrides[admin_module.get_secret_manager] = (
            lambda: secret_manager
        )

        client = TestClient(app)
        with patch.object(settings, "admin_secret_token", "test-admin-key"):
            with patch.object(
                settings, "admin_token_scopes", {"test-admin-key": ["admin:config"]}
            ):
                response = client.post(
                    "/admin/toggles",
                    json={"feature": "issue_273_toggle", "enabled": True},
                    headers=_auth_headers(),
                )

        assert response.status_code == 500
        assert (
            real_conn.execute(
                "SELECT COUNT(*) FROM admin_toggles WHERE feature = ?",
                ("issue_273_toggle",),
            ).fetchone()[0]
            == 0
        )
        assert (
            real_conn.execute(
                "SELECT COUNT(*) FROM audit_toggle_log WHERE feature = ?",
                ("issue_273_toggle",),
            ).fetchone()[0]
            == 0
        )
        assert toggle_manager.get_toggle("issue_273_toggle", False) is False
    finally:
        real_conn.close()
        pool.close_all()
        Path(db_path).unlink(missing_ok=True)
