"""
Integration tests for FR-014: Scoped rotatable service-account API keys.

Tests cover:
- Service account issuance (raw key returned once, only hash stored)
- Service-account-gated endpoint access with valid key
- Rotation: old key stops working, new key works
- Revocation: key stops working immediately
- Scope enforcement: insufficient scopes → 403
- List endpoint: raw keys never returned
- Invalid/unknown key → 401
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
from app.models.database import get_pool
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
    """Create a temporary directory with app.db (the expected sqlite_path).

    This fixture creates a temp directory so that settings.sqlite_path
    (which returns data_dir / "app.db") points to our test database.
    """
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
def sa_app(sa_db_path: str) -> FastAPI:
    """Build a minimal FastAPI app with the service_accounts router."""
    app = FastAPI()
    app.include_router(service_accounts_router, prefix="/api")
    return app


@pytest.fixture
def sa_client(sa_app: FastAPI, sa_db_path: str) -> TestClient:
    """TestClient bound to a test database.

    Patches settings.data_dir so that settings.sqlite_path computes to sa_db_path.
    Clears the pool cache and installs a pool that uses the test database.
    """
    from app.models.database import SQLiteConnectionPool, _pool_cache

    temp_dir = str(Path(sa_db_path).parent)

    _pool_cache.clear()

    test_pool = SQLiteConnectionPool(sa_db_path, max_size=1)

    def _get_pool(path, **kw):
        return test_pool

    with patch("app.models.database.get_pool", side_effect=_get_pool):
        with patch.object(settings, "data_dir", Path(temp_dir)):
            with patch.object(settings, "admin_secret_token", ADMIN_TOKEN):
                with patch.object(settings, "admin_token_scopes", {ADMIN_TOKEN: ["admin:config"]}):
                    client = TestClient(sa_app)
                    yield client
                    client.close()

    _pool_cache.clear()
    test_pool.close_all()


@pytest.fixture
def admin_headers() -> dict:
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


# ---------------------------------------------------------------------------
# Test: issue service account (raw key returned once, only hash stored)
# ---------------------------------------------------------------------------


class TestServiceAccountIssue:
    """Tests for POST /api/service-accounts."""

    def test_issue_returns_raw_key_once(
        self, sa_client: TestClient, admin_headers: dict
    ):
        """Verify issuance returns the raw key and it has the expected prefix."""
        response = sa_client.post(
            "/api/service-accounts",
            json={"name": "test-sa", "scopes": ["documents:read"]},
            headers=admin_headers,
        )
        assert response.status_code == 201, response.json()
        data = response.json()
        assert "key" in data
        assert data["key"].startswith("sak_")
        assert len(data["key"]) > 20  # cryptographically random

    def test_issue_key_is_hash_stored_not_plaintext(
        self, sa_client: TestClient, sa_db_path: str, admin_headers: dict
    ):
        """Verify the raw key is NOT in the database — only its sha256 hash."""
        response = sa_client.post(
            "/api/service-accounts",
            json={"name": "hash-check-sa", "scopes": ["documents:read"]},
            headers=admin_headers,
        )
        assert response.status_code == 201, response.json()
        raw_key = response.json()["key"]

        # Check database: raw key must not appear
        conn = sqlite3.connect(sa_db_path)
        rows = conn.execute(
            "SELECT key_hash FROM service_accounts WHERE name = ?",
            ("hash-check-sa",),
        ).fetchall()
        conn.close()
        assert len(rows) == 1
        key_hash = rows[0][0]
        # The hash must be sha256 of the raw key
        expected_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        assert key_hash == expected_hash
        # Raw key must NOT be stored
        assert raw_key not in key_hash

    def test_issue_requires_auth(self, sa_client: TestClient):
        """Verify issuance requires the admin Bearer token."""
        response = sa_client.post(
            "/api/service-accounts",
            json={"name": "test-sa", "scopes": ["documents:read"]},
        )
        assert response.status_code == 401

    def test_issue_requires_admin_scope(self, sa_client: TestClient):
        """Verify issuance requires admin:config scope."""
        # Token is valid but has wrong scope
        with patch.object(settings, "admin_secret_token", ADMIN_TOKEN):
            with patch.object(settings, "admin_token_scopes", {ADMIN_TOKEN: ["other:scope"]}):
                response = sa_client.post(
                    "/api/service-accounts",
                    json={"name": "test-sa", "scopes": ["documents:read"]},
                    headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
                )
        assert response.status_code == 403

    def test_issue_validates_empty_name(self, sa_client: TestClient, admin_headers: dict):
        """Verify empty name is rejected."""
        response = sa_client.post(
            "/api/service-accounts",
            json={"name": "   ", "scopes": ["documents:read"]},
            headers=admin_headers,
        )
        assert response.status_code == 400
        assert "name" in response.json()["detail"].lower()

    def test_issue_validates_empty_scopes(self, sa_client: TestClient, admin_headers: dict):
        """Verify empty scopes list is rejected."""
        response = sa_client.post(
            "/api/service-accounts",
            json={"name": "test-sa", "scopes": []},
            headers=admin_headers,
        )
        assert response.status_code == 400
        assert "scope" in response.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Test: list service accounts (keys never returned)
# ---------------------------------------------------------------------------


class TestServiceAccountList:
    """Tests for GET /api/service-accounts."""

    def test_list_returns_no_keys(
        self, sa_client: TestClient, admin_headers: dict
    ):
        """Verify list never returns raw keys."""
        # Create a service account first
        create_resp = sa_client.post(
            "/api/service-accounts",
            json={"name": "list-test-sa", "scopes": ["documents:read"]},
            headers=admin_headers,
        )
        assert create_resp.status_code == 201
        raw_key = create_resp.json()["key"]

        # List
        list_resp = sa_client.get("/api/service-accounts", headers=admin_headers)
        assert list_resp.status_code == 200
        accounts = list_resp.json()
        assert len(accounts) >= 1
        for account in accounts:
            assert "key" not in account
            assert "key_hash" not in account
            assert "name" in account
            assert "scopes" in account
        # Raw key must not appear anywhere in the response
        resp_text = list_resp.text
        assert raw_key not in resp_text

    def test_list_returns_metadata(
        self, sa_client: TestClient, admin_headers: dict
    ):
        """Verify list returns id, name, scopes, timestamps."""
        sa_client.post(
            "/api/service-accounts",
            json={"name": "meta-test-sa", "scopes": ["documents:read", "documents:write"]},
            headers=admin_headers,
        )
        list_resp = sa_client.get("/api/service-accounts", headers=admin_headers)
        assert list_resp.status_code == 200
        accounts = [a for a in list_resp.json() if a["name"] == "meta-test-sa"]
        assert len(accounts) == 1
        account = accounts[0]
        assert account["id"] is not None
        assert account["name"] == "meta-test-sa"
        assert set(account["scopes"]) == {"documents:read", "documents:write"}
        assert account["created_at"] is not None
        assert account["revoked_at"] is None  # Not revoked yet


# ---------------------------------------------------------------------------
# Test: rotate service account key
# ---------------------------------------------------------------------------


class TestServiceAccountRotate:
    """Tests for POST /api/service-accounts/{id}/rotate."""

    def _create_sa(self, client: TestClient, headers: dict) -> tuple[int, str]:
        """Helper: create a service account and return (id, raw_key)."""
        resp = client.post(
            "/api/service-accounts",
            json={"name": "rotate-test-sa", "scopes": ["documents:read"]},
            headers=headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        return data["id"], data["key"]

    def test_rotate_returns_new_key(
        self, sa_client: TestClient, admin_headers: dict
    ):
        """Verify rotation returns a new raw key."""
        sa_id, _ = self._create_sa(sa_client, admin_headers)
        rotate_resp = sa_client.post(
            f"/api/service-accounts/{sa_id}/rotate",
            headers=admin_headers,
        )
        assert rotate_resp.status_code == 200, rotate_resp.json()
        data = rotate_resp.json()
        assert "key" in data
        assert data["key"].startswith("sak_")

    def test_rotate_invalidates_old_key(
        self, sa_client: TestClient, sa_db_path: str, admin_headers: dict
    ):
        """Verify the old key is immediately invalidated after rotation."""
        sa_id, old_key = self._create_sa(sa_client, admin_headers)

        # Use old key on a service-account-protected endpoint — should work BEFORE rotation
        with self._sa_protected_app(old_key, sa_db_path) as client:
            resp = client.get(
                "/sa-protected", headers={"Authorization": f"Bearer {old_key}"}
            )
            assert resp.status_code == 200, f"Old key should work before rotation: {resp.json()}"

        # Rotate
        rotate_resp = sa_client.post(
            f"/api/service-accounts/{sa_id}/rotate",
            headers=admin_headers,
        )
        assert rotate_resp.status_code == 200
        new_key = rotate_resp.json()["key"]

        # Old key should now 401
        with self._sa_protected_app(old_key, sa_db_path) as client:
            resp = client.get(
                "/sa-protected", headers={"Authorization": f"Bearer {old_key}"}
            )
            assert resp.status_code == 401, f"Old key should be invalid after rotation: {resp.json()}"

        # New key should work
        with self._sa_protected_app(new_key, sa_db_path) as client:
            resp = client.get(
                "/sa-protected", headers={"Authorization": f"Bearer {new_key}"}
            )
            assert resp.status_code == 200, f"New key should work: {resp.json()}"

    def test_rotate_nonexistent_returns_404(
        self, sa_client: TestClient, admin_headers: dict
    ):
        """Verify rotating a nonexistent service account returns 404."""
        response = sa_client.post(
            "/api/service-accounts/99999/rotate",
            headers=admin_headers,
        )
        assert response.status_code == 404
    @contextmanager
    def _sa_protected_app(self, raw_key: str, db_path: str):
        """Build a minimal FastAPI app protected by require_service_account.

        Yields so the patch stays active for the lifetime of the TestClient.
        """
        from app.models.database import SQLiteConnectionPool, _pool_cache

        app = FastAPI()

        @app.get("/sa-protected")
        def sa_protected_endpoint(
            _auth: dict = Depends(require_service_account(["documents:read"])),
        ):
            return {"status": "ok", "sa_id": _auth["service_account_id"]}

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
# Test: revoke service account
# ---------------------------------------------------------------------------


class TestServiceAccountRevoke:
    """Tests for POST /api/service-accounts/{id}/revoke."""

    def _create_sa(self, client: TestClient, headers: dict) -> tuple[int, str]:
        """Helper: create a service account and return (id, raw_key)."""
        resp = client.post(
            "/api/service-accounts",
            json={"name": "revoke-test-sa", "scopes": ["documents:read"]},
            headers=headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        return data["id"], data["key"]

    def test_revoke_invalidates_key_immediately(
        self, sa_client: TestClient, sa_db_path: str, admin_headers: dict
    ):
        """Verify the key stops working immediately after revocation."""
        sa_id, raw_key = self._create_sa(sa_client, admin_headers)

        # Key should work before revocation
        with self._sa_protected_app(raw_key, sa_db_path) as client:
            resp = client.get(
                "/sa-protected", headers={"Authorization": f"Bearer {raw_key}"}
            )
            assert resp.status_code == 200

        # Revoke
        revoke_resp = sa_client.post(
            f"/api/service-accounts/{sa_id}/revoke",
            headers=admin_headers,
        )
        assert revoke_resp.status_code == 200
        assert revoke_resp.json()["id"] == sa_id
        assert revoke_resp.json()["revoked_at"] is not None

        # Key must 401 immediately after revocation
        with self._sa_protected_app(raw_key, sa_db_path) as client:
            resp = client.get(
                "/sa-protected", headers={"Authorization": f"Bearer {raw_key}"}
            )
            assert resp.status_code == 401, f"Key should be invalid after revocation: {resp.json()}"

    def test_revoke_twice_returns_400(
        self, sa_client: TestClient, admin_headers: dict
    ):
        """Verify revoking an already-revoked account returns 400."""
        sa_id, _ = self._create_sa(sa_client, admin_headers)
        sa_client.post(
            f"/api/service-accounts/{sa_id}/revoke",
            headers=admin_headers,
        )
        response = sa_client.post(
            f"/api/service-accounts/{sa_id}/revoke",
            headers=admin_headers,
        )
        assert response.status_code == 400
        assert "already revoked" in response.json()["detail"].lower()

    def test_revoke_nonexistent_returns_404(
        self, sa_client: TestClient, admin_headers: dict
    ):
        """Verify revoking a nonexistent service account returns 404."""
        response = sa_client.post(
            "/api/service-accounts/99999/revoke",
            headers=admin_headers,
        )
        assert response.status_code == 404
    @contextmanager
    def _sa_protected_app(self, raw_key: str, db_path: str):
        """Build a minimal FastAPI app protected by require_service_account.

        Yields so the patch stays active for the lifetime of the TestClient.
        """
        from app.models.database import SQLiteConnectionPool, _pool_cache

        app = FastAPI()

        @app.get("/sa-protected")
        def sa_protected_endpoint(
            _auth: dict = Depends(require_service_account(["documents:read"])),
        ):
            return {"status": "ok", "sa_id": _auth["service_account_id"]}

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
# Test: require_service_account dependency (scope enforcement, 401, 403)
# ---------------------------------------------------------------------------


class TestRequireServiceAccount:
    """Unit-style tests for require_service_account dependency."""

    @contextmanager
    def _sa_protected_app(self, raw_key: str, db_path: str):
        """Build a minimal FastAPI app with a require_service_account endpoint.

        Yields so the patch stays active for the lifetime of the TestClient.
        """
        from app.models.database import SQLiteConnectionPool, _pool_cache

        app = FastAPI()

        @app.get("/sa-protected")
        def sa_protected_endpoint(
            _auth: dict = Depends(require_service_account(["documents:read", "documents:write"])),
        ):
            return {
                "status": "ok",
                "sa_id": _auth["service_account_id"],
                "scopes": _auth["scopes"],
            }

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

    def _create_sa_direct(
        self, db_path: str, name: str, scopes: list[str]
    ) -> tuple[int, str]:
        """Create a service account directly in the DB and return (id, raw_key)."""
        import secrets

        raw_key = f"sak_{secrets.token_urlsafe(32)}"
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        scopes_str = ",".join(scopes)

        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            """
            INSERT INTO service_accounts (name, key_hash, scopes, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (name, key_hash, scopes_str, now),
        )
        conn.commit()
        sa_id = cursor.lastrowid
        conn.close()
        return sa_id, raw_key

    def test_valid_key_with_sufficient_scope_returns_200(
        self, sa_db_path: str
    ):
        """Verify a valid key with sufficient scopes grants access."""
        sa_id, raw_key = self._create_sa_direct(
            sa_db_path, "scope-test-sa", ["documents:read", "documents:write"]
        )
        with self._sa_protected_app(raw_key, sa_db_path) as client:
            resp = client.get(
                "/sa-protected", headers={"Authorization": f"Bearer {raw_key}"}
            )
            assert resp.status_code == 200, resp.json()
            assert resp.json()["sa_id"] == sa_id

    def test_valid_key_with_insufficient_scope_returns_403(
        self, sa_db_path: str
    ):
        """Verify a valid key with insufficient scopes returns 403."""
        # SA has only documents:read, endpoint requires documents:read + documents:write
        sa_id, raw_key = self._create_sa_direct(
            sa_db_path, "insufficient-scope-sa", ["documents:read"]
        )
        with self._sa_protected_app(raw_key, sa_db_path) as client:
            resp = client.get(
                "/sa-protected", headers={"Authorization": f"Bearer {raw_key}"}
            )
            assert resp.status_code == 403, resp.json()
            assert "Insufficient" in resp.json()["detail"]

    def test_invalid_key_returns_401(self, sa_db_path: str):
        """Verify an unknown/invalid key returns 401."""
        with self._sa_protected_app("sak_invalidkey123456789", sa_db_path) as client:
            resp = client.get(
                "/sa-protected", headers={"Authorization": "Bearer sak_invalidkey123456789"}
            )
            assert resp.status_code == 401, resp.json()
            assert "Invalid" in resp.json()["detail"] or "unknown" in resp.json()["detail"].lower()

    def test_revoked_key_returns_401(self, sa_db_path: str):
        """Verify a revoked key returns 401."""
        sa_id, raw_key = self._create_sa_direct(
            sa_db_path, "revoked-test-sa", ["documents:read"]
        )
        # Revoke it directly in the DB
        from datetime import datetime, timezone

        conn = sqlite3.connect(sa_db_path)
        conn.execute(
            "UPDATE service_accounts SET revoked_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), sa_id),
        )
        conn.commit()
        conn.close()

        with self._sa_protected_app(raw_key, sa_db_path) as client:
            resp = client.get(
                "/sa-protected", headers={"Authorization": f"Bearer {raw_key}"}
            )
            assert resp.status_code == 401, resp.json()
            assert "revoked" in resp.json()["detail"].lower()

    def test_missing_authorization_header_returns_401(self, sa_db_path: str):
        """Verify a missing Authorization header returns 401."""
        from app.models.database import SQLiteConnectionPool

        app = FastAPI()

        @app.get("/sa-protected")
        def sa_protected_endpoint(
            _auth: dict = Depends(require_service_account(["documents:read"])),
        ):
            return {"status": "ok"}

        test_pool = SQLiteConnectionPool(sa_db_path, max_size=1)
        with patch("app.models.database.get_pool", side_effect=lambda path, **kw: test_pool):
            client = TestClient(app)
            resp = client.get("/sa-protected")  # No auth header
            assert resp.status_code == 401

    def test_non_bearer_authorization_returns_401(self, sa_db_path: str):
        """Verify a non-Bearer Authorization header returns 401."""
        from app.models.database import SQLiteConnectionPool

        app = FastAPI()

        @app.get("/sa-protected")
        def sa_protected_endpoint(
            _auth: dict = Depends(require_service_account(["documents:read"])),
        ):
            return {"status": "ok"}

        test_pool = SQLiteConnectionPool(sa_db_path, max_size=1)
        with patch("app.models.database.get_pool", side_effect=lambda path, **kw: test_pool):
            client = TestClient(app)
            resp = client.get(
                "/sa-protected",
                headers={"Authorization": "Basic dXNlcjpwYXNz"},
            )
            assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Test: service-account auth is independent of human JWT auth
# ---------------------------------------------------------------------------


class TestServiceAccountIndependence:
    """Verify service accounts are independent of human user authentication."""

    def test_service_account_auth_works_when_users_enabled_true(
        self, sa_client: TestClient, sa_db_path: str, admin_headers: dict
    ):
        """Verify SA auth works even when USERS_ENABLED=true (no user context)."""
        # Create SA
        resp = sa_client.post(
            "/api/service-accounts",
            json={"name": "independent-sa", "scopes": ["documents:read"]},
            headers=admin_headers,
        )
        assert resp.status_code == 201
        raw_key = resp.json()["key"]

        # Verify users_enabled=True patch doesn't affect SA auth
        app = FastAPI()

        @app.get("/sa-protected")
        def sa_protected_endpoint(
            _auth: dict = Depends(require_service_account(["documents:read"])),
        ):
            return {"status": "ok"}

        temp_dir = str(Path(sa_db_path).parent)
        with patch.object(settings, "data_dir", Path(temp_dir)):
            with patch.object(settings, "users_enabled", True):
                from app.models.database import SQLiteConnectionPool, _pool_cache

                _pool_cache.clear()
                test_pool = SQLiteConnectionPool(sa_db_path, max_size=1)
                with patch("app.models.database.get_pool", side_effect=lambda path, **kw: test_pool):
                    client = TestClient(app)
                    resp = client.get(
                        "/sa-protected",
                        headers={"Authorization": f"Bearer {raw_key}"},
                    )
                    assert resp.status_code == 200, resp.json()
                test_pool.close_all()
