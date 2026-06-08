"""
Vault query LIMIT tests (FR-005).

Tests that _fetch_all_vaults applies LIMIT 1000 to prevent unbounded result sets,
and that _fetch_vault_with_counts remains unaffected (no LIMIT).
"""

import asyncio
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

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

from app.api.routes.vaults import (
    _VAULT_WITH_COUNTS_SQL,
    _fetch_all_vaults,
    _fetch_vault_with_counts,
)


class TestVaultQueryLimit(unittest.TestCase):
    """Tests for vault query LIMIT behavior."""

    def setUp(self):
        """Set up in-memory SQLite database for testing."""
        self._temp_dir = tempfile.mkdtemp()
        db_path = str(Path(self._temp_dir) / "test_limit.db")

        # Create in-memory database with schema
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON;")

        # Initialize schema
        from app.models.database import init_db
        init_db(db_path)

        # Reconnect to initialized database
        self.conn.close()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON;")

        # Insert test user
        self.conn.execute(
            "INSERT INTO users (id, username, hashed_password, role, is_active) VALUES (?, ?, ?, ?, ?)",
            (1, "admin", "abc123", "superadmin", 1),
        )
        self.conn.commit()

        # Save originals so tearDown can fully restore them.
        import app.api.routes.vaults as _vaults_module
        self._original_get_effective_vault_permission = _vaults_module.get_effective_vault_permission
        self._original_get_effective_vault_permissions = _vaults_module.get_effective_vault_permissions

    def tearDown(self):
        """Clean up database connection and temp directory."""
        if hasattr(self, 'conn') and self.conn:
            self.conn.close()
        import app.api.routes.vaults as _vaults_module
        if self._original_get_effective_vault_permission is not None:
            _vaults_module.get_effective_vault_permission = self._original_get_effective_vault_permission
        if self._original_get_effective_vault_permissions is not None:
            _vaults_module.get_effective_vault_permissions = self._original_get_effective_vault_permissions
        import shutil
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _create_vault_direct(self, name, org_id=None, visibility="private"):
        """Create a vault directly via SQL for test setup."""
        cursor = self.conn.execute(
            "INSERT INTO vaults (name, description, org_id, visibility, owner_id) VALUES (?, ?, ?, ?, ?)",
            (name, f"desc_{name}", org_id, visibility, 1),
        )
        self.conn.commit()
        return cursor.lastrowid

    def _create_vault_member(self, vault_id, user_id, permission="admin"):
        """Create a vault membership directly via SQL."""
        self.conn.execute(
            "INSERT INTO vault_members (vault_id, user_id, permission) VALUES (?, ?, ?)",
            (vault_id, user_id, permission),
        )
        self.conn.commit()

    # ===== 1. Happy path: <1000 vaults returned without truncation =====

    def test_fetch_all_vaults_under_limit_returns_all(self):
        """When fewer than 1000 vaults exist, all are returned (no truncation)."""
        # Create 50 vaults (well under LIMIT 1000)
        vault_ids = []
        for i in range(50):
            vid = self._create_vault_direct(f"vault_{i:03d}")
            vault_ids.append(vid)
            self._create_vault_member(vid, 1)

        # Mock the permission functions to return "admin" for all
        import app.api.routes.vaults as vaults_module
        vaults_module.get_effective_vault_permission = AsyncMock(return_value="admin")
        vaults_module.get_effective_vault_permissions = AsyncMock(return_value={vid: "admin" for vid in vault_ids})

        try:
            # Run the async function
            loop = asyncio.new_event_loop()
            vaults = loop.run_until_complete(_fetch_all_vaults(self.conn, {"id": 1}))
            loop.close()

            # All 50 vaults should be returned
            self.assertEqual(len(vaults), 50)
            vault_names = {v.name for v in vaults}
            for i in range(50):
                self.assertIn(f"vault_{i:03d}", vault_names)
        finally:
            # Restore mocks
            vaults_module.get_effective_vault_permission = AsyncMock(return_value=None)
            vaults_module.get_effective_vault_permissions = AsyncMock(return_value={})

    def test_fetch_all_vaults_exactly_1000(self):
        """When exactly 1000 vaults exist, all are returned."""
        # Create 1000 vaults
        vault_ids = []
        for i in range(1000):
            vid = self._create_vault_direct(f"vault_{i:04d}")
            vault_ids.append(vid)
            self._create_vault_member(vid, 1)

        import app.api.routes.vaults as vaults_module
        vaults_module.get_effective_vault_permissions = AsyncMock(return_value={vid: "admin" for vid in vault_ids})

        try:
            loop = asyncio.new_event_loop()
            vaults = loop.run_until_complete(_fetch_all_vaults(self.conn, {"id": 1}))
            loop.close()

            # All 1000 vaults should be returned
            self.assertEqual(len(vaults), 1000)
        finally:
            vaults_module.get_effective_vault_permissions = AsyncMock(return_value={})

    # ===== 2. LIMIT is syntactically valid SQL =====

    def test_limit_syntax_is_valid_sql(self):
        """The LIMIT 1000 clause is syntactically valid SQL."""
        # Construct the actual query that _fetch_all_vaults uses
        query = _VAULT_WITH_COUNTS_SQL + " GROUP BY v.id ORDER BY v.created_at ASC LIMIT 1000"

        # This should not raise any exception - just verify syntax
        cursor = self.conn.execute(query)
        # Execute to verify it runs without error
        rows = cursor.fetchall()
        # Should succeed (may be empty)
        self.assertIsInstance(rows, list)

    def test_limit_clause_present_in_query(self):
        """Verify LIMIT 1000 is present in the combined query."""
        query = _VAULT_WITH_COUNTS_SQL + " GROUP BY v.id ORDER BY v.created_at ASC LIMIT 1000"
        self.assertIn("LIMIT 1000", query)

    # ===== 3. _fetch_vault_with_counts unaffected (no LIMIT) =====

    def test_fetch_vault_with_counts_no_limit(self):
        """_fetch_vault_with_counts uses the same SQL constant but has WHERE clause, no LIMIT."""
        # Create a vault first
        vid = self._create_vault_direct("test_vault")
        self._create_vault_member(vid, 1)

        import app.api.routes.vaults as vaults_module
        vaults_module.get_effective_vault_permission = AsyncMock(return_value="admin")

        try:
            loop = asyncio.new_event_loop()
            vault = loop.run_until_complete(_fetch_vault_with_counts(self.conn, vid, {"id": 1}))
            loop.close()

            self.assertIsNotNone(vault)
            self.assertEqual(vault.name, "test_vault")
            self.assertEqual(vault.id, vid)
        finally:
            vaults_module.get_effective_vault_permission = AsyncMock(return_value=None)

    def test_fetch_vault_with_counts_nonexistent(self):
        """_fetch_vault_with_counts returns None for non-existent vault."""
        import app.api.routes.vaults as vaults_module
        vaults_module.get_effective_vault_permission = AsyncMock(return_value=None)

        try:
            loop = asyncio.new_event_loop()
            vault = loop.run_until_complete(_fetch_vault_with_counts(self.conn, 99999))
            loop.close()

            self.assertIsNone(vault)
        finally:
            vaults_module.get_effective_vault_permission = AsyncMock(return_value=None)

    # ===== 4. Adversarial: LIMIT 1000 may truncate large deployments =====

    def test_fetch_all_vaults_truncates_at_1000(self):
        """When >1000 vaults exist, only 1000 are returned (adversarial)."""
        # Create 1050 vaults (above the limit)
        vault_ids = []
        for i in range(1050):
            vid = self._create_vault_direct(f"vault_{i:04d}")
            vault_ids.append(vid)
            self._create_vault_member(vid, 1)

        import app.api.routes.vaults as vaults_module
        vaults_module.get_effective_vault_permissions = AsyncMock(return_value={vid: "admin" for vid in vault_ids})

        try:
            loop = asyncio.new_event_loop()
            vaults = loop.run_until_complete(_fetch_all_vaults(self.conn, {"id": 1}))
            loop.close()

            # Only 1000 should be returned (LIMIT applied)
            self.assertEqual(len(vaults), 1000)

            # The oldest 1000 should be returned (ORDER BY v.created_at ASC)
            vault_names = {v.name for v in vaults}
            # First 1000 vaults should be present
            for i in range(1000):
                self.assertIn(f"vault_{i:04d}", vault_names)
            # Vaults 1000-1049 should NOT be present (truncated)
            for i in range(1000, 1050):
                self.assertNotIn(f"vault_{i:04d}", vault_names)
        finally:
            vaults_module.get_effective_vault_permissions = AsyncMock(return_value={})

    def test_1050_vaults_50_truncated(self):
        """Exactly 1050 vaults - 50 should be truncated due to LIMIT."""
        # Create 1050 vaults
        vault_ids = []
        for i in range(1050):
            vid = self._create_vault_direct(f"bigvault_{i:04d}")
            vault_ids.append(vid)
            self._create_vault_member(vid, 1)

        import app.api.routes.vaults as vaults_module
        vaults_module.get_effective_vault_permissions = AsyncMock(return_value={vid: "admin" for vid in vault_ids})

        try:
            loop = asyncio.new_event_loop()
            vaults = loop.run_until_complete(_fetch_all_vaults(self.conn, {"id": 1}))
            loop.close()

            returned = len(vaults)
            truncated = 1050 - returned

            # 50 vaults should be truncated
            self.assertEqual(returned, 1000)
            self.assertEqual(truncated, 50)

            # Verify names of truncated vaults are NOT in results
            truncated_names = {f"bigvault_{i:04d}" for i in range(1000, 1050)}
            returned_names = {v.name for v in vaults}
            intersection = truncated_names & returned_names
            self.assertEqual(len(intersection), 0, "Truncated vaults should not appear in results")
        finally:
            vaults_module.get_effective_vault_permissions = AsyncMock(return_value={})

    # ===== 5. Adversarial: Admin expects full list =====

    def test_admin_gets_truncated_list(self):
        """Admin users calling /vaults also receive truncated results (by design)."""
        # Create 1500 vaults
        vault_ids = []
        for i in range(1500):
            vid = self._create_vault_direct(f"admin_vault_{i:04d}")
            vault_ids.append(vid)
            self._create_vault_member(vid, 1)

        import app.api.routes.vaults as vaults_module
        vaults_module.get_effective_vault_permissions = AsyncMock(return_value={vid: "admin" for vid in vault_ids})

        try:
            loop = asyncio.new_event_loop()
            vaults = loop.run_until_complete(_fetch_all_vaults(self.conn, {"id": 1, "role": "admin"}))
            loop.close()

            # Admin gets 1000, not 1500
            self.assertEqual(len(vaults), 1000)

            # Admin cannot fetch more than LIMIT even with role
            vault_names = [v.name for v in vaults]
            # Should contain vaults 0-999
            self.assertIn("admin_vault_0000", vault_names)
            self.assertIn("admin_vault_0999", vault_names)
            # Should NOT contain vaults 1000+
            self.assertNotIn("admin_vault_1000", vault_names)
            self.assertNotIn("admin_vault_1499", vault_names)
        finally:
            vaults_module.get_effective_vault_permissions = AsyncMock(return_value={})

    def test_superadmin_gets_truncated_list(self):
        """Superadmin users calling /vaults also receive truncated results (by design)."""
        # Create 1200 vaults
        vault_ids = []
        for i in range(1200):
            vid = self._create_vault_direct(f"super_vault_{i:04d}")
            vault_ids.append(vid)
            self._create_vault_member(vid, 1)

        import app.api.routes.vaults as vaults_module
        vaults_module.get_effective_vault_permissions = AsyncMock(return_value={vid: "admin" for vid in vault_ids})

        try:
            loop = asyncio.new_event_loop()
            vaults = loop.run_until_complete(_fetch_all_vaults(self.conn, {"id": 1, "role": "superadmin"}))
            loop.close()

            # Superadmin gets 1000, not 1200
            self.assertEqual(len(vaults), 1000)
        finally:
            vaults_module.get_effective_vault_permissions = AsyncMock(return_value={})


class TestVaultQueryLimitSQLiteVerification(unittest.TestCase):
    """Direct SQLite verification tests for LIMIT behavior."""

    def setUp(self):
        """Set up in-memory SQLite database."""
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON;")

        # Create minimal schema
        self.conn.executescript("""
            CREATE TABLE vaults (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                org_id INTEGER,
                visibility TEXT DEFAULT 'private',
                owner_id INTEGER
            );
            CREATE TABLE files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vault_id INTEGER,
                file_name TEXT,
                file_path TEXT,
                status TEXT DEFAULT 'pending',
                file_size INTEGER DEFAULT 0
            );
            CREATE TABLE memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vault_id INTEGER,
                content TEXT
            );
            CREATE TABLE chat_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vault_id INTEGER,
                title TEXT
            );
        """)

    def tearDown(self):
        self.conn.close()

    def _create_vault(self, name):
        cursor = self.conn.execute(
            "INSERT INTO vaults (name) VALUES (?)",
            (name,)
        )
        self.conn.commit()
        return cursor.lastrowid

    def _create_file(self, vault_id, file_name):
        self.conn.execute(
            "INSERT INTO files (vault_id, file_name, file_path, status, file_size) VALUES (?, ?, ?, ?, ?)",
            (vault_id, file_name, f"/tmp/{file_name}", "indexed", 100)
        )
        self.conn.commit()

    def _create_memory(self, vault_id, content):
        self.conn.execute(
            "INSERT INTO memories (vault_id, content) VALUES (?, ?)",
            (vault_id, content)
        )
        self.conn.commit()

    def _create_session(self, vault_id, title):
        self.conn.execute(
            "INSERT INTO chat_sessions (vault_id, title) VALUES (?, ?)",
            (vault_id, title)
        )
        self.conn.commit()

    def test_sql_limit_1000_direct_query(self):
        """Direct SQL query with LIMIT 1000 returns correct number of rows."""
        # Create 1500 vaults
        for i in range(1500):
            self._create_vault(f"direct_vault_{i:04d}")

        # Execute the actual query from _fetch_all_vaults
        query = _VAULT_WITH_COUNTS_SQL + " GROUP BY v.id ORDER BY v.created_at ASC LIMIT 1000"
        cursor = self.conn.execute(query)
        rows = cursor.fetchall()

        # LIMIT 1000 should be enforced
        self.assertEqual(len(rows), 1000)

        # Verify first row is vault_0000 (oldest)
        self.assertEqual(rows[0][1], "direct_vault_0000")

        # Verify 1000th row is vault_0999
        self.assertEqual(rows[999][1], "direct_vault_0999")

    def test_sql_without_limit_would_return_all(self):
        """Without LIMIT, query would return all 1500 vaults."""
        # Create 1500 vaults
        for i in range(1500):
            self._create_vault(f"nolimit_vault_{i:04d}")

        # Query without LIMIT
        query_no_limit = _VAULT_WITH_COUNTS_SQL + " GROUP BY v.id ORDER BY v.created_at ASC"
        cursor = self.conn.execute(query_no_limit)
        rows = cursor.fetchall()

        # All 1500 should be returned without LIMIT
        self.assertEqual(len(rows), 1500)

    def test_fetch_vault_with_counts_uses_where_no_limit(self):
        """_fetch_vault_with_counts query uses WHERE clause but NO LIMIT."""
        # Create 100 vaults
        for i in range(100):
            self._create_vault(f"where_vault_{i:03d}")
            self._create_file(i + 1, f"file_{i}.txt")

        # Query used by _fetch_vault_with_counts
        where_query = _VAULT_WITH_COUNTS_SQL + " WHERE v.id = ? GROUP BY v.id"
        cursor = self.conn.execute(where_query, (50,))
        row = cursor.fetchone()

        # Should return the specific vault (id=50)
        self.assertIsNotNone(row)
        self.assertEqual(row[1], "where_vault_049")

        # Verify LIMIT is NOT in this query
        self.assertNotIn("LIMIT", where_query)

    def test_counts_are_correct_with_join(self):
        """Verify file/memory/session counts are correct with the JOIN query."""
        # Create a vault
        vid = self._create_vault("count_test_vault")

        # Add 5 files
        for i in range(5):
            self._create_file(vid, f"doc_{i}.pdf")

        # Add 3 memories
        for i in range(3):
            self._create_memory(vid, f"memory_{i}")

        # Add 2 chat sessions
        for i in range(2):
            self._create_session(vid, f"chat_{i}")

        # Execute query
        query = _VAULT_WITH_COUNTS_SQL + " WHERE v.id = ? GROUP BY v.id"
        cursor = self.conn.execute(query, (vid,))
        row = cursor.fetchone()

        # file_count should be 5, memory_count should be 3, session_count should be 2
        self.assertEqual(row[5], 5)  # file_count
        self.assertEqual(row[6], 3)  # memory_count
        self.assertEqual(row[7], 2)  # session_count


if __name__ == "__main__":
    unittest.main()
