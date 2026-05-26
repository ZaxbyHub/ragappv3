"""
Tests for vault creation permission auto-grant.

Verifies that when a user creates a vault, they are automatically
added to vault_members with 'admin' permission in an atomic transaction.
"""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies
try:
    import lancedb
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow
except ImportError:
    import types

    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

try:
    from unstructured.partition.auto import partition
except ImportError:
    import types

    _unstructured = types.ModuleType("unstructured")
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType("unstructured.partition")
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType("unstructured.partition.auto")
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    _unstructured.chunking = types.ModuleType("unstructured.chunking")
    _unstructured.chunking.__path__ = []
    _unstructured.chunking.title = types.ModuleType("unstructured.chunking.title")
    _unstructured.chunking.title.chunk_by_title = lambda *args, **kwargs: []
    _unstructured.documents = types.ModuleType("unstructured.documents")
    _unstructured.documents.__path__ = []
    _unstructured.documents.elements = types.ModuleType(
        "unstructured.documents.elements"
    )
    _unstructured.documents.elements.Element = type("Element", (), {})
    sys.modules["unstructured"] = _unstructured
    sys.modules["unstructured.partition"] = _unstructured.partition
    sys.modules["unstructured.partition.auto"] = _unstructured.partition.auto
    sys.modules["unstructured.chunking"] = _unstructured.chunking
    sys.modules["unstructured.chunking.title"] = _unstructured.chunking.title
    sys.modules["unstructured.documents"] = _unstructured.documents
    sys.modules["unstructured.documents.elements"] = _unstructured.documents.elements

import unittest

from _db_pool import SimpleConnectionPool
from fastapi.testclient import TestClient

from app.api.deps import get_current_active_user, get_db, get_vector_store
from app.main import app


def _make_member_user_dep(user_id=42, username="member1", role="member"):
    """Return a dependency override callable that returns a member user dict."""

    async def _override():
        return {
            "id": user_id,
            "username": username,
            "full_name": "Member User",
            "role": role,
            "is_active": True,
            "must_change_password": False,
        }

    return _override


class TestVaultCreatePermissionAutoGrant(unittest.TestCase):
    """Test that creating a vault auto-grants admin permission to the creator."""

    def setUp(self):
        self.client = TestClient(app)
        self._temp_dir = tempfile.mkdtemp()
        db_path = str(Path(self._temp_dir) / "test.db")
        from app.models.database import init_db

        init_db(db_path)
        self._connection_pool = SimpleConnectionPool(db_path)

        def override_get_db():
            conn = self._connection_pool.get_connection()
            try:
                yield conn
            finally:
                self._connection_pool.release_connection(conn)

        self._mock_vector_store = MagicMock()
        self._mock_vector_store.delete_by_vault = MagicMock(return_value=0)

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_vector_store] = lambda: self._mock_vector_store
        self._db_path = db_path
        self._next_user_id = 100  # auto-increment for test users

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_vector_store, None)
        app.dependency_overrides.pop(get_current_active_user, None)
        if hasattr(self, "_connection_pool"):
            self._connection_pool.close_all()
        import shutil

        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _get_conn(self):
        return self._connection_pool.get_connection()

    def _release_conn(self, conn):
        self._connection_pool.release_connection(conn)

    def _insert_user(self, user_id, username=None, role="member"):
        """Insert a test user into the database (required for FK constraint)."""
        if username is None:
            username = f"user_{user_id}"
        conn = self._get_conn()
        try:
            conn.execute(
                "INSERT INTO users (id, username, hashed_password, role) VALUES (?, ?, ?, ?)",
                (user_id, username, "fakehash", role),
            )
            conn.commit()
        finally:
            self._release_conn(conn)

    def _create_member_user(self, role="member"):
        """Create a test user in DB and return (user_id, override_callable)."""
        uid = self._next_user_id
        self._next_user_id += 1
        self._insert_user(uid, role=role)
        return uid, _make_member_user_dep(uid, f"user_{uid}", role)

    def _query_vault_members(self, vault_id, user_id):
        """Return the vault_members permission for a given vault+user, or None."""
        conn = self._get_conn()
        try:
            cursor = conn.execute(
                "SELECT permission FROM vault_members WHERE vault_id = ? AND user_id = ?",
                (vault_id, user_id),
            )
            row = cursor.fetchone()
            return row["permission"] if row else None
        finally:
            self._release_conn(conn)

    def _count_vault_members(self, vault_id):
        """Return number of vault_members rows for a vault."""
        conn = self._get_conn()
        try:
            cursor = conn.execute(
                "SELECT COUNT(*) as cnt FROM vault_members WHERE vault_id = ?",
                (vault_id,),
            )
            return cursor.fetchone()["cnt"]
        finally:
            self._release_conn(conn)

    def _count_vaults(self):
        """Return total number of vaults."""
        conn = self._get_conn()
        try:
            cursor = conn.execute("SELECT COUNT(*) as cnt FROM vaults")
            return cursor.fetchone()["cnt"]
        finally:
            self._release_conn(conn)

    # ------------------------------------------------------------------ #
    # Scenario 1: Member user creates vault → auto-added as admin
    # ------------------------------------------------------------------ #

    def test_member_creates_vault_auto_grants_admin(self):
        """Member user creates vault → vault_members row with permission='admin'."""
        user_id, override = self._create_member_user()
        app.dependency_overrides[get_current_active_user] = override

        resp = self.client.post(
            "/api/vaults",
            json={"name": "MemberVault", "description": "test"},
        )
        self.assertEqual(resp.status_code, 201)
        vault_id = resp.json()["id"]

        permission = self._query_vault_members(vault_id, user_id)
        self.assertEqual(permission, "admin")

    def test_member_creates_vault_single_member_row(self):
        """Only one vault_members row is created for the new vault."""
        user_id, override = self._create_member_user()
        app.dependency_overrides[get_current_active_user] = override

        resp = self.client.post("/api/vaults", json={"name": "SoloVault"})
        self.assertEqual(resp.status_code, 201)
        vault_id = resp.json()["id"]

        self.assertEqual(self._count_vault_members(vault_id), 1)

    def test_superadmin_creates_vault_also_gets_admin_row(self):
        """Superadmin creating a vault also gets vault_members entry."""
        user_id, override = self._create_member_user(role="superadmin")
        app.dependency_overrides[get_current_active_user] = override

        resp = self.client.post("/api/vaults", json={"name": "AdminVault"})
        self.assertEqual(resp.status_code, 201)
        vault_id = resp.json()["id"]

        permission = self._query_vault_members(vault_id, user_id)
        self.assertEqual(permission, "admin")

    # ------------------------------------------------------------------ #
    # Scenario 2: Duplicate name → 409, no vault_members row
    # ------------------------------------------------------------------ #

    def test_duplicate_name_returns_409_no_member_row(self):
        """Duplicate vault name returns 409 and creates NO vault_members row."""
        user_id, override = self._create_member_user()
        app.dependency_overrides[get_current_active_user] = override

        # First create succeeds
        resp1 = self.client.post("/api/vaults", json={"name": "DupTest"})
        self.assertEqual(resp1.status_code, 201)
        first_vault_id = resp1.json()["id"]
        first_vault_count = self._count_vaults()

        # Second create with same name → 409
        resp2 = self.client.post("/api/vaults", json={"name": "DupTest"})
        self.assertEqual(resp2.status_code, 409)
        self.assertIn("already exists", resp2.json()["detail"])

        # No extra vault was created
        self.assertEqual(self._count_vaults(), first_vault_count)

        # Only the first vault has a member row
        self.assertEqual(self._count_vault_members(first_vault_id), 1)

    def test_duplicate_name_no_orphan_vault_members(self):
        """After a 409, no vault_members row exists without a corresponding vault."""
        user_id, override = self._create_member_user()
        app.dependency_overrides[get_current_active_user] = override

        self.client.post("/api/vaults", json={"name": "UniqueName"})
        self.client.post("/api/vaults", json={"name": "UniqueName"})  # 409

        conn = self._get_conn()
        try:
            cursor = conn.execute("""
                SELECT vm.vault_id FROM vault_members vm
                LEFT JOIN vaults v ON vm.vault_id = v.id
                WHERE v.id IS NULL
            """)
            orphans = cursor.fetchall()
            self.assertEqual(len(orphans), 0, "Found orphan vault_members rows")
        finally:
            self._release_conn(conn)

    # ------------------------------------------------------------------ #
    # Scenario 3: Creator sees vault in list_vaults
    # ------------------------------------------------------------------ #

    def test_creator_sees_vault_in_list(self):
        """After creating a vault, member user sees it in GET /api/vaults."""
        user_id, override = self._create_member_user()
        app.dependency_overrides[get_current_active_user] = override

        resp = self.client.post("/api/vaults", json={"name": "VisibleVault"})
        self.assertEqual(resp.status_code, 201)
        vault_id = resp.json()["id"]

        # List vaults as the same member user
        list_resp = self.client.get("/api/vaults")
        self.assertEqual(list_resp.status_code, 200)
        vault_ids = [v["id"] for v in list_resp.json()["vaults"]]
        self.assertIn(vault_id, vault_ids)

    def test_member_cannot_see_other_users_vault(self):
        """Member user without permission does NOT see another user's vault."""
        # User A creates a vault
        user_a, override_a = self._create_member_user()
        app.dependency_overrides[get_current_active_user] = override_a
        resp = self.client.post("/api/vaults", json={"name": "PrivateVault"})
        self.assertEqual(resp.status_code, 201)
        vault_id = resp.json()["id"]

        # User B lists vaults — should NOT see user A's vault
        user_b, override_b = self._create_member_user()
        app.dependency_overrides[get_current_active_user] = override_b
        list_resp = self.client.get("/api/vaults")
        vault_ids = [v["id"] for v in list_resp.json()["vaults"]]
        self.assertNotIn(vault_id, vault_ids)

    # ------------------------------------------------------------------ #
    # Scenario 4: Atomic commit — vault_members failure rolls back vault
    # ------------------------------------------------------------------ #

    def test_atomic_commit_member_and_vault_consistent(self):
        """vault_members row and vault row are created together atomically."""
        user_id, override = self._create_member_user()
        app.dependency_overrides[get_current_active_user] = override

        resp = self.client.post("/api/vaults", json={"name": "AtomicTest"})
        self.assertEqual(resp.status_code, 201)
        vault_id = resp.json()["id"]

        # Both the vault and the member row exist
        permission = self._query_vault_members(vault_id, user_id)
        self.assertEqual(permission, "admin")

        conn = self._get_conn()
        try:
            cursor = conn.execute("SELECT id FROM vaults WHERE id = ?", (vault_id,))
            self.assertIsNotNone(cursor.fetchone())
        finally:
            self._release_conn(conn)

    def test_create_vault_response_includes_vault_fields(self):
        """POST /api/vaults returns full VaultResponse with expected fields."""
        user_id, override = self._create_member_user()
        app.dependency_overrides[get_current_active_user] = override

        resp = self.client.post(
            "/api/vaults",
            json={"name": "FieldsTest", "description": "desc"},
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(data["name"], "FieldsTest")
        self.assertEqual(data["description"], "desc")
        self.assertIn("id", data)
        self.assertIn("created_at", data)
        self.assertIn("updated_at", data)
        self.assertEqual(data["file_count"], 0)
        self.assertEqual(data["memory_count"], 0)
        self.assertEqual(data["session_count"], 0)


if __name__ == "__main__":
    unittest.main()
