"""
Vault API tests — CRUD operations, isolation, cascade delete, and edge cases.
"""

import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from queue import Empty, Queue
from unittest.mock import MagicMock, patch

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Stub missing optional dependencies
try:
    import lancedb
except ImportError:
    import types
    sys.modules['lancedb'] = types.ModuleType('lancedb')

try:
    import pyarrow
except ImportError:
    import types
    sys.modules['pyarrow'] = types.ModuleType('pyarrow')

try:
    from unstructured.partition.auto import partition
except ImportError:
    import types
    _unstructured = types.ModuleType('unstructured')
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType('unstructured.partition')
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType('unstructured.partition.auto')
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    _unstructured.chunking = types.ModuleType('unstructured.chunking')
    _unstructured.chunking.__path__ = []
    _unstructured.chunking.title = types.ModuleType('unstructured.chunking.title')
    _unstructured.chunking.title.chunk_by_title = lambda *args, **kwargs: []
    _unstructured.documents = types.ModuleType('unstructured.documents')
    _unstructured.documents.__path__ = []
    _unstructured.documents.elements = types.ModuleType('unstructured.documents.elements')
    _unstructured.documents.elements.Element = type('Element', (), {})
    sys.modules['unstructured'] = _unstructured
    sys.modules['unstructured.partition'] = _unstructured.partition
    sys.modules['unstructured.partition.auto'] = _unstructured.partition.auto
    sys.modules['unstructured.chunking'] = _unstructured.chunking
    sys.modules['unstructured.chunking.title'] = _unstructured.chunking.title
    sys.modules['unstructured.documents'] = _unstructured.documents
    sys.modules['unstructured.documents.elements'] = _unstructured.documents.elements

from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

# Create a temporary database for testing
TEST_DB_PATH = None
TEST_DATA_DIR = None

def setup_test_db():
    global TEST_DB_PATH, TEST_DATA_DIR
    TEST_DATA_DIR = tempfile.mkdtemp()
    TEST_DB_PATH = Path(TEST_DATA_DIR) / "test.db"
    from app.models.database import init_db
    init_db(str(TEST_DB_PATH))
    return str(TEST_DB_PATH)

setup_test_db()

from app.api.deps import (
    get_current_active_user,
    get_db,
    get_embedding_service,
    get_evaluate_policy,
    get_memory_store,
    get_rag_engine,
    get_vector_store,
)
from app.main import app


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
            except:
                conn.close()

    def close_all(self):
        self._closed = True
        while True:
            try:
                conn = self._pool.get_nowait()
                conn.close()
            except Empty:
                break


class TestVaultEndpoints(unittest.TestCase):
    """Comprehensive test suite for vault API endpoints."""

    def setUp(self):
        self.client = TestClient(app)
        self._temp_dir = tempfile.mkdtemp()
        db_path = str(Path(self._temp_dir) / "test.db")
        from app.models.database import init_db
        init_db(db_path)
        self._connection_pool = SimpleConnectionPool(db_path)

        # Insert a test user for FK constraints on vault_members
        conn = self._connection_pool.get_connection()
        conn.execute(
            "INSERT INTO users (id, username, hashed_password, role, is_active) VALUES (?, ?, ?, ?, ?)",
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

        # Mock vector store for delete tests
        self._mock_vector_store = MagicMock()
        self._mock_vector_store.delete_by_vault = MagicMock(return_value=0)

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
        self._db_path = db_path

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_vector_store, None)
        app.dependency_overrides.pop(get_current_active_user, None)
        if hasattr(self, '_connection_pool'):
            self._connection_pool.close_all()
        import shutil
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _get_db_conn(self):
        """Get a raw connection for test data setup."""
        conn = self._connection_pool.get_connection()
        return conn

    def _create_vault_via_api(self, name, description=""):
        """Helper to create a vault and return its id."""
        resp = self.client.post("/api/vaults", json={"name": name, "description": description})
        self.assertEqual(resp.status_code, 201)
        return resp.json()["id"]

    def _insert_file(self, conn, vault_id, file_name="test.txt"):
        """Insert a test file record."""
        conn.execute(
            "INSERT INTO files (file_name, file_path, status, vault_id, file_size) VALUES (?, ?, ?, ?, ?)",
            (file_name, f"/tmp/{file_name}", "indexed", vault_id, 1000)
        )
        conn.commit()

    def _insert_memory(self, conn, vault_id, content="test memory"):
        """Insert a test memory record."""
        conn.execute(
            "INSERT INTO memories (content, vault_id) VALUES (?, ?)",
            (content, vault_id)
        )
        conn.commit()

    def _insert_chat_session(self, conn, vault_id, title="Test Chat"):
        """Insert a test chat session record."""
        conn.execute(
            "INSERT INTO chat_sessions (title, vault_id) VALUES (?, ?)",
            (title, vault_id)
        )
        conn.commit()

    # 1. List vaults tests

    def test_list_vaults_default(self):
        """GET /api/vaults returns list with explicitly created vault (no Default vault auto-created)."""
        # Create a vault explicitly - no Default vault is auto-created anymore
        vault_id = self._create_vault_via_api("MyVault", "Test description")
        resp = self.client.get("/api/vaults")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("vaults", data)
        vaults = data["vaults"]
        self.assertGreaterEqual(len(vaults), 1)
        my_vault = next((v for v in vaults if v["name"] == "MyVault"), None)
        self.assertIsNotNone(my_vault)
        self.assertEqual(my_vault["id"], vault_id)
        self.assertEqual(my_vault["description"], "Test description")

    # 2. Create vault tests

    def test_create_vault(self):
        """POST /api/vaults with name/description returns 201 with correct fields."""
        resp = self.client.post(
            "/api/vaults",
            json={"name": "Research", "description": "My research"}
        )
        self.assertEqual(resp.status_code, 201)
        vault = resp.json()
        self.assertEqual(vault["name"], "Research")
        self.assertEqual(vault["description"], "My research")
        self.assertIn("id", vault)
        self.assertIn("created_at", vault)
        self.assertIn("updated_at", vault)
        self.assertEqual(vault["file_count"], 0)
        self.assertEqual(vault["memory_count"], 0)
        self.assertEqual(vault["session_count"], 0)

    def test_create_vault_duplicate_name(self):
        """POST same name twice returns 409."""
        self.client.post("/api/vaults", json={"name": "Research"})
        resp = self.client.post("/api/vaults", json={"name": "Research"})
        self.assertEqual(resp.status_code, 409)
        self.assertIn("already exists", resp.json()["detail"])

    def test_create_vault_whitespace_name(self):
        """POST with '  Research  ' creates vault with name='Research' (stripped)."""
        resp = self.client.post("/api/vaults", json={"name": "  Research  "})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["name"], "Research")

    def test_create_vault_empty_name(self):
        """POST with empty name returns 422 (validation error, min_length=1)."""
        resp = self.client.post("/api/vaults", json={"name": ""})
        self.assertEqual(resp.status_code, 422)

    def test_create_vault_whitespace_only_name(self):
        """POST with whitespace-only name returns 422 after stripping to empty."""
        resp = self.client.post("/api/vaults", json={"name": "   "})
        self.assertEqual(resp.status_code, 422)

    # 3. Get single vault tests

    def test_get_vault(self):
        """GET /api/vaults/{id} returns the vault."""
        vault_id = self._create_vault_via_api("Research")
        resp = self.client.get(f"/api/vaults/{vault_id}")
        self.assertEqual(resp.status_code, 200)
        vault = resp.json()
        self.assertEqual(vault["id"], vault_id)
        self.assertEqual(vault["name"], "Research")

    def test_get_vault_not_found(self):
        """GET /api/vaults/999 returns 404."""
        resp = self.client.get("/api/vaults/999")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("not found", resp.json()["detail"])

    # 4. Update vault tests

    def test_update_vault(self):
        """PUT /api/vaults/{id} with description returns updated vault."""
        vault_id = self._create_vault_via_api("Research", "Original description")
        resp = self.client.put(
            f"/api/vaults/{vault_id}",
            json={"description": "Updated description"}
        )
        self.assertEqual(resp.status_code, 200)
        vault = resp.json()
        self.assertEqual(vault["description"], "Updated description")
        self.assertEqual(vault["name"], "Research")  # Unchanged

    def test_update_vault_name(self):
        """Create vault, PUT with new name, verify name changed."""
        vault_id = self._create_vault_via_api("Research")
        resp = self.client.put(
            f"/api/vaults/{vault_id}",
            json={"name": "Renamed"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["name"], "Renamed")

    def test_update_vault_not_found(self):
        """PUT /api/vaults/999 returns 404."""
        resp = self.client.put("/api/vaults/999", json={"description": "Updated"})
        self.assertEqual(resp.status_code, 404)

    def test_update_vault_rename_succeeds(self):
        """PUT /api/vaults/{id} with name returns 200 (no rename guard)."""
        vault_id = self._create_vault_via_api("Original")
        resp = self.client.put(f"/api/vaults/{vault_id}", json={"name": "NewName"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["name"], "NewName")

    def test_update_default_vault_description_allowed(self):
        """PUT /api/vaults/{id} with description returns 200."""
        vault_id = self._create_vault_via_api("Research")
        resp = self.client.put(f"/api/vaults/{vault_id}", json={"description": "Updated"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["description"], "Updated")
        self.assertEqual(resp.json()["name"], "Research")  # Name unchanged

    def test_update_vault_duplicate_name(self):
        """Create 2 vaults, rename one to other's name -> 409."""
        self._create_vault_via_api("Research")
        vault_id = self._create_vault_via_api("Projects")
        resp = self.client.put(
            f"/api/vaults/{vault_id}",
            json={"name": "Research"}
        )
        self.assertEqual(resp.status_code, 409)
        self.assertIn("already exists", resp.json()["detail"])

    def test_update_vault_no_fields(self):
        """PUT with no fields returns current record (no-op)."""
        vault_id = self._create_vault_via_api("Research", "Original")
        resp = self.client.put(f"/api/vaults/{vault_id}", json={})
        self.assertEqual(resp.status_code, 200)
        vault = resp.json()
        self.assertEqual(vault["name"], "Research")
        self.assertEqual(vault["description"], "Original")

    # 5. Delete vault tests

    def test_delete_vault(self):
        """Create vault, DELETE it -> 200 with success message."""
        vault_id = self._create_vault_via_api("Research")
        resp = self.client.delete(f"/api/vaults/{vault_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("deleted successfully", resp.json()["message"])
        # Verify vault is gone
        resp = self.client.get(f"/api/vaults/{vault_id}")
        self.assertEqual(resp.status_code, 404)

    def test_delete_vault_succeeds(self):
        """DELETE /api/vaults/{id} returns 200 (no delete guard)."""
        vault_id = self._create_vault_via_api("ToDelete")
        resp = self.client.delete(f"/api/vaults/{vault_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("deleted successfully", resp.json()["message"])

    def test_delete_vault_not_found(self):
        """DELETE /api/vaults/999 returns 404."""
        resp = self.client.delete("/api/vaults/999")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("not found", resp.json()["detail"])

    # 6. Cascade delete tests

    def test_delete_vault_cascade_files(self):
        """Create vault, insert file, delete vault -> verify file is gone."""
        vault_id = self._create_vault_via_api("Research")
        conn = self._get_db_conn()
        try:
            self._insert_file(conn, vault_id, "test.pdf")
            # Verify file exists
            cursor = conn.execute("SELECT id FROM files WHERE vault_id = ?", (vault_id,))
            self.assertIsNotNone(cursor.fetchone())
        finally:
            self._connection_pool.release_connection(conn)

        # Delete vault
        self.client.delete(f"/api/vaults/{vault_id}")

        # Verify file is gone
        conn = self._get_db_conn()
        try:
            cursor = conn.execute("SELECT id FROM files WHERE vault_id = ?", (vault_id,))
            self.assertIsNone(cursor.fetchone())
        finally:
            self._connection_pool.release_connection(conn)

    def test_delete_vault_cascade_sessions(self):
        """Create vault, insert chat session, delete vault -> verify session is gone."""
        vault_id = self._create_vault_via_api("Research")
        conn = self._get_db_conn()
        try:
            self._insert_chat_session(conn, vault_id, "My Chat")
            # Verify session exists
            cursor = conn.execute("SELECT id FROM chat_sessions WHERE vault_id = ?", (vault_id,))
            self.assertIsNotNone(cursor.fetchone())
        finally:
            self._connection_pool.release_connection(conn)

        # Delete vault
        self.client.delete(f"/api/vaults/{vault_id}")

        # Verify session is gone
        conn = self._get_db_conn()
        try:
            cursor = conn.execute("SELECT id FROM chat_sessions WHERE vault_id = ?", (vault_id,))
            self.assertIsNone(cursor.fetchone())
        finally:
            self._connection_pool.release_connection(conn)

    def test_delete_vault_cascade_memories_reassigned(self):
        """Create vault, insert memory, delete vault -> memory has vault_id=NULL."""
        vault_id = self._create_vault_via_api("Research")
        conn = self._get_db_conn()
        try:
            self._insert_memory(conn, vault_id, "test memory content")
            # Verify memory exists with vault_id
            cursor = conn.execute("SELECT vault_id FROM memories WHERE content = ?", ("test memory content",))
            row = cursor.fetchone()
            self.assertEqual(row["vault_id"], vault_id)
        finally:
            self._connection_pool.release_connection(conn)

        # Delete vault
        self.client.delete(f"/api/vaults/{vault_id}")

        # Verify memory still exists but vault_id is NULL
        conn = self._get_db_conn()
        try:
            cursor = conn.execute("SELECT vault_id FROM memories WHERE content = ?", ("test memory content",))
            row = cursor.fetchone()
            self.assertIsNotNone(row)  # Memory still exists
            self.assertIsNone(row["vault_id"])  # Reassigned to global
        finally:
            self._connection_pool.release_connection(conn)

    def test_delete_vault_calls_vector_store(self):
        """Delete vault -> verify vector_store.delete_by_vault was called."""
        vault_id = self._create_vault_via_api("Research")
        self._mock_vector_store.delete_by_vault.reset_mock()

        self.client.delete(f"/api/vaults/{vault_id}")

        self._mock_vector_store.delete_by_vault.assert_called_once_with(str(vault_id))

    # 7. Count tests

    def test_list_vaults_with_counts(self):
        """Create vault with file/memory/session, GET /api/vaults -> verify counts."""
        vault_id = self._create_vault_via_api("Research")
        conn = self._get_db_conn()
        try:
            self._insert_file(conn, vault_id, "doc1.pdf")
            self._insert_file(conn, vault_id, "doc2.pdf")
            self._insert_memory(conn, vault_id, "memory1")
            self._insert_memory(conn, vault_id, "memory2")
            self._insert_memory(conn, vault_id, "memory3")
            self._insert_chat_session(conn, vault_id, "Chat 1")
        finally:
            self._connection_pool.release_connection(conn)

        resp = self.client.get("/api/vaults")
        self.assertEqual(resp.status_code, 200)
        vaults = resp.json()["vaults"]

        # Find the Research vault
        research_vault = next((v for v in vaults if v["name"] == "Research"), None)
        self.assertIsNotNone(research_vault)
        self.assertEqual(research_vault["id"], vault_id)
        self.assertEqual(research_vault["file_count"], 2)
        self.assertEqual(research_vault["memory_count"], 3)
        self.assertEqual(research_vault["session_count"], 1)

        # No Default vault exists unless explicitly created
        default_vault = next((v for v in vaults if v["name"] == "Default"), None)
        self.assertIsNone(default_vault)

    def test_get_vault_with_counts(self):
        """Create vault with data, GET /api/vaults/{id} -> verify counts."""
        vault_id = self._create_vault_via_api("Research")
        conn = self._get_db_conn()
        try:
            self._insert_file(conn, vault_id, "doc.pdf")
            self._insert_memory(conn, vault_id, "a memory")
            self._insert_chat_session(conn, vault_id, "Chat")
        finally:
            self._connection_pool.release_connection(conn)

        resp = self.client.get(f"/api/vaults/{vault_id}")
        self.assertEqual(resp.status_code, 200)
        vault = resp.json()
        self.assertEqual(vault["file_count"], 1)
        self.assertEqual(vault["memory_count"], 1)
        self.assertEqual(vault["session_count"], 1)

    # 8. Additional edge case tests

    def test_create_vault_max_length_name(self):
        """POST with name exactly at max_length=255 succeeds."""
        long_name = "A" * 255
        resp = self.client.post("/api/vaults", json={"name": long_name})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["name"], long_name)

    def test_create_vault_max_length_description(self):
        """POST with description exactly at max_length=1000 succeeds."""
        long_desc = "B" * 1000
        resp = self.client.post("/api/vaults", json={"name": "Test", "description": long_desc})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["description"], long_desc)

    def test_update_vault_both_fields(self):
        """PUT with both name and description updates both."""
        vault_id = self._create_vault_via_api("Original", "Original desc")
        resp = self.client.put(
            f"/api/vaults/{vault_id}",
            json={"name": "Updated Name", "description": "Updated desc"}
        )
        self.assertEqual(resp.status_code, 200)
        vault = resp.json()
        self.assertEqual(vault["name"], "Updated Name")
        self.assertEqual(vault["description"], "Updated desc")

    def test_multiple_vaults_list_ordered(self):
        """Create multiple vaults, verify list is ordered by created_at."""
        self._create_vault_via_api("Alpha")
        self._create_vault_via_api("Beta")
        self._create_vault_via_api("Gamma")

        resp = self.client.get("/api/vaults")
        vaults = resp.json()["vaults"]

        # Vaults are ordered by created_at; only explicitly created vaults exist
        self.assertEqual(len(vaults), 3)

        # Extract vault names in order
        names = [v["name"] for v in vaults]
        self.assertEqual(names, ["Alpha", "Beta", "Gamma"])

    def test_delete_vault_vector_store_failure_continues(self):
        """Delete vault with vector_store failure -> still returns 200."""
        vault_id = self._create_vault_via_api("Research")
        # Set vector store delete to raise exception
        self._mock_vector_store.delete_by_vault.side_effect = Exception("vector store down")

        resp = self.client.delete(f"/api/vaults/{vault_id}")

        # Should still succeed even if vector store fails
        self.assertEqual(resp.status_code, 200)
        self.assertIn("deleted successfully", resp.json()["message"])

    def test_update_vault_same_name(self):
        """Update vault with its existing name -> returns 200 (not 409)."""
        vault_id = self._create_vault_via_api("Research", "Original description")
        resp = self.client.put(
            f"/api/vaults/{vault_id}",
            json={"name": "Research"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["name"], "Research")


class TestVaultScopedRoutes(unittest.TestCase):
    """Test suite for vault_id filtering/passthrough on document/memory/chat/search routes."""

    def setUp(self):
        self.client = TestClient(app)
        self._temp_dir = tempfile.mkdtemp()
        db_path = str(Path(self._temp_dir) / "test.db")
        from app.models.database import init_db
        init_db(db_path)
        self._connection_pool = SimpleConnectionPool(db_path)

        # Patch evaluate_policy in route modules that use standalone evaluate_policy
        # (bypasses global pool and uses override DB instead)
        self._evaluate_policy_patches = []
        for module_name in [
            "app.api.routes.chat",
            "app.api.routes.memories",
            "app.api.routes.wiki",
        ]:
            patcher = patch(f"{module_name}.evaluate_policy")
            mock_ep = patcher.start()
            mock_ep.return_value = True
            self._evaluate_policy_patches.append(patcher)

        def override_get_db():
            conn = self._connection_pool.get_connection()
            try:
                yield conn
            finally:
                self._connection_pool.release_connection(conn)

        # Mock vector store
        self._mock_vector_store = MagicMock()
        self._mock_vector_store.search = MagicMock(return_value=[])
        self._mock_vector_store.init_table = MagicMock()

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_vector_store] = lambda: self._mock_vector_store
        app.dependency_overrides[get_current_active_user] = lambda: {
            "id": 1,
            "username": "test-admin",
            "role": "admin",
        }

        async def allow_policy(user, resource_type, resource_id, action):
            return True

        app.dependency_overrides[get_evaluate_policy] = lambda: allow_policy
        self._db_path = db_path

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_vector_store, None)
        app.dependency_overrides.pop(get_current_active_user, None)
        app.dependency_overrides.pop(get_evaluate_policy, None)
        app.dependency_overrides.pop(get_memory_store, None)
        app.dependency_overrides.pop(get_rag_engine, None)
        app.dependency_overrides.pop(get_embedding_service, None)
        for patcher in getattr(self, '_evaluate_policy_patches', []):
            patcher.stop()
        if hasattr(self, '_connection_pool'):
            self._connection_pool.close_all()
        import shutil
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _get_db_conn(self):
        """Get a raw connection for test data setup."""
        conn = self._connection_pool.get_connection()
        return conn

    def _create_vault(self, name):
        """Helper to create a vault and return its id via direct SQL."""
        conn = self._get_db_conn()
        try:
            cursor = conn.execute(
                "INSERT INTO vaults (name, description) VALUES (?, ?)",
                (name, f"{name} description")
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            self._connection_pool.release_connection(conn)

    # 1. Document routes (vault_id as Query param)

    def test_list_documents_filtered_by_vault(self):
        """GET /api/documents?vault_id=2 returns only vault 2 files."""
        vault_id_1 = self._create_vault("Vault1")
        vault_id_2 = self._create_vault("Vault2")

        # Insert files into vault 1 and vault 2
        conn = self._get_db_conn()
        try:
            conn.execute(
                "INSERT INTO files (file_name, file_path, status, vault_id, file_size) VALUES (?, ?, 'indexed', ?, 1000)",
                ("file1.txt", "/tmp/file1.txt", vault_id_1)
            )
            conn.execute(
                "INSERT INTO files (file_name, file_path, status, vault_id, file_size) VALUES (?, ?, 'indexed', ?, 1000)",
                ("file2.txt", "/tmp/file2.txt", vault_id_1)
            )
            conn.execute(
                "INSERT INTO files (file_name, file_path, status, vault_id, file_size) VALUES (?, ?, 'indexed', ?, 1000)",
                ("file3.txt", "/tmp/file3.txt", vault_id_2)
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        # Query for vault 2 files only
        resp = self.client.get(f"/api/documents?vault_id={vault_id_2}")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("documents", data)
        files = data["documents"]
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["file_name"], "file3.txt")

        # Query without filter returns all
        resp = self.client.get("/api/documents")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("documents", data)
        files = data["documents"]
        self.assertEqual(len(files), 3)

    def test_list_documents_no_vault_returns_all(self):
        """GET /api/documents with no vault_id returns all files."""
        vault_id_1 = self._create_vault("Vault1")
        vault_id_2 = self._create_vault("Vault2")

        conn = self._get_db_conn()
        try:
            conn.execute(
                "INSERT INTO files (file_name, file_path, status, vault_id, file_size) VALUES (?, ?, 'indexed', ?, 1000)",
                ("file1.txt", "/tmp/file1.txt", vault_id_1)
            )
            conn.execute(
                "INSERT INTO files (file_name, file_path, status, vault_id, file_size) VALUES (?, ?, 'indexed', ?, 1000)",
                ("file2.txt", "/tmp/file2.txt", vault_id_2)
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        resp = self.client.get("/api/documents")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("documents", data)
        files = data["documents"]
        self.assertEqual(len(files), 2)

    def test_document_stats_filtered_by_vault(self):
        """GET /api/documents/stats?vault_id=2 returns stats only for vault 2."""
        vault_id_1 = self._create_vault("Vault1")
        vault_id_2 = self._create_vault("Vault2")

        conn = self._get_db_conn()
        try:
            conn.execute(
                "INSERT INTO files (file_name, file_path, status, vault_id, chunk_count, file_size) VALUES (?, ?, 'indexed', ?, 10, 1000)",
                ("file1.txt", "/tmp/file1.txt", vault_id_1)
            )
            conn.execute(
                "INSERT INTO files (file_name, file_path, status, vault_id, chunk_count, file_size) VALUES (?, ?, 'indexed', ?, 20, 1000)",
                ("file2.txt", "/tmp/file2.txt", vault_id_2)
            )
            conn.execute(
                "INSERT INTO files (file_name, file_path, status, vault_id, chunk_count, file_size) VALUES (?, ?, 'indexed', ?, 30, 1000)",
                ("file3.txt", "/tmp/file3.txt", vault_id_2)
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        resp = self.client.get(f"/api/documents/stats?vault_id={vault_id_2}")
        self.assertEqual(resp.status_code, 200)
        stats = resp.json()
        self.assertEqual(stats["total_files"], 2)
        self.assertEqual(stats["total_chunks"], 50)

    def test_document_stats_no_vault_returns_all(self):
        """GET /api/documents/stats returns totals across all vaults."""
        vault_id_1 = self._create_vault("Vault1")
        vault_id_2 = self._create_vault("Vault2")

        conn = self._get_db_conn()
        try:
            conn.execute(
                "INSERT INTO files (file_name, file_path, status, vault_id, chunk_count, file_size) VALUES (?, ?, 'indexed', ?, 10, 1000)",
                ("file1.txt", "/tmp/file1.txt", vault_id_1)
            )
            conn.execute(
                "INSERT INTO files (file_name, file_path, status, vault_id, chunk_count, file_size) VALUES (?, ?, 'indexed', ?, 20, 1000)",
                ("file2.txt", "/tmp/file2.txt", vault_id_2)
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        resp = self.client.get("/api/documents/stats")
        self.assertEqual(resp.status_code, 200)
        stats = resp.json()
        self.assertEqual(stats["total_files"], 2)
        self.assertEqual(stats["total_documents"], 2)
        self.assertEqual(stats["total_chunks"], 30)
        self.assertEqual(stats["total_size_bytes"], 2000)
        self.assertEqual(stats["documents_by_status"], {"indexed": 2})

    # 2. Memory routes (vault_id as Query param for list, in body for create/search)

    def test_list_memories_filtered_by_vault(self):
        """GET /api/memories?vault_id=2 returns only vault 2 memories."""
        vault_id_1 = self._create_vault("Vault1")
        vault_id_2 = self._create_vault("Vault2")

        conn = self._get_db_conn()
        try:
            conn.execute(
                "INSERT INTO memories (content, vault_id) VALUES (?, ?)",
                ("memory 1", vault_id_1)
            )
            conn.execute(
                "INSERT INTO memories (content, vault_id) VALUES (?, ?)",
                ("memory 2", vault_id_2)
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        # Query for vault 2 memories only
        resp = self.client.get(f"/api/memories?vault_id={vault_id_2}")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("memories", data)
        memories = data["memories"]
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0]["content"], "memory 2")

        # Query without filter returns all
        resp = self.client.get("/api/memories")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("memories", data)
        memories = data["memories"]
        self.assertEqual(len(memories), 2)

    def test_create_memory_with_vault_id(self):
        """POST /api/memories with vault_id passes it to memory_store.add_memory."""
        from app.services.memory_store import MemoryRecord

        vault_id = 2
        mock_memory_store = MagicMock()
        mock_record = MemoryRecord(
            id=1, content="test", category=None, tags=None,
            source=None, created_at="2026-01-01", updated_at="2026-01-01", vault_id=vault_id
        )
        mock_memory_store.add_memory = MagicMock(return_value=mock_record)
        app.dependency_overrides[get_memory_store] = lambda: mock_memory_store

        resp = self.client.post(
            "/api/memories",
            json={"content": "test", "vault_id": vault_id}
        )

        self.assertEqual(resp.status_code, 200)
        mock_memory_store.add_memory.assert_called_once()
        call_kwargs = mock_memory_store.add_memory.call_args[1]
        self.assertEqual(call_kwargs.get("vault_id"), vault_id)

    def test_search_memories_get_with_vault_id(self):
        """GET /api/memories/search?query=test&vault_id=2 passes vault_id."""
        mock_memory_store = MagicMock()
        mock_memory_store.search_memories = MagicMock(return_value=[])
        app.dependency_overrides[get_memory_store] = lambda: mock_memory_store

        resp = self.client.get("/api/memories/search?query=test&vault_id=2")

        self.assertEqual(resp.status_code, 200)
        mock_memory_store.search_memories.assert_called_once()
        call_kwargs = mock_memory_store.search_memories.call_args[1]
        self.assertEqual(call_kwargs.get("vault_id"), 2)

    def test_search_memories_post_with_vault_id(self):
        """POST /api/memories/search with vault_id in body passes it."""
        mock_memory_store = MagicMock()
        mock_memory_store.search_memories = MagicMock(return_value=[])
        app.dependency_overrides[get_memory_store] = lambda: mock_memory_store

        resp = self.client.post(
            "/api/memories/search",
            json={"query": "test", "vault_id": 2}
        )

        self.assertEqual(resp.status_code, 200)
        mock_memory_store.search_memories.assert_called_once()
        call_kwargs = mock_memory_store.search_memories.call_args[1]
        self.assertEqual(call_kwargs.get("vault_id"), 2)

    # 3. Chat routes (vault_id in request body)

    def test_chat_passes_vault_id(self):
        """POST /api/chat passes vault_id to rag_engine.query."""
        captured_kwargs = {}

        async def mock_query(message, history, stream=False, **kwargs):
            captured_kwargs.update(kwargs)
            yield {"type": "content", "content": "hello"}
            yield {"type": "done", "sources": [], "memories_used": []}

        mock_rag = MagicMock()
        mock_rag.query = mock_query
        app.dependency_overrides[get_rag_engine] = lambda: mock_rag

        resp = self.client.post(
            "/api/chat",
            json={"message": "hi", "vault_id": 3}
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(captured_kwargs.get("vault_id"), 3)

    def test_chat_stream_passes_vault_id(self):
        """POST /api/chat/stream passes vault_id to rag_engine.query."""
        captured_kwargs = {}

        async def mock_query(message, history, stream=False, **kwargs):
            captured_kwargs.update(kwargs)
            yield {"type": "content", "content": "hello"}
            yield {"type": "done", "sources": [], "memories_used": []}

        mock_rag = MagicMock()
        mock_rag.query = mock_query
        app.dependency_overrides[get_rag_engine] = lambda: mock_rag

        resp = self.client.post(
            "/api/chat/stream",
            json={"messages": [{"role": "user", "content": "hi"}], "vault_id": 3}
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(captured_kwargs.get("vault_id"), 3)

    # 4. Search route (vault_id in request body → converted to string)

    def test_search_passes_vault_id(self):
        """POST /api/search with vault_id passes it as string to vector_store.search."""
        mock_embedding = MagicMock()
        mock_embedding.embed_single = AsyncMock(return_value=[0.1] * 768)

        mock_vs = MagicMock()
        mock_vs.search = MagicMock(return_value=[])
        mock_vs.init_table = MagicMock()

        app.dependency_overrides[get_embedding_service] = lambda: mock_embedding
        app.dependency_overrides[get_vector_store] = lambda: mock_vs

        resp = self.client.post(
            "/api/search",
            json={"query": "test", "vault_id": 5}
        )

        self.assertEqual(resp.status_code, 200)
        mock_vs.search.assert_called_once()
        call_kwargs = mock_vs.search.call_args[1]
        self.assertEqual(call_kwargs.get("vault_id"), "5")  # String!

    def test_search_no_vault_id_passes_none(self):
        """POST /api/search without vault_id passes None to vector_store.search."""
        mock_embedding = MagicMock()
        mock_embedding.embed_single = AsyncMock(return_value=[0.1] * 768)

        mock_vs = MagicMock()
        mock_vs.search = MagicMock(return_value=[])
        mock_vs.init_table = MagicMock()

        app.dependency_overrides[get_embedding_service] = lambda: mock_embedding
        app.dependency_overrides[get_vector_store] = lambda: mock_vs

        resp = self.client.post(
            "/api/search",
            json={"query": "test"}
        )

        self.assertEqual(resp.status_code, 200)
        mock_vs.search.assert_called_once()
        call_kwargs = mock_vs.search.call_args[1]
        self.assertIsNone(call_kwargs.get("vault_id"))


if __name__ == '__main__':
    unittest.main()
