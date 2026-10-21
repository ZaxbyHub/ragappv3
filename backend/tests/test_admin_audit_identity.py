"""Acceptance pins for the durable admin audit identity (issues #942/#944/#894).

Checks C1-C3 of the 942-944-894 trace pin the NEW-row contract for
``audit_toggle_log``: the write paths that append audit rows must record the
non-secret scope fingerprint of the acting admin token — never the bearer
token itself — and the audit HMAC must be computed over that non-secret
identity, so no signed payload depends on (or is verifiable against) the raw
token.

These are PRESERVING pins: green at base (the write path was fixed by squash
commit 31197279 / PR #890) and they must stay green after the #894 startup
redaction of historical rows lands. Historical-row redaction is pinned
separately in ``test_audit_hmac_redaction.py``; this module only exercises
the routes' write behavior.

The harnesses mirror the established patterns in
``test_admin_routes_issue273.py`` (toggle route) and
``test_issue597_maintenance_audit.py`` (maintenance route); expected
fingerprints are always computed with BOTH ``settings.admin_secret_token``
and ``settings.admin_token_scopes`` patched, because
``admin_scope_fingerprint`` reads the scope map from settings.
"""

import hashlib
import hmac
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import admin as admin_module
from app.config import settings
from app.models.database import SQLiteConnectionPool, init_db
from app.security import admin_scope_fingerprint
from app.services.maintenance import MaintenanceService
from app.services.toggle_manager import ToggleManager

# The maintenance POST resolves the csrf_protect dependency; this suite does
# not exercise CSRF, so the pytest-only bypass applies (conftest contract).
CSRF_TEST_POLICY = "naive"

ADMIN_TOKEN = "test-admin-key"
TEST_KEY = b"audit-identity-test-key"
TEST_KEY_VERSION = "v-audit-identity"
TOGGLE_FEATURE = "audit_identity_toggle"
# Starlette's TestClient always reports the client host as "testclient".
TESTCLIENT_HOST = "testclient"


@contextmanager
def _temp_db():
    """Yield a fresh db path with the full schema; always remove the dir."""
    temp_dir = tempfile.mkdtemp(prefix="audit_identity_")
    db_path = str(Path(temp_dir) / "app.db")
    try:
        init_db(db_path)
        yield db_path
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@contextmanager
def _admin_auth():
    """Patch BOTH token settings so fingerprints are deterministic."""
    with patch.object(settings, "admin_secret_token", ADMIN_TOKEN):
        with patch.object(
            settings, "admin_token_scopes", {ADMIN_TOKEN: ["admin:config"]}
        ):
            yield


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def _secret_manager() -> MagicMock:
    manager = MagicMock()
    manager.get_hmac_key.return_value = (TEST_KEY, TEST_KEY_VERSION)
    return manager


def _toggle_app(pool: SQLiteConnectionPool, route_conn: sqlite3.Connection) -> FastAPI:
    """App mounting the admin router over the toggle-write harness."""
    app = FastAPI()
    app.include_router(admin_module.router)
    app.dependency_overrides[admin_module.get_db] = lambda: route_conn
    app.dependency_overrides[admin_module.get_toggle_manager] = lambda: ToggleManager(
        pool
    )
    app.dependency_overrides[admin_module.get_secret_manager] = lambda: (
        _secret_manager()
    )
    return app


def _maintenance_app(service: MaintenanceService) -> FastAPI:
    """App mounting the admin router over the maintenance harness."""
    app = FastAPI()
    app.include_router(admin_module.router, prefix="/api")
    app.dependency_overrides[admin_module.get_maintenance_service] = lambda: service
    app.dependency_overrides[admin_module.get_secret_manager] = lambda: (
        _secret_manager()
    )
    return app


def _audit_rows(conn: sqlite3.Connection, feature: str) -> list[tuple]:
    return conn.execute(
        "SELECT feature, enabled, user_id, ip, timestamp, key_version, hmac_sha256 "
        "FROM audit_toggle_log WHERE feature = ? ORDER BY id",
        (feature,),
    ).fetchall()


def _assert_fingerprint_identity(row: tuple) -> str:
    """Assert the row's user_id is the token fingerprint, never the token.

    Fingerprint-VALUE pin (not merely "differs from the token"): the expected
    value is computed with the same patched settings the request ran under.
    Also guards that the raw token appears in NO text column of the row.
    """
    feature, enabled, user_id, ip, timestamp, key_version, digest = row
    expected = admin_scope_fingerprint(ADMIN_TOKEN)
    assert user_id == expected, (
        f"audit row user_id must be admin_scope_fingerprint(token) ({expected!r}), "
        f"got {user_id!r}"
    )
    assert user_id != ADMIN_TOKEN
    for value in (feature, user_id, ip, timestamp, key_version, digest):
        assert ADMIN_TOKEN not in str(value), (
            f"raw admin token material must not appear in audit row columns; "
            f"found in {value!r}"
        )
    return user_id


def test_toggle_audit_row_never_contains_token():
    """C1/AC1: POST /api/admin/toggles persists the fingerprint, not the token.

    PRESERVING pin: green at base and must stay green. The audit_toggle_log
    row written by the toggle route must carry the non-secret scope
    fingerprint as its durable identity.
    """
    with _temp_db() as db_path:
        pool = SQLiteConnectionPool(db_path, max_size=2)
        route_conn = sqlite3.connect(db_path, check_same_thread=False)
        verify = sqlite3.connect(db_path)
        try:
            client = TestClient(_toggle_app(pool, route_conn))
            with _admin_auth():
                response = client.post(
                    "/admin/toggles",
                    json={"feature": TOGGLE_FEATURE, "enabled": True},
                    headers=_auth_headers(),
                )
                assert response.status_code == 200, (
                    f"toggle POST expected 200, got {response.status_code}"
                )
                rows = _audit_rows(verify, TOGGLE_FEATURE)
                assert len(rows) == 1, (
                    f"expected exactly one audit row for the toggle write, "
                    f"found {len(rows)}"
                )
                row = rows[0]
                assert row[0] == TOGGLE_FEATURE and row[1] == 1
                _assert_fingerprint_identity(row)
        finally:
            verify.close()
            route_conn.close()
            pool.close_all()


def test_maintenance_audit_row_never_contains_token():
    """C2/AC2: the maintenance audit row persists the same non-secret identity.

    PRESERVING pin: green at base and must stay green. POST /api/admin/
    maintenance must write its audit_toggle_log row with the scope
    fingerprint as user_id, exactly like the toggle route.
    """
    with _temp_db() as db_path:
        pool = SQLiteConnectionPool(db_path, max_size=2)
        verify = sqlite3.connect(db_path)
        try:
            service = MaintenanceService(pool)
            client = TestClient(_maintenance_app(service))
            with _admin_auth():
                response = client.post(
                    "/api/admin/maintenance",
                    json={"enabled": True, "reason": "audit identity pin"},
                    headers=_auth_headers(),
                )
                assert response.status_code == 200, (
                    f"maintenance POST expected 200, got {response.status_code}"
                )
                rows = _audit_rows(verify, "maintenance")
                assert len(rows) == 1, (
                    f"expected exactly one audit row for the maintenance write, "
                    f"found {len(rows)}"
                )
                row = rows[0]
                assert row[0] == "maintenance" and row[1] == 1
                _assert_fingerprint_identity(row)
        finally:
            verify.close()
            pool.close_all()


def test_audit_hmac_uses_fingerprint_identity():
    """C3/AC3: the audit HMAC is computed over the non-secret identity.

    PRESERVING pin: green at base and must stay green. The stored hmac_sha256
    must verify against the production message layout
    ``feature|int(enabled)|user_id|ip|timestamp`` with the secret manager's
    key over the PERSISTED user_id, and must NOT equal the digest of the
    counterfactual message that substitutes the raw bearer token — i.e. the
    signed payload does not depend on the raw token.
    """
    with _temp_db() as db_path:
        pool = SQLiteConnectionPool(db_path, max_size=2)
        route_conn = sqlite3.connect(db_path, check_same_thread=False)
        verify = sqlite3.connect(db_path)
        try:
            client = TestClient(_toggle_app(pool, route_conn))
            with _admin_auth():
                response = client.post(
                    "/admin/toggles",
                    json={"feature": TOGGLE_FEATURE, "enabled": True},
                    headers=_auth_headers(),
                )
                assert response.status_code == 200, (
                    f"toggle POST expected 200, got {response.status_code}"
                )
                rows = _audit_rows(verify, TOGGLE_FEATURE)
                assert len(rows) == 1
                feature, enabled, user_id, ip, timestamp, key_version, digest = (
                    rows[0]
                )
                _assert_fingerprint_identity(rows[0])

                message = (
                    f"{feature}|{int(enabled)}|{user_id or ''}|{ip or ''}|{timestamp}"
                )
                expected = hmac.new(
                    TEST_KEY, message.encode("utf-8"), hashlib.sha256
                ).hexdigest()
                assert digest == expected, (
                    "audit hmac_sha256 must verify over the persisted "
                    f"(fingerprint) identity with message {message!r}"
                )
                assert key_version == TEST_KEY_VERSION, (
                    "audit row key_version must equal the secret manager's "
                    f"version ({TEST_KEY_VERSION!r}), got {key_version!r}"
                )

                counterfactual = (
                    f"{feature}|{int(enabled)}|{ADMIN_TOKEN}|{ip or ''}|{timestamp}"
                )
                token_digest = hmac.new(
                    TEST_KEY, counterfactual.encode("utf-8"), hashlib.sha256
                ).hexdigest()
                assert digest != token_digest, (
                    "audit hmac_sha256 must not be derivable from the raw "
                    "bearer token: the signed payload depends only on the "
                    "non-secret fingerprint identity"
                )
        finally:
            verify.close()
            route_conn.close()
            pool.close_all()
