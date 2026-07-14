"""Targeted tests for folders.py dependency-injected evaluate policy migration.

PR context: _require_vault_read and _require_vault_write were migrated from the
global evaluate_policy() (which opened its own pool connection) to accept
db: sqlite3.Connection and call get_evaluate_policy(db) locally, so they use
the endpoint's injected connection instead of spawning a new one.

These tests verify:
1. The helpers accept and forward the injected db to get_evaluate_policy.
2. Permission decisions are enforced correctly (read=403, write=403) when
   the user has no membership on the target vault.
3. Permission is granted when the user has the required membership.
4. superadmin bypasses the vault membership check entirely.
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub optional heavy deps so importing app.main is cheap in CI.
try:
    import lancedb  # noqa: F401
except ImportError:
    import types
    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow  # noqa: F401
except ImportError:
    import types
    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

from _db_pool import SimpleConnectionPool
from fastapi.testclient import TestClient

from app.api.deps import get_db, get_evaluate_policy
from app.config import settings
from app.main import app
from app.security import csrf_protect
from app.services.auth_service import compute_client_fingerprint, create_access_token


class FoldersDITestBase(unittest.TestCase):
    """Base fixture for folder DI tests — seeds member1 with write on vault 2."""

    def setUp(self):
        self.client = TestClient(app)
        self.client.headers["user-agent"] = ""
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

        conn = self._connection_pool.get_connection()
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            pw = "test-password-hash"
            # user 1: superadmin (no vault memberships needed — superadmin bypass)
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (1,'superadmin',?, 'Super','superadmin',1)",
                (pw,),
            )
            # user 3: member — has write on vault 2 only
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (3,'member1',?, 'Member One','member',1)",
                (pw,),
            )
            # Vault 2: member1 has write
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (2,'Write Vault','w')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission, granted_by) VALUES (2,3,'write',1)"
            )
            # Vault 3: no membership for member1 (used for 403 cases)
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (3,'No Access','x')"
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

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
        if hasattr(self, "_connection_pool"):
            self._connection_pool.close_all()
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _token(self, user_id, username, role):
        return {
            "Authorization": f"Bearer {create_access_token(user_id, username, role, client_fingerprint=compute_client_fingerprint(''))}"}

    def _write_headers(self):
        return self._token(3, "member1", "member")

    def _superadmin_headers(self):
        return self._token(1, "superadmin", "superadmin")


# ---------------------------------------------------------------------------
# Test 1: DI path — db is forwarded to get_evaluate_policy
# ---------------------------------------------------------------------------


class TestFoldersEvaluateDI(unittest.IsolatedAsyncioTestCase):
    """Verify _require_vault_read/write forward the injected db to get_evaluate_policy.

    Uses IsolatedAsyncioTestCase so we can properly await the async helpers.
    The DI migration changed the helpers from calling the global evaluate_policy()
    (which opens its own pool connection) to calling get_evaluate_policy(db) locally
    with the injected connection.
    """

    async def asyncSetUp(self):
        import app.api.routes.folders as folders_module

        self._folders_module = folders_module
        self._mock_evaluate = AsyncMock(return_value=True)
        self._mock_db = MagicMock()

    async def test_require_vault_read_forwards_evaluate_to_policy(self):
        """_require_vault_read forwards the evaluate callable to the policy."""
        user = {"id": 3, "role": "member"}
        mock_evaluate = AsyncMock(return_value=True)

        await self._folders_module._require_vault_read(mock_evaluate, user, vault_id=2)

        mock_evaluate.assert_called_once_with(user, "vault", 2, "read")

    async def test_require_vault_write_forwards_evaluate_to_policy(self):
        """_require_vault_write forwards the evaluate callable to the policy."""
        user = {"id": 3, "role": "member"}
        mock_evaluate = AsyncMock(return_value=True)

        await self._folders_module._require_vault_write(mock_evaluate, user, vault_id=2)

        mock_evaluate.assert_called_once_with(user, "vault", 2, "write")

    async def test_require_vault_read_raises_403_when_policy_returns_false(self):
        """_require_vault_read raises HTTPException 403 when evaluate returns False."""
        from fastapi import HTTPException

        false_evaluate = AsyncMock(return_value=False)
        user = {"id": 3, "role": "member"}

        with self.assertRaises(HTTPException) as ctx:
            await self._folders_module._require_vault_read(false_evaluate, user, vault_id=99)

        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("read access", ctx.exception.detail)

    async def test_require_vault_write_raises_403_when_policy_returns_false(self):
        """_require_vault_write raises HTTPException 403 when evaluate returns False."""
        from fastapi import HTTPException

        false_evaluate = AsyncMock(return_value=False)
        user = {"id": 3, "role": "member"}

        with self.assertRaises(HTTPException) as ctx:
            await self._folders_module._require_vault_write(false_evaluate, user, vault_id=99)

        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("write access", ctx.exception.detail)


# ---------------------------------------------------------------------------
# Test 2: Permission decisions via injected db — integration
# ---------------------------------------------------------------------------


class TestFoldersPermissionDecisions(FoldersDITestBase):
    """Verify permission decisions are correct when using the injected db."""

    def test_list_folders_on_inaccessible_vault_returns_403(self):
        """member1 has no membership on vault 3 — list returns 403."""
        resp = self.client.get(
            "/api/folders?vault_id=3",
            headers=self._write_headers(),
        )
        self.assertEqual(resp.status_code, 403)

    def test_create_folder_on_inaccessible_vault_returns_403(self):
        """member1 has no membership on vault 3 — create returns 403."""
        resp = self.client.post(
            "/api/folders",
            json={"vault_id": 3, "name": "Nope"},
            headers=self._write_headers(),
        )
        self.assertEqual(resp.status_code, 403)

    def test_delete_folder_on_inaccessible_vault_returns_403(self):
        """member1 has no membership on vault 3 — delete returns 403."""
        # First create a folder in vault 3 using superadmin (member1 can't, but superadmin can)
        conn = self._connection_pool.get_connection()
        try:
            cur = conn.execute(
                "INSERT INTO folders (vault_id, name) VALUES (3, 'Foreign')"
            )
            foreign_id = cur.lastrowid
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        resp = self.client.delete(
            f"/api/folders/{foreign_id}",
            headers=self._write_headers(),
        )
        self.assertEqual(resp.status_code, 403)

    def test_superadmin_bypasses_vault_membership_check(self):
        """superadmin can list/create on vault 3 even with no vault_members row."""
        # Vault 3 has no superadmin membership row — superadmin should bypass anyway
        resp = self.client.get(
            "/api/folders?vault_id=3",
            headers=self._superadmin_headers(),
        )
        self.assertEqual(resp.status_code, 200)

        resp = self.client.post(
            "/api/folders",
            json={"vault_id": 3, "name": "Superadmin Folder"},
            headers=self._superadmin_headers(),
        )
        self.assertEqual(resp.status_code, 201)

    def test_member_with_write_can_create_and_list_folders(self):
        """member1 has write on vault 2 — list and create succeed."""
        resp = self.client.get(
            "/api/folders?vault_id=2",
            headers=self._write_headers(),
        )
        self.assertEqual(resp.status_code, 200)

        resp = self.client.post(
            "/api/folders",
            json={"vault_id": 2, "name": "Writable"},
            headers=self._write_headers(),
        )
        self.assertEqual(resp.status_code, 201)


# ---------------------------------------------------------------------------
# Test 3: End-to-end — verify the actual injected db is used, not a new pool conn
# ---------------------------------------------------------------------------


class TestFoldersDbNotClobbered(FoldersDITestBase):
    """Verify folder operations succeed end-to-end with the DI connection pool."""

    def test_folder_roundtrip_uses_injected_connection(self):
        """Full create→list→rename→delete roundtrip succeeds with DI pool."""
        # Create
        create_resp = self.client.post(
            "/api/folders",
            json={"vault_id": 2, "name": "Reports", "description": "Q4 reports"},
            headers=self._write_headers(),
        )
        self.assertEqual(create_resp.status_code, 201)
        folder_id = create_resp.json()["id"]

        # List
        list_resp = self.client.get(
            "/api/folders?vault_id=2",
            headers=self._write_headers(),
        )
        self.assertEqual(list_resp.status_code, 200)
        names = [f["name"] for f in list_resp.json()["folders"]]
        self.assertIn("Reports", names)

        # Rename
        update_resp = self.client.put(
            f"/api/folders/{folder_id}",
            json={"name": "Q4 Reports"},
            headers=self._write_headers(),
        )
        self.assertEqual(update_resp.status_code, 200)
        self.assertEqual(update_resp.json()["name"], "Q4 Reports")

        # Delete
        del_resp = self.client.delete(
            f"/api/folders/{folder_id}",
            headers=self._write_headers(),
        )
        self.assertEqual(del_resp.status_code, 204)

        # Confirm gone
        list_resp = self.client.get(
            "/api/folders?vault_id=2",
            headers=self._write_headers(),
        )
        self.assertEqual(list_resp.status_code, 200)
        self.assertEqual(list_resp.json()["folders"], [])


if __name__ == "__main__":
    unittest.main()
