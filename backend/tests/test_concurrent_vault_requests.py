"""Concurrency integration test for vault-protected routes.

Fires 10 simultaneous vault-protected requests and asserts every response is
2xx with no 503s. Targets FR-006 and uses the canonical route-test
dependency-override / SimpleConnectionPool pattern.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx

try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")
from _db_pool import SimpleConnectionPool

from app.api.deps import (
    get_current_active_user,
    get_db,
    get_evaluate_policy,
)
from app.config import settings
from app.main import app
from app.security import csrf_protect
from app.services.auth_service import create_access_token


class ConcurrentVaultRequestsTest(unittest.TestCase):
    def setUp(self):
        self.client = app

        self._temp_dir = tempfile.mkdtemp()

        self._original_jwt_secret = settings.jwt_secret_key
        self._original_users_enabled = settings.users_enabled
        self._original_data_dir = settings.data_dir

        settings.data_dir = Path(self._temp_dir)
        settings.jwt_secret_key = os.urandom(32).hex()
        settings.users_enabled = True

        self._db_path = str(Path(self._temp_dir) / "app.db")

        from app.models.database import _pool_cache, _pool_cache_lock

        with _pool_cache_lock:
            for _path, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()

        from app.models.database import init_db, run_migrations

        init_db(self._db_path)
        run_migrations(self._db_path)
        self._connection_pool = SimpleConnectionPool(self._db_path)

        def override_get_db():
            conn = self._connection_pool.get_connection()
            try:
                yield conn
            finally:
                self._connection_pool.release_connection(conn)

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[csrf_protect] = lambda: "test-csrf"

        app.dependency_overrides[get_current_active_user] = lambda: {
            "id": 1,
            "username": "concurrent-user",
            "full_name": "Concurrent User",
            "role": "superadmin",
            "is_active": True,
            "must_change_password": False,
        }

        async def _allow_policy(principal, resource_type, resource_id, action):
            return True

        app.dependency_overrides[get_evaluate_policy] = lambda: _allow_policy

        conn = self._connection_pool.get_connection()
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) "
                "VALUES (1,'concurrent-user','test-hash','Concurrent User','superadmin',1)"
            )
            for user_id in range(2, 13):
                conn.execute(
                    "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) "
                    f"VALUES ({user_id},'concurrent-user-{user_id}','test-hash','Concurrent User {user_id}','member',1)"
                )
            conn.commit()
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (10,'Concurrent Vault','concurrency target')"
            )
            conn.commit()
            for user_id in range(1, 11):
                permission = "admin" if user_id <= 4 else "write" if user_id <= 7 else "read"
                conn.execute(
                    "INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission, granted_by) "
                    f"VALUES (10,{user_id},'{permission}',1)"
                )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        self._tokens = [
            create_access_token(
                user_id,
                f"concurrent-user-{user_id}",
                "superadmin" if user_id == 1 else "member",
            )
            for user_id in range(1, 11)
        ]
        self._target_vault_id = 10

    def tearDown(self):
        from app.models.database import _pool_cache, _pool_cache_lock

        with _pool_cache_lock:
            for _path, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()

        settings.jwt_secret_key = self._original_jwt_secret
        settings.users_enabled = self._original_users_enabled
        settings.data_dir = self._original_data_dir
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(csrf_protect, None)
        app.dependency_overrides.pop(get_current_active_user, None)
        app.dependency_overrides.pop(get_evaluate_policy, None)
        if hasattr(self, "_connection_pool"):
            self._connection_pool.close_all()
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def test_ten_concurrent_vault_requests_all_succeed(self):
        auth_headers = [{"Authorization": f"Bearer {token}"} for token in self._tokens]

        async def fire_requests():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.client),
                base_url="http://testserver",
            ) as client:
                tasks = []
                for index, headers in enumerate(auth_headers):
                    if index <= 2:
                        tasks.append(
                            client.get(
                                f"/api/vaults/{self._target_vault_id}",
                                headers=headers,
                            )
                        )
                    elif index <= 4:
                        tasks.append(
                            client.post(
                                f"/api/vaults/{self._target_vault_id}/members/",
                                headers=headers,
                                json={"member_user_id": 11 + (index - 3), "permission": "read"},
                            )
                        )
                    elif index <= 6:
                        tasks.append(
                            client.put(
                                f"/api/vaults/{self._target_vault_id}",
                                headers=headers,
                                json={"name": f"Updated Vault {index}", "description": "updated"},
                            )
                        )
                    else:
                        tasks.append(
                            client.get(
                                f"/api/vaults/{self._target_vault_id}/members/",
                                headers=headers,
                            )
                        )
                return await asyncio.gather(*tasks, return_exceptions=True)

        responses = asyncio.run(fire_requests())
        status_codes = []
        failures = []
        for index, response in enumerate(responses):
            if isinstance(response, Exception):
                failures.append(f"Request {index} raised {response}")
                continue
            status_codes.append(response.status_code)
            if response.status_code < 200 or response.status_code >= 300:
                failures.append(
                    f"Request {index} returned status {response.status_code}: {response.text}"
                )

        self.assertEqual(len(responses), 10)
        self.assertEqual(len(failures), 0, "\n".join(failures))
        self.assertEqual(status_codes.count(503), 0, "Unexpected 503 response")


if __name__ == "__main__":
    unittest.main()
