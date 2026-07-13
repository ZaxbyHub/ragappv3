"""Tests for vault_members IntegrityError fix (FR-001, FR-002, FR-003).

Verifies that:
1. add_vault_member() returns 409 on sqlite3.IntegrityError (duplicate)
2. add_vault_member() returns 500 on other exceptions (connection error, etc.)
3. grant_vault_group_access() returns 409 on sqlite3.IntegrityError (duplicate)
4. grant_vault_group_access() returns 500 on other exceptions
5. update/remove operations re-raise all exceptions (no behavior change)
"""

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from backend.tests.schema_constants import TEST_SCHEMA
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_db
from app.api.routes.auth import router as auth_router
from app.api.routes.vault_members import (
    group_access_router,
)
from app.api.routes.vault_members import (
    router as vault_members_router,
)
from app.services.auth_service import (
    compute_client_fingerprint,
    create_access_token,
    hash_password,
)


@pytest.fixture(autouse=True)
def setup_db(monkeypatch):
    """Set up test database with schema and seed data."""
    temp_dir = tempfile.mkdtemp()
    db_path = str(Path(temp_dir) / "app.db")

    # Clear pool cache BEFORE setting up new database
    from app.models.database import _pool_cache, _pool_cache_lock

    with _pool_cache_lock:
        for path, pool in list(_pool_cache.items()):
            pool.close_all()
        _pool_cache.clear()

    # Initialize schema manually with valid SQL
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(TEST_SCHEMA)
    conn.commit()
    conn.close()

    # Patch settings
    monkeypatch.setattr("app.config.settings.data_dir", Path(temp_dir))
    monkeypatch.setattr(
        "app.config.settings.jwt_secret_key",
        "test-secret-key-for-testing-only-min-32-chars!!",
    )
    monkeypatch.setattr("app.config.settings.users_enabled", True)

    # Seed test users
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    pw = hash_password("testpass")
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
        (1, "superadmin", pw, "Super Admin", "superadmin"),
    )
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
        (2, "admin1", pw, "Admin One", "admin"),
    )
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
        (3, "member1", pw, "Member One", "member"),
    )
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
        (4, "member2", pw, "Member Two", "member"),
    )
    # Seed test organization
    conn.execute(
        "INSERT INTO organizations (id, name, slug) VALUES (1, 'Test Org', 'test-org')"
    )
    # Seed test groups
    conn.execute("INSERT INTO groups (id, org_id, name) VALUES (1, 1, 'Admins')")
    conn.execute("INSERT INTO groups (id, org_id, name) VALUES (2, 1, 'Developers')")
    conn.execute("INSERT INTO groups (id, org_id, name) VALUES (3, 1, 'Viewers')")
    # Seed test vault
    conn.execute("INSERT INTO vaults (id, name) VALUES (1, 'Test Vault')")
    # Seed admin user as vault admin
    conn.execute(
        "INSERT INTO vault_members (vault_id, user_id, permission, granted_by) VALUES (?, ?, ?, ?)",
        (1, 2, "admin", 1),
    )
    conn.commit()
    conn.close()

    yield db_path

    # Cleanup
    with _pool_cache_lock:
        if db_path in _pool_cache:
            _pool_cache[db_path].close_all()
            del _pool_cache[db_path]

    import shutil

    shutil.rmtree(temp_dir, ignore_errors=True)


def _get_db_conn():
    """Get a direct connection to the test database for setup."""
    from app.config import settings

    return sqlite3.connect(str(settings.sqlite_path))


def superadmin_token():
    return create_access_token(1, "superadmin", "superadmin",
                            client_fingerprint=compute_client_fingerprint(""))


def admin_token():
    return create_access_token(2, "admin1", "admin",
                            client_fingerprint=compute_client_fingerprint(""))


def auth_headers(token_fn):
    return {"Authorization": f"Bearer {token_fn()}"}


def _mock_db_conn(exc, auth_user_row=None):
    """Build a mock DB connection whose cursor.execute() raises ``exc``.

    Used to override ``get_db`` so route handlers receive a failing connection
    via DI (replacing the legacy get_pool patch). The connection's top-level
    ``execute()`` (used by get_current_active_user's auth queries) returns
    None for the denylist check and ``auth_user_row`` for the user lookup so
    auth still resolves; the handler's own cursor-based queries raise ``exc``.
    """
    mock_conn = MagicMock()

    # Top-level execute() is used by auth: first the denylist check
    # (is_access_token_denied -> fetchone() must be None so the token is NOT
    # denied), then _fetch_user_row_with_pwc (fetchone() returns the user).
    auth_cursor = MagicMock()
    auth_cursor.fetchone.side_effect = [None, auth_user_row, auth_user_row]
    mock_conn.execute.return_value = auth_cursor

    # Handler cursor (conn.cursor().execute()) raises the injected exception.
    mock_cursor = MagicMock()
    mock_cursor.execute.side_effect = exc
    mock_conn.cursor.return_value = mock_cursor
    mock_conn.commit.return_value = None
    mock_conn.rollback.return_value = None
    return mock_conn


def _client_with_failing_db(client, exc):
    """Override ``get_db`` on ``client``'s app to yield a failing mock.

    The mock connection's cursor.execute() raises ``exc``. Routes now receive
    their connection via ``Depends(get_db)`` instead of calling ``get_pool()``
    directly, so we override the DI dependency rather than patching get_pool.

    Auth (get_current_active_user) also resolves through ``Depends(get_db)``,
    so the mock connection's top-level ``execute()`` returns the seeded
    superadmin user row, letting auth succeed while the handler's own cursor-
    based queries raise ``exc``. This preserves the original test intent:
    only the route handler's DB operation fails, auth resolves normally.
    """
    # The superadmin row matches _fetch_user_row_with_pwc's 7-column shape.
    auth_user_row = (1, "superadmin", "Super Admin", "superadmin", 1, 0, 0.0)
    client.app.dependency_overrides[get_db] = lambda: _mock_db_conn(
        exc, auth_user_row=auth_user_row
    )
    return client


@pytest.fixture
def client():
    """Create test client with routers."""
    app = FastAPI()
    app.include_router(auth_router, prefix="/api")
    app.include_router(vault_members_router, prefix="/api")
    app.include_router(group_access_router, prefix="/api")
    # raise_server_exceptions=False allows exceptions in route handlers
    # to be caught by Starlette's exception middleware and returned as 500 responses.
    tc = TestClient(app, raise_server_exceptions=False)
    # Override default User-Agent so fingerprint validation matches token
    tc.headers["user-agent"] = ""
    return tc


class TestAddVaultMemberIntegrityError:
    """Tests for add_vault_member() IntegrityError handling (FR-001)."""

    def test_returns_409_on_integrity_error_duplicate_member(self, client):
        """Returns 409 when duplicate member is added (IntegrityError -> 409)."""
        # Add member1 to vault first
        conn = _get_db_conn()
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO vault_members (vault_id, user_id, permission, granted_by) VALUES (?, ?, ?, ?)",
            (1, 3, "read", 1),
        )
        conn.commit()
        conn.close()

        response = client.post(
            "/api/vaults/1/members",
            json={"member_user_id": 3, "permission": "read"},
            headers=auth_headers(superadmin_token),
        )
        # UNIQUE constraint violation -> 409 Conflict
        assert response.status_code == 409
        assert "already a member" in response.json()["detail"]

    def test_returns_500_on_connection_error_not_409(self, client):
        """Returns 500 (not 409) when a non-IntegrityError exception occurs.

        FR-001: Only UNIQUE constraint violations should return 409.
        Unexpected errors (e.g., connection failures) must propagate as 500.

        The exception must be raised INSIDE the try block so it is not caught
        by the `except sqlite3.IntegrityError` handler in the route.
        """
        failing_client = _client_with_failing_db(
            client=client, exc=sqlite3.OperationalError("database is locked")
        )
        response = failing_client.post(
            "/api/vaults/1/members",
            json={"member_user_id": 3, "permission": "read"},
            headers=auth_headers(superadmin_token),
        )
        # Connection error is NOT IntegrityError -> should be 500 Internal Server Error
        assert response.status_code == 500, (
            f"Expected 500 for non-IntegrityError, got {response.status_code}. "
            "Non-IntegrityError exceptions must not be caught as 409."
        )

    def test_returns_500_on_foreign_key_error_not_409(self, client):
        """Returns 500 (not 409) when FK constraint fails but is NOT IntegrityError.

        Note: FK violations ARE IntegrityError in SQLite, but we test with a
        generic Exception to ensure only sqlite3.IntegrityError is caught.
        """
        failing_client = _client_with_failing_db(
            client=client, exc=RuntimeError("unexpected error during transaction")
        )
        response = failing_client.post(
            "/api/vaults/1/members",
            json={"member_user_id": 3, "permission": "read"},
            headers=auth_headers(superadmin_token),
        )
        # RuntimeError is not IntegrityError -> should be 500
        assert response.status_code == 500


class TestGrantVaultGroupAccessIntegrityError:
    """Tests for grant_vault_group_access() IntegrityError handling (FR-002)."""

    def test_returns_409_on_integrity_error_duplicate_group_access(self, client):
        """Returns 409 when duplicate group access is granted (IntegrityError -> 409)."""
        # Add group 1 access to vault first
        conn = _get_db_conn()
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO vault_group_access (vault_id, group_id, permission, granted_by) VALUES (?, ?, ?, ?)",
            (1, 1, "read", 1),
        )
        conn.commit()
        conn.close()

        response = client.post(
            "/api/vaults/1/group-access",
            json={"group_id": 1, "permission": "write"},
            headers=auth_headers(superadmin_token),
        )
        # UNIQUE constraint violation -> 409 Conflict
        assert response.status_code == 409
        assert "already has access" in response.json()["detail"]

    def test_returns_500_on_connection_error_not_409(self, client):
        """Returns 500 (not 409) when a non-IntegrityError exception occurs.

        FR-002: Only UNIQUE constraint violations should return 409.
        Unexpected errors must propagate as 500.

        The exception must be raised INSIDE the try block so it is not caught
        by the `except sqlite3.IntegrityError` handler in the route.
        """
        failing_client = _client_with_failing_db(
            client=client, exc=sqlite3.OperationalError("database is locked")
        )
        response = failing_client.post(
            "/api/vaults/1/group-access",
            json={"group_id": 1, "permission": "read"},
            headers=auth_headers(superadmin_token),
        )
        # Connection error is NOT IntegrityError -> should be 500
        assert response.status_code == 500, (
            f"Expected 500 for non-IntegrityError, got {response.status_code}. "
            "Non-IntegrityError exceptions must not be caught as 409."
        )

    def test_returns_500_on_generic_exception_not_409(self, client):
        """Returns 500 (not 409) when a generic Exception occurs.

        The code must only catch sqlite3.IntegrityError, not broad Exception.
        """
        failing_client = _client_with_failing_db(
            client=client, exc=RuntimeError("unexpected error during transaction")
        )
        response = failing_client.post(
            "/api/vaults/1/group-access",
            json={"group_id": 1, "permission": "read"},
            headers=auth_headers(superadmin_token),
        )
        # RuntimeError is not IntegrityError -> should be 500
        assert response.status_code == 500


class TestUpdateRemoveOperationsReraiseExceptions:
    """Tests that update/remove operations re-raise all exceptions (FR-003).

    These operations use broad `except Exception` which is intentional —
    they must propagate all errors, not convert them to specific HTTP codes.
    """

    def test_update_vault_member_reraises_connection_error(self, client):
        """Update operation re-raises connection errors as 500."""
        # Add member first
        conn = _get_db_conn()
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO vault_members (vault_id, user_id, permission, granted_by) VALUES (?, ?, ?, ?)",
            (1, 3, "read", 1),
        )
        conn.commit()
        conn.close()

        failing_client = _client_with_failing_db(
            client=client, exc=sqlite3.OperationalError("database is locked")
        )
        response = failing_client.patch(
            "/api/vaults/1/members/3",
            json={"permission": "write"},
            headers=auth_headers(superadmin_token),
        )
        # Connection error should propagate as 500
        assert response.status_code == 500

    def test_remove_vault_member_reraises_connection_error(self, client):
        """Remove operation re-raises connection errors as 500."""
        # Add member first
        conn = _get_db_conn()
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO vault_members (vault_id, user_id, permission, granted_by) VALUES (?, ?, ?, ?)",
            (1, 3, "read", 1),
        )
        conn.commit()
        conn.close()

        failing_client = _client_with_failing_db(
            client=client, exc=sqlite3.OperationalError("database is locked")
        )
        response = failing_client.delete(
            "/api/vaults/1/members/3",
            headers=auth_headers(superadmin_token),
        )
        # Connection error should propagate as 500
        assert response.status_code == 500

    def test_update_vault_group_access_reraises_connection_error(self, client):
        """Update group access operation re-raises connection errors as 500."""
        # Add group access first
        conn = _get_db_conn()
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO vault_group_access (vault_id, group_id, permission, granted_by) VALUES (?, ?, ?, ?)",
            (1, 1, "read", 1),
        )
        conn.commit()
        conn.close()

        failing_client = _client_with_failing_db(
            client=client, exc=sqlite3.OperationalError("database is locked")
        )
        response = failing_client.patch(
            "/api/vaults/1/group-access/1",
            json={"permission": "write"},
            headers=auth_headers(superadmin_token),
        )
        # Connection error should propagate as 500
        assert response.status_code == 500

    def test_revoke_vault_group_access_reraises_connection_error(self, client):
        """Revoke group access operation re-raises connection errors as 500."""
        # Add group access first
        conn = _get_db_conn()
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO vault_group_access (vault_id, group_id, permission, granted_by) VALUES (?, ?, ?, ?)",
            (1, 1, "read", 1),
        )
        conn.commit()
        conn.close()

        failing_client = _client_with_failing_db(
            client=client, exc=sqlite3.OperationalError("database is locked")
        )
        response = failing_client.delete(
            "/api/vaults/1/group-access/1",
            headers=auth_headers(superadmin_token),
        )
        # Connection error should propagate as 500
        assert response.status_code == 500
