"""Tests for Phase 3 document organization: tags CRUD + assignment, document
list sorting and tag filtering, and the atomic vault-wide delete (DD-C011).

Mirrors the connection-pool / dependency-override pattern used by
test_kms_routes.py.
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from queue import Empty, Queue
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

from fastapi.testclient import TestClient

from app.api.deps import get_db, get_vector_store
from app.config import settings
from app.main import app
from app.security import csrf_protect
from app.services.auth_service import create_access_token


class SimpleConnectionPool:
    def __init__(self, db_path):
        self.db_path = db_path
        self._pool = Queue(maxsize=5)
        self._lock = threading.Lock()
        self._closed = False

    def get_connection(self):
        if self._closed:
            raise RuntimeError("Pool closed")
        try:
            return self._pool.get_nowait()
        except Empty:
            return self._create_connection()

    def _create_connection(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def release_connection(self, conn):
        if not self._closed:
            try:
                self._pool.put_nowait(conn)
            except Exception:
                conn.close()

    def close_all(self):
        self._closed = True
        while True:
            try:
                conn = self._pool.get_nowait()
                conn.close()
            except Empty:
                break


class TagsTestBase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
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
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (1,'superadmin',?, 'Super','superadmin',1)",
                (pw,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (3,'member1',?, 'Member One','member',1)",
                (pw,),
            )
            # Vault 2: member1 has write. Vault 9: member1 has no access.
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (2,'Write Vault','w')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (9,'Other Vault','o')"
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

    def _headers(self, user_id=3, username="member1", role="member"):
        return {"Authorization": f"Bearer {create_access_token(user_id, username, role)}"}

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


class TestTagCRUD(TagsTestBase):
    def test_create_list_update_delete_tag(self):
        # Create
        resp = self.client.post(
            "/api/tags",
            json={"vault_id": 2, "name": "Finance", "color": "#0a0"},
            headers=self._headers(),
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        tag = resp.json()
        self.assertEqual(tag["name"], "Finance")
        self.assertEqual(tag["color"], "#0a0")
        tag_id = tag["id"]

        # List
        resp = self.client.get("/api/tags?vault_id=2", headers=self._headers())
        self.assertEqual(resp.status_code, 200)
        names = [t["name"] for t in resp.json()["tags"]]
        self.assertIn("Finance", names)

        # Update
        resp = self.client.put(
            f"/api/tags/{tag_id}",
            json={"name": "Finance 2024", "color": "#f00"},
            headers=self._headers(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["name"], "Finance 2024")

        # Delete
        resp = self.client.delete(f"/api/tags/{tag_id}", headers=self._headers())
        self.assertEqual(resp.status_code, 204)
        resp = self.client.get("/api/tags?vault_id=2", headers=self._headers())
        self.assertEqual(len(resp.json()["tags"]), 0)

    def test_duplicate_tag_name_returns_409(self):
        self.client.post(
            "/api/tags",
            json={"vault_id": 2, "name": "Dup"},
            headers=self._headers(),
        )
        resp = self.client.post(
            "/api/tags",
            json={"vault_id": 2, "name": "Dup"},
            headers=self._headers(),
        )
        self.assertEqual(resp.status_code, 409)

    def test_empty_name_returns_422(self):
        resp = self.client.post(
            "/api/tags",
            json={"vault_id": 2, "name": ""},
            headers=self._headers(),
        )
        self.assertEqual(resp.status_code, 422)

    def test_create_tag_no_vault_access_returns_403(self):
        resp = self.client.post(
            "/api/tags",
            json={"vault_id": 9, "name": "Nope"},
            headers=self._headers(),
        )
        self.assertEqual(resp.status_code, 403)


class TestTagAssignment(TagsTestBase):
    def _make_tag(self, name, vault_id=2):
        resp = self.client.post(
            "/api/tags",
            json={"vault_id": vault_id, "name": name},
            headers=self._headers(),
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        return resp.json()["id"]

    def test_assign_and_list_document_tags(self):
        f1 = self._seed_file(2, "a.txt")
        f2 = self._seed_file(2, "b.txt")
        t1 = self._make_tag("alpha")
        t2 = self._make_tag("beta")

        resp = self.client.post(
            "/api/tags/assign",
            json={"vault_id": 2, "file_ids": [f1, f2], "tag_ids": [t1, t2]},
            headers=self._headers(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["assigned"], 4)

        resp = self.client.get(
            f"/api/tags/documents/{f1}?vault_id=2", headers=self._headers()
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual({t["name"] for t in resp.json()["tags"]}, {"alpha", "beta"})

    def test_assign_is_idempotent(self):
        f1 = self._seed_file(2, "a.txt")
        t1 = self._make_tag("alpha")
        first = self.client.post(
            "/api/tags/assign",
            json={"vault_id": 2, "file_ids": [f1], "tag_ids": [t1]},
            headers=self._headers(),
        )
        self.assertEqual(first.json()["assigned"], 1)
        second = self.client.post(
            "/api/tags/assign",
            json={"vault_id": 2, "file_ids": [f1], "tag_ids": [t1]},
            headers=self._headers(),
        )
        self.assertEqual(second.json()["assigned"], 0)

    def test_cannot_tag_file_in_other_vault(self):
        # File in vault 9 (no access), tag in vault 2.
        other_file = self._seed_file(9, "secret.txt")
        t1 = self._make_tag("alpha")
        resp = self.client.post(
            "/api/tags/assign",
            json={"vault_id": 2, "file_ids": [other_file], "tag_ids": [t1]},
            headers=self._headers(),
        )
        # Cross-vault file is filtered out -> nothing assigned.
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["assigned"], 0)

    def test_set_and_unassign_document_tags(self):
        f1 = self._seed_file(2, "a.txt")
        t1 = self._make_tag("alpha")
        t2 = self._make_tag("beta")
        # Set to [t1, t2]
        resp = self.client.put(
            f"/api/tags/documents/{f1}",
            json={"vault_id": 2, "tag_ids": [t1, t2]},
            headers=self._headers(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()["tags"]), 2)
        # Replace with just [t1]
        resp = self.client.put(
            f"/api/tags/documents/{f1}",
            json={"vault_id": 2, "tag_ids": [t1]},
            headers=self._headers(),
        )
        self.assertEqual({t["name"] for t in resp.json()["tags"]}, {"alpha"})
        # Unassign t1
        resp = self.client.delete(
            f"/api/tags/{t1}/documents/{f1}?vault_id=2", headers=self._headers()
        )
        self.assertEqual(resp.status_code, 204)
        resp = self.client.get(
            f"/api/tags/documents/{f1}?vault_id=2", headers=self._headers()
        )
        self.assertEqual(resp.json()["tags"], [])

    def test_deleting_tag_cascades_assignments(self):
        f1 = self._seed_file(2, "a.txt")
        t1 = self._make_tag("alpha")
        self.client.post(
            "/api/tags/assign",
            json={"vault_id": 2, "file_ids": [f1], "tag_ids": [t1]},
            headers=self._headers(),
        )
        self.client.delete(f"/api/tags/{t1}", headers=self._headers())
        resp = self.client.get(
            f"/api/tags/documents/{f1}?vault_id=2", headers=self._headers()
        )
        self.assertEqual(resp.json()["tags"], [])

    def test_deleting_file_cascades_to_document_tags(self):
        # Coverage gap: ON DELETE CASCADE on document_tags.file_id must clear
        # assignment rows when the file row is removed.
        f1 = self._seed_file(2, "a.txt")
        t1 = self._make_tag("alpha")
        self.client.post(
            "/api/tags/assign",
            json={"vault_id": 2, "file_ids": [f1], "tag_ids": [t1]},
            headers=self._headers(),
        )
        conn = self._connection_pool.get_connection()
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM document_tags WHERE file_id = ?", (f1,)
                ).fetchone()[0],
                1,
            )
            conn.execute("DELETE FROM files WHERE id = ?", (f1,))
            conn.commit()
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM document_tags WHERE file_id = ?", (f1,)
                ).fetchone()[0],
                0,
            )
        finally:
            self._connection_pool.release_connection(conn)

    def test_getter_excludes_cross_vault_tag(self):
        # Regression (F-005): even if a document_tags row links a file to a tag
        # from a different vault, the getters must not surface it.
        from app.services.tag_store import TagStore

        f1 = self._seed_file(2, "a.txt")
        conn = self._connection_pool.get_connection()
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            # Tag belonging to vault 9, assigned to a vault-2 file directly.
            cur = conn.execute(
                "INSERT INTO tags (vault_id, name, created_at, updated_at) "
                "VALUES (9, 'leak', '', '')"
            )
            leak_tag_id = cur.lastrowid
            conn.execute(
                "INSERT INTO document_tags (file_id, tag_id, created_at) VALUES (?,?,'')",
                (f1, leak_tag_id),
            )
            conn.commit()

            store = TagStore(conn)
            self.assertEqual(store.get_tags_for_document(f1), [])
            self.assertEqual(store.get_tags_for_documents([f1])[f1], [])
        finally:
            self._connection_pool.release_connection(conn)


class TestDocumentSortAndFilter(TagsTestBase):
    def test_sort_by_file_name_asc_desc(self):
        self._seed_file(2, "banana.txt")
        self._seed_file(2, "apple.txt")
        self._seed_file(2, "cherry.txt")

        asc = self.client.get(
            "/api/documents?vault_id=2&sort_by=file_name&sort_order=asc",
            headers=self._headers(),
        )
        self.assertEqual(asc.status_code, 200, asc.text)
        names_asc = [d["file_name"] for d in asc.json()["documents"]]
        self.assertEqual(names_asc, ["apple.txt", "banana.txt", "cherry.txt"])

        desc = self.client.get(
            "/api/documents?vault_id=2&sort_by=file_name&sort_order=desc",
            headers=self._headers(),
        )
        names_desc = [d["file_name"] for d in desc.json()["documents"]]
        self.assertEqual(names_desc, ["cherry.txt", "banana.txt", "apple.txt"])

    def test_invalid_sort_column_falls_back_to_default(self):
        self._seed_file(2, "a.txt")
        resp = self.client.get(
            "/api/documents?vault_id=2&sort_by=file_path;DROP&sort_order=weird",
            headers=self._headers(),
        )
        # Off-whitelist values are ignored, not injected.
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_filter_by_tag_id(self):
        f1 = self._seed_file(2, "a.txt")
        self._seed_file(2, "b.txt")
        tag_resp = self.client.post(
            "/api/tags", json={"vault_id": 2, "name": "x"}, headers=self._headers()
        )
        t1 = tag_resp.json()["id"]
        self.client.post(
            "/api/tags/assign",
            json={"vault_id": 2, "file_ids": [f1], "tag_ids": [t1]},
            headers=self._headers(),
        )
        resp = self.client.get(
            f"/api/documents?vault_id=2&tag_id={t1}", headers=self._headers()
        )
        self.assertEqual(resp.status_code, 200)
        docs = resp.json()["documents"]
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["id"], f1)
        self.assertEqual({t["name"] for t in docs[0]["tags"]}, {"x"})

    def test_list_includes_tags(self):
        f1 = self._seed_file(2, "a.txt")
        tag_resp = self.client.post(
            "/api/tags", json={"vault_id": 2, "name": "y"}, headers=self._headers()
        )
        t1 = tag_resp.json()["id"]
        self.client.post(
            "/api/tags/assign",
            json={"vault_id": 2, "file_ids": [f1], "tag_ids": [t1]},
            headers=self._headers(),
        )
        resp = self.client.get("/api/documents?vault_id=2", headers=self._headers())
        doc = next(d for d in resp.json()["documents"] if d["id"] == f1)
        self.assertEqual({t["name"] for t in doc["tags"]}, {"y"})

    def test_get_single_document_with_tags(self):
        f1 = self._seed_file(2, "a.txt")
        tag_resp = self.client.post(
            "/api/tags", json={"vault_id": 2, "name": "z"}, headers=self._headers()
        )
        t1 = tag_resp.json()["id"]
        self.client.post(
            "/api/tags/assign",
            json={"vault_id": 2, "file_ids": [f1], "tag_ids": [t1]},
            headers=self._headers(),
        )
        resp = self.client.get(f"/api/documents/{f1}", headers=self._headers())
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["id"], f1)
        self.assertEqual({t["name"] for t in resp.json()["tags"]}, {"z"})

    def test_list_includes_vault_id(self):
        # Regression (F-001): list responses must populate vault_id, not null.
        f1 = self._seed_file(2, "a.txt")
        resp = self.client.get("/api/documents?vault_id=2", headers=self._headers())
        self.assertEqual(resp.status_code, 200, resp.text)
        doc = next(d for d in resp.json()["documents"] if d["id"] == f1)
        self.assertEqual(doc["vault_id"], 2)

    def test_get_single_document_404(self):
        resp = self.client.get("/api/documents/99999", headers=self._headers())
        self.assertEqual(resp.status_code, 404)

    def test_stats_route_not_shadowed_by_single_doc(self):
        self._seed_file(2, "a.txt")
        resp = self.client.get("/api/documents/stats?vault_id=2", headers=self._headers())
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("total_documents", resp.json())


class TestAtomicVaultDelete(TagsTestBase):
    def _admin_member(self):
        # Grant member1 admin on vault 2 for the delete-all endpoint.
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "UPDATE vault_members SET permission='admin' WHERE vault_id=2 AND user_id=3"
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def _count_files(self, vault_id):
        conn = self._connection_pool.get_connection()
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM files WHERE vault_id = ?", (vault_id,)
            ).fetchone()[0]
        finally:
            self._connection_pool.release_connection(conn)

    def test_delete_all_removes_every_row(self):
        self._admin_member()
        for i in range(5):
            self._seed_file(2, f"f{i}.txt")
        self.assertEqual(self._count_files(2), 5)

        resp = self.client.delete("/api/documents/vault/2/all", headers=self._headers())
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["deleted_count"], 5)
        self.assertEqual(self._count_files(2), 0)

    def test_delete_all_empty_vault(self):
        self._admin_member()
        resp = self.client.delete("/api/documents/vault/2/all", headers=self._headers())
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["deleted_count"], 0)


if __name__ == "__main__":
    unittest.main()
