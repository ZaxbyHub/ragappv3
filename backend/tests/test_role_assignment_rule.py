"""Behavioral acceptance check: one role-assignment rule across all user
role-mutation surfaces (issue #560, check C1).

Encoded rule (issue #560 stated assumption A1, strict variant):

    "Any actual role change on an existing user, and any grant of
    admin/superadmin at creation, requires a superadmin actor; admins may
    create member/viewer users and may PATCH role equal to the target's
    current role (no-op)."

Implied carve-out pinned below: an admin actor may never touch the role
column of a superadmin target, even with a no-op value (PATCHing a
superadmin target stays 403 for non-superadmin actors).

The three RED tests below fail at base (DISCRIMINATING): today an admin
gets 200 from PATCH /users/{id} {"role": "admin"} and from POST /users/
{"role": "admin"}, while PATCH /users/{id}/role correctly 403s — the same
actor+value+mutation must not yield two different answers. The PRESERVING
tests pass at base AND after the fix.
"""

import os
import shutil
import tempfile

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


class TestRoleAssignmentRule:
    """The single rule for who may assign which users.role value, asserted
    against the REAL route handlers on a standalone users-router app."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Temp sqlite DB + users router + dependency overrides (harness
        mirrored from test_users_routes.py)."""
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")

        from app.models.database import _pool_cache

        _pool_cache.clear()

        self.conn = setup_test_db(self.db_path)

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

        from app.api.routes.users import router as users_router

        app = FastAPI()
        app.include_router(users_router)

        from app.api import deps
        from app.models.database import SQLiteConnectionPool

        test_pool = SQLiteConnectionPool(self.db_path, max_size=3)

        def override_get_db():
            conn = test_pool.get_connection()
            try:
                yield conn
            finally:
                test_pool.release_connection(conn)

        from app.api.routes import users

        self.original_get_pool = users.get_pool
        users.get_pool = lambda path: test_pool

        app.dependency_overrides[deps.get_db] = override_get_db

        # Mutating user endpoints require csrf_protect. This standalone app
        # has no csrf_manager on state, so override the dependency to a
        # pass-through — these tests assert the role rule, not CSRF wiring
        # (mirrors test_users_routes.py).
        from app.security import csrf_protect

        app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"

        self.test_pool = test_pool

        from app.config import settings

        self._orig_users_enabled = settings.users_enabled
        self._orig_jwt_secret = settings.jwt_secret_key
        settings.users_enabled = True
        settings.jwt_secret_key = os.environ["JWT_SECRET_KEY"]

        self.client = TestClient(app)
        # Override default User-Agent so fingerprint validation matches token
        self.client.headers["user-agent"] = ""

        yield

        self.client.close()
        _pool_cache.clear()
        self.conn.close()
        users.get_pool = self.original_get_pool
        self.test_pool.close_all()
        settings.users_enabled = self._orig_users_enabled
        settings.jwt_secret_key = self._orig_jwt_secret
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _db_role(self, user_id: int) -> str:
        row = self.conn.execute(
            "SELECT role FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return row[0]

    def _auth(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    # ------------------------------------------------------------------
    # RED at base: the rule must make all three surfaces agree
    # ------------------------------------------------------------------

    def test_admin_cannot_patch_member_to_admin(self):
        """RED at base (200 today): admin PATCH /users/{member} with
        role=admin must be 403 and must not mutate the DB."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.patch(
            f"/users/{self.member_id}",
            json={"role": "admin"},
            headers=self._auth(token),
        )
        assert response.status_code == 403, (
            "admin granting role=admin via PATCH /users/{id} must be 403, "
            f"got {response.status_code}: {response.text}"
        )
        assert self._db_role(self.member_id) == "member"

    def test_admin_cannot_create_admin_peer(self):
        """RED at base (200 today): admin POST /users/ with role=admin must
        be 403 and must not create the user."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.post(
            "/users/",
            json={
                "username": "peeradmin",
                "password": "SecurePass123!",
                "full_name": "Peer Admin",
                "role": "admin",
            },
            headers=self._auth(token),
        )
        assert response.status_code == 403, (
            "admin granting role=admin at creation via POST /users/ must be "
            f"403, got {response.status_code}: {response.text}"
        )
        # Wording pin (plan-critic Round 1): the 403 detail must name the
        # superadmin requirement, not merely reject.
        assert "superadmin" in response.json()["detail"].lower(), (
            "the 403 detail must name the superadmin requirement, got: "
            f"{response.json()['detail']!r}"
        )
        row = self.conn.execute(
            "SELECT role FROM users WHERE username = ?", ("peeradmin",)
        ).fetchone()
        assert row is None

    def test_admin_cannot_change_role_via_role_endpoint(self):
        """Part of the trio: PATCH /users/{id}/role already 403s for admins
        (pinned by test_users_routes.py:291) and must stay 403, so all three
        surfaces agree after the fix."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.patch(
            f"/users/{self.member_id}/role",
            json={"role": "admin"},
            headers=self._auth(token),
        )
        assert response.status_code == 403
        assert self._db_role(self.member_id) == "member"

    # ------------------------------------------------------------------
    # PRESERVING: pass at base AND after the fix
    # ------------------------------------------------------------------

    def test_superadmin_can_patch_member_to_admin(self):
        """Superadmin promoting a member via PATCH stays allowed (200)."""
        token = get_token(self.superadmin_id, "superadmin", "superadmin")
        response = self.client.patch(
            f"/users/{self.member_id}",
            json={"role": "admin"},
            headers=self._auth(token),
        )
        assert response.status_code == 200
        assert response.json()["role"] == "admin"
        assert self._db_role(self.member_id) == "admin"

    def test_admin_can_create_member(self):
        """Admin creating a member stays allowed (200, pinned at base by
        test_users_routes.py:770)."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.post(
            "/users/",
            json={
                "username": "newmember",
                "password": "SecurePass123!",
                "full_name": "New Member",
                "role": "member",
            },
            headers=self._auth(token),
        )
        assert response.status_code == 200
        assert response.json()["role"] == "member"
        assert self._db_role(response.json()["id"]) == "member"

    def test_admin_can_create_viewer(self):
        """Admin creating a viewer stays allowed (the rule reserves only
        admin/superadmin grants)."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.post(
            "/users/",
            json={
                "username": "newviewer",
                "password": "SecurePass123!",
                "full_name": "New Viewer",
                "role": "viewer",
            },
            headers=self._auth(token),
        )
        assert response.status_code == 200
        assert response.json()["role"] == "viewer"

    def test_admin_patch_noop_role_and_full_name_on_other_admin(self):
        """Edit-dialog no-op path: admin PATCHes another admin with role
        equal to that target's CURRENT role plus a full_name change -> 200
        (the no-op role must not 403 the save)."""
        admin2_id = create_user(
            self.conn, "admin2", "pass123", "admin", "Second Admin"
        )
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.patch(
            f"/users/{admin2_id}",
            json={"role": "admin", "full_name": "Renamed Admin"},
            headers=self._auth(token),
        )
        assert response.status_code == 200
        assert response.json()["role"] == "admin"
        assert response.json()["full_name"] == "Renamed Admin"
        row = self.conn.execute(
            "SELECT role, full_name FROM users WHERE id = ?", (admin2_id,)
        ).fetchone()
        assert row == ("admin", "Renamed Admin")

    def test_admin_patch_full_name_only(self):
        """PATCH without a role key keeps working (200)."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.patch(
            f"/users/{self.member_id}",
            json={"full_name": "Renamed Member"},
            headers=self._auth(token),
        )
        assert response.status_code == 200
        assert response.json()["full_name"] == "Renamed Member"
        assert self._db_role(self.member_id) == "member"

    def test_admin_patch_superadmin_target_stays_403(self):
        """Carve-out: a non-superadmin actor may not touch the role column
        of a superadmin target, even with a no-op value (403 today, must
        stay 403)."""
        token = get_token(self.admin_id, "admin", "admin")
        response = self.client.patch(
            f"/users/{self.superadmin_id}",
            json={"role": "superadmin"},
            headers=self._auth(token),
        )
        assert response.status_code == 403
        assert self._db_role(self.superadmin_id) == "superadmin"
