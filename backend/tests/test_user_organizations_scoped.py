"""
Regression test for FR-006 / LOW A1-1: get_user_organizations shared-org scoping.

Verifies that when a non-superadmin admin calls GET /users/{target_user_id}/organizations:
(a) admin1 in org X calls for target_user in org Y only → 403
    'Cannot view organizations of users outside your organization'
(b) After adding target_user to org X, same call → 200 with correct org list including X
(c) Superadmin can call regardless of shared orgs → 200

This test MUST fail against commit 12f1db6 (pre-fix code) because the
shared-org intersection guard was absent in get_user_organizations.
"""

import os
import shutil
import sqlite3
import tempfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Set up test environment BEFORE importing app modules
os.environ["JWT_SECRET_KEY"] = (
    "test-jwt-secret-key-for-testing-only-12345678901234567890"
)
os.environ["USERS_ENABLED"] = "true"

from backend.tests.schema_constants import TEST_SCHEMA

from app.services.auth_service import (
    compute_client_fingerprint,
    create_access_token,
    hash_password,
)


def setup_test_db(db_path: str) -> sqlite3.Connection:
    """Set up test database with full schema."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(TEST_SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (1, 'Default', 'Default vault')"
    )
    conn.commit()
    return conn


def create_user(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    role: str,
    full_name: str = "",
    is_active: int = 1,
) -> int:
    """Create a test user and return its ID."""
    hashed = hash_password(password)
    cursor = conn.execute(
        """INSERT INTO users (username, hashed_password, full_name, role, is_active)
           VALUES (?, ?, ?, ?, ?)""",
        (username, hashed, full_name, role, is_active),
    )
    conn.commit()
    return cursor.lastrowid


def create_organization(conn: sqlite3.Connection, name: str, slug: str = None) -> int:
    """Create a test organization and return its ID."""
    if slug is None:
        slug = name.lower().replace(" ", "-")
    cursor = conn.execute(
        "INSERT INTO organizations (name, slug) VALUES (?, ?)",
        (name, slug),
    )
    conn.commit()
    return cursor.lastrowid


def add_user_to_org(
    conn: sqlite3.Connection, user_id: int, org_id: int, role: str = "member"
) -> None:
    """Add a user to an organization."""
    conn.execute(
        "INSERT INTO org_members (user_id, org_id, role) VALUES (?, ?, ?)",
        (user_id, org_id, role),
    )
    conn.commit()


def get_token(user_id: int, username: str, role: str) -> str:
    """Generate a JWT token for a test user."""
    return create_access_token(
        user_id, username, role, client_fingerprint=compute_client_fingerprint("")
    )


class TestUserOrganizationsScopedAccess:
    """Tests for shared-org scoping on GET /users/{user_id}/organizations."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up test database with two orgs and users in separate orgs."""
        # Create temp directory and database
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")

        # Ensure settings.users_enabled=True so JWT tokens are accepted
        from app.config import settings as _app_settings

        self._prev_users_enabled = _app_settings.users_enabled
        _app_settings.users_enabled = True

        # Clear the global pool cache to ensure test isolation
        from app.models.database import _pool_cache

        _pool_cache.clear()

        # Set up test database
        self.conn = setup_test_db(self.db_path)

        # Create two organizations
        self.org_x_id = create_organization(self.conn, "Org X", "org-x")
        self.org_y_id = create_organization(self.conn, "Org Y", "org-y")

        # Create users:
        # - superadmin: superadmin role (no org membership required)
        # - admin_x: admin in org X only
        # - target_user: member in org Y only (initial state)
        self.superadmin_id = create_user(
            self.conn, "superadmin", "pass123", "superadmin", "Super Admin"
        )
        self.admin_x_id = create_user(
            self.conn, "admin_x", "pass123", "admin", "Admin X"
        )
        self.target_user_id = create_user(
            self.conn, "target_user", "pass123", "member", "Target User"
        )

        # Admin is in org X only
        add_user_to_org(self.conn, self.admin_x_id, self.org_x_id, "admin")
        # Target user is in org Y only (initial state before scenario (c))
        add_user_to_org(self.conn, self.target_user_id, self.org_y_id, "member")

        # Create app with users router
        from app.api.routes.users import router as users_router
        from app.models.database import SQLiteConnectionPool

        app = FastAPI()
        app.include_router(users_router)

        # Create a test pool
        test_pool = SQLiteConnectionPool(self.db_path, max_size=3)

        def override_get_db():
            """Override get_db to return a connection from test pool."""
            conn = test_pool.get_connection()
            try:
                yield conn
            finally:
                test_pool.release_connection(conn)

        # Patch get_pool in users module to return our test pool
        from app.api.routes import users

        original_get_pool = users.get_pool
        users.get_pool = lambda path: test_pool

        # Also patch deps.get_pool for evaluate_policy
        from app.api import deps

        original_deps_pool = deps.get_pool
        deps.get_pool = lambda path: test_pool

        app.dependency_overrides[deps.get_db] = override_get_db

        # Store for cleanup
        self.test_pool = test_pool
        self.original_get_pool = original_get_pool
        self.original_deps_pool = original_deps_pool

        self.client = TestClient(app)
        self.client.headers["user-agent"] = ""

        yield

        # Cleanup
        self.client.close()
        _pool_cache.clear()
        self.conn.close()
        self.test_pool.close_all()

        # Restore settings
        _app_settings.users_enabled = self._prev_users_enabled

        # Restore original get_pool
        users.get_pool = self.original_get_pool
        deps.get_pool = self.original_deps_pool

        # Clean up temp directory
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_admin_in_org_x_gets_403_for_target_in_org_y_only(self):
        """(a) Admin in org X calling for user in org Y only → 403."""
        token = get_token(self.admin_x_id, "admin_x", "admin")
        response = self.client.get(
            f"/users/{self.target_user_id}/organizations",
            headers={"Authorization": f"Bearer {token}"},
        )

        if response.status_code != 403:
            raise AssertionError(
                f"Expected 403 for admin in org X calling for user in org Y only, "
                f"got {response.status_code}: {response.json()}"
            )
        data = response.json()
        if data.get("detail") != "Cannot view organizations of users outside your organization":
            raise AssertionError(
                f"Expected specific 403 detail, got: {data.get('detail')}"
            )

    def test_admin_in_org_x_gets_200_after_adding_target_to_org_x(self):
        """(b) After adding target_user to org X, admin in org X gets 200 with correct orgs."""
        # Add target_user to org X as well (now in both X and Y)
        add_user_to_org(self.conn, self.target_user_id, self.org_x_id, "member")

        token = get_token(self.admin_x_id, "admin_x", "admin")
        response = self.client.get(
            f"/users/{self.target_user_id}/organizations",
            headers={"Authorization": f"Bearer {token}"},
        )

        if response.status_code != 200:
            raise AssertionError(
                f"Expected 200 after adding target to org X, "
                f"got {response.status_code}: {response.json()}"
            )
        data = response.json()
        orgs = data.get("organizations", [])
        org_ids = {org["id"] for org in orgs}
        if self.org_x_id not in org_ids:
            raise AssertionError(
                f"Expected org X ({self.org_x_id}) in response, got org IDs: {org_ids}"
            )
        if self.org_y_id not in org_ids:
            raise AssertionError(
                f"Expected org Y ({self.org_y_id}) in response, got org IDs: {org_ids}"
            )

    def test_superadmin_can_view_any_user_orgs(self):
        """(c) Superadmin can view orgs of user in any org regardless of shared orgs."""
        # target_user is still only in org Y; superadmin has no org membership
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.get(
            f"/users/{self.target_user_id}/organizations",
            headers={"Authorization": f"Bearer {token}"},
        )

        if response.status_code != 200:
            raise AssertionError(
                f"Expected 200 for superadmin viewing any user's orgs, "
                f"got {response.status_code}: {response.json()}"
            )
        data = response.json()
        orgs = data.get("organizations", [])
        org_ids = {org["id"] for org in orgs}
        if self.org_y_id not in org_ids:
            raise AssertionError(
                f"Expected superadmin to see org Y ({self.org_y_id}), got org IDs: {org_ids}"
            )
