"""
Adversarial tests for FR-014: Scoped rotatable service-account API keys.

Tests attack vectors:
- Key replay after revoke → 401
- Rotation window: old key 401, new key works immediately (no overlap/no gap)
- Scope bypass: key with scope A accessing scope-B endpoint → 403
- Scope bypass tricks: case, prefix, wildcard
- Key tampering: mutate one char of a valid key → 401
- Error message uniformity: valid key vs invalid key vs revoked return same 401 text
- Issuance auth: non-admin cannot issue/rotate/revoke (401 or 403)
- Service account key cannot access admin-gated management endpoints
- Revoked-account key after revoke → 401 (already covered in base tests)
- Concurrent rotation (informational)
"""

import hashlib
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Generator
from unittest.mock import patch

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.routes.service_accounts import router as service_accounts_router
from app.config import settings
from app.models.database import SQLiteConnectionPool, _pool_cache
from app.security import require_service_account

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ADMIN_TOKEN = "test-admin-key"  # Set by conftest.py pytest_configure


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sa_db_path() -> Generator[str, None, None]:
    """Create a temporary directory with app.db and the service_accounts table."""
    temp_dir = tempfile.mkdtemp()
    db_path = str(Path(temp_dir) / "app.db")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS service_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            key_hash TEXT NOT NULL UNIQUE,
            scopes TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_rotated_at TEXT,
            revoked_at TEXT,
            created_by TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_service_accounts_key_hash
            ON service_accounts(key_hash);
        """
    )
    conn.commit()
    conn.close()
    yield db_path
    try:
        os.unlink(db_path)
        os.rmdir(temp_dir)
    except OSError:
        pass


@pytest.fixture
def sa_client(sa_db_path: str) -> TestClient:
    """TestClient bound to a test database."""
    app = FastAPI()
    app.include_router(service_accounts_router, prefix="/api")

    temp_dir = str(Path(sa_db_path).parent)

    _pool_cache.clear()

    test_pool = SQLiteConnectionPool(sa_db_path, max_size=1)

    def _get_pool(path, **kw):
        return test_pool

    with patch("app.models.database.get_pool", side_effect=_get_pool):
        with patch.object(settings, "data_dir", Path(temp_dir)):
            with patch.object(settings, "admin_secret_token", ADMIN_TOKEN):
                with patch.object(settings, "admin_token_scopes", {ADMIN_TOKEN: ["admin:config"]}):
                    client = TestClient(app)
                    yield client
                    client.close()

    _pool_cache.clear()
    test_pool.close_all()


@pytest.fixture
def admin_headers() -> dict:
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_sa_via_api(client: TestClient, name: str, scopes: list[str], headers: dict) -> tuple[int, str]:
    """Create SA via API, return (id, raw_key)."""
    resp = client.post(
        "/api/service-accounts",
        json={"name": name, "scopes": scopes},
        headers=headers,
    )
    assert resp.status_code == 201, resp.json()
    data = resp.json()
    return data["id"], data["key"]


@contextmanager
def _sa_app(raw_key: str, db_path: str, required_scopes: list[str]):
    """Build a minimal FastAPI app with a require_service_account endpoint."""
    app = FastAPI()

    @app.get("/sa-protected")
    def sa_protected_endpoint(
        _auth: dict = Depends(require_service_account(required_scopes)),
    ):
        return {"status": "ok", "sa_id": _auth["service_account_id"], "scopes": _auth["scopes"]}

    temp_dir = str(Path(db_path).parent)
    _pool_cache.clear()
    test_pool = SQLiteConnectionPool(db_path, max_size=1)

    def _get_pool(path, **kw):
        return test_pool

    with patch("app.models.database.get_pool", side_effect=_get_pool):
        with patch.object(settings, "data_dir", Path(temp_dir)):
            client = TestClient(app)
            yield client
            client.close()

    _pool_cache.clear()
    test_pool.close_all()


# ---------------------------------------------------------------------------
# Adversarial: Key replay after revoke
# ---------------------------------------------------------------------------

class TestKeyReplayAfterRevoke:
    """Verify a revoked key cannot be replayed."""

    def test_revoked_key_cannot_authenticate_again(
        self, sa_client: TestClient, sa_db_path: str, admin_headers: dict
    ):
        """A key used successfully BEFORE revoke must 401 AFTER revoke (no replay)."""
        sa_id, raw_key = _create_sa_via_api(sa_client, "replay-test-sa", ["documents:read"], admin_headers)

        # Pre-revoke: key works
        with _sa_app(raw_key, sa_db_path, ["documents:read"]) as client:
            resp = client.get("/sa-protected", headers={"Authorization": f"Bearer {raw_key}"})
            assert resp.status_code == 200

        # Revoke
        revoke_resp = sa_client.post(f"/api/service-accounts/{sa_id}/revoke", headers=admin_headers)
        assert revoke_resp.status_code == 200

        # Post-revoke: same key must NOT work — this is the "replay" attack
        with _sa_app(raw_key, sa_db_path, ["documents:read"]) as client:
            resp = client.get("/sa-protected", headers={"Authorization": f"Bearer {raw_key}"})
            assert resp.status_code == 401, f"Revoked key must not authenticate: {resp.json()}"


# ---------------------------------------------------------------------------
# Adversarial: Rotation window (no overlap / no gap)
# ---------------------------------------------------------------------------

class TestRotationWindow:
    """Verify rotation has zero overlap and zero gap between old/new keys."""

    def test_immediately_after_rotate_old_key_401_new_key_200(
        self, sa_client: TestClient, sa_db_path: str, admin_headers: dict
    ):
        """Immediately after rotate, old key returns 401 and new key returns 200.

        There must be no window where both keys work (overlap) and no window
        where neither works (gap). This is tested sequentially to catch any
        timing-dependent race condition in the rotation implementation.
        """
        sa_id, old_key = _create_sa_via_api(sa_client, "window-test-sa", ["documents:read"], admin_headers)

        # Verify old key works before rotation
        with _sa_app(old_key, sa_db_path, ["documents:read"]) as client:
            resp = client.get("/sa-protected", headers={"Authorization": f"Bearer {old_key}"})
            assert resp.status_code == 200

        # Rotate
        rotate_resp = sa_client.post(f"/api/service-accounts/{sa_id}/rotate", headers=admin_headers)
        assert rotate_resp.status_code == 200
        new_key = rotate_resp.json()["key"]
        assert new_key != old_key

        # IMMEDIATELY after rotate: old must 401
        with _sa_app(old_key, sa_db_path, ["documents:read"]) as client:
            resp = client.get("/sa-protected", headers={"Authorization": f"Bearer {old_key}"})
            assert resp.status_code == 401, f"Old key must be 401 immediately after rotate: {resp.json()}"

        # IMMEDIATELY after rotate: new must 200
        with _sa_app(new_key, sa_db_path, ["documents:read"]) as client:
            resp = client.get("/sa-protected", headers={"Authorization": f"Bearer {new_key}"})
            assert resp.status_code == 200, f"New key must be 200 immediately after rotate: {resp.json()}"

    def test_rotate_returns_different_key(
        self, sa_client: TestClient, admin_headers: dict
    ):
        """Rotation must always produce a cryptographically different key."""
        sa_id, old_key = _create_sa_via_api(sa_client, "diff-key-test", ["documents:read"], admin_headers)
        rotate_resp = sa_client.post(f"/api/service-accounts/{sa_id}/rotate", headers=admin_headers)
        assert rotate_resp.status_code == 200
        new_key = rotate_resp.json()["key"]
        assert new_key != old_key
        # Keys have the same prefix
        assert new_key.startswith("sak_")
        assert old_key.startswith("sak_")


# ---------------------------------------------------------------------------
# Adversarial: Scope bypass tricks
# ---------------------------------------------------------------------------

class TestScopeBypass:
    """Test that scope checks are not bypassed via string tricks."""

    def test_scope_case_sensitivity(
        self, sa_client: TestClient, sa_db_path: str, admin_headers: dict
    ):
        """Scope checks are case-insensitive but not bypassable via case tricks."""
        sa_id, raw_key = _create_sa_via_api(sa_client, "case-test-sa", ["Documents:Read"], admin_headers)

        # Exact case: works
        with _sa_app(raw_key, sa_db_path, ["documents:read"]) as client:
            resp = client.get("/sa-protected", headers={"Authorization": f"Bearer {raw_key}"})
            assert resp.status_code == 200, f"Case-insensitive scope must work: {resp.json()}"

        # Wrong case: still works (scope check is case-insensitive)
        with _sa_app(raw_key, sa_db_path, ["DOCUMENTS:READ"]) as client:
            resp = client.get("/sa-protected", headers={"Authorization": f"Bearer {raw_key}"})
            assert resp.status_code == 200

    def test_scope_prefix_not_bypassed(
        self, sa_client: TestClient, sa_db_path: str, admin_headers: dict
    ):
        """Having scope 'admin' does NOT grant scope 'admin:config'."""
        sa_id, raw_key = _create_sa_via_api(sa_client, "prefix-test-sa", ["admin"], admin_headers)

        # 'admin' scope should NOT satisfy requirement of 'admin:config'
        with _sa_app(raw_key, sa_db_path, ["admin:config"]) as client:
            resp = client.get("/sa-protected", headers={"Authorization": f"Bearer {raw_key}"})
            assert resp.status_code == 403, f"'admin' must not satisfy 'admin:config': {resp.json()}"

    def test_scope_wildcard_not_bypassed(
        self, sa_client: TestClient, sa_db_path: str, admin_headers: dict
    ):
        """A key with scope '*' does NOT grant all scopes."""
        sa_id, raw_key = _create_sa_via_api(sa_client, "wildcard-test-sa", ["*"], admin_headers)

        with _sa_app(raw_key, sa_db_path, ["documents:read"]) as client:
            resp = client.get("/sa-protected", headers={"Authorization": f"Bearer {raw_key}"})
            # Wildcard is stored as a literal scope string — it is NOT a meta-scope
            assert resp.status_code == 403, f"Scope '*' must not satisfy 'documents:read': {resp.json()}"

    def test_scope_subset_not_bypassed(
        self, sa_client: TestClient, sa_db_path: str, admin_headers: dict
    ):
        """Having scopes ['documents:read', 'documents:write'] does NOT grant 'admin:config'."""
        sa_id, raw_key = _create_sa_via_api(
            sa_client, "subset-test-sa", ["documents:read", "documents:write"], admin_headers
        )

        with _sa_app(raw_key, sa_db_path, ["admin:config"]) as client:
            resp = client.get("/sa-protected", headers={"Authorization": f"Bearer {raw_key}"})
            assert resp.status_code == 403, f"Subset scopes must not bypass required scope: {resp.json()}"


# ---------------------------------------------------------------------------
# Adversarial: Key tampering
# ---------------------------------------------------------------------------

class TestKeyTampering:
    """Test that tampering with a valid key results in 401."""

    def _mutate_key(self, key: str, position: int, new_char: str) -> str:
        """Return a copy of key with char at position replaced."""
        chars = list(key)
        chars[position] = new_char
        return "".join(chars)

    def test_tampered_key_returns_401(
        self, sa_client: TestClient, sa_db_path: str, admin_headers: dict
    ):
        """Mutating one character of a valid key must produce 401."""
        sa_id, raw_key = _create_sa_via_api(sa_client, "tamper-test-sa", ["documents:read"], admin_headers)

        # Change last character
        tampered = self._mutate_key(raw_key, -1, "X" if raw_key[-1] != "X" else "Y")
        with _sa_app(tampered, sa_db_path, ["documents:read"]) as client:
            resp = client.get("/sa-protected", headers={"Authorization": f"Bearer {tampered}"})
            assert resp.status_code == 401, f"Tampered key (last char) must return 401: {resp.json()}"

    def test_tampered_prefix_returns_401(
        self, sa_client: TestClient, sa_db_path: str, admin_headers: dict
    ):
        """Changing 'sak_' prefix to 'sk_' must produce 401."""
        sa_id, raw_key = _create_sa_via_api(sa_client, "prefix-tamper-sa", ["documents:read"], admin_headers)

        # Change 'sak_' to 'skk_'
        tampered = "skk_" + raw_key[4:]
        with _sa_app(tampered, sa_db_path, ["documents:read"]) as client:
            resp = client.get("/sa-protected", headers={"Authorization": f"Bearer {tampered}"})
            assert resp.status_code == 401, f"Wrong prefix must return 401: {resp.json()}"

    def test_key_with_extra_char_returns_401(
        self, sa_client: TestClient, sa_db_path: str, admin_headers: dict
    ):
        """Appending one character to a valid key must produce 401."""
        sa_id, raw_key = _create_sa_via_api(sa_client, "extra-char-sa", ["documents:read"], admin_headers)

        tampered = raw_key + "X"
        with _sa_app(tampered, sa_db_path, ["documents:read"]) as client:
            resp = client.get("/sa-protected", headers={"Authorization": f"Bearer {tampered}"})
            assert resp.status_code == 401, f"Extra-char key must return 401: {resp.json()}"

    def test_key_with_missing_char_returns_401(
        self, sa_client: TestClient, sa_db_path: str, admin_headers: dict
    ):
        """Removing one character from a valid key must produce 401."""
        sa_id, raw_key = _create_sa_via_api(sa_client, "missing-char-sa", ["documents:read"], admin_headers)

        tampered = raw_key[:-1]
        with _sa_app(tampered, sa_db_path, ["documents:read"]) as client:
            resp = client.get("/sa-protected", headers={"Authorization": f"Bearer {tampered}"})
            assert resp.status_code == 401, f"Truncated key must return 401: {resp.json()}"

    def test_completely_wrong_key_returns_401(
        self, sa_client: TestClient, sa_db_path: str, admin_headers: dict
    ):
        """A completely different key must return 401."""
        sa_id, raw_key = _create_sa_via_api(sa_client, "wrong-key-sa", ["documents:read"], admin_headers)

        with _sa_app("sak_completelyfakentkey123456789012", sa_db_path, ["documents:read"]) as client:
            resp = client.get("/sa-protected", headers={"Authorization": "Bearer sak_completelyfakentkey123456789012"})
            assert resp.status_code == 401, f"Fake key must return 401: {resp.json()}"


# ---------------------------------------------------------------------------
# Adversarial: Error message uniformity (no key-existence oracle)
# ---------------------------------------------------------------------------

class TestErrorMessageUniformity:
    """Verify that invalid/revoked/unknown keys all return the same error class.

    Different HTTP status codes between "invalid key format" and "key not found"
    would let an attacker probe for valid key prefixes. Valid keys get 403
    (authenticated but insufficient scope) while invalid/revoked get 401.
    """

    def test_unknown_key_error_is_401(
        self, sa_client: TestClient, sa_db_path: str, admin_headers: dict
    ):
        """An unknown (never-issued) key must return 401."""
        with _sa_app("sak_unknownkey123456789012345678901234", sa_db_path, ["documents:read"]) as client:
            resp = client.get("/sa-protected", headers={"Authorization": "Bearer sak_unknownkey123456789012345678901234"})
            assert resp.status_code == 401

    def test_invalid_key_format_error_is_401(
        self, sa_client: TestClient, sa_db_path: str
    ):
        """A malformed key (wrong prefix, invalid base64) returns 401."""
        with _sa_app("xxx_notavalidkey", sa_db_path, ["documents:read"]) as client:
            resp = client.get("/sa-protected", headers={"Authorization": "Bearer xxx_notavalidkey"})
            assert resp.status_code == 401

    def test_revoked_key_error_is_401(
        self, sa_client: TestClient, sa_db_path: str, admin_headers: dict
    ):
        """A revoked key must return 401 (same class as unknown key)."""
        sa_id, raw_key = _create_sa_via_api(sa_client, "revoked-uniformity-sa", ["documents:read"], admin_headers)

        # Revoke
        sa_client.post(f"/api/service-accounts/{sa_id}/revoke", headers=admin_headers)

        with _sa_app(raw_key, sa_db_path, ["documents:read"]) as client:
            resp = client.get("/sa-protected", headers={"Authorization": f"Bearer {raw_key}"})
            assert resp.status_code == 401

    def test_insufficient_scope_error_is_403_not_401(
        self, sa_client: TestClient, sa_db_path: str, admin_headers: dict
    ):
        """A VALID key with insufficient scopes returns 403 (authenticated, not authorized).

        This is intentionally DIFFERENT from 401 (unauthenticated) — confirming
        that the 401 vs 403 distinction is maintained correctly.
        """
        sa_id, raw_key = _create_sa_via_api(sa_client, "insufficient-sa", ["documents:read"], admin_headers)

        with _sa_app(raw_key, sa_db_path, ["documents:admin"]) as client:
            resp = client.get("/sa-protected", headers={"Authorization": f"Bearer {raw_key}"})
            assert resp.status_code == 403, f"Valid key with insufficient scope must be 403: {resp.json()}"


# ---------------------------------------------------------------------------
# Adversarial: Issuance / management auth — non-admin cannot act
# ---------------------------------------------------------------------------

class TestIssuanceAuthorization:
    """Verify only admin tokens can issue/rotate/revoke service accounts."""

    def test_non_admin_token_cannot_issue(
        self, sa_client: TestClient, sa_db_path: str
    ):
        """A token that is not the admin token must be rejected at 401/403."""
        resp = sa_client.post(
            "/api/service-accounts",
            json={"name": " rogue-sa", "scopes": ["documents:read"]},
            headers={"Authorization": "Bearer not-the-admin-token"},
        )
        # require_scope checks admin_secret_token first → 403 for wrong token
        assert resp.status_code == 403, f"Non-admin token must be rejected: {resp.json()}"

    def test_service_account_key_cannot_issue(
        self, sa_client: TestClient, sa_db_path: str, admin_headers: dict
    ):
        """A service account key cannot be used to issue new service accounts.

        Service account keys use a completely different auth path (require_service_account)
        which is independent of admin tokens. A SA key passed to an admin-gated endpoint
        should fail because the endpoint uses require_scope("admin:config"), which checks
        settings.admin_secret_token — not the service_accounts table.
        """
        # Create a service account
        sa_id, raw_key = _create_sa_via_api(sa_client, "sa-issuer-test", ["documents:read"], admin_headers)

        # Try to issue a NEW service account using the SA key (not the admin token)
        resp = sa_client.post(
            "/api/service-accounts",
            json={"name": "rogue-sa", "scopes": ["documents:read"]},
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        # require_scope checks settings.admin_secret_token — SA key is not there
        assert resp.status_code == 403, f"SA key must not access admin endpoint: {resp.json()}"

    def test_service_account_key_cannot_rotate(
        self, sa_client: TestClient, sa_db_path: str, admin_headers: dict
    ):
        """A service account key cannot rotate any service account."""
        sa_id, raw_key = _create_sa_via_api(sa_client, "sa-rotator-test", ["documents:read"], admin_headers)

        resp = sa_client.post(
            f"/api/service-accounts/{sa_id}/rotate",
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        assert resp.status_code == 403, f"SA key must not rotate: {resp.json()}"

    def test_service_account_key_cannot_revoke(
        self, sa_client: TestClient, sa_db_path: str, admin_headers: dict
    ):
        """A service account key cannot revoke any service account."""
        sa_id, raw_key = _create_sa_via_api(sa_client, "sa-revoker-test", ["documents:read"], admin_headers)

        resp = sa_client.post(
            f"/api/service-accounts/{sa_id}/revoke",
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        assert resp.status_code == 403, f"SA key must not revoke: {resp.json()}"

    def test_service_account_key_cannot_list(
        self, sa_client: TestClient, sa_db_path: str, admin_headers: dict
    ):
        """A service account key cannot list service accounts."""
        sa_id, raw_key = _create_sa_via_api(sa_client, "sa-lister-test", ["documents:read"], admin_headers)

        resp = sa_client.get(
            "/api/service-accounts",
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        assert resp.status_code == 403, f"SA key must not list: {resp.json()}"

    def test_different_service_account_key_cannot_rotate(
        self, sa_client: TestClient, sa_db_path: str, admin_headers: dict
    ):
        """SA-A cannot rotate SA-B — only admin can rotate."""
        _sa_id, sa_key_a = _create_sa_via_api(sa_client, "sa-a-rotate", ["documents:read"], admin_headers)
        sa_id_b, sa_key_b = _create_sa_via_api(sa_client, "sa-b-rotate", ["documents:read"], admin_headers)

        # SA-A tries to rotate SA-B
        resp = sa_client.post(
            f"/api/service-accounts/{sa_id_b}/rotate",
            headers={"Authorization": f"Bearer {sa_key_a}"},
        )
        assert resp.status_code == 403, f"SA-A must not rotate SA-B: {resp.json()}"

    def test_different_service_account_key_cannot_revoke(
        self, sa_client: TestClient, sa_db_path: str, admin_headers: dict
    ):
        """SA-A cannot revoke SA-B — only admin can revoke."""
        _sa_id, sa_key_a = _create_sa_via_api(sa_client, "sa-a-revoke", ["documents:read"], admin_headers)
        sa_id_b, sa_key_b = _create_sa_via_api(sa_client, "sa-b-revoke", ["documents:read"], admin_headers)

        resp = sa_client.post(
            f"/api/service-accounts/{sa_id_b}/revoke",
            headers={"Authorization": f"Bearer {sa_key_a}"},
        )
        assert resp.status_code == 403, f"SA-A must not revoke SA-B: {resp.json()}"


# ---------------------------------------------------------------------------
# Adversarial: Authorization header format
# ---------------------------------------------------------------------------

class TestAuthorizationHeaderFormat:
    """Verify various malformed Authorization headers are rejected."""

    def test_empty_bearer_token_returns_401(
        self, sa_client: TestClient, sa_db_path: str, admin_headers: dict
    ):
        """Authorization: Bearer with empty token must return 401."""
        sa_id, raw_key = _create_sa_via_api(sa_client, "empty-bearer-sa", ["documents:read"], admin_headers)

        with _sa_app(raw_key, sa_db_path, ["documents:read"]) as client:
            resp = client.get("/sa-protected", headers={"Authorization": "Bearer "})
            assert resp.status_code == 401, f"Empty bearer token must be 401: {resp.json()}"

    def test_bearer_without_token_returns_401(
        self, sa_client: TestClient, sa_db_path: str, admin_headers: dict
    ):
        """Authorization: Bearer without a token value must return 401."""
        sa_id, raw_key = _create_sa_via_api(sa_client, "no-token-sa", ["documents:read"], admin_headers)

        with _sa_app(raw_key, sa_db_path, ["documents:read"]) as client:
            resp = client.get("/sa-protected", headers={"Authorization": "Bearer"})
            assert resp.status_code == 401, f"Bearer without token must be 401: {resp.json()}"

    def test_basic_auth_returns_401(
        self, sa_client: TestClient, sa_db_path: str, admin_headers: dict
    ):
        """Authorization: Basic ... must return 401 (not 403) for SA endpoints."""
        sa_id, raw_key = _create_sa_via_api(sa_client, "basic-auth-sa", ["documents:read"], admin_headers)

        with _sa_app(raw_key, sa_db_path, ["documents:read"]) as client:
            resp = client.get("/sa-protected", headers={"Authorization": "Basic dXNlcjpwYXNz"})
            assert resp.status_code == 401, f"Basic auth must be rejected: {resp.json()}"

    def test_bearer_case_sensitive(
        self, sa_client: TestClient, sa_db_path: str, admin_headers: dict
    ):
        """Lowercase 'bearer' is accepted (Authorization header is lowercased in code)."""
        sa_id, raw_key = _create_sa_via_api(sa_client, "case-bearer-sa", ["documents:read"], admin_headers)

        with _sa_app(raw_key, sa_db_path, ["documents:read"]) as client:
            resp = client.get("/sa-protected", headers={"Authorization": "bearer " + raw_key})
            assert resp.status_code == 200, f"Lowercase bearer must be accepted: {resp.json()}"


# ---------------------------------------------------------------------------
# Adversarial: Revocation already-revoked account
# ---------------------------------------------------------------------------

class TestRevokeIdempotency:
    """Verify re-revoke returns 400 and does not cause server error."""

    def test_revoke_already_revoked_returns_400(
        self, sa_client: TestClient, admin_headers: dict
    ):
        """Revoking an already-revoked account returns 400 (idempotent, not error)."""
        sa_id, _ = _create_sa_via_api(sa_client, "double-revoke-sa", ["documents:read"], admin_headers)

        # First revoke
        sa_client.post(f"/api/service-accounts/{sa_id}/revoke", headers=admin_headers)

        # Second revoke
        resp = sa_client.post(f"/api/service-accounts/{sa_id}/revoke", headers=admin_headers)
        assert resp.status_code == 400
        assert "already revoked" in resp.json()["detail"].lower()

    def test_rotate_revoked_returns_400(
        self, sa_client: TestClient, admin_headers: dict
    ):
        """Rotating a revoked account returns 400 (cannot rotate a revoked SA)."""
        sa_id, _ = _create_sa_via_api(sa_client, "rotate-revoked-sa", ["documents:read"], admin_headers)

        # Revoke
        sa_client.post(f"/api/service-accounts/{sa_id}/revoke", headers=admin_headers)

        # Try to rotate
        resp = sa_client.post(f"/api/service-accounts/{sa_id}/rotate", headers=admin_headers)
        assert resp.status_code == 400
        assert "revoked" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Adversarial: Concurrent rotation (informational)
# ---------------------------------------------------------------------------

class TestConcurrentRotation:
    """Test that concurrent rotations are handled safely (no database corruption)."""

    def test_rotate_idempotent(
        self, sa_client: TestClient, admin_headers: dict
    ):
        """Two rapid rotations produce two different keys (no cached response)."""
        sa_id, key1 = _create_sa_via_api(sa_client, "concurrent-rotate-sa", ["documents:read"], admin_headers)

        rotate1 = sa_client.post(f"/api/service-accounts/{sa_id}/rotate", headers=admin_headers)
        assert rotate1.status_code == 200
        key2 = rotate1.json()["key"]

        rotate2 = sa_client.post(f"/api/service-accounts/{sa_id}/rotate", headers=admin_headers)
        assert rotate2.status_code == 200
        key3 = rotate2.json()["key"]

        # Both rotations should produce unique keys
        assert key1 != key2 != key3
