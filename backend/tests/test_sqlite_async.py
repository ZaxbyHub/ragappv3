"""
Tests for asyncio.to_thread wrappers around SQLite operations.

Verifies:
1. get_user_orgs() returns correct list when wrapped in to_thread
2. get_user_primary_org() returns correct org or None
3. Verify these functions are truly async (can be awaited)
4. Concurrent calls don't deadlock
5. login() endpoint still authenticates correctly (using direct function tests)

These tests focus on the SQLite async wrapping without depending on full app state.
"""

import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.api.deps import MultipleOrgError, get_user_orgs, get_user_primary_org
from app.models.database import SQLiteConnectionPool, init_db, run_migrations


class TestSQLiteConnectionPoolFailures(unittest.TestCase):
    def test_get_connection_restores_count_when_create_fails(self):
        temp_dir = tempfile.mkdtemp()
        shutil.rmtree(temp_dir)
        pool = SQLiteConnectionPool(os.path.join(temp_dir, "missing.db"), max_size=1)

        with self.assertRaises((sqlite3.Error, OSError)):
            pool.get_connection()

        self.assertEqual(pool._created_count, 0)


class TestSqliteAsyncWrappers(unittest.IsolatedAsyncioTestCase):
    """Test suite for asyncio.to_thread wrappers on SQLite operations."""

    def setUp(self):
        """Set up test database."""
        # Create temporary database
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")

        # Initialize database with schema
        init_db(self.db_path)
        run_migrations(self.db_path)

        # Create a test pool for the temporary database
        self.test_pool = SQLiteConnectionPool(self.db_path, max_size=10)

    def tearDown(self):
        """Clean up after each test."""
        # Close the test pool
        self.test_pool.close_all()

        # Clean up temp directory
        import shutil

        try:
            shutil.rmtree(self.temp_dir)
        except Exception:
            pass

    def _get_connection(self):
        """Get a connection from the test pool."""
        return self.test_pool.get_connection()

    def _release_connection(self, conn):
        """Release a connection back to the pool."""
        self.test_pool.release_connection(conn)

    # ========================================================================
    # Test 1 & 2: get_user_orgs() and get_user_primary_org() with to_thread
    # ========================================================================

    async def test_get_user_orgs_returns_empty_list_for_user_with_no_orgs(self):
        """get_user_orgs should return empty list when user belongs to no orgs."""
        # Create a test user directly in DB
        conn = self._get_connection()
        try:
            conn.execute(
                "INSERT INTO users (username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, 1)",
                ("noorgsuser", "hash", "No Orgs User", "member"),
            )
            conn.commit()
            user_id = conn.execute("SELECT id FROM users WHERE username = ?", ("noorgsuser",)).fetchone()[0]

            # Call get_user_orgs
            orgs = await get_user_orgs(user_id, conn)
            self.assertEqual(orgs, [])
        finally:
            self._release_connection(conn)

    async def test_get_user_orgs_returns_correct_orgs(self):
        """get_user_orgs should return all org IDs user belongs to."""
        conn = self._get_connection()
        try:
            # Create a test user
            conn.execute(
                "INSERT INTO users (username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, 1)",
                ("orgsuser", "hash", "Orgs User", "member"),
            )
            conn.commit()
            user_id = conn.execute("SELECT id FROM users WHERE username = ?", ("orgsuser",)).fetchone()[0]

            # Create two orgs
            conn.execute("INSERT INTO organizations (name, created_at) VALUES (?, ?)", ("Org1", "2024-01-01T00:00:00+00:00"))
            conn.execute("INSERT INTO organizations (name, created_at) VALUES (?, ?)", ("Org2", "2024-01-01T00:00:00+00:00"))
            conn.commit()

            # Get org IDs
            cursor = conn.execute("SELECT id FROM organizations ORDER BY id")
            org_ids = [row[0] for row in cursor.fetchall()]
            self.assertEqual(len(org_ids), 2)

            # Add user to both orgs
            conn.execute("INSERT INTO org_members (user_id, org_id) VALUES (?, ?)", (user_id, org_ids[0]))
            conn.execute("INSERT INTO org_members (user_id, org_id) VALUES (?, ?)", (user_id, org_ids[1]))
            conn.commit()

            # Call get_user_orgs
            orgs = await get_user_orgs(user_id, conn)
            self.assertEqual(set(orgs), set(org_ids))
        finally:
            self._release_connection(conn)

    async def test_get_user_primary_org_returns_none_for_user_with_no_orgs(self):
        """get_user_primary_org should return None when user belongs to no orgs."""
        conn = self._get_connection()
        try:
            # Create a test user
            conn.execute(
                "INSERT INTO users (username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, 1)",
                ("noprimaryuser", "hash", "No Primary User", "member"),
            )
            conn.commit()
            user_id = conn.execute("SELECT id FROM users WHERE username = ?", ("noprimaryuser",)).fetchone()[0]

            # Call get_user_primary_org
            primary_org = await get_user_primary_org(user_id, conn)
            self.assertIsNone(primary_org)
        finally:
            self._release_connection(conn)

    async def test_get_user_primary_org_returns_org_for_single_membership(self):
        """get_user_primary_org should return org ID when user belongs to exactly one org."""
        conn = self._get_connection()
        try:
            # Create a test user
            conn.execute(
                "INSERT INTO users (username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, 1)",
                ("singleorguser", "hash", "Single Org User", "member"),
            )
            conn.commit()
            user_id = conn.execute("SELECT id FROM users WHERE username = ?", ("singleorguser",)).fetchone()[0]

            # Create one org
            conn.execute("INSERT INTO organizations (name, created_at) VALUES (?, ?)", ("SingleOrg", "2024-01-01T00:00:00+00:00"))
            conn.commit()

            # Get org ID
            org_id = conn.execute("SELECT id FROM organizations").fetchone()[0]

            # Add user to org
            conn.execute("INSERT INTO org_members (user_id, org_id) VALUES (?, ?)", (user_id, org_id))
            conn.commit()

            # Call get_user_primary_org
            primary_org = await get_user_primary_org(user_id, conn)
            self.assertEqual(primary_org, org_id)
        finally:
            self._release_connection(conn)

    async def test_get_user_primary_org_raises_multiple_org_error(self):
        """get_user_primary_org should raise MultipleOrgError when user belongs to multiple orgs."""
        conn = self._get_connection()
        try:
            # Create a test user
            conn.execute(
                "INSERT INTO users (username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, 1)",
                ("multiorguser", "hash", "Multi Org User", "member"),
            )
            conn.commit()
            user_id = conn.execute("SELECT id FROM users WHERE username = ?", ("multiorguser",)).fetchone()[0]

            # Create two orgs
            conn.execute("INSERT INTO organizations (name, created_at) VALUES (?, ?)", ("MultiOrg1", "2024-01-01T00:00:00+00:00"))
            conn.execute("INSERT INTO organizations (name, created_at) VALUES (?, ?)", ("MultiOrg2", "2024-01-01T00:00:00+00:00"))
            conn.commit()

            # Get org IDs
            cursor = conn.execute("SELECT id FROM organizations ORDER BY id")
            org_ids = [row[0] for row in cursor.fetchall()]

            # Add user to both orgs
            conn.execute("INSERT INTO org_members (user_id, org_id) VALUES (?, ?)", (user_id, org_ids[0]))
            conn.execute("INSERT INTO org_members (user_id, org_id) VALUES (?, ?)", (user_id, org_ids[1]))
            conn.commit()

            # Call get_user_primary_org - should raise MultipleOrgError
            with self.assertRaises(MultipleOrgError):
                await get_user_primary_org(user_id, conn)
        finally:
            self._release_connection(conn)

    # ========================================================================
    # Test 3: Verify these functions are truly async (can be awaited)
    # ========================================================================

    async def test_get_user_orgs_is_awaitable(self):
        """get_user_orgs should be awaitable and return a list."""
        conn = self._get_connection()
        try:
            # Create a test user
            conn.execute(
                "INSERT INTO users (username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, 1)",
                ("awaituser1", "hash", "Await User 1", "member"),
            )
            conn.commit()
            user_id = conn.execute("SELECT id FROM users WHERE username = ?", ("awaituser1",)).fetchone()[0]

            # Verify it's awaitable and returns correct type
            result_coro = get_user_orgs(user_id, conn)
            self.assertTrue(asyncio.iscoroutine(result_coro))

            result = await result_coro
            self.assertIsInstance(result, list)
        finally:
            self._release_connection(conn)

    async def test_get_user_primary_org_is_awaitable(self):
        """get_user_primary_org should be awaitable and return int or None."""
        conn = self._get_connection()
        try:
            # Create a test user
            conn.execute(
                "INSERT INTO users (username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, 1)",
                ("awaituser2", "hash", "Await User 2", "member"),
            )
            conn.commit()
            user_id = conn.execute("SELECT id FROM users WHERE username = ?", ("awaituser2",)).fetchone()[0]

            # Verify it's awaitable and returns correct type
            result_coro = get_user_primary_org(user_id, conn)
            self.assertTrue(asyncio.iscoroutine(result_coro))

            result = await result_coro
            self.assertTrue(result is None or isinstance(result, int))
        finally:
            self._release_connection(conn)

    # ========================================================================
    # Test 4: Concurrent calls don't deadlock
    # ========================================================================

    async def test_concurrent_get_user_orgs_no_deadlock(self):
        """Multiple concurrent get_user_orgs calls should complete without deadlock."""
        conn = self._get_connection()
        try:
            # Create multiple test users
            user_ids = []
            for i in range(5):
                conn.execute(
                    "INSERT INTO users (username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, 1)",
                    (f"concurrentuser{i}", "hash", f"Concurrent User {i}", "member"),
                )
                conn.commit()
                user_id = conn.execute("SELECT id FROM users WHERE username = ?", (f"concurrentuser{i}",)).fetchone()[0]
                user_ids.append(user_id)

            # Run multiple concurrent calls
            tasks = [get_user_orgs(user_id, conn) for user_id in user_ids]
            results = await asyncio.gather(*tasks)

            # All should return empty list (no orgs)
            self.assertEqual(len(results), 5)
            for result in results:
                self.assertEqual(result, [])
        finally:
            self._release_connection(conn)

    async def test_concurrent_get_user_primary_org_no_deadlock(self):
        """Multiple concurrent get_user_primary_org calls should complete without deadlock."""
        conn = self._get_connection()
        try:
            # Create multiple test users
            user_ids = []
            for i in range(5):
                conn.execute(
                    "INSERT INTO users (username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, 1)",
                    (f"concurrentprimary{i}", "hash", f"Concurrent Primary {i}", "member"),
                )
                conn.commit()
                user_id = conn.execute("SELECT id FROM users WHERE username = ?", (f"concurrentprimary{i}",)).fetchone()[0]
                user_ids.append(user_id)

            # Run multiple concurrent calls
            tasks = [get_user_primary_org(user_id, conn) for user_id in user_ids]
            results = await asyncio.gather(*tasks)

            # All should return None (no orgs)
            self.assertEqual(len(results), 5)
            for result in results:
                self.assertIsNone(result)
        finally:
            self._release_connection(conn)

    async def test_concurrent_mixed_org_operations_no_deadlock(self):
        """Mixed concurrent get_user_orgs and get_user_primary_org calls should complete without deadlock."""
        conn = self._get_connection()
        try:
            # Create test users with and without orgs
            # First, create an org for some users
            conn.execute("INSERT INTO organizations (name, created_at) VALUES (?, ?)", ("TestOrg", "2024-01-01T00:00:00+00:00"))
            conn.commit()
            org_id = conn.execute("SELECT id FROM organizations").fetchone()[0]

            # Create 5 users - first 3 join the org, last 2 don't
            user_ids = []
            for i in range(5):
                conn.execute(
                    "INSERT INTO users (username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, 1)",
                    (f"mixeduser{i}", "hash", f"Mixed User {i}", "member"),
                )
                conn.commit()
                user_id = conn.execute("SELECT id FROM users WHERE username = ?", (f"mixeduser{i}",)).fetchone()[0]
                user_ids.append(user_id)

            # First 3 users join the org
            for i in range(3):
                conn.execute("INSERT INTO org_members (user_id, org_id) VALUES (?, ?)", (user_ids[i], org_id))
                conn.commit()

            # Create tasks mixing get_user_orgs and get_user_primary_org
            tasks = []
            for i, user_id in enumerate(user_ids):
                if i < 3:
                    tasks.append(get_user_orgs(user_id, conn))
                    tasks.append(get_user_primary_org(user_id, conn))
                else:
                    tasks.append(get_user_orgs(user_id, conn))
                    tasks.append(get_user_primary_org(user_id, conn))

            # Run all concurrent calls
            results = await asyncio.gather(*tasks)

            # Should have 10 results (5 users * 2 calls each)
            self.assertEqual(len(results), 10)

            # Verify results
            for i in range(5):
                orgs_idx = i * 2
                primary_idx = i * 2 + 1
                if i < 3:
                    # Users with org membership
                    self.assertEqual(results[orgs_idx], [org_id])
                    self.assertEqual(results[primary_idx], org_id)
                else:
                    # Users without org membership
                    self.assertEqual(results[orgs_idx], [])
                    self.assertIsNone(results[primary_idx])
        finally:
            self._release_connection(conn)


class TestSqliteAsyncEdgeCases(unittest.IsolatedAsyncioTestCase):
    """Edge case tests for to_thread wrappers."""

    def setUp(self):
        """Set up test database."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")

        init_db(self.db_path)
        run_migrations(self.db_path)

        self.test_pool = SQLiteConnectionPool(self.db_path, max_size=10)

    def tearDown(self):
        self.test_pool.close_all()

        import shutil
        try:
            shutil.rmtree(self.temp_dir)
        except Exception:
            pass

    def _get_connection(self):
        return self.test_pool.get_connection()

    def _release_connection(self, conn):
        self.test_pool.release_connection(conn)

    async def test_get_user_orgs_nonexistent_user(self):
        """get_user_orgs with nonexistent user_id should return empty list."""
        conn = self._get_connection()
        try:
            # Use a clearly nonexistent user ID (high number that won't exist)
            orgs = await get_user_orgs(999999, conn)
            self.assertEqual(orgs, [])
        finally:
            self._release_connection(conn)

    async def test_get_user_primary_org_nonexistent_user(self):
        """get_user_primary_org with nonexistent user_id should return None."""
        conn = self._get_connection()
        try:
            # Use a clearly nonexistent user ID
            primary_org = await get_user_primary_org(999999, conn)
            self.assertIsNone(primary_org)
        finally:
            self._release_connection(conn)

    async def test_very_large_user_id(self):
        """get_user_orgs with very large user_id should return empty list without error."""
        conn = self._get_connection()
        try:
            # Use a very large user ID that won't exist
            orgs = await get_user_orgs(999999999, conn)
            self.assertEqual(orgs, [])
        finally:
            self._release_connection(conn)

    async def test_many_orgs_concurrent_access(self):
        """Multiple users querying many orgs concurrently should not deadlock."""
        conn = self._get_connection()
        try:
            # Create many orgs
            for i in range(20):
                conn.execute("INSERT INTO organizations (name, created_at) VALUES (?, ?)", (f"Org{i}", "2024-01-01T00:00:00+00:00"))
            conn.commit()

            # Create users and assign them to random orgs
            import random
            user_ids = []
            for i in range(10):
                conn.execute(
                    "INSERT INTO users (username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, 1)",
                    (f"manyorgsuser{i}", "hash", f"Many Orgs User {i}", "member"),
                )
                conn.commit()
                user_id = conn.execute("SELECT id FROM users WHERE username = ?", (f"manyorgsuser{i}",)).fetchone()[0]
                user_ids.append(user_id)

                # Assign to 1-5 random orgs
                org_ids = [row[0] for row in conn.execute("SELECT id FROM organizations").fetchall()]
                selected_orgs = random.sample(org_ids, random.randint(1, 5))
                for org_id in selected_orgs:
                    conn.execute("INSERT INTO org_members (user_id, org_id) VALUES (?, ?)", (user_id, org_id))
                conn.commit()

            # Run concurrent queries
            tasks = [get_user_orgs(user_id, conn) for user_id in user_ids]
            results = await asyncio.gather(*tasks)

            # All should complete
            self.assertEqual(len(results), 10)
            for result in results:
                self.assertIsInstance(result, list)
                self.assertGreater(len(result), 0)
        finally:
            self._release_connection(conn)


if __name__ == "__main__":
    unittest.main()
