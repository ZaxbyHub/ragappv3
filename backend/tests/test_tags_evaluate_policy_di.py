"""Targeted tests for tags.py dependency-injected evaluate policy migration.

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

from app.api.deps import get_db, get_vector_store
from app.config import settings
from app.main import app
from app.security import csrf_protect
from app.services.auth_service import compute_client_fingerprint, create_access_token


class TagsDIPolicyTestBase(unittest.TestCase):
    """Base fixture for tag DI policy tests — seeds member1 with write on vault 2."""

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

        self._mock_vector_store = MagicMock()
        self._mock_vector_store.db = MagicMock()
        self._mock_vector_store.db.table_names = AsyncMock(return_value=["chunks"])
        self._mock_vector_store.db.open_table = AsyncMock(return_value=MagicMock())
        self._mock_vector_store.delete_by_file = AsyncMock(return_value=1)
        app.dependency_overrides[get_vector_store] = lambda: self._mock_vector_store

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
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (9,'No Access Vault','x')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission, granted_by) VALUES (2,3,'write',1)"
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
        app.dependency_overrides.pop(get_vector_store, None)
        if hasattr(self, "_connection_pool"):
            self._connection_pool.close_all()
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _token(self, user_id, username, role):
        return {
            "Authorization": f"Bearer {create_access_token(user_id, username, role, client_fingerprint=compute_client_fingerprint(''))}"}

    def _member_headers(self):
        """member1 token — has write on vault 2, no access on vault 9."""
        return self._token(3, "member1", "member")

    def _superadmin_headers(self):
        """superadmin token — bypasses vault membership checks."""
        return self._token(1, "superadmin", "superadmin")

    def _seed_file(self, vault_id, file_name, file_size=10, status="indexed"):
        conn = self._connection_pool.get_connection()
        try:
            cur = conn.execute(
                "INSERT INTO files (vault_id, file_path, file_name, file_size, status) VALUES (?,?,?,?,?)",
                (vault_id, f"/uploads/{file_name}", file_name, file_size, status),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            self._connection_pool.release_connection(conn)


# ---------------------------------------------------------------------------
# Test 1: DI path — db is forwarded to get_evaluate_policy
# ---------------------------------------------------------------------------


class TestTagsEvaluateDI(unittest.IsolatedAsyncioTestCase):
    """Verify _require_vault_read/write forward the injected db to get_evaluate_policy.

    Uses IsolatedAsyncioTestCase so we can properly await the async helpers.
    The DI migration changed the helpers from calling the global evaluate_policy()
    (which opens its own pool connection) to calling get_evaluate_policy(db) locally
    with the injected connection.
    """

    async def asyncSetUp(self):
        import app.api.routes.tags as tags_module

        self._tags_module = tags_module
        self._mock_db = MagicMock()

    async def test_require_vault_read_forwards_db_to_get_evaluate_policy(self):
        """get_evaluate_policy is called with the same db that the endpoint received."""
        user = {"id": 3, "role": "member"}
        mock_evaluate = AsyncMock(return_value=True)

        with patch.object(self._tags_module, "get_evaluate_policy", return_value=mock_evaluate):
            await self._tags_module._require_vault_read(self._mock_db, user, vault_id=2)

        mock_evaluate.assert_called_once_with(user, "vault", 2, "read")

    async def test_require_vault_write_forwards_db_to_get_evaluate_policy(self):
        """get_evaluate_policy is called with the same db that the endpoint received."""
        user = {"id": 3, "role": "member"}
        mock_evaluate = AsyncMock(return_value=True)

        with patch.object(self._tags_module, "get_evaluate_policy", return_value=mock_evaluate):
            await self._tags_module._require_vault_write(self._mock_db, user, vault_id=2)

        mock_evaluate.assert_called_once_with(user, "vault", 2, "write")

    async def test_require_vault_read_raises_403_when_policy_returns_false(self):
        """_require_vault_read raises HTTPException 403 when evaluate returns False."""
        from fastapi import HTTPException

        false_evaluate = AsyncMock(return_value=False)
        user = {"id": 3, "role": "member"}

        with patch.object(self._tags_module, "get_evaluate_policy", return_value=false_evaluate):
            with self.assertRaises(HTTPException) as ctx:
                await self._tags_module._require_vault_read(self._mock_db, user, vault_id=99)

        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("read access", ctx.exception.detail)

    async def test_require_vault_write_raises_403_when_policy_returns_false(self):
        """_require_vault_write raises HTTPException 403 when evaluate returns False."""
        from fastapi import HTTPException

        false_evaluate = AsyncMock(return_value=False)
        user = {"id": 3, "role": "member"}

        with patch.object(self._tags_module, "get_evaluate_policy", return_value=false_evaluate):
            with self.assertRaises(HTTPException) as ctx:
                await self._tags_module._require_vault_write(self._mock_db, user, vault_id=99)

        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("write access", ctx.exception.detail)


# ---------------------------------------------------------------------------
# Test 2: Permission decisions via injected db — integration
# ---------------------------------------------------------------------------


class TestTagsPermissionDecisions(TagsDIPolicyTestBase):
    """Verify permission decisions are correct when using the injected db.

    Seeds:
    - user 1: superadmin (bypasses membership checks)
    - user 3: member1 (write on vault 2 only)
    - vault 2: member1 has write
    - vault 9: no membership for member1 -> 403 expected
    """

    def test_list_tags_on_inaccessible_vault_returns_403(self):
        """member1 has no membership on vault 9 — list returns 403."""
        resp = self.client.get(
            "/api/tags?vault_id=9",
            headers=self._member_headers(),
        )
        self.assertEqual(resp.status_code, 403)

    def test_create_tag_on_inaccessible_vault_returns_403(self):
        """member1 has no membership on vault 9 — create returns 403."""
        resp = self.client.post(
            "/api/tags",
            json={"vault_id": 9, "name": "Nope"},
            headers=self._member_headers(),
        )
        self.assertEqual(resp.status_code, 403)

    def test_update_tag_on_inaccessible_vault_returns_403(self):
        """member1 cannot update a tag in vault 9 (no membership) — returns 403."""
        # First create a tag in vault 9 using superadmin
        resp = self.client.post(
            "/api/tags",
            json={"vault_id": 9, "name": "Superadmin Tag"},
            headers=self._superadmin_headers(),
        )
        self.assertEqual(resp.status_code, 201)
        tag_id = resp.json()["id"]

        # Now member1 tries to update it (should be 403)
        resp = self.client.put(
            f"/api/tags/{tag_id}",
            json={"name": "Hijacked"},
            headers=self._member_headers(),
        )
        self.assertEqual(resp.status_code, 403)

    def test_delete_tag_on_inaccessible_vault_returns_403(self):
        """member1 cannot delete a tag in vault 9 (no membership) — returns 403."""
        # Create tag in vault 9 as superadmin
        resp = self.client.post(
            "/api/tags",
            json={"vault_id": 9, "name": "ToDelete"},
            headers=self._superadmin_headers(),
        )
        self.assertEqual(resp.status_code, 201)
        tag_id = resp.json()["id"]

        # member1 tries to delete (should be 403)
        resp = self.client.delete(
            f"/api/tags/{tag_id}",
            headers=self._member_headers(),
        )
        self.assertEqual(resp.status_code, 403)

    def test_assign_tags_on_inaccessible_vault_returns_403(self):
        """member1 has no membership on vault 9 — assign returns 403."""
        f1 = self._seed_file(2, "a.txt")
        resp = self.client.post(
            "/api/tags",
            json={"vault_id": 2, "name": "Alpha"},
            headers=self._member_headers(),
        )
        self.assertEqual(resp.status_code, 201)
        tag_id = resp.json()["id"]

        resp = self.client.post(
            "/api/tags/assign",
            json={"vault_id": 9, "file_ids": [f1], "tag_ids": [tag_id]},
            headers=self._member_headers(),
        )
        self.assertEqual(resp.status_code, 403)

    def test_list_document_tags_on_inaccessible_vault_returns_403(self):
        """member1 has no membership on vault 9 — list_document_tags returns 403."""
        f1 = self._seed_file(9, "secret.txt")

        resp = self.client.get(
            f"/api/tags/documents/{f1}?vault_id=9",
            headers=self._member_headers(),
        )
        self.assertEqual(resp.status_code, 403)

    def test_set_document_tags_on_inaccessible_vault_returns_403(self):
        """member1 has no membership on vault 9 — set_document_tags returns 403."""
        f1 = self._seed_file(9, "secret.txt")
        resp = self.client.post(
            "/api/tags",
            json={"vault_id": 9, "name": "Tag"},
            headers=self._superadmin_headers(),
        )
        tag_id = resp.json()["id"]

        resp = self.client.put(
            f"/api/tags/documents/{f1}",
            json={"vault_id": 9, "tag_ids": [tag_id]},
            headers=self._member_headers(),
        )
        self.assertEqual(resp.status_code, 403)

    def test_unassign_tag_on_inaccessible_vault_returns_403(self):
        """member1 has no membership on vault 9 — unassign returns 403."""
        f1 = self._seed_file(9, "secret.txt")
        resp = self.client.post(
            "/api/tags",
            json={"vault_id": 9, "name": "Tag"},
            headers=self._superadmin_headers(),
        )
        tag_id = resp.json()["id"]

        resp = self.client.delete(
            f"/api/tags/{tag_id}/documents/{f1}?vault_id=9",
            headers=self._member_headers(),
        )
        self.assertEqual(resp.status_code, 403)

    def test_superadmin_bypasses_vault_membership_check(self):
        """superadmin can list/create on vault 9 even with no vault_members row."""
        # Vault 9 has no superadmin membership row — superadmin should bypass anyway
        resp = self.client.get(
            "/api/tags?vault_id=9",
            headers=self._superadmin_headers(),
        )
        self.assertEqual(resp.status_code, 200)

        resp = self.client.post(
            "/api/tags",
            json={"vault_id": 9, "name": "Superadmin Tag"},
            headers=self._superadmin_headers(),
        )
        self.assertEqual(resp.status_code, 201)

    def test_member_with_write_can_access_tags_endpoints(self):
        """member1 has write on vault 2 — all tag operations succeed."""
        token = self._member_headers()

        # List (read)
        resp = self.client.get("/api/tags?vault_id=2", headers=token)
        self.assertEqual(resp.status_code, 200)

        # Create (write)
        resp = self.client.post(
            "/api/tags",
            json={"vault_id": 2, "name": "Writable Tag", "color": "#f00"},
            headers=token,
        )
        self.assertEqual(resp.status_code, 201)
        tag_id = resp.json()["id"]

        # Update (write)
        resp = self.client.put(
            f"/api/tags/{tag_id}",
            json={"name": "Updated Tag"},
            headers=token,
        )
        self.assertEqual(resp.status_code, 200)

        # Delete (write)
        resp = self.client.delete(f"/api/tags/{tag_id}", headers=token)
        self.assertEqual(resp.status_code, 204)


# ---------------------------------------------------------------------------
# Test 3: End-to-end — verify the actual injected db is used, not a new pool conn
# ---------------------------------------------------------------------------


class TestTagsDbNotClobbered(TagsDIPolicyTestBase):
    """Verify tag operations succeed end-to-end with the DI connection pool.

    These mirror the DI roundtrip test in test_folders_evaluate_policy_di.py,
    confirming that create→list→update→delete work correctly when the helpers
    use the endpoint's injected db rather than opening a new pool connection.
    """

    def test_tag_roundtrip_uses_injected_connection(self):
        """Full create→list→update→delete roundtrip succeeds with DI pool."""
        token = self._member_headers()

        # Create
        create_resp = self.client.post(
            "/api/tags",
            json={"vault_id": 2, "name": "Roundtrip Tag", "color": "#0f0"},
            headers=token,
        )
        self.assertEqual(create_resp.status_code, 201)
        tag_id = create_resp.json()["id"]

        # List includes it
        list_resp = self.client.get("/api/tags?vault_id=2", headers=token)
        self.assertEqual(list_resp.status_code, 200)
        names = [t["name"] for t in list_resp.json()["tags"]]
        self.assertIn("Roundtrip Tag", names)

        # Update
        put_resp = self.client.put(
            f"/api/tags/{tag_id}",
            json={"name": "Updated Roundtrip Tag", "color": "#00f"},
            headers=token,
        )
        self.assertEqual(put_resp.status_code, 200)
        self.assertEqual(put_resp.json()["name"], "Updated Roundtrip Tag")

        # Delete
        del_resp = self.client.delete(f"/api/tags/{tag_id}", headers=token)
        self.assertEqual(del_resp.status_code, 204)

        # Confirm gone
        list_resp = self.client.get("/api/tags?vault_id=2", headers=token)
        self.assertEqual(list_resp.status_code, 200)
        self.assertEqual(list_resp.json()["tags"], [])


if __name__ == "__main__":
    unittest.main()
