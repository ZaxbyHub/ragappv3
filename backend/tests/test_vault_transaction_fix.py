"""
Verification tests for vault creation transaction behavior (FR-004).

Tests that verify:
1. On success, both vault and vault_members are created and committed
2. On duplicate name (IntegrityError), rollback occurs and 409 is returned (no orphan)
3. On vault_members failure, rollback occurs and vault row is removed (no orphan)
4. The transaction is properly committed only after both INSERTs succeed
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

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
    get_vector_store,
)
from app.main import app


class TestVaultTransactionBehavior(unittest.TestCase):
    """Test suite for vault creation transaction behavior (FR-004)."""

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

        # Mock vector store
        self._mock_vector_store = MagicMock()
        self._mock_vector_store.delete_by_vault = MagicMock(return_value=0)

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
        self._get_db = get_db

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_vector_store, None)
        app.dependency_overrides.pop(get_current_active_user, None)
        if hasattr(self, '_connection_pool'):
            self._connection_pool.close_all()
        import shutil
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _get_db_conn(self):
        """Get a raw connection for test data inspection."""
        conn = self._connection_pool.get_connection()
        return conn

    def _count_vaults(self):
        """Return count of vaults in database."""
        conn = self._get_db_conn()
        try:
            cursor = conn.execute("SELECT COUNT(*) FROM vaults")
            count = cursor.fetchone()[0]
        finally:
            self._connection_pool.release_connection(conn)
        return count

    def _count_vault_members(self, vault_id=None):
        """Return count of vault_members in database, optionally filtered by vault_id."""
        conn = self._get_db_conn()
        try:
            if vault_id is None:
                cursor = conn.execute("SELECT COUNT(*) FROM vault_members")
            else:
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM vault_members WHERE vault_id = ?",
                    (vault_id,)
                )
            count = cursor.fetchone()[0]
        finally:
            self._connection_pool.release_connection(conn)
        return count

    def _get_vault_names(self):
        """Return list of all vault names."""
        conn = self._get_db_conn()
        try:
            cursor = conn.execute("SELECT name FROM vaults ORDER BY id")
            return [row[0] for row in cursor.fetchall()]
        finally:
            self._connection_pool.release_connection(conn)

    # Test 1: On success, both vault and vault_members are created and committed

    def test_create_vault_success_creates_both_records(self):
        """POST /api/vaults on success creates vault AND vault_members records."""
        vault_count_before = self._count_vaults()
        member_count_before = self._count_vault_members()

        def override_get_db():
            conn = self._connection_pool.get_connection()
            try:
                yield conn
            finally:
                self._connection_pool.release_connection(conn)

        app.dependency_overrides[get_db] = override_get_db

        try:
            resp = self.client.post(
                "/api/vaults",
                json={"name": "Research", "description": "My research"}
            )

            self.assertEqual(resp.status_code, 201)
            vault = resp.json()
            self.assertEqual(vault["name"], "Research")
            self.assertIn("id", vault)

            # Verify vault was created
            self.assertEqual(self._count_vaults(), vault_count_before + 1)

            # Verify vault_members was also created
            self.assertEqual(
                self._count_vault_members(vault["id"]),
                member_count_before + 1
            )

            # Verify vault_members has correct permission
            conn = self._get_db_conn()
            try:
                cursor = conn.execute(
                    "SELECT permission FROM vault_members WHERE vault_id = ? AND user_id = ?",
                    (vault["id"], 1)
                )
                row = cursor.fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row["permission"], "admin")
            finally:
                self._connection_pool.release_connection(conn)
        finally:
            app.dependency_overrides.pop(get_db, None)

    # Test 2: On duplicate name (IntegrityError), rollback occurs and 409 is returned (no orphan)

    def test_create_vault_duplicate_name_rollback_no_orphan(self):
        """POST duplicate name returns 409 and rolls back - no orphan vault."""
        def override_get_db():
            conn = self._connection_pool.get_connection()
            try:
                yield conn
            finally:
                self._connection_pool.release_connection(conn)

        app.dependency_overrides[get_db] = override_get_db

        try:
            # First create a vault
            resp1 = self.client.post(
                "/api/vaults",
                json={"name": "DuplicateTest"}
            )
            self.assertEqual(resp1.status_code, 201)
            vault_id_1 = resp1.json()["id"]

            vault_count_before = self._count_vaults()
            member_count_before = self._count_vault_members()

            # Try to create another vault with the same name
            resp2 = self.client.post(
                "/api/vaults",
                json={"name": "DuplicateTest"}
            )

            self.assertEqual(resp2.status_code, 409)
            self.assertIn("already exists", resp2.json()["detail"])

            # Verify no new vault was created (rollback occurred)
            self.assertEqual(self._count_vaults(), vault_count_before)

            # Verify no new vault_members were created (rollback occurred)
            self.assertEqual(self._count_vault_members(), member_count_before)

            # Verify the original vault is still intact
            conn = self._get_db_conn()
            try:
                cursor = conn.execute(
                    "SELECT id, name FROM vaults WHERE id = ?",
                    (vault_id_1,)
                )
                row = cursor.fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row["name"], "DuplicateTest")
            finally:
                self._connection_pool.release_connection(conn)
        finally:
            app.dependency_overrides.pop(get_db, None)

    # Test 3: On vault_members failure, rollback occurs and vault row is removed (no orphan)
    # This test simulates vault_members failure by using a connection that fails on the second INSERT

    def test_create_vault_members_failure_rollback_no_orphan(self):
        """When vault_members insert fails, vault is rolled back - no orphan vault."""
        vault_count_before = self._count_vaults()
        member_count_before = self._count_vault_members()

        # Create a wrapper around the pool that will fail on vault_members INSERT
        original_pool = self._connection_pool
        fail_on_members = [False]  # Use list to allow mutation in closure

        class FailingConnectionWrapper:
            """Wraps a connection to fail on vault_members INSERT."""
            def __init__(self, conn):
                self._conn = conn

            @property
            def row_factory(self):
                return self._conn.row_factory

            @row_factory.setter
            def row_factory(self, value):
                self._conn.row_factory = value

            def execute(self, sql, params=None):
                # Check if this is a vault_members INSERT
                if sql.startswith("INSERT INTO vault_members"):
                    if fail_on_members[0]:
                        # Raise a generic Exception (not IntegrityError) to simulate
                        # a non-Uniqueness constraint failure (e.g., FK violation, NOT null, etc.)
                        raise Exception("Simulated vault_members failure")
                # Forward to real connection
                if params is not None:
                    return self._conn.execute(sql, params)
                else:
                    return self._conn.execute(sql)

            def commit(self):
                return self._conn.commit()

            def rollback(self):
                return self._conn.rollback()

            def close(self):
                return self._conn.close()

            def cursor(self):
                return self._conn.cursor()

        class FailingPool:
            """Pool that returns connections that fail on vault_members INSERT."""
            def __init__(self, real_pool):
                self._real_pool = real_pool

            def get_connection(self):
                real_conn = self._real_pool.get_connection()
                return FailingConnectionWrapper(real_conn)

            def release_connection(self, conn):
                # Get the real connection from wrapper and release it
                if isinstance(conn, FailingConnectionWrapper):
                    self._real_pool.release_connection(conn._conn)
                else:
                    self._real_pool.release_connection(conn)

            def close_all(self):
                self._real_pool.close_all()

        failing_pool = FailingPool(original_pool)

        def override_get_db():
            conn = failing_pool.get_connection()
            try:
                yield conn
            finally:
                failing_pool.release_connection(conn)

        app.dependency_overrides[get_db] = override_get_db

        try:
            # Enable failure on next vault_members INSERT
            fail_on_members[0] = True

            # Now make the API call - it should fail with 500
            resp = self.client.post(
                "/api/vaults",
                json={"name": "TransactionTest", "description": "Test"}
            )

            # The API should return 500 because vault_members insert failed
            self.assertEqual(resp.status_code, 500)

            # Verify no new vault was created (rollback occurred)
            self.assertEqual(self._count_vaults(), vault_count_before)

            # Verify no new vault_members were created (rollback occurred)
            self.assertEqual(self._count_vault_members(), member_count_before)

        finally:
            fail_on_members[0] = False
            app.dependency_overrides.pop(get_db, None)

    # Test 4: The transaction is properly committed only after both INSERTs succeed
    # This test verifies the transactional behavior by checking final state

    def test_create_vault_transaction_atomic_success(self):
        """Transaction ensures both vault and vault_members are committed atomically."""
        # Create multiple vaults and verify each has its own vault_members entry
        vault_ids = []

        def override_get_db():
            conn = self._connection_pool.get_connection()
            try:
                yield conn
            finally:
                self._connection_pool.release_connection(conn)

        app.dependency_overrides[get_db] = override_get_db

        try:
            for i in range(3):
                resp = self.client.post(
                    "/api/vaults",
                    json={"name": f"AtomicVault{i}", "description": f"Test {i}"}
                )
                self.assertEqual(resp.status_code, 201)
                vault_ids.append(resp.json()["id"])

            # Verify all vaults exist
            self.assertEqual(len(self._get_vault_names()), 3)

            # Verify each vault has exactly one vault_members entry
            for vault_id in vault_ids:
                self.assertEqual(
                    self._count_vault_members(vault_id),
                    1,
                    f"Vault {vault_id} should have exactly 1 vault_members entry"
                )

            # Verify all vault_members belong to existing vaults
            conn = self._get_db_conn()
            try:
                cursor = conn.execute("SELECT vault_id FROM vault_members")
                all_vault_ids = {row[0] for row in cursor.fetchall()}
                for vault_id in vault_ids:
                    self.assertIn(vault_id, all_vault_ids)
            finally:
                self._connection_pool.release_connection(conn)

        finally:
            app.dependency_overrides.pop(get_db, None)

    # Test 5: Verify that if vault INSERT succeeds but vault_members fails,
    # the vault INSERT is rolled back (no orphan)

    def test_create_vault_no_orphan_when_members_fail(self):
        """Verify vault is not left as orphan when vault_members INSERT fails."""
        # First, create a valid vault normally to establish baseline
        def override_get_db():
            conn = self._connection_pool.get_connection()
            try:
                yield conn
            finally:
                self._connection_pool.release_connection(conn)

        app.dependency_overrides[get_db] = override_get_db

        try:
            # Create a normal vault first
            resp1 = self.client.post(
                "/api/vaults",
                json={"name": "NormalVault"}
            )
            self.assertEqual(resp1.status_code, 201)

            # Now try to create a vault with failing vault_members
            original_pool = self._connection_pool
            fail_on_members = [False]

            class FailingConnectionWrapper:
                def __init__(self, conn):
                    self._conn = conn

                @property
                def row_factory(self):
                    return self._conn.row_factory

                @row_factory.setter
                def row_factory(self, value):
                    self._conn.row_factory = value

                def execute(self, sql, params=None):
                    if sql.startswith("INSERT INTO vault_members"):
                        if fail_on_members[0]:
                            raise Exception("Simulated vault_members failure")
                    if params is not None:
                        return self._conn.execute(sql, params)
                    else:
                        return self._conn.execute(sql)

                def commit(self):
                    return self._conn.commit()

                def rollback(self):
                    return self._conn.rollback()

                def close(self):
                    return self._conn.close()

                def cursor(self):
                    return self._conn.cursor()

            class FailingPool:
                def __init__(self, real_pool):
                    self._real_pool = real_pool

                def get_connection(self):
                    return FailingConnectionWrapper(self._real_pool.get_connection())

                def release_connection(self, conn):
                    if isinstance(conn, FailingConnectionWrapper):
                        self._real_pool.release_connection(conn._conn)
                    else:
                        self._real_pool.release_connection(conn)

                def close_all(self):
                    self._real_pool.close_all()

            failing_pool = FailingPool(original_pool)

            def override_get_db_failing():
                conn = failing_pool.get_connection()
                try:
                    yield conn
                finally:
                    failing_pool.release_connection(conn)

            app.dependency_overrides[get_db] = override_get_db_failing

            fail_on_members[0] = True
            vault_count_before = self._count_vaults()

            resp2 = self.client.post(
                "/api/vaults",
                json={"name": "OrphanTest", "description": "Should be rolled back"}
            )

            # Should fail with 500
            self.assertEqual(resp2.status_code, 500)

            # Verify no new vault was created
            self.assertEqual(self._count_vaults(), vault_count_before)

            # Verify "OrphanTest" does not exist in the database
            self.assertNotIn("OrphanTest", self._get_vault_names())

        finally:
            app.dependency_overrides.pop(get_db, None)


if __name__ == '__main__':
    unittest.main()
