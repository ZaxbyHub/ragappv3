"""Regression tests for issue #300: Onboarding hardening.

Covers the changes in this PR:
1. POST /api/auth/register rejects with 403 when users_enabled=False (auth.py).
2. POST /api/organizations/{org_id}/invites rejects viewers at creation time
   with a clear 400 instead of creating a permanently dead-end invite
   (organizations.py).

Note: The must_change_password=1 fix in users.py was delivered separately
by PR #316 and is not part of this PR.
"""

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest
from backend.tests.schema_constants import build_test_schema
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Additional tables needed by the register flow but not in TEST_SCHEMA.
_EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    refresh_token_hash TEXT NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_used_at TIMESTAMP,
    ip_address TEXT,
    user_agent TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
"""

# Set up test environment BEFORE importing app modules
os.environ["JWT_SECRET_KEY"] = (
    "test-jwt-secret-key-for-testing-only-12345678901234567890"
)
os.environ["USERS_ENABLED"] = "true"

from app.api.routes.auth import router as auth_router
from app.api.routes.organizations import router as organizations_router
from app.api.routes.users import router as users_router
from app.models.database import _pool_cache
from app.security import csrf_protect
from app.services.auth_service import (
    compute_client_fingerprint,
    create_access_token,
    hash_password,
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _make_token(user_id: int, username: str, role: str) -> str:
    return create_access_token(
        user_id, username, role,
        client_fingerprint=compute_client_fingerprint(""),
    )


def _build_app(*routers) -> FastAPI:
    app = FastAPI()
    for r in routers:
        app.include_router(r, prefix="/api")
    app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
    return app


def _seed_user(conn, username, role, password="TestPass123!"):
    conn.execute(
        "INSERT INTO users (username, hashed_password, full_name, role, is_active) "
        "VALUES (?, ?, ?, ?, 1)",
        (username, hash_password(password), username, role),
    )
    conn.commit()
    return conn.execute(
        "SELECT id FROM users WHERE username = ?", (username,)
    ).fetchone()[0]


def _create_org(conn, owner_id, name="TestOrg"):
    cursor = conn.execute(
        "INSERT INTO organizations (name, description, slug, created_by) "
        "VALUES (?, ?, ?, ?)",
        (name, "desc", name.lower(), owner_id),
    )
    org_id = cursor.lastrowid
    conn.execute(
        "INSERT INTO org_members (org_id, user_id, role) VALUES (?, ?, 'owner')",
        (org_id, owner_id),
    )
    conn.commit()
    return org_id


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def db_path(monkeypatch):
    """Create a temp DB with schema + seed users, monkeypatch settings.data_dir
    so all get_pool() calls route to it, and clean up after the test."""
    temp_dir = tempfile.mkdtemp()
    path = str(Path(temp_dir) / "app.db")

    with _pool_cache_lock():
        for p in list(_pool_cache.values()):
            p.close_all()
        _pool_cache.clear()

    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    build_test_schema(conn)
    conn.executescript(_EXTRA_SCHEMA)

    superadmin_id = _seed_user(conn, "superadmin", "superadmin")
    admin_id = _seed_user(conn, "admin1", "admin")
    member_id = _seed_user(conn, "member1", "member")
    viewer_id = _seed_user(conn, "viewer1", "viewer")
    conn.close()

    # Point settings.data_dir so settings.sqlite_path resolves to our test DB.
    # This ensures all get_pool() calls across every module use the same file.
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", Path(temp_dir))
    monkeypatch.setattr(
        settings, "jwt_secret_key",
        "test-secret-key-for-testing-only-min-32-chars!!",
    )
    monkeypatch.setattr(settings, "users_enabled", True)

    yield {
        "path": path,
        "superadmin_id": superadmin_id,
        "admin_id": admin_id,
        "member_id": member_id,
        "viewer_id": viewer_id,
    }

    with _pool_cache_lock():
        for p in list(_pool_cache.values()):
            p.close_all()
        _pool_cache.clear()

    import shutil

    shutil.rmtree(temp_dir, ignore_errors=True)


def _pool_cache_lock():
    from app.models.database import _pool_cache_lock
    return _pool_cache_lock


# --------------------------------------------------------------------------- #
# Finding 1: /register rejected when users_enabled=False
# --------------------------------------------------------------------------- #


class TestRegisterUsersEnabledGate:
    """POST /api/auth/register must 403 when users_enabled is False."""

    def test_register_rejected_when_users_disabled(self, db_path, monkeypatch):
        """When users_enabled=False, /register returns 403 and does not
        insert any user rows."""
        from app.config import settings

        monkeypatch.setattr(settings, "users_enabled", False)

        app = _build_app(auth_router)
        client = TestClient(app)
        client.headers["user-agent"] = ""

        response = client.post(
            "/api/auth/register",
            json={
                "username": "attacker",
                "password": "StrongPass123!",
                "full_name": "Attacker",
            },
        )

        assert response.status_code == 403
        assert "disabled" in response.json()["detail"].lower()

        # Verify no user row was inserted
        conn = sqlite3.connect(db_path["path"])
        count = conn.execute(
            "SELECT COUNT(*) FROM users WHERE username = 'attacker'"
        ).fetchone()[0]
        conn.close()
        assert count == 0, "No user row should be planted in single-admin mode"


# --------------------------------------------------------------------------- #
# Finding 2: create_user sets must_change_password=1
# --------------------------------------------------------------------------- #


class TestCreateUserForcesPasswordRotation:
    """POST /api/users/ must set must_change_password=1 on admin-created users."""

    def test_created_user_has_must_change_password(self, db_path):
        """Admin-created user has must_change_password=1 in DB after creation."""
        from app.api.routes import users

        orig_get_pool = users.get_pool
        from app.models.database import get_pool

        # get_pool will naturally route via settings.sqlite_path to our test DB
        # (thanks to monkeypatch in fixture). But create_user imports its own
        # reference to get_pool, so we still need to ensure it's the same.
        # Since the fixture monkeypatches settings.data_dir, all get_pool calls
        # in any module will resolve to the test DB file.

        app = _build_app(users_router)

        token = _make_token(db_path["admin_id"], "admin1", "admin")
        client = TestClient(app)
        client.headers["user-agent"] = ""

        response = client.post(
            "/api/users/",
            json={
                "username": "newmember300",
                "password": "SecurePass123!",
                "full_name": "New Member",
                "role": "member",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200

        # Verify must_change_password=1 in DB
        conn = sqlite3.connect(db_path["path"])
        row = conn.execute(
            "SELECT must_change_password FROM users WHERE username = 'newmember300'"
        ).fetchone()
        conn.close()
        assert row is not None, "User should exist"
        assert row[0] == 1, "must_change_password must be 1 for admin-created users"


# --------------------------------------------------------------------------- #
# Finding 3: create_org_invite rejects viewer invitees at creation time
# --------------------------------------------------------------------------- #


class TestOrgInviteViewerRejection:
    """POST /api/organizations/{org_id}/invites rejects viewers at creation."""

    def test_invite_viewer_returns_400(self, db_path):
        """Inviting a user whose global role is 'viewer' returns 400."""
        # Create org owned by admin
        conn = sqlite3.connect(db_path["path"])
        org_id = _create_org(conn, db_path["admin_id"])
        conn.close()

        token = _make_token(db_path["admin_id"], "admin1", "admin")
        app = _build_app(organizations_router)
        client = TestClient(app)
        client.headers["user-agent"] = ""

        response = client.post(
            f"/api/organizations/{org_id}/invites",
            json={"email": "viewer1", "role": "member"},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 400
        assert "below member" in response.json()["detail"].lower()

    def test_invite_member_succeeds(self, db_path):
        """Inviting a user whose global role is 'member' succeeds (201)."""
        conn = sqlite3.connect(db_path["path"])
        org_id = _create_org(conn, db_path["admin_id"])
        conn.close()

        token = _make_token(db_path["admin_id"], "admin1", "admin")
        app = _build_app(organizations_router)
        client = TestClient(app)
        client.headers["user-agent"] = ""

        response = client.post(
            f"/api/organizations/{org_id}/invites",
            json={"email": "member1", "role": "member"},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 201

    def test_invite_nonexistent_user_succeeds(self, db_path):
        """Inviting a user that doesn't exist yet should still succeed
        (the user may be provisioned later with a sufficient role)."""
        conn = sqlite3.connect(db_path["path"])
        org_id = _create_org(conn, db_path["admin_id"])
        conn.close()

        token = _make_token(db_path["admin_id"], "admin1", "admin")
        app = _build_app(organizations_router)
        client = TestClient(app)
        client.headers["user-agent"] = ""

        response = client.post(
            f"/api/organizations/{org_id}/invites",
            json={"email": "futureuser@x.com", "role": "member"},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 201
