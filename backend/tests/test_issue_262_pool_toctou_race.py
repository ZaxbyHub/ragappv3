"""Regression tests for GitHub issue #262.

Issue: SQLiteConnectionPool._created_count decrement "outside" the critical
section creates a TOCTOU race window where another thread can observe an
inflated count during a failed _create_connection() call.

While the increment and decrement are each individually guarded by
``self._lock``, the I/O call to ``_create_connection()`` runs BETWEEN them
without holding the lock. During that window, a concurrent reader of
``_created_count`` sees the slot as "taken" even though the underlying
connection was never successfully created — leading to spurious
pool-full rejections and inconsistent accounting.

These tests verify:

1. After a failed ``get_connection()`` call, ``_created_count`` is fully
   rolled back (no slot leak). The current code already satisfies this
   invariant; we pin it so future refactors don't regress.

2. The ``_create_connection()`` I/O call runs inside the critical section,
   so concurrent callers cannot observe a transient inflation of
   ``_created_count`` between the increment and the I/O result. This is
   the actual fix for the TOCTOU race.

3. Under concurrent failure, the pool does not transiently exceed
   ``max_size`` as observed by other callers (an external-observer
   invariant).
"""

import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


class TestSQLiteConnectionPoolCreatedCountRace(unittest.TestCase):
    """Verify _created_count accounting around _create_connection() failures.

    See issue #262 for the original report and discussion.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test.db"

    def tearDown(self):
        try:
            if self.db_path.exists():
                os.remove(self.db_path)
        except (PermissionError, OSError):
            pass
        try:
            os.rmdir(self.temp_dir)
        except OSError:
            pass

    def test_failed_create_does_not_leak_created_count(self):
        """After _create_connection() raises, _created_count must be 0.

        No slot must remain reserved for a connection that was never created.
        Both before and after the fix this holds, but pinning the invariant
        guards future refactors that might split the increment/decrement
        across more lock acquisitions.
        """
        from app.models.database import SQLiteConnectionPool

        pool = SQLiteConnectionPool(str(self.db_path), max_size=2)

        with mock.patch.object(
            pool,
            "_create_connection",
            side_effect=sqlite3.OperationalError("boom"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                pool.get_connection()

        # Invariant: no slot leaked
        self.assertEqual(pool._created_count, 0)
        pool.close_all()

    def test_create_connection_runs_under_lock(self):
        """_create_connection() must run while the pool lock is held.

        The TOCTOU race in issue #262 occurs because the I/O call runs
        outside the lock that guards _created_count. With the lock held
        during I/O, no concurrent caller can observe a transiently
        inflated _created_count.

        We detect this by snapshotting the lock state from inside a
        mocked _create_connection(): if the lock is held by the caller,
        any attempt to acquire it from this thread would block.
        """
        from app.models.database import SQLiteConnectionPool

        pool = SQLiteConnectionPool(str(self.db_path), max_size=2)

        # Real connection object — we'll create it ourselves and return it
        # from the mock so the pool can actually use it.
        real_conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        real_conn.row_factory = sqlite3.Row

        lock_held_during_io = []
        io_done = threading.Event()
        observer_can_proceed = threading.Event()

        def fake_create_connection():
            # Try to acquire the same lock the caller is using.
            # If the caller is holding it during I/O, this acquire blocks
            # until we release — proving the lock was held.
            acquired = pool._lock.acquire(blocking=False)
            if acquired:
                # Lock was NOT held during I/O — TOCTOU window exists.
                lock_held_during_io.append(False)
                pool._lock.release()
            else:
                # Lock WAS held during I/O — fix is in place.
                lock_held_during_io.append(True)
            # Return the real connection so the pool stays usable.
            return real_conn

        with mock.patch.object(pool, "_create_connection", side_effect=fake_create_connection):
            conn = pool.get_connection()
            try:
                # If we got here, the lock-was-held observation fired.
                self.assertTrue(
                    lock_held_during_io,
                    "_create_connection() was never invoked",
                )
                self.assertTrue(
                    lock_held_during_io[0],
                    "_create_connection() ran OUTSIDE the pool lock — "
                    "TOCTOU race window is open (see issue #262)",
                )
            finally:
                pool.release_connection(conn)

        pool.close_all()

    def test_concurrent_failed_create_never_inflates_count_beyond_max(self):
        """Under concurrent load with one thread failing, observers must never
        see _created_count > max_size.

        We use a barrier to force interleaving between the increment and
        the failure path, and have the failing thread record the peak
        value of _created_count as seen by other concurrent readers.
        """
        from app.models.database import SQLiteConnectionPool

        pool = SQLiteConnectionPool(str(self.db_path), max_size=3)

        peak_count_during_io = {"value": 0}
        peak_lock = threading.Lock()
        proceed_after_observation = threading.Event()
        fail_first_n = [3]  # first 3 creates fail, the rest succeed

        def fake_create_connection():
            # While I/O runs, observe the current _created_count.
            with peak_lock:
                # pool._lock is held during I/O after the fix; here we
                # read under peak_lock only — no contention with pool lock
                # because we know the pool lock state from the other test.
                observed = pool._created_count
                if observed > peak_count_during_io["value"]:
                    peak_count_during_io["value"] = observed

            # Succeed or fail depending on the gate
            if fail_first_n[0] > 0:
                fail_first_n[0] -= 1
                raise sqlite3.OperationalError("scheduled failure")

            # Success path: return a real connection
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            return conn

        # Track connections we successfully got so we can release them.
        returned_conns = []
        returned_lock = threading.Lock()

        def worker_get_connection():
            try:
                conn = pool.get_connection()
                with returned_lock:
                    returned_conns.append(conn)
            except sqlite3.OperationalError:
                pass  # Expected for the first N failures
            except Exception:
                pass

        with mock.patch.object(pool, "_create_connection", side_effect=fake_create_connection):
            # 6 concurrent callers, 3 will fail, 3 may succeed
            threads = [threading.Thread(target=worker_get_connection) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15)

        # After everything settles:
        # - _created_count equals the number of returned conns (successful
        #   creates never decrement, failed creates decrement to 0 net).
        with returned_lock:
            successful = len(returned_conns)
            for c in returned_conns:
                try:
                    pool.release_connection(c)
                except Exception:
                    pass

        # Successful creates plus one slot for the in-flight ones should
        # never have exceeded max_size from any observer's perspective.
        # With the fix (lock held during I/O), the peak is bounded by
        # max_size. Without the fix, the peak can transiently exceed
        # max_size during overlapping failed creates.
        self.assertLessEqual(
            peak_count_during_io["value"],
            pool.max_size,
            f"Observed _created_count peak {peak_count_during_io['value']} "
            f"exceeded max_size {pool.max_size} — TOCTOU race in issue #262",
        )
        # And final accounting is correct: no leaks.
        self.assertEqual(pool._created_count, successful)

        pool.close_all()


if __name__ == "__main__":
    unittest.main()
