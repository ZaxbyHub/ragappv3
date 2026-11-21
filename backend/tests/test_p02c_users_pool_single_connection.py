"""Companion coverage for the #798 users-route single-connection contract.

Real-app legs through the users router with the REAL get_db dependency and
a counting pool: one checkout per request (auth + handler share), denied
requests make no second checkout, and checkouts are released on success,
denial, and exception outcomes. NOT a frozen check file.
"""
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("ADMIN_SECRET_TOKEN", "test-admin-secret")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from user_route_helpers import get_token, setup_test_db  # noqa: E402

from app.api import deps as deps_mod  # noqa: E402
from app.api.routes.users import router as users_router  # noqa: E402
from app.config import settings  # noqa: E402
from app.models.database import SQLiteConnectionPool, _pool_cache  # noqa: E402
from app.security import csrf_protect  # noqa: E402


class _CountingPool:
    """Wraps a real pool and counts checkouts/releases."""

    def __init__(self, path: str):
        self.real = SQLiteConnectionPool(path, max_size=5)
        self.checkouts = 0
        self.releases = 0

    def get_connection(self):
        self.checkouts += 1
        return self.real.get_connection()

    def release_connection(self, conn):
        self.releases += 1
        self.real.release_connection(conn)

    def close_all(self):
        self.real.close_all()


class _Env:
    def __init__(self, tmp_path: Path):
        self.dir = tempfile.mkdtemp(prefix="p02c_")
        self.db_path = os.path.join(self.dir, "p02c.db")
        conn = setup_test_db(self.db_path)
        conn.execute(
            "INSERT OR IGNORE INTO users (username, hashed_password, full_name, role, is_active, must_change_password) "
            "VALUES ('root', 'x', 'Root', 'superadmin', 1, 0)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO users (username, hashed_password, full_name, role, is_active, must_change_password) "
            "VALUES ('m1', 'x', 'Member', 'member', 1, 0)"
        )
        conn.commit()
        conn.close()
        _pool_cache.clear()

        self.pool = _CountingPool(self.db_path)
        self._saved_pool = deps_mod.get_pool
        deps_mod.get_pool = lambda _p: self.pool
        self._saved_users_enabled = settings.users_enabled
        self._saved_jwt = settings.jwt_secret_key
        settings.users_enabled = True
        settings.jwt_secret_key = os.environ["JWT_SECRET_KEY"]

        app = FastAPI()
        app.include_router(users_router)
        app.dependency_overrides[csrf_protect] = lambda: "test-csrf"
        self.app = app
        self.client = TestClient(app)
        self.client.headers["user-agent"] = ""

    @property
    def admin_headers(self):
        token = get_token(1, "root", "superadmin")
        return {"Authorization": f"Bearer {token}"}

    @property
    def member_headers(self):
        token = get_token(2, "m1", "member")
        return {"Authorization": f"Bearer {token}"}

    def close(self):
        deps_mod.get_pool = self._saved_pool
        settings.users_enabled = self._saved_users_enabled
        settings.jwt_secret_key = self._saved_jwt
        self.pool.close_all()
        _pool_cache.clear()


def test_admin_list_users_is_one_checkout_one_release(tmp_path):
    env = _Env(tmp_path)
    try:
        r = env.client.get("/users", headers=env.admin_headers)
        assert r.status_code == 200, r.text
        assert env.pool.checkouts == 1, (
            "auth dependency and handler must share ONE pooled connection"
        )
        assert env.pool.releases == 1, "the request-scoped connection is released"
    finally:
        env.close()


def test_denied_member_request_no_second_checkout(tmp_path):
    env = _Env(tmp_path)
    try:
        r = env.client.get("/users", headers=env.member_headers)
        assert r.status_code == 403
        assert env.pool.checkouts == 1, (
            "a denied request performs only the auth read; no standalone pool checkout"
        )
        assert env.pool.releases == 1
    finally:
        env.close()


def test_exception_path_releases_the_request_connection(tmp_path):
    env = _Env(tmp_path)
    try:
        with patch(
            "app.api.routes.users.async_hash_password",
            side_effect=RuntimeError("probe"),
        ):
            fault_client = TestClient(
                env.app, raise_server_exceptions=False
            )
            fault_client.headers["user-agent"] = ""
            r = fault_client.post(
                "/users",
                headers=env.admin_headers,
                json={
                    "username": "new1",
                    "password": "Password123x",
                    "full_name": "N",
                    "role": "member",
                },
            )
        assert r.status_code == 500
        assert env.pool.releases == env.pool.checkouts, (
            "every checkout is released even when the handler raises"
        )
    finally:
        env.close()
