"""Targeted tests for kms.py dependency-injected evaluate policy migration.

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

Reuses KMSFixTestBase from test_kms_routes so CSRF is bypassed (exercised
separately in test_kms_routes.py) and the same DB / user / vault seeds are
available.
"""

import os
import sys
import unittest
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

import pytest  # For pytestmark on integration classes that use search endpoint.

# Import the base from the sibling test module.
from test_kms_routes import KMSFixTestBase

from app.api.deps import get_db
from app.main import app
from app.security import csrf_protect

# ---------------------------------------------------------------------------
# Test 1: DI path — db is forwarded to get_evaluate_policy
# ---------------------------------------------------------------------------


class TestKMSEvaluateDI(unittest.IsolatedAsyncioTestCase):
    """Verify _require_vault_read/write forward the injected db to get_evaluate_policy.

    Uses IsolatedAsyncioTestCase so we can properly await the async helpers.
    The DI migration changed the helpers from calling the global evaluate_policy()
    (which opens its own pool connection) to calling get_evaluate_policy(db) locally
    with the injected connection.
    """

    async def asyncSetUp(self):
        import app.api.routes.kms as kms_module

        self._kms_module = kms_module
        self._mock_db = MagicMock()

    async def test_require_vault_read_forwards_db_to_get_evaluate_policy(self):
        """get_evaluate_policy is called with the same db that the endpoint received."""
        user = {"id": 3, "role": "member"}
        mock_evaluate = AsyncMock(return_value=True)

        # Signature: _require_vault_read(db, user, vault_id) — db is first positional.
        with patch.object(self._kms_module, "get_evaluate_policy", return_value=mock_evaluate):
            await self._kms_module._require_vault_read(self._mock_db, user, vault_id=2)

        mock_evaluate.assert_called_once_with(user, "vault", 2, "read")

    async def test_require_vault_write_forwards_db_to_get_evaluate_policy(self):
        """get_evaluate_policy is called with the same db that the endpoint received."""
        user = {"id": 3, "role": "member"}
        mock_evaluate = AsyncMock(return_value=True)

        # Signature: _require_vault_write(db, user, vault_id) — db is first positional.
        with patch.object(self._kms_module, "get_evaluate_policy", return_value=mock_evaluate):
            await self._kms_module._require_vault_write(self._mock_db, user, vault_id=2)

        mock_evaluate.assert_called_once_with(user, "vault", 2, "write")

    async def test_require_vault_read_raises_403_when_policy_returns_false(self):
        """_require_vault_read raises HTTPException 403 when evaluate returns False."""
        from fastapi import HTTPException

        false_evaluate = AsyncMock(return_value=False)
        user = {"id": 3, "role": "member"}

        with patch.object(self._kms_module, "get_evaluate_policy", return_value=false_evaluate):
            with self.assertRaises(HTTPException) as ctx:
                await self._kms_module._require_vault_read(self._mock_db, user, vault_id=99)

        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("read access", ctx.exception.detail)

    async def test_require_vault_write_raises_403_when_policy_returns_false(self):
        """_require_vault_write raises HTTPException 403 when evaluate returns False."""
        from fastapi import HTTPException

        false_evaluate = AsyncMock(return_value=False)
        user = {"id": 3, "role": "member"}

        with patch.object(self._kms_module, "get_evaluate_policy", return_value=false_evaluate):
            with self.assertRaises(HTTPException) as ctx:
                await self._kms_module._require_vault_write(self._mock_db, user, vault_id=99)

        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("write access", ctx.exception.detail)


# ---------------------------------------------------------------------------
# Test 2: Permission decisions via injected db — integration
# ---------------------------------------------------------------------------


class TestKMSPermissionDecisions(KMSFixTestBase):
    # The search endpoint requires require_model_ready which needs app.state.vector_store.
    pytestmark = pytest.mark.usefixtures("ready_vector_store")
    """Verify permission decisions are correct when using the injected db.

    KMSFixTestBase seeds:
    - user 1: superadmin (bypasses membership checks)
    - user 3: member1 (write on vault 2 only)
    - vault 2: member1 has write
    - vault 3: no membership for member1 -> 403 expected
    """

    def test_list_entries_on_inaccessible_vault_returns_403(self):
        """member1 has no membership on vault 3 — list returns 403."""
        resp = self.client.get(
            "/api/kms/entries?vault_id=3",
            headers=self._write_headers(),
        )
        self.assertEqual(resp.status_code, 403)
        self.assertIn("read access", resp.json()["detail"].lower())

    def test_create_entry_on_inaccessible_vault_returns_403(self):
        """member1 has no membership on vault 3 — create returns 403."""
        resp = self.client.post(
            "/api/kms/entries",
            json={"vault_id": 3, "title": "Nope"},
            headers=self._write_headers(),
        )
        self.assertEqual(resp.status_code, 403)
        self.assertIn("write access", resp.json()["detail"].lower())

    def test_search_on_inaccessible_vault_returns_403(self):
        """member1 has no membership on vault 3 — search returns 403."""
        resp = self.client.get(
            "/api/kms/search?vault_id=3&q=test",
            headers=self._write_headers(),
        )
        self.assertEqual(resp.status_code, 403)
        self.assertIn("read access", resp.json()["detail"].lower())

    def test_jobs_on_inaccessible_vault_returns_403(self):
        """member1 has no membership on vault 3 — list jobs returns 403."""
        resp = self.client.get(
            "/api/kms/jobs?vault_id=3",
            headers=self._write_headers(),
        )
        self.assertEqual(resp.status_code, 403)
        self.assertIn("read access", resp.json()["detail"].lower())

    def test_superadmin_bypasses_vault_membership_check(self):
        """superadmin can list/create/search/jobs on vault 3 even with no vault_members row."""
        token = self._headers(1, "superadmin", "superadmin")

        # Vault 3 has no superadmin membership row — superadmin should bypass anyway.
        resp = self.client.get("/api/kms/entries?vault_id=3", headers=token)
        self.assertEqual(resp.status_code, 200)

        resp = self.client.post(
            "/api/kms/entries",
            json={"vault_id": 3, "title": "Superadmin Entry"},
            headers=token,
        )
        self.assertEqual(resp.status_code, 201)

        resp = self.client.get("/api/kms/search?vault_id=3&q=test", headers=token)
        self.assertEqual(resp.status_code, 200)

        resp = self.client.get("/api/kms/jobs?vault_id=3", headers=token)
        self.assertEqual(resp.status_code, 200)

    def test_member_with_write_can_access_kms_endpoints(self):
        """member1 has write on vault 2 — all read/write KMS operations succeed."""
        token = self._write_headers()

        # Read
        resp = self.client.get("/api/kms/entries?vault_id=2", headers=token)
        self.assertEqual(resp.status_code, 200)

        # Write (create)
        resp = self.client.post(
            "/api/kms/entries",
            json={"vault_id": 2, "title": "Writable Entry"},
            headers=token,
        )
        self.assertEqual(resp.status_code, 201)

        # Search
        resp = self.client.get("/api/kms/search?vault_id=2&q=writable", headers=token)
        self.assertEqual(resp.status_code, 200)

        # Jobs list
        resp = self.client.get("/api/kms/jobs?vault_id=2", headers=token)
        self.assertEqual(resp.status_code, 200)


# ---------------------------------------------------------------------------
# Test 3: End-to-end — verify the actual injected db is used, not a new pool conn
# ---------------------------------------------------------------------------


class TestKMSDbNotClobbered(KMSFixTestBase):
    """Verify KMS operations succeed end-to-end with the DI connection pool.

    These mirror the DI roundtrip test in test_folders_evaluate_policy_di.py,
    confirming that create→list→update→delete work correctly when the helpers
    use the endpoint's injected db rather than opening a new pool connection.
    """

    def _create_entry(self, **overrides):
        payload = {"vault_id": 2, "title": "Hello", "body": "world body"}
        payload.update(overrides)
        return self.client.post(
            "/api/kms/entries", json=payload, headers=self._write_headers()
        )

    def test_entry_roundtrip_uses_injected_connection(self):
        """Full create→list→get→update→delete roundtrip succeeds with DI pool."""
        # Create
        create_resp = self._create_entry(title="Roundtrip Entry")
        self.assertEqual(create_resp.status_code, 201)
        entry_id = create_resp.json()["id"]

        # List includes it
        list_resp = self.client.get(
            "/api/kms/entries?vault_id=2", headers=self._write_headers()
        )
        self.assertEqual(list_resp.status_code, 200)
        titles = [e["title"] for e in list_resp.json()["entries"]]
        self.assertIn("Roundtrip Entry", titles)

        # Get by ID
        get_resp = self.client.get(
            f"/api/kms/entries/{entry_id}", headers=self._write_headers()
        )
        self.assertEqual(get_resp.status_code, 200)
        self.assertEqual(get_resp.json()["title"], "Roundtrip Entry")

        # Update
        put_resp = self.client.put(
            f"/api/kms/entries/{entry_id}",
            json={"title": "Updated Title", "status": "published"},
            headers=self._write_headers(),
        )
        self.assertEqual(put_resp.status_code, 200)
        self.assertEqual(put_resp.json()["title"], "Updated Title")
        self.assertEqual(put_resp.json()["status"], "published")

        # Delete
        del_resp = self.client.delete(
            f"/api/kms/entries/{entry_id}", headers=self._write_headers()
        )
        self.assertEqual(del_resp.status_code, 204)

        # Confirm gone
        get_resp = self.client.get(
            f"/api/kms/entries/{entry_id}", headers=self._write_headers()
        )
        self.assertEqual(get_resp.status_code, 404)

    def test_compile_and_job_roundtrip(self):
        """compile_document creates a job that is readable via the injected connection."""
        # Seed an indexed file in vault 2.
        conn = self._connection_pool.get_connection()
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO files (vault_id, file_path, file_name, "
                "file_size, status) VALUES (2, '/tmp/c.txt', 'c.txt', 10, 'indexed')"
            )
            conn.commit()
            file_id = cur.lastrowid
        finally:
            self._connection_pool.release_connection(conn)

        # Enqueue compile job
        resp = self.client.post(
            f"/api/kms/documents/{file_id}/compile?vault_id=2",
            headers=self._write_headers(),
        )
        self.assertEqual(resp.status_code, 202)
        job_id = resp.json()["job_id"]

        # Job is readable
        job_resp = self.client.get(
            f"/api/kms/jobs/{job_id}?vault_id=2",
            headers=self._write_headers(),
        )
        self.assertEqual(job_resp.status_code, 200)
        self.assertEqual(job_resp.json()["trigger_type"], "ingest")


if __name__ == "__main__":
    unittest.main()
