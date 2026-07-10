"""Adversarial tests for MemoryStore pool-size configuration.

Attack surface:
  - memory_store_pool_size must be >= 5 (validated by Pydantic field_validator)
  - Pool max_size=0 or max_size=-1 creates a deadlocked pool (confirmed: get_connection
    raises RuntimeError after 15s because _created_count < max_size is always False
    when max_size <= 0, and the blocking get() waits on an always-empty unbounded queue)
  - Type confusion: non-integer env values for memory_store_pool_size
  - Concurrent access: pool exhaustion correctly raises RuntimeError after 3 attempts (15s)
  - Improper cleanup: close_all marks _closed=True preventing new connections
  - Resource leak: if release_connection is never called, pool eventually blocks/raises

Key findings from testing:
  - max_size=0 and max_size=-1 DEADLOCK the pool (RuntimeError after 15s) — confirmed vulnerability
  - memory_store_pool_size has range validator requiring >= 5 (matches ingestion_queue_max_size behavior)
  - Non-integer strings are rejected by Pydantic (type-level validation)
  - Default memory_store_pool_size = 10 (per the change requirement)
"""

import os
import shutil
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from unittest import mock

import pydantic


class TestMemoryStorePoolSizeConfigInjection(unittest.TestCase):
    """Adversarial tests: malformed/out-of-range memory_store_pool_size values reach the pool."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test.db"

    def tearDown(self):
        # Use ignore_errors=True because on Windows, leaked connections
        # (deliberate in adversarial tests) keep the DB file locked
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_config_memory_store_pool_size_zero_rejected_by_validator(self):
        """memory_store_pool_size=0 is rejected by the Pydantic range validator."""
        from app.config import Settings

        with mock.patch.dict(os.environ, {"MEMORY_STORE_POOL_SIZE": "0"}):
            with self.assertRaises(pydantic.ValidationError):
                Settings()

    def test_config_memory_store_pool_size_negative_rejected_by_validator(self):
        """memory_store_pool_size=-1 is rejected by the Pydantic range validator."""
        from app.config import Settings

        with mock.patch.dict(os.environ, {"MEMORY_STORE_POOL_SIZE": "-1"}):
            with self.assertRaises(pydantic.ValidationError):
                Settings()

    def test_config_memory_store_pool_size_non_integer_string_raises(self):
        """memory_store_pool_size='abc' must raise a ValueError at Settings construction."""
        from app.config import Settings

        with mock.patch.dict(os.environ, {"MEMORY_STORE_POOL_SIZE": "not_a_number"}):
            with self.assertRaises(ValueError):
                Settings()

    def test_config_memory_store_pool_size_float_string_raises(self):
        """memory_store_pool_size='10.5' must raise because the field is int-typed."""
        from app.config import Settings

        with mock.patch.dict(os.environ, {"MEMORY_STORE_POOL_SIZE": "10.5"}):
            with self.assertRaises(ValueError):
                Settings()

    def test_config_memory_store_pool_size_very_large_accepted(self):
        """memory_store_pool_size=2147483647 is accepted without range validation.

        The pool is created; resource exhaustion depends on runtime allocation.
        """
        from app.config import Settings

        with mock.patch.dict(os.environ, {"MEMORY_STORE_POOL_SIZE": str(sys.maxsize)}):
            s = Settings()
            self.assertEqual(s.memory_store_pool_size, sys.maxsize)

    def test_config_default_memory_store_pool_size_is_10(self):
        """Default memory_store_pool_size is 10 (per the change requirement)."""
        from app.config import Settings

        s = Settings()
        self.assertEqual(s.memory_store_pool_size, 10)


class TestSQLiteConnectionPoolBoundaryConditions(unittest.TestCase):
    """Adversarial tests: SQLiteConnectionPool behavior at boundary max_size values."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test.db"
        self._open_conns = []  # Track connections for cleanup

    def _track_conn(self, conn):
        """Track a connection so we can close it in tearDown."""
        self._open_conns.append(conn)
        return conn

    def tearDown(self):
        # Close any tracked connections first (Windows requires this before file deletion)
        for c in self._open_conns:
            try:
                c.close()
            except Exception:
                pass
        self._open_conns.clear()

        # Try to remove the DB file; on Windows with held handles this may fail.
        # That's expected for adversarial "leak" tests — the OS will reclaim on exit.
        try:
            if self.db_path.exists():
                os.remove(self.db_path)
        except (PermissionError, OSError):
            pass  # Windows: file handle still held by un-released connection

        try:
            os.rmdir(self.temp_dir)
        except OSError:
            pass  # Directory not empty when files couldn't be deleted

    def test_pool_max_size_zero_deadlocks_on_get_connection(self):
        """Pool with max_size=0 deadlocks: _created_count < max_size is always False,
        and the blocking get() times out waiting on an empty unbounded queue.

        Confirmed: raises RuntimeError after 3 attempts. The internal per-attempt
        Queue.get timeout (5s) is patched to raise Empty immediately so this
        proves the deadlock behavior in <1s instead of 15s (C3-2).
        """
        import queue as _queue
        from unittest.mock import patch

        from app.models.database import SQLiteConnectionPool

        pool = SQLiteConnectionPool(str(self.db_path), max_size=0)
        self.assertEqual(pool.max_size, 0)
        self.assertEqual(pool._pool.maxsize, 0)  # Queue is unbounded

        # Patch the blocking get to raise Empty immediately so the loop
        # exhausts max_wait_attempts without burning real wall-clock time.
        real_get = _queue.Queue.get

        def _instant_empty(self, *a, **kw):
            raise _queue.Empty

        with patch.object(_queue.Queue, "get", _instant_empty):
            with self.assertRaises(RuntimeError) as ctx:
                pool.get_connection()
        self.assertIn("Could not obtain", str(ctx.exception))
        # Sanity: the real get signature is unchanged (patch only affected this test).
        _ = real_get

        pool.close_all()

    def test_pool_max_size_negative_deadlocks_on_get_connection(self):
        """Pool with max_size=-1 deadlocks for the same reason.

        Queue.get timeout patched to raise Empty immediately (C3-2).
        """
        import queue as _queue
        from unittest.mock import patch

        from app.models.database import SQLiteConnectionPool

        pool = SQLiteConnectionPool(str(self.db_path), max_size=-1)
        self.assertEqual(pool.max_size, -1)

        def _instant_empty(self, *a, **kw):
            raise _queue.Empty

        with patch.object(_queue.Queue, "get", _instant_empty):
            with self.assertRaises(RuntimeError) as ctx:
                pool.get_connection()
        self.assertIn("Could not obtain", str(ctx.exception))

        pool.close_all()

    def test_pool_max_size_one_correctly_limits_connections(self):
        """Pool with max_size=1 correctly enforces the limit: second get blocks."""
        from app.models.database import SQLiteConnectionPool

        pool = SQLiteConnectionPool(str(self.db_path), max_size=1)

        conn1 = pool.get_connection()
        self.assertEqual(pool._created_count, 1)

        # Second get must block (not create an extra connection)
        future = ThreadPoolExecutor(max_workers=1).submit(pool.get_connection)
        with self.assertRaises(TimeoutError):
            future.result(timeout=1.5)

        pool.release_connection(conn1)
        pool.close_all()

    def test_pool_close_all_prevents_new_connections(self):
        """After close_all(), get_connection raises RuntimeError."""
        from app.models.database import SQLiteConnectionPool

        pool = SQLiteConnectionPool(str(self.db_path), max_size=2)
        conn = pool.get_connection()
        pool.close_all()

        with self.assertRaises(RuntimeError) as ctx:
            pool.get_connection()
        self.assertIn("closed", str(ctx.exception).lower())

        # The obtained conn is still valid
        conn.execute("SELECT 1")

    def test_pool_release_after_close_raises_runtime_error(self):
        """Releasing a connection to a closed pool must raise RuntimeError."""
        from app.models.database import SQLiteConnectionPool

        pool = SQLiteConnectionPool(str(self.db_path), max_size=1)
        conn = pool.get_connection()
        pool.close_all()

        with self.assertRaises(RuntimeError) as ctx:
            pool.release_connection(conn)
        self.assertIn("closed", str(ctx.exception).lower())

    def test_connection_leak_without_release_blocks_future_gets(self):
        """If release_connection is never called, the pool eventually blocks
        all subsequent callers (not an infinite loop — it times out after 15s)."""
        from app.models.database import SQLiteConnectionPool

        pool = SQLiteConnectionPool(str(self.db_path), max_size=1)
        conn = pool.get_connection()

        # Without releasing, a second caller should block/timeout
        future = ThreadPoolExecutor(max_workers=1).submit(pool.get_connection)
        with self.assertRaises(TimeoutError):
            future.result(timeout=3.0)

        pool.close_all()  # Clean up without releasing


class TestMemoryStorePoolIntegration(unittest.TestCase):
    """End-to-end adversarial tests: MemoryStore with various pool configurations."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test.db"
        from app.models.database import init_db
        init_db(str(self.db_path))

    def tearDown(self):
        try:
            if hasattr(self, "store"):
                self.store.close_all()
        except Exception:
            pass
        try:
            if self.db_path.exists():
                os.remove(self.db_path)
        except PermissionError:
            pass
        finally:
            os.rmdir(self.temp_dir)

    def test_memory_store_with_zero_pool_size_deadlocks(self):
        """MemoryStore created with pool max_size=0 (via config injection) deadlocks.

        Queue.get timeout patched to raise Empty immediately (C3-2).
        """
        import queue as _queue
        from unittest.mock import patch

        from app.models.database import SQLiteConnectionPool
        from app.services.memory_store import MemoryStore

        pool = SQLiteConnectionPool(str(self.db_path), max_size=0)
        store = MemoryStore(pool=pool)

        def _instant_empty(self, *a, **kw):
            raise _queue.Empty

        with patch.object(_queue.Queue, "get", _instant_empty):
            with self.assertRaises(RuntimeError) as ctx:
                store.add_memory("test memory")
        self.assertIn("Could not obtain", str(ctx.exception))

        pool.close_all()

    def test_memory_store_with_default_pool_size_10_works_normally(self):
        """MemoryStore with the new default max_size=10 works for concurrent retrieval."""
        from app.models.database import SQLiteConnectionPool
        from app.services.memory_store import MemoryStore

        pool = SQLiteConnectionPool(str(self.db_path), max_size=10)
        store = MemoryStore(pool=pool)

        for i in range(5):
            store.add_memory(f"memory {i}")

        results = store.search_memories("memory")
        self.assertGreater(len(results), 0)

        pool.close_all()

    def test_concurrent_search_with_small_pool_is_safe(self):
        """Concurrent search_memories with max_size=1 pool must be safe (block, not crash)."""
        from app.models.database import SQLiteConnectionPool
        from app.services.memory_store import MemoryStore

        pool = SQLiteConnectionPool(str(self.db_path), max_size=1)
        store = MemoryStore(pool=pool)

        for i in range(3):
            store.add_memory(f"concurrent memory {i}")

        errors = []

        def search():
            try:
                store.search_memories("memory")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=search) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        pool.close_all()

    def test_memory_store_close_all_closes_pool(self):
        """MemoryStore.close_all must close the underlying pool."""
        from app.models.database import SQLiteConnectionPool
        from app.services.memory_store import MemoryStore

        pool = SQLiteConnectionPool(str(self.db_path), max_size=2)
        store = MemoryStore(pool=pool)

        self.assertFalse(store.pool._closed)
        store.close_all()
        self.assertTrue(store.pool._closed)

        pool.close_all()  # Idempotent

    def test_search_when_pool_exhausted_blocks_or_times_out(self):
        """search_memories called when pool is exhausted must block/time out
        rather than creating a connection beyond max_size."""
        from app.models.database import SQLiteConnectionPool
        from app.services.memory_store import MemoryStore

        pool = SQLiteConnectionPool(str(self.db_path), max_size=1)
        store = MemoryStore(pool=pool)
        store.add_memory("test memory")

        # Exhaust pool
        conn = store.pool.get_connection()

        future = ThreadPoolExecutor(max_workers=1).submit(
            store.search_memories, "test"
        )
        with self.assertRaises(TimeoutError):
            future.result(timeout=3.0)

        store.pool.release_connection(conn)
        store.pool.close_all()
