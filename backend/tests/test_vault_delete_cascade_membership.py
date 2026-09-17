"""Vault delete must cascade vault_members and vault_group_access (issue #202, AC4).

The schema declares ON DELETE CASCADE for both membership tables (database.py).
Existing cascade tests only pin files/sessions/memories; these two tests pin the
membership tables so a pragma or schema regression that orphans access grants
fails loudly.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Route CRUD only; no CSRF enforcement is exercised (declared for the classifier).
CSRF_TEST_POLICY = "naive"

from _db_pool import SimpleConnectionPool
from fastapi.testclient import TestClient

from app.api.deps import get_current_active_user, get_db, get_vector_store
from app.main import app
from app.models.database import init_db
from app.security import csrf_protect


class TestVaultDeleteCascadeMembership(unittest.TestCase):
    """Delete a vault with members and group-access grants; assert rows are gone."""

    def setUp(self):
        self.client = TestClient(app)
        self._temp_dir = tempfile.mkdtemp()
        db_path = str(Path(self._temp_dir) / "test.db")
        init_db(db_path)
        self._connection_pool = SimpleConnectionPool(db_path)

        conn = self._connection_pool.get_connection()
        conn.execute(
            "INSERT INTO users (id, username, hashed_password, role, is_active) "
            "VALUES (?, ?, ?, ?, ?)",
            (1, "admin", "abc123", "superadmin", 1),
        )
        conn.commit()
        self._connection_pool.release_connection(conn)

        def override_get_db():
            conn = self._connection_pool.get_connection()
            try:
                yield conn
            finally:
                self._connection_pool.release_connection(conn)

        self._mock_vector_store = MagicMock()
        self._mock_vector_store.delete_by_vault = AsyncMock(return_value=0)

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_vector_store] = lambda: self._mock_vector_store
        app.dependency_overrides[get_current_active_user] = lambda: {
            "id": 1,
            "username": "admin",
            "full_name": "Admin",
            "role": "superadmin",
            "is_active": True,
            "must_change_password": False,
        }
        app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
        self._csrf_protect = csrf_protect

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_vector_store, None)
        app.dependency_overrides.pop(get_current_active_user, None)
        app.dependency_overrides.pop(self._csrf_protect, None)
        self._connection_pool.close_all()
        import shutil

        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _create_vault_via_api(self, name):
        resp = self.client.post("/api/vaults", json={"name": name, "description": ""})
        self.assertEqual(resp.status_code, 201)
        return resp.json()["id"]

    def _count(self, conn, table, vault_id):
        return conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE vault_id = ?", (vault_id,)
        ).fetchone()[0]

    def test_delete_vault_cascade_vault_members(self):
        """vault_members rows for the deleted vault are removed."""
        vault_id = self._create_vault_via_api("CascadeMembers")

        # create_vault inserts the creator as an admin member; add a second member.
        conn = self._connection_pool.get_connection()
        try:
            self.assertGreaterEqual(self._count(conn, "vault_members", vault_id), 1)
            conn.execute(
                "INSERT INTO users (id, username, hashed_password, role, is_active) "
                "VALUES (?, ?, ?, ?, ?)",
                (2, "member2", "abc123", "member", 1),
            )
            conn.execute(
                "INSERT INTO vault_members (vault_id, user_id, permission) "
                "VALUES (?, ?, ?)",
                (vault_id, 2, "write"),
            )
            conn.commit()
            self.assertEqual(self._count(conn, "vault_members", vault_id), 2)
        finally:
            self._connection_pool.release_connection(conn)

        resp = self.client.delete(f"/api/vaults/{vault_id}")
        self.assertEqual(resp.status_code, 200)

        conn = self._connection_pool.get_connection()
        try:
            self.assertEqual(
                self._count(conn, "vault_members", vault_id),
                0,
                "vault_members rows must cascade on vault delete",
            )
        finally:
            self._connection_pool.release_connection(conn)

    def test_delete_vault_cascade_vault_group_access(self):
        """vault_group_access rows for the deleted vault are removed."""
        vault_id = self._create_vault_via_api("CascadeGroupAccess")

        conn = self._connection_pool.get_connection()
        try:
            conn.execute("INSERT INTO organizations (id, name) VALUES (?, ?)", (50, "Org A"))
            conn.execute(
                "INSERT INTO groups (id, org_id, name) VALUES (?, ?, ?)",
                (60, 50, "Editors"),
            )
            conn.execute(
                "INSERT INTO vault_group_access (vault_id, group_id, permission) "
                "VALUES (?, ?, ?)",
                (vault_id, 60, "write"),
            )
            conn.commit()
            self.assertEqual(self._count(conn, "vault_group_access", vault_id), 1)
        finally:
            self._connection_pool.release_connection(conn)

        resp = self.client.delete(f"/api/vaults/{vault_id}")
        self.assertEqual(resp.status_code, 200)

        conn = self._connection_pool.get_connection()
        try:
            self.assertEqual(
                self._count(conn, "vault_group_access", vault_id),
                0,
                "vault_group_access rows must cascade on vault delete",
            )
        finally:
            self._connection_pool.release_connection(conn)


if __name__ == "__main__":
    unittest.main()
