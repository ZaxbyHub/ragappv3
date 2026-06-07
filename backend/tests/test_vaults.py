"""
Vault API tests — CRUD operations, isolation, cascade delete, and edge cases.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
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

from _db_pool import SimpleConnectionPool

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
        self._mock_vector_store.delete_by_vault.side_effect = OSError("vector store down")

        resp = self.client.delete(f"/api/vaults/{vault_id}")

        # Should still succeed even if vector store fails
        self.assertEqual(resp.status_code, 200)
        self.assertIn("deleted successfully", resp.json()["message"])
        # Verify vault is gone from DB despite vector store failure
        resp = self.client.get(f"/api/vaults/{vault_id}")
        self.assertEqual(resp.status_code, 404)

    def test_update_vault_same_name(self):
        """Update vault with its existing name -> returns 200 (not 409)."""
        vault_id = self._create_vault_via_api("Research", "Original description")
        resp = self.client.put(
             f"/api/vaults/{vault_id}",
             json={"name": "Research"}
         )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["name"], "Research")


class TestAccessibleVaultsEndpoint(unittest.TestCase):
    """Test suite for /vaults/accessible endpoint with permission filtering."""

    def setUp(self):
        self.client = TestClient(app)
        self._temp_dir = tempfile.mkdtemp()
        db_path = str(Path(self._temp_dir) / "test.db")
        from app.models.database import init_db
        init_db(db_path)
        self._connection_pool = SimpleConnectionPool(db_path)

        # Insert test users (required for FK constraints)
        conn = self._connection_pool.get_connection()
        conn.execute(
            "INSERT INTO users (id, username, hashed_password, role, is_active) VALUES (?, ?, ?, ?, ?)",
            (1, "admin", "abc123", "superadmin", 1),
        )
        conn.execute(
            "INSERT INTO users (id, username, hashed_password, role, is_active) VALUES (?, ?, ?, ?, ?)",
            (2, "user2", "abc123", "member", 1),
        )
        conn.execute(
            "INSERT INTO users (id, username, hashed_password, role, is_active) VALUES (?, ?, ?, ?, ?)",
            (3, "adminuser", "abc123", "admin", 1),
        )
        conn.commit()
        self._connection_pool.release_connection(conn)

        def override_get_db():
            conn = self._connection_pool.get_connection()
            try:
                yield conn
            finally:
                self._connection_pool.release_connection(conn)

        app.dependency_overrides[get_db] = override_get_db

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_active_user, None)
        if hasattr(self, '_connection_pool'):
            self._connection_pool.close_all()
        import shutil
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _get_db_conn(self):
        """Get a raw connection for test data setup."""
        conn = self._connection_pool.get_connection()
        return conn

    def _insert_user(self, conn, user_id, username, role="member", is_active=1):
        """Insert a test user."""
        conn.execute(
            "INSERT INTO users (id, username, hashed_password, role, is_active) VALUES (?, ?, ?, ?, ?)",
            (user_id, username, "abc123", role, is_active)
        )
        conn.commit()

    def _insert_vault(self, conn, name, description="", org_id=None, visibility="private", owner_id=1):
        """Insert a vault and return its id."""
        cursor = conn.execute(
            "INSERT INTO vaults (name, description, org_id, visibility, owner_id) VALUES (?, ?, ?, ?, ?)",
            (name, description, org_id, visibility, owner_id)
        )
        conn.commit()
        return cursor.lastrowid

    def _insert_vault_member(self, conn, vault_id, user_id, permission="read", granted_by=1):
        """Insert a vault membership."""
        conn.execute(
            "INSERT INTO vault_members (vault_id, user_id, permission, granted_by) VALUES (?, ?, ?, ?)",
            (vault_id, user_id, permission, granted_by)
        )
        conn.commit()

    def _insert_org(self, conn, name):
        """Insert an organization and return its id."""
        cursor = conn.execute(
            "INSERT INTO organizations (name, created_by) VALUES (?, ?)",
            (name, 1)
        )
        conn.commit()
        return cursor.lastrowid

    def _insert_group(self, conn, name, org_id, description=""):
        """Insert a group and return its id."""
        cursor = conn.execute(
            "INSERT INTO groups (name, org_id, description) VALUES (?, ?, ?)",
            (name, org_id, description)
        )
        conn.commit()
        return cursor.lastrowid

    def _insert_group_member(self, conn, group_id, user_id):
        """Insert a group membership."""
        conn.execute(
            "INSERT INTO group_members (group_id, user_id) VALUES (?, ?)",
            (group_id, user_id)
        )
        conn.commit()

    def _insert_vault_group_access(self, conn, vault_id, group_id, permission="read", granted_by=1):
        """Insert vault group access."""
        conn.execute(
            "INSERT INTO vault_group_access (vault_id, group_id, permission, granted_by) VALUES (?, ?, ?, ?)",
            (vault_id, group_id, permission, granted_by)
        )
        conn.commit()

    def _insert_org_member(self, conn, org_id, user_id, role="member"):
        """Insert an organization membership."""
        conn.execute(
            "INSERT INTO org_members (org_id, user_id, role) VALUES (?, ?, ?)",
            (org_id, user_id, role)
        )
        conn.commit()

    def _mock_user(self, user_id, username, role="member", is_active=1):
        """Set up a mock user for the current test."""
        app.dependency_overrides[get_current_active_user] = lambda: {
            "id": user_id,
            "username": username,
            "role": role,
            "is_active": is_active,
            "full_name": username,
            "must_change_password": False,
        }

    # 1. Non-admin user tests

    def test_accessible_vaults_empty_for_new_user(self):
        """Non-admin user with no vault access sees empty list."""
        self._mock_user(2, "newuser", role="user")
        resp = self.client.get("/api/vaults/accessible")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("vaults", data)
        self.assertEqual(len(data["vaults"]), 0)

    def test_accessible_vaults_direct_member_sees_vault(self):
        """User with direct vault membership sees vault in accessible list."""
        # Create vault
        conn = self._get_db_conn()
        try:
            vault_id = self._insert_vault(conn, "MyVault")
            # Add user as member
            self._insert_vault_member(conn, vault_id, 2, "read")
        finally:
            self._connection_pool.release_connection(conn)

        # User 2 should see the vault
        self._mock_user(2, "user2", role="user")
        resp = self.client.get("/api/vaults/accessible")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("vaults", data)
        self.assertEqual(len(data["vaults"]), 1)
        self.assertEqual(data["vaults"][0]["name"], "MyVault")
        self.assertEqual(data["vaults"][0]["current_user_permission"], "read")

    def test_accessible_vaults_not_seen_without_permission(self):
        """User without vault access does NOT see it in accessible list."""
        # Create vault for user 1
        conn = self._get_db_conn()
        try:
            self._insert_vault(conn, "OtherVault")
        finally:
            self._connection_pool.release_connection(conn)

        # User 2 should NOT see it
        self._mock_user(2, "user2", role="user")
        resp = self.client.get("/api/vaults/accessible")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("vaults", data)
        self.assertEqual(len(data["vaults"]), 0)

    def test_accessible_vaults_group_member_sees_vault(self):
        """User with group access sees vault in accessible list."""
        conn = self._get_db_conn()
        try:
            # Create vault and grant group access
            vault_id = self._insert_vault(conn, "GroupVault")
            org_id = self._insert_org(conn, "TestOrg")
            group_id = self._insert_group(conn, "TestGroup", org_id)
            self._insert_group_member(conn, group_id, 2)  # User 2 in group
            self._insert_vault_group_access(conn, vault_id, group_id, "write")
        finally:
            self._connection_pool.release_connection(conn)

        self._mock_user(2, "user2", role="user")
        resp = self.client.get("/api/vaults/accessible")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("vaults", data)
        self.assertEqual(len(data["vaults"]), 1)
        self.assertEqual(data["vaults"][0]["name"], "GroupVault")
        self.assertEqual(data["vaults"][0]["current_user_permission"], "write")

    def test_accessible_vaults_public_sees_public_vault(self):
        """User can see public vaults without explicit membership."""
        conn = self._get_db_conn()
        try:
            self._insert_vault(conn, "PublicVault", visibility="public")
        finally:
            self._connection_pool.release_connection(conn)

        self._mock_user(2, "user2", role="user")
        resp = self.client.get("/api/vaults/accessible")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("vaults", data)
        self.assertEqual(len(data["vaults"]), 1)
        self.assertEqual(data["vaults"][0]["name"], "PublicVault")

    def test_accessible_vaults_org_member_sees_org_vault(self):
        """User in org can see org-level public/visible vaults."""
        conn = self._get_db_conn()
        try:
            org_id = self._insert_org(conn, "MyOrg")
            self._insert_org_member(conn, org_id, 2)  # User 2 in org
            self._insert_vault(conn, "OrgVault", org_id=org_id, visibility="org")
        finally:
            self._connection_pool.release_connection(conn)

        self._mock_user(2, "user2", role="user")
        resp = self.client.get("/api/vaults/accessible")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("vaults", data)
        self.assertEqual(len(data["vaults"]), 1)
        self.assertEqual(data["vaults"][0]["name"], "OrgVault")

    def test_accessible_vaults_org_member_cant_see_other_org_vault(self):
        """User cannot see vaults from other organizations."""
        conn = self._get_db_conn()
        try:
            # Create two organizations
            org1_id = self._insert_org(conn, "Org1")
            org2_id = self._insert_org(conn, "Org2")
            self._insert_org_member(conn, org1_id, 2)  # User 2 only in org1

            # Create vault in org2 (private visibility - not visible to org1 user)
            self._insert_vault(conn, "OtherOrgVault", org_id=org2_id, visibility="private")
        finally:
            self._connection_pool.release_connection(conn)

        self._mock_user(2, "user2", role="user")
        resp = self.client.get("/api/vaults/accessible")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("vaults", data)
        self.assertEqual(len(data["vaults"]), 0)

    # 2. Admin user tests - should see ALL vaults

    def test_accessible_vaults_admin_sees_all(self):
        """Admin user sees ALL vaults via /vaults/accessible."""
        conn = self._get_db_conn()
        try:
            # Create multiple vaults
            self._insert_vault(conn, "Vault1")
            self._insert_vault(conn, "Vault2")
            self._insert_vault(conn, "Vault3")
        finally:
            self._connection_pool.release_connection(conn)

        # Admin user should see all vaults
        self._mock_user(3, "adminuser", role="admin")
        resp = self.client.get("/api/vaults/accessible")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("vaults", data)
        self.assertEqual(len(data["vaults"]), 3)
        vault_names = {v["name"] for v in data["vaults"]}
        self.assertEqual(vault_names, {"Vault1", "Vault2", "Vault3"})

    def test_accessible_vaults_superadmin_sees_all(self):
        """Superadmin user sees ALL vaults via /vaults/accessible."""
        conn = self._get_db_conn()
        try:
            # Create multiple vaults
            self._insert_vault(conn, "Vault1")
            self._insert_vault(conn, "Vault2")
        finally:
            self._connection_pool.release_connection(conn)

        # Superadmin user should see all vaults
        self._mock_user(2, "superadmin", role="superadmin")
        resp = self.client.get("/api/vaults/accessible")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("vaults", data)
        self.assertEqual(len(data["vaults"]), 2)

    # FR-001 test - verify admin endpoint /vaults still works and shows ALL vaults
    def test_admin_vaults_endpoint_shows_all(self):
        """Admin /vaults endpoint returns ALL vaults (not filtered)."""
        conn = self._get_db_conn()
        try:
            # Create vaults that user 2 would NOT have access to directly
            self._insert_vault(conn, "Vault1")
            self._insert_vault(conn, "Vault2")
        finally:
            self._connection_pool.release_connection(conn)

        # Admin user accessing /vaults (not /vaults/accessible)
        self._mock_user(3, "adminuser", role="admin")
        resp = self.client.get("/api/vaults")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("vaults", data)
        self.assertEqual(len(data["vaults"]), 2)


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


class TestFetchAccessibleVaultsAdversarial(unittest.TestCase):
    """Adversarial tests for _fetch_accessible_vaults SQL prefilter security."""

    def setUp(self):
        self._temp_dir = tempfile.mkdtemp()
        db_path = str(Path(self._temp_dir) / "test.db")
        from app.models.database import init_db
        init_db(db_path)
        self._connection_pool = SimpleConnectionPool(db_path)

        # Insert test users
        conn = self._connection_pool.get_connection()
        conn.execute(
            "INSERT INTO users (id, username, hashed_password, role, is_active) VALUES (?, ?, ?, ?, ?)",
            (1, "admin", "abc123", "superadmin", 1),
        )
        conn.execute(
            "INSERT INTO users (id, username, hashed_password, role, is_active) VALUES (?, ?, ?, ?, ?)",
            (2, "regularuser", "abc123", "member", 1),
        )
        conn.commit()
        self._connection_pool.release_connection(conn)

        # We need to import asyncio to run async functions
        import app.api.routes.vaults as vaults_module
        self._vaults_module = vaults_module

    def tearDown(self):
        if hasattr(self, '_connection_pool'):
            self._connection_pool.close_all()
        import shutil
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _get_db_conn(self):
        """Get a raw connection for test data setup."""
        conn = self._connection_pool.get_connection()
        return conn

    def _insert_vault(self, conn, name, visibility="private", org_id=None):
        """Insert a vault and return its id."""
        cursor = conn.execute(
            "INSERT INTO vaults (name, description, visibility, org_id, owner_id) VALUES (?, ?, ?, ?, ?)",
            (name, f"desc_{name}", visibility, org_id, 1)
        )
        conn.commit()
        return cursor.lastrowid

    def _insert_vault_member(self, conn, vault_id, user_id, permission="read"):
        """Insert a vault membership."""
        conn.execute(
            "INSERT INTO vault_members (vault_id, user_id, permission, granted_by) VALUES (?, ?, ?, ?)",
            (vault_id, user_id, permission, 1)
        )
        conn.commit()

    def _run_async(self, coro):
        """Run an async coroutine synchronously for testing."""
        import asyncio
        return asyncio.run(coro)

    # ===== 1. SQL Injection via crafted user_id values =====

    def test_adversarial_sql_injection_or_true(self):
        """SQL injection attempt: ' OR '1'='1 should be safely parameterized."""
        conn = self._get_db_conn()
        try:
            # Create a vault that user 2 has access to
            vault_id = self._insert_vault(conn, "LegitimateVault")
            self._insert_vault_member(conn, vault_id, 2, "read")

            # Attempt SQL injection via user_id
            malicious_user = {"id": "' OR '1'='1", "role": "member"}

            result = self._run_async(
                self._vaults_module._fetch_accessible_vaults(conn, malicious_user)
            )

            # Should return empty - injection string is not a valid user_id
            # The parameterized query treats it as a literal string, not SQL
            self.assertEqual(len(result), 0)
        finally:
            self._connection_pool.release_connection(conn)

    def test_adversarial_sql_injection_drop_table(self):
        """SQL injection attempt: '; DROP TABLE users;-- should be safely parameterized."""
        conn = self._get_db_conn()
        try:
            vault_id = self._insert_vault(conn, "TestVault")
            self._insert_vault_member(conn, vault_id, 2, "read")

            # Attempt SQL injection with DROP TABLE
            malicious_user = {"id": "'; DROP TABLE users;--", "role": "member"}

            # Verify tables still exist after the call
            result = self._run_async(
                self._vaults_module._fetch_accessible_vaults(conn, malicious_user)
            )

            # Should return empty and NOT drop any tables
            self.assertEqual(len(result), 0)

            # Verify users table still exists
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'")
            self.assertIsNotNone(cursor.fetchone(), "users table should still exist after injection attempt")
        finally:
            self._connection_pool.release_connection(conn)

    def test_adversarial_sql_injection_union_bypass(self):
        """SQL injection attempt: UNION-based bypass should be safely parameterized."""
        conn = self._get_db_conn()
        try:
            vault_id = self._insert_vault(conn, "PrivateVault")
            # Note: user 999 does NOT have access to this vault
            self._insert_vault_member(conn, vault_id, 2, "read")

            # Attempt UNION injection
            malicious_user = {"id": "1 UNION SELECT id FROM users--", "role": "member"}

            result = self._run_async(
                self._vaults_module._fetch_accessible_vaults(conn, malicious_user)
            )

            # Should return empty - injection is treated as literal string
            self.assertEqual(len(result), 0)
        finally:
            self._connection_pool.release_connection(conn)

    def test_adversarial_sql_injection_comment_eof(self):
        """SQL injection attempt: trailing comments should be safely parameterized."""
        conn = self._get_db_conn()
        try:
            vault_id = self._insert_vault(conn, "TestVault")
            self._insert_vault_member(conn, vault_id, 2, "read")

            # Attempt with trailing comment
            malicious_user = {"id": "2 --", "role": "member"}

            result = self._run_async(
                self._vaults_module._fetch_accessible_vaults(conn, malicious_user)
            )

            # user_id "2 --" is treated as literal string, not comment
            # Should not match user 2's actual vault
            self.assertEqual(len(result), 0)
        finally:
            self._connection_pool.release_connection(conn)

    # ===== 2. User with no id field =====

    def test_adversarial_user_missing_id_field(self):
        """User dict with no 'id' key should return empty list."""
        conn = self._get_db_conn()
        try:
            vault_id = self._insert_vault(conn, "TestVault")
            self._insert_vault_member(conn, vault_id, 2, "read")

            # User without id field
            user_no_id = {"username": "someuser", "role": "member"}

            result = self._run_async(
                self._vaults_module._fetch_accessible_vaults(conn, user_no_id)
            )

            self.assertEqual(len(result), 0)
        finally:
            self._connection_pool.release_connection(conn)

    def test_adversarial_user_id_is_none(self):
        """User dict with id=None should return empty list."""
        conn = self._get_db_conn()
        try:
            vault_id = self._insert_vault(conn, "TestVault")
            self._insert_vault_member(conn, vault_id, 2, "read")

            user_with_none_id = {"id": None, "role": "member"}

            result = self._run_async(
                self._vaults_module._fetch_accessible_vaults(conn, user_with_none_id)
            )

            self.assertEqual(len(result), 0)
        finally:
            self._connection_pool.release_connection(conn)

    def test_adversarial_user_id_empty_string(self):
        """User dict with id='' should return empty list."""
        conn = self._get_db_conn()
        try:
            vault_id = self._insert_vault(conn, "TestVault")
            self._insert_vault_member(conn, vault_id, 2, "read")

            user_with_empty_id = {"id": "", "role": "member"}

            result = self._run_async(
                self._vaults_module._fetch_accessible_vaults(conn, user_with_empty_id)
            )

            self.assertEqual(len(result), 0)
        finally:
            self._connection_pool.release_connection(conn)

    # ===== 3. User is None =====

    def test_adversarial_user_is_none(self):
        """user=None should return empty list."""
        conn = self._get_db_conn()
        try:
            vault_id = self._insert_vault(conn, "TestVault")
            self._insert_vault_member(conn, vault_id, 2, "read")

            result = self._run_async(
                self._vaults_module._fetch_accessible_vaults(conn, None)
            )

            self.assertEqual(len(result), 0)
        finally:
            self._connection_pool.release_connection(conn)

    # ===== 4. Special characters in user_id =====

    def test_adversarial_user_id_unicode(self):
        """Unicode characters in user_id should be safely handled."""
        conn = self._get_db_conn()
        try:
            vault_id = self._insert_vault(conn, "TestVault")
            self._insert_vault_member(conn, vault_id, 2, "read")

            unicode_user = {"id": "\u0000\u2027\uFEFF", "role": "member"}

            result = self._run_async(
                self._vaults_module._fetch_accessible_vaults(conn, unicode_user)
            )

            # Should return empty - unicode is not a valid user_id
            self.assertEqual(len(result), 0)
        finally:
            self._connection_pool.release_connection(conn)

    def test_adversarial_user_id_newlines(self):
        """Newlines and control characters in user_id should be safely handled."""
        conn = self._get_db_conn()
        try:
            vault_id = self._insert_vault(conn, "TestVault")
            self._insert_vault_member(conn, vault_id, 2, "read")

            malicious_user = {"id": "2\nDROP TABLE users;\n", "role": "member"}

            result = self._run_async(
                self._vaults_module._fetch_accessible_vaults(conn, malicious_user)
            )

            # Should return empty and NOT drop any tables
            self.assertEqual(len(result), 0)

            # Verify users table still exists
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'")
            self.assertIsNotNone(cursor.fetchone(), "users table should still exist")
        finally:
            self._connection_pool.release_connection(conn)

    def test_adversarial_user_id_sql_wildcards(self):
        """SQL wildcards % and _ in user_id should be treated as literals."""
        conn = self._get_db_conn()
        try:
            vault_id = self._insert_vault(conn, "TestVault")
            self._insert_vault_member(conn, vault_id, 2, "read")

            # User id with SQL wildcards
            wildcard_user = {"id": "%", "role": "member"}

            result = self._run_async(
                self._vaults_module._fetch_accessible_vaults(conn, wildcard_user)
            )

            # Should return empty - % is not a valid user_id
            self.assertEqual(len(result), 0)

            wildcard_user2 = {"id": "_", "role": "member"}
            result2 = self._run_async(
                self._vaults_module._fetch_accessible_vaults(conn, wildcard_user2)
            )
            self.assertEqual(len(result2), 0)
        finally:
            self._connection_pool.release_connection(conn)

    # ===== 5. Large number of vault memberships (DoS prevention) =====

    def test_adversarial_many_vault_memberships_performance(self):
        """User with many vault memberships should not cause DoS."""
        conn = self._get_db_conn()
        try:
            # Create 100 vaults and add user 2 to all of them
            vault_ids = []
            for i in range(100):
                vid = self._insert_vault(conn, f"Vault_{i}")
                self._insert_vault_member(conn, vid, 2, "read")
                vault_ids.append(vid)

            user = {"id": 2, "role": "member"}

            import time
            start = time.time()
            result = self._run_async(
                self._vaults_module._fetch_accessible_vaults(conn, user)
            )
            elapsed = time.time() - start

            # Should complete in reasonable time (< 5 seconds)
            self.assertLess(elapsed, 5.0, "Should not take more than 5 seconds for 100 vaults")
            # Should return all 100 vaults
            self.assertEqual(len(result), 100)
        finally:
            self._connection_pool.release_connection(conn)

    def test_adversarial_empty_vault_ids_result(self):
        """User with access to no vaults should return empty list efficiently."""
        conn = self._get_db_conn()
        try:
            # Create vault but DON'T give user 2 access
            self._insert_vault(conn, "PrivateVault")

            user = {"id": 2, "role": "member"}

            result = self._run_async(
                self._vaults_module._fetch_accessible_vaults(conn, user)
            )

            self.assertEqual(len(result), 0)
        finally:
            self._connection_pool.release_connection(conn)

    # ===== 6. Phantom/non-existent vault IDs - handled by SQL FK constraints =====

    def test_adversarial_phantom_vault_id_not_possible(self):
        """Cannot insert phantom vault_id due to FK constraint - verified by SQLite."""
        conn = self._get_db_conn()
        try:
            # Verify FK constraints prevent phantom vault references
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO vault_members (vault_id, user_id, permission, granted_by) VALUES (?, ?, ?, ?)",
                    (99999, 2, "read", 1)
                )
                conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    # ===== 7. Role bypass attempts =====

    def test_adversarial_fake_admin_role(self):
        """Function trusts role field - passing admin returns all vaults.

        Note: The _fetch_accessible_vaults function trusts the role field in the
        user dict. Actual admin verification happens at the route layer via
        get_current_active_user dependency. This test documents that the function
        itself does not validate admin status against the database.
        """
        conn = self._get_db_conn()
        try:
            # Create vault
            self._insert_vault(conn, "OtherVault")

            # User 2 claims to be admin - function trusts this
            user_fake_admin = {"id": 2, "role": "admin"}

            result = self._run_async(
                self._vaults_module._fetch_accessible_vaults(conn, user_fake_admin)
            )

            # Since role="admin", function calls _fetch_all_vaults and returns ALL vaults
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].name, "OtherVault")
        finally:
            self._connection_pool.release_connection(conn)

    def test_adversarial_fake_superadmin_role(self):
        """Function trusts role field - passing superadmin returns all vaults.

        Same as above - the function trusts the role field. Actual security
        is enforced at the route layer.
        """
        conn = self._get_db_conn()
        try:
            # Create vault that user 2 does NOT have access to
            self._insert_vault(conn, "TrulyPrivateVault", visibility="private")

            user_fake_superadmin = {"id": 2, "role": "superadmin"}

            result = self._run_async(
                self._vaults_module._fetch_accessible_vaults(conn, user_fake_superadmin)
            )

            # Since role="superadmin", function calls _fetch_all_vaults
            # and returns ALL vaults regardless of actual permissions
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].name, "TrulyPrivateVault")
        finally:
            self._connection_pool.release_connection(conn)




class TestVaultResponseOrgId(unittest.TestCase):
    """Tests for VaultResponse.org_id field mapping and org-scoped vault filtering.

    Verifies that:
    - org_id is None when a vault has no organization assigned.
    - org_id is correctly populated from the database row for org-scoped vaults.
    - The /api/vaults list endpoint returns org_id in each vault response.
    """

    def setUp(self):
        self.client = TestClient(app)
        self._temp_dir = tempfile.mkdtemp()
        db_path = str(Path(self._temp_dir) / "test.db")
        from app.models.database import init_db
        init_db(db_path)
        self._connection_pool = SimpleConnectionPool(db_path)

        conn = self._connection_pool.get_connection()
        conn.execute(
            "INSERT INTO users (id, username, hashed_password, role, is_active) VALUES (?, ?, ?, ?, ?)",
            (1, "admin", "abc123", "superadmin", 1),
        )
        conn.commit()
        self._connection_pool.release_connection(conn)

        def override_get_db():
            c = self._connection_pool.get_connection()
            try:
                yield c
            finally:
                self._connection_pool.release_connection(c)

        app.dependency_overrides[get_db] = override_get_db
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
        app.dependency_overrides.pop(get_current_active_user, None)
        if hasattr(self, '_connection_pool'):
            self._connection_pool.close_all()
        import shutil
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _insert_org(self, conn, name):
        cursor = conn.execute(
            "INSERT INTO organizations (name, created_by) VALUES (?, ?)",
            (name, 1),
        )
        conn.commit()
        return cursor.lastrowid

    # ---- org_id field tests ----

    def test_vault_response_org_id_is_none_for_global_vault(self):
        """A vault created without an org returns org_id=None in the response."""
        resp = self.client.post("/api/vaults", json={"name": "GlobalVault", "description": ""})
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertIn("org_id", data)
        self.assertIsNone(data["org_id"])

    def test_vault_response_org_id_is_populated_for_org_vault(self):
        """A vault created with an explicit org_id returns that org_id in the response."""
        conn = self._connection_pool.get_connection()
        org_id = self._insert_org(conn, "Acme Corp")
        # Also add creator as org member so the route accepts org_id
        conn.execute(
            "INSERT INTO org_members (org_id, user_id, role) VALUES (?, ?, ?)",
            (org_id, 1, "owner"),
        )
        conn.commit()
        self._connection_pool.release_connection(conn)

        resp = self.client.post(
            "/api/vaults",
            json={"name": "AcmeVault", "description": "", "org_id": org_id},
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(data["org_id"], org_id)

    def test_list_vaults_includes_org_id_field(self):
        """GET /api/vaults returns org_id for every vault in the list."""
        resp_create = self.client.post(
            "/api/vaults", json={"name": "ListVault", "description": ""}
        )
        self.assertEqual(resp_create.status_code, 201)

        resp_list = self.client.get("/api/vaults")
        self.assertEqual(resp_list.status_code, 200)
        vaults = resp_list.json()["vaults"]
        self.assertGreater(len(vaults), 0)
        for vault in vaults:
            self.assertIn("org_id", vault)
        created_vault = next(v for v in vaults if v["name"] == "ListVault")
        self.assertIsNone(created_vault["org_id"])

    def test_get_single_vault_includes_org_id(self):
        """GET /api/vaults/{id} includes org_id=None for a vault without an org."""
        resp = self.client.post("/api/vaults", json={"name": "SingleVault", "description": ""})
        self.assertEqual(resp.status_code, 201)
        vault_id = resp.json()["id"]

        resp_get = self.client.get(f"/api/vaults/{vault_id}")
        self.assertEqual(resp_get.status_code, 200)
        self.assertIn("org_id", resp_get.json())
        self.assertIsNone(resp_get.json()["org_id"])

    def test_org_vault_org_id_preserved_after_update(self):
        """org_id is not cleared when a vault's name or description is updated."""
        conn = self._connection_pool.get_connection()
        org_id = self._insert_org(conn, "UpdateOrg")
        conn.execute(
            "INSERT INTO org_members (org_id, user_id, role) VALUES (?, ?, ?)",
            (org_id, 1, "owner"),
        )
        conn.commit()
        self._connection_pool.release_connection(conn)

        resp = self.client.post(
            "/api/vaults",
            json={"name": "OrgVaultUpdate", "description": "", "org_id": org_id},
        )
        self.assertEqual(resp.status_code, 201)
        vault_id = resp.json()["id"]

        resp_put = self.client.put(
            f"/api/vaults/{vault_id}",
            json={"description": "Updated description"},
        )
        self.assertEqual(resp_put.status_code, 200)
        self.assertEqual(resp_put.json()["org_id"], org_id)


if __name__ == '__main__':
    unittest.main()
