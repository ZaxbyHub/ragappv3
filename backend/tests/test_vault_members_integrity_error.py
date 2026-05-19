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
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.auth import router as auth_router
from app.api.routes.vault_members import (
    group_access_router,
)
from app.api.routes.vault_members import (
    router as vault_members_router,
)
from app.services.auth_service import create_access_token, hash_password

# Valid SQLite schema matching test_vault_group_access_routes.py
TEST_SCHEMA = """
-- Organizations table
CREATE TABLE IF NOT EXISTS organizations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT DEFAULT '',
    slug TEXT UNIQUE,
    created_by INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (created_by) REFERENCES users(id)
);

-- Users table
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    hashed_password TEXT NOT NULL,
    full_name TEXT DEFAULT '',
    role TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('superadmin','admin','member','viewer')),
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_login_at TIMESTAMP,
    must_change_password INTEGER DEFAULT 0
);

-- Vaults table
CREATE TABLE IF NOT EXISTS vaults (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    owner_id INTEGER,
    org_id INTEGER,
    visibility TEXT DEFAULT 'private' CHECK (visibility IN ('private', 'org', 'public')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Groups table
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE,
    UNIQUE(org_id, name)
);

-- Organization members table
CREATE TABLE IF NOT EXISTS org_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    UNIQUE(org_id, user_id)
);

-- Group members table
CREATE TABLE IF NOT EXISTS group_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    UNIQUE(group_id, user_id)
);

-- Vault members table
CREATE TABLE IF NOT EXISTS vault_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    permission TEXT NOT NULL DEFAULT 'read' CHECK (permission IN ('read','write','admin')),
    granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    granted_by INTEGER,
    FOREIGN KEY (vault_id) REFERENCES vaults(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (granted_by) REFERENCES users(id) ON DELETE SET NULL,
    UNIQUE(vault_id, user_id)
);

-- Vault group access table
CREATE TABLE IF NOT EXISTS vault_group_access (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_id INTEGER NOT NULL,
    group_id INTEGER NOT NULL,
    permission TEXT NOT NULL DEFAULT 'read' CHECK (permission IN ('read','write','admin')),
    granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    granted_by INTEGER,
    FOREIGN KEY (vault_id) REFERENCES vaults(id) ON DELETE CASCADE,
    FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE,
    FOREIGN KEY (granted_by) REFERENCES users(id) ON DELETE SET NULL,
    UNIQUE(vault_id, group_id)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);
CREATE INDEX IF NOT EXISTS idx_vault_members_vault_id ON vault_members(vault_id);
CREATE INDEX IF NOT EXISTS idx_vault_members_user_id ON vault_members(user_id);
CREATE INDEX IF NOT EXISTS idx_groups_org_id ON groups(org_id);
CREATE INDEX IF NOT EXISTS idx_group_members_group_id ON group_members(group_id);
CREATE INDEX IF NOT EXISTS idx_group_members_user_id ON group_members(user_id);
CREATE INDEX IF NOT EXISTS idx_vault_group_access_vault_id ON vault_group_access(vault_id);
CREATE INDEX IF NOT EXISTS idx_vault_group_access_group_id ON vault_group_access(group_id);
"""


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
    return create_access_token(1, "superadmin", "superadmin")


def admin_token():
    return create_access_token(2, "admin1", "admin")


def auth_headers(token_fn):
    return {"Authorization": f"Bearer {token_fn()}"}


@pytest.fixture
def client():
    """Create test client with routers."""
    app = FastAPI()
    app.include_router(auth_router, prefix="/api")
    app.include_router(vault_members_router, prefix="/api")
    app.include_router(group_access_router, prefix="/api")
    # raise_server_exceptions=False allows exceptions in route handlers
    # to be caught by Starlette's exception middleware and returned as 500 responses.
    return TestClient(app, raise_server_exceptions=False)


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
        with patch("app.api.routes.vault_members.get_pool") as mock_get_pool:
            # Create a mock connection whose cursor.execute() raises the error.
            # This ensures the exception occurs inside the try block.
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_cursor.execute.side_effect = sqlite3.OperationalError(
                "database is locked"
            )
            mock_conn.cursor.return_value = mock_cursor
            mock_conn.commit.return_value = None

            mock_pool = MagicMock()
            mock_pool.get_connection.return_value = mock_conn
            mock_pool.release_connection.return_value = None
            mock_get_pool.return_value = mock_pool

            response = client.post(
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
        with patch("app.api.routes.vault_members.get_pool") as mock_get_pool:
            # Create a mock connection whose cursor.execute() raises the error.
            # This ensures the exception occurs inside the try block.
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_cursor.execute.side_effect = RuntimeError(
                "unexpected error during transaction"
            )
            mock_conn.cursor.return_value = mock_cursor
            mock_conn.commit.return_value = None

            mock_pool = MagicMock()
            mock_pool.get_connection.return_value = mock_conn
            mock_pool.release_connection.return_value = None
            mock_get_pool.return_value = mock_pool

            response = client.post(
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
        with patch("app.api.routes.vault_members.get_pool") as mock_get_pool:
            # Create a mock connection whose cursor.execute() raises the error.
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_cursor.execute.side_effect = sqlite3.OperationalError(
                "database is locked"
            )
            mock_conn.cursor.return_value = mock_cursor
            mock_conn.commit.return_value = None

            mock_pool = MagicMock()
            mock_pool.get_connection.return_value = mock_conn
            mock_pool.release_connection.return_value = None
            mock_get_pool.return_value = mock_pool

            response = client.post(
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
        with patch("app.api.routes.vault_members.get_pool") as mock_get_pool:
            # Create a mock connection whose cursor.execute() raises the error.
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_cursor.execute.side_effect = RuntimeError(
                "unexpected error during transaction"
            )
            mock_conn.cursor.return_value = mock_cursor
            mock_conn.commit.return_value = None

            mock_pool = MagicMock()
            mock_pool.get_connection.return_value = mock_conn
            mock_pool.release_connection.return_value = None
            mock_get_pool.return_value = mock_pool

            response = client.post(
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

        with patch("app.api.routes.vault_members.get_pool") as mock_get_pool:
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_cursor.execute.side_effect = sqlite3.OperationalError(
                "database is locked"
            )
            mock_conn.cursor.return_value = mock_cursor
            mock_conn.commit.return_value = None

            mock_pool = MagicMock()
            mock_pool.get_connection.return_value = mock_conn
            mock_pool.release_connection.return_value = None
            mock_get_pool.return_value = mock_pool

            response = client.patch(
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

        with patch("app.api.routes.vault_members.get_pool") as mock_get_pool:
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_cursor.execute.side_effect = sqlite3.OperationalError(
                "database is locked"
            )
            mock_conn.cursor.return_value = mock_cursor
            mock_conn.commit.return_value = None

            mock_pool = MagicMock()
            mock_pool.get_connection.return_value = mock_conn
            mock_pool.release_connection.return_value = None
            mock_get_pool.return_value = mock_pool

            response = client.delete(
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

        with patch("app.api.routes.vault_members.get_pool") as mock_get_pool:
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_cursor.execute.side_effect = sqlite3.OperationalError(
                "database is locked"
            )
            mock_conn.cursor.return_value = mock_cursor
            mock_conn.commit.return_value = None

            mock_pool = MagicMock()
            mock_pool.get_connection.return_value = mock_conn
            mock_pool.release_connection.return_value = None
            mock_get_pool.return_value = mock_pool

            response = client.patch(
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

        with patch("app.api.routes.vault_members.get_pool") as mock_get_pool:
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_cursor.execute.side_effect = sqlite3.OperationalError(
                "database is locked"
            )
            mock_conn.cursor.return_value = mock_cursor
            mock_conn.commit.return_value = None

            mock_pool = MagicMock()
            mock_pool.get_connection.return_value = mock_conn
            mock_pool.release_connection.return_value = None
            mock_get_pool.return_value = mock_pool

            response = client.delete(
                "/api/vaults/1/group-access/1",
                headers=auth_headers(superadmin_token),
            )
            # Connection error should propagate as 500
            assert response.status_code == 500
