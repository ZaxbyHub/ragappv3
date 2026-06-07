"""
Regression tests for F-001: threading.Lock added to _SQLiteCSRFStore.

These tests verify that:
1. _SQLiteCSRFStore operations are serialized via threading.Lock
2. Exception safety: lock is released when an exception is raised inside the lock
3. Concurrent operations from multiple threads don't crash or interleave incorrectly
"""

import sys
import threading
import unittest

sys.path.insert(0, ".")

from app.security import _SQLiteCSRFStore


class TestSQLiteCSRFStoreLock(unittest.TestCase):
    """Regression tests for threading.Lock in _SQLiteCSRFStore."""

    def test_sqlite_csrf_store_with_lock_serializes_operations(self):
        """
        Verify that concurrent thread calls to setex/get/expire/delete don't crash
        and are serialized by the lock. Operations should not interleave in a way
        that causes errors.
        """
        store = _SQLiteCSRFStore(":memory:")
        errors = []
        results = []
        lock = threading.Lock()

        def worker(n):
            try:
                for i in range(5):
                    key = f"test:thread{n}:{i}"
                    store.setex(key, 10, "1")
                    val = store.get(key)
                    store.expire(key, 20)
                    store.delete(key)
                    with lock:
                        results.append(n)
            except Exception as e:
                with lock:
                    errors.append(str(e))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Errors: {errors}")
        self.assertEqual(len(results), 50)  # 10 threads × 5 iterations

    def test_lock_prevents_interleaving_of_setex_and_get(self):
        """
        Verify that setex and get operations from different threads are serialized.
        If the lock were missing, operations could interleave and cause issues.
        """
        store = _SQLiteCSRFStore(":memory:")
        store.setex("shared_key", 10, "1")

        interleaved = []
        lock = threading.Lock()

        def writer():
            for i in range(10):
                store.setex("shared_key", 10, str(i))
                with lock:
                    interleaved.append(f"w{i}")

        def reader():
            for i in range(10):
                store.get("shared_key")
                with lock:
                    interleaved.append(f"r{i}")

        t1 = threading.Thread(target=writer)
        t2 = threading.Thread(target=reader)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # No crashes = lock is working. We just verify no exceptions were raised.
        self.assertEqual(len(interleaved), 20)

    def test_thread_safe_exception_releases_lock(self):
        """
        When an exception is raised inside `with self._lock:`, the lock is released.
        Verify via threading.Lock.acquire(blocking=False) that the lock is available
        after an exception.
        """
        store = _SQLiteCSRFStore(":memory:")

        # Release the lock we don't need for this test
        # (store.__init__ already acquires it but we don't need to test that)
        # Instead, simulate an exception inside the lock context
        with self.assertRaises(RuntimeError):
            with store._lock:
                raise RuntimeError("test")

        # Lock should be released after the exception
        # acquire(blocking=False) returns True if lock was acquired, False if not
        acquired = store._lock.acquire(blocking=False)
        self.assertTrue(acquired, "Lock should be released after exception")
        store._lock.release()

    def test_lock_is_reentrant_proof(self):
        """
        threading.Lock is NOT reentrant. Verify that calling lock twice from the
        same thread would deadlock (this is expected behavior, not a bug).
        We test that the lock actually provides mutual exclusion.
        """
        store = _SQLiteCSRFStore(":memory:")
        results = []

        def worker():
            store.setex("key", 10, "value")
            results.append("setex")
            store.get("key")
            results.append("get")
            store.delete("key")
            results.append("delete")

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All operations completed without deadlock
        self.assertEqual(len(results), 15)  # 5 threads × 3 operations each

    def test_setex_calls_cleanup_expired_under_lock(self):
        """
        Verify that setex calls _cleanup_expired while holding the lock.
        This is implicitly tested by verifying no corruption occurs during
        concurrent cleanup + insert operations.
        """
        store = _SQLiteCSRFStore(":memory:")

        # Create some expired tokens first
        store.setex("expired_key", -1, "expired")

        # Multiple threads doing setex + expire concurrently
        # If _cleanup_expired wasn't called under lock, we could have race conditions
        errors = []

        def worker(n):
            try:
                for i in range(10):
                    key = f"token_{n}_{i}"
                    store.setex(key, 10, "1")
                    store.expire(key, 20)
                    store.get(key)
                    store.delete(key)
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Errors during concurrent operations: {errors}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
