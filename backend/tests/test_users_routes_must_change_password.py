"""
Verification tests for must_change_password=1 in POST /users/ (create_user) INSERT.

These tests complement the existing TestCreateUser.test_create_user_sets_must_change_password
test by verifying:
1. All roles get must_change_password=1 on creation (superadmin, admin, member, viewer)
2. Both is_active=1 AND must_change_password=1 are set atomically in the INSERT
3. The INSERT literal values are hardcoded (not parameterized)

These are source-inspection + behavioral tests that verify the INSERT statement itself.
"""

import os
import tempfile
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Set up test environment BEFORE importing app modules
os.environ["JWT_SECRET_KEY"] = (
    "test-jwt-secret-key-for-testing-only-12345678901234567890"
)
os.environ["USERS_ENABLED"] = "true"

from backend.tests.user_route_helpers import (
    create_user,
    get_token,
    setup_test_db,
)


class TestMustChangePasswordInCreateUserInsert:
    """Tests verifying must_change_password=1 and is_active=1 are set in create_user INSERT."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up test database for each test."""
        # Create temp directory and database
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")

        # Clear the global pool cache to ensure test isolation
        from app.models.database import _pool_cache

        _pool_cache.clear()

        # Set up test database
        self.conn = setup_test_db(self.db_path)

        # Create test users
        self.superadmin_id = create_user(
            self.conn, "superadmin", "pass123", "superadmin", "Super Admin"
        )
        self.admin_id = create_user(
            self.conn, "admin", "pass123", "admin", "Admin User"
        )
        self.member_id = create_user(
            self.conn, "member", "pass123", "member", "Regular Member"
        )
        self.viewer_id = create_user(
            self.conn, "viewer", "pass123", "viewer", "Viewer User"
        )

        # Create app with users router
        from app.api.routes.users import router as users_router

        app = FastAPI()
        app.include_router(users_router)

        # Override the get_db dependency to use our test database
        from app.api import deps
        from app.models.database import SQLiteConnectionPool

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

        app.dependency_overrides[deps.get_db] = override_get_db

        from app.security import csrf_protect

        app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"

        # Store for cleanup
        self.test_pool = test_pool
        self.original_get_pool = original_get_pool

        from app.config import settings
        self._orig_users_enabled = settings.users_enabled
        self._orig_jwt_secret = settings.jwt_secret_key
        settings.users_enabled = True
        settings.jwt_secret_key = os.environ["JWT_SECRET_KEY"]

        self.client = TestClient(app)
        # Override default User-Agent so fingerprint validation matches token
        self.client.headers["user-agent"] = ""

        yield

        # Cleanup
        self.client.close()
        _pool_cache.clear()
        self.conn.close()

        # Restore original get_pool
        from app.api.routes import users

        users.get_pool = self.original_get_pool
        self.test_pool.close_all()
        settings.users_enabled = self._orig_users_enabled
        settings.jwt_secret_key = self._orig_jwt_secret

        # Clean up temp directory
        import shutil

        try:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        except Exception:
            pass

    def _create_user_via_api(self, username: str, role: str) -> dict:
        """Helper to create a user via the API and return the response data."""
        from app.security import csrf_protect

        self.client.app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.post(
            "/users/",
            json={
                "username": username,
                "password": "SecurePass123!",
                "full_name": f"Test {role}",
                "role": role,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200, f"Failed to create {role}: {response.json()}"
        return response.json()

    def _get_user_flags_from_db(self, username: str) -> tuple:
        """Get (is_active, must_change_password) from DB for a user."""
        row = self.conn.execute(
            "SELECT is_active, must_change_password FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        assert row is not None, f"User {username} not found in DB"
        return (row[0], row[1])

    def test_create_user_member_sets_both_flags(self):
        """Member user gets is_active=1 AND must_change_password=1 atomically."""
        self._create_user_via_api("test_member", "member")
        is_active, must_change = self._get_user_flags_from_db("test_member")
        assert is_active == 1, f"Expected is_active=1, got {is_active}"
        assert must_change == 1, f"Expected must_change_password=1, got {must_change}"

    def test_create_user_admin_sets_both_flags(self):
        """Admin user gets is_active=1 AND must_change_password=1 atomically."""
        self._create_user_via_api("test_admin", "admin")
        is_active, must_change = self._get_user_flags_from_db("test_admin")
        assert is_active == 1, f"Expected is_active=1, got {is_active}"
        assert must_change == 1, f"Expected must_change_password=1, got {must_change}"

    def test_create_user_superadmin_sets_both_flags(self):
        """Superadmin user gets is_active=1 AND must_change_password=1 atomically."""
        # Use superadmin token to create another superadmin
        from app.security import csrf_protect

        self.client.app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.post(
            "/users/",
            json={
                "username": "test_superadmin2",
                "password": "SecurePass123!",
                "full_name": "Test Superadmin 2",
                "role": "superadmin",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200, f"Failed to create superadmin: {response.json()}"

        is_active, must_change = self._get_user_flags_from_db("test_superadmin2")
        assert is_active == 1, f"Expected is_active=1, got {is_active}"
        assert must_change == 1, f"Expected must_change_password=1, got {must_change}"

    def test_create_user_viewer_sets_both_flags(self):
        """Viewer user gets is_active=1 AND must_change_password=1 atomically."""
        self._create_user_via_api("test_viewer", "viewer")
        is_active, must_change = self._get_user_flags_from_db("test_viewer")
        assert is_active == 1, f"Expected is_active=1, got {is_active}"
        assert must_change == 1, f"Expected must_change_password=1, got {must_change}"

    def test_insert_statement_has_hardcoded_flags(self):
        """Source inspection: INSERT uses hardcoded 1,1 for is_active and must_change_password.

        This is a source-inspection test that verifies the INSERT statement in the source
        code has the literal VALUES (?, ?, ?, ?, 1, 1) with hardcoded 1s for the flags.
        This prevents accidental parameterization of these security-critical flags.
        """
        import inspect
        from app.api.routes import users

        # Get the source code of the create_user function
        source = inspect.getsource(users.create_user)

        # The INSERT statement spans multiple lines:
        #   INSERT INTO users (username, hashed_password, full_name, role, is_active, must_change_password)
        #   VALUES (?, ?, ?, ?, 1, 1)
        #
        # We verify:
        # 1. The INSERT lists is_active and must_change_password columns
        # 2. The VALUES clause has hardcoded 1, 1 for these columns (not ? placeholders)

        # Verify INSERT lists both flag columns in the column list
        assert "is_active" in source and "must_change_password" in source, (
            "INSERT should list both is_active and must_change_password columns"
        )

        # Verify VALUES has the hardcoded 1, 1 pattern
        # The actual source is: VALUES (?, ?, ?, ?, 1, 1)
        values_pattern = r'VALUES\s*\(\s*\?\s*,\s*\?\s*,\s*\?\s*,\s*\?\s*,\s*1\s*,\s*1\s*\)'
        match = re.search(values_pattern, source, re.IGNORECASE | re.DOTALL)

        assert match is not None, (
            "INSERT VALUES clause does not have hardcoded '1, 1' for is_active and must_change_password. "
            "These flags must be hardcoded to 1 in the VALUES clause, not parameterized."
        )

    def test_insert_values_order_matches_columns(self):
        """Source inspection: VALUES count matches column count in INSERT.

        Verifies the INSERT has exactly 6 columns and 6 values:
        (username, hashed_password, full_name, role, is_active, must_change_password)
        VALUES (?, ?, ?, ?, 1, 1)
        """
        import inspect
        from app.api.routes import users

        source = inspect.getsource(users.create_user)

        # Count columns in INSERT
        columns_match = re.search(
            r'INSERT\s+INTO\s+users\s*\(([^)]+)\)\s*VALUES',
            source,
            re.IGNORECASE | re.DOTALL,
        )
        assert columns_match is not None, "Could not find INSERT columns in source"

        columns = [c.strip() for c in columns_match.group(1).split(",")]
        assert len(columns) == 6, f"Expected 6 columns, got {len(columns)}: {columns}"
        assert columns[4] == "is_active", f"Column 5 should be is_active, got {columns[4]}"
        assert columns[5] == "must_change_password", f"Column 6 should be must_change_password, got {columns[5]}"

        # Count VALUES placeholders
        values_match = re.search(
            r'VALUES\s*\(([^)]+)\)',
            source,
            re.IGNORECASE | re.DOTALL,
        )
        assert values_match is not None, "Could not find VALUES in source"

        values = [v.strip() for v in values_match.group(1).split(",")]
        assert len(values) == 6, f"Expected 6 VALUES, got {len(values)}: {values}"

        # The last two values should be hardcoded 1s
        assert values[4] == "1", f"VALUES[4] (is_active) should be '1', got {values[4]}"
        assert values[5] == "1", f"VALUES[5] (must_change_password) should be '1', got {values[5]}"


class TestMustChangePasswordBehaviorVsAdminReset:
    """Verify create_user sets same flags as admin_reset_password endpoint."""

    def test_create_user_and_admin_reset_produce_same_flags(self):
        """Both create_user and admin_reset_password set must_change_password=1.

        This verifies consistency: a newly created user has the same must_change_password
        flag as one whose password was admin-reset.
        """
        tmpdir = tempfile.mkdtemp()
        db_path = os.path.join(tmpdir, "test.db")

        from app.models.database import _pool_cache

        _pool_cache.clear()

        conn = setup_test_db(db_path)
        superadmin_id = create_user(conn, "superadmin", "pass123", "superadmin", "Super Admin")
        admin_id = create_user(conn, "admin", "pass123", "admin", "Admin User")
        member_id = create_user(conn, "member", "pass123", "member", "Regular Member")

        from app.api.routes.users import router as users_router

        app = FastAPI()
        app.include_router(users_router)

        from app.api import deps
        from app.models.database import SQLiteConnectionPool

        test_pool = SQLiteConnectionPool(db_path, max_size=3)

        def override_get_db():
            conn = test_pool.get_connection()
            try:
                yield conn
            finally:
                test_pool.release_connection(conn)

        from app.api.routes import users

        original_get_pool = users.get_pool
        users.get_pool = lambda path: test_pool

        app.dependency_overrides[deps.get_db] = override_get_db

        from app.security import csrf_protect

        app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"

        from app.config import settings

        _orig_users_enabled = settings.users_enabled
        _orig_jwt_secret = settings.jwt_secret_key
        settings.users_enabled = True
        settings.jwt_secret_key = os.environ["JWT_SECRET_KEY"]

        client = TestClient(app)
        client.headers["user-agent"] = ""

        try:
            # Create a user via API
            token = get_token(admin_id, "admin", "admin")
            response = client.post(
                "/users/",
                json={
                    "username": "newuser",
                    "password": "SecurePass123!",
                    "full_name": "New User",
                    "role": "member",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 200

            # Check flags after create_user
            row = conn.execute(
                "SELECT is_active, must_change_password FROM users WHERE username = ?",
                ("newuser",),
            ).fetchone()
            create_is_active, create_must_change = row[0], row[1]

            # Now admin-reset password on member_id
            token = get_token(admin_id, "admin", "admin")
            response = client.patch(
                f"/users/{member_id}/password",
                json={"new_password": "AnotherSecurePass123!"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 200

            # Check flags after admin_reset_password
            row = conn.execute(
                "SELECT is_active, must_change_password FROM users WHERE id = ?",
                (member_id,),
            ).fetchone()
            reset_is_active, reset_must_change = row[0], row[1]

            # Both should have must_change_password=1
            assert create_must_change == 1, f"create_user: must_change_password should be 1, got {create_must_change}"
            assert reset_must_change == 1, f"admin_reset_password: must_change_password should be 1, got {reset_must_change}"

            # Both should have is_active=1
            assert create_is_active == 1, f"create_user: is_active should be 1, got {create_is_active}"
            assert reset_is_active == 1, f"admin_reset_password: is_active should remain 1, got {reset_is_active}"

        finally:
            client.close()
            _pool_cache.clear()
            conn.close()
            users.get_pool = original_get_pool
            test_pool.close_all()
            settings.users_enabled = _orig_users_enabled
            settings.jwt_secret_key = _orig_jwt_secret

            import shutil

            try:
                shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:
                pass
