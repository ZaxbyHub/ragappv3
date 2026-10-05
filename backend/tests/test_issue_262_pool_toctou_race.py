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

2. Under concurrent SUCCESSFUL creation, ``_created_count`` never exceeds
   ``max_size`` and every slot maps to a live distinct connection. This
   originally pinned the mechanism (creation I/O inside the shared pool
   lock); issue #700 moved creation behind a dedicated creation mutex, so
   the test now pins the count invariant instead.

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

    def test_concurrent_create_never_inflates_count_beyond_max(self):
        """Concurrent creations must never push _created_count past max_size.

        Issue #262's TOCTOU race was a transiently inflated ``_created_count``
        observable by concurrent callers. This test originally pinned the
        MECHANISM (asserting ``_create_connection()`` runs while the shared
        pool lock is held). Issue #700 deliberately moves the creation I/O off
        the shared lock — behind a dedicated creation mutex — so the shared
        lock is never held across connection-creation syscalls (event-loop
        reachability of ``recent_capacity_wait``). Per issue #700's mandate,
        the mechanism check is replaced here by the #262 COUNT INVARIANT:
        under concurrent creation no caller can acquire more than
        ``max_size`` slots and after all creators finish the count equals
        the number of live, distinct connections (no leak). The headline
        bound is enforced structurally by the capacity-checked increment
        (this test cannot discriminate it — the discriminating power for
        #262 lives in the failure-path twin below and issue #700's
        lock-scope checks). The
        failure-path twin lives in
        ``test_concurrent_failed_create_never_inflates_count_beyond_max``.
        """
        from app.models.database import SQLiteConnectionPool

        pool = SQLiteConnectionPool(str(self.db_path), max_size=3)

        sampled_counts = []
        real_create = pool._create_connection

        def sampling_create():
            # Runs after the caller incremented _created_count for THIS
            # slot; record what a concurrent observer could see.
            sampled_counts.append(pool._created_count)
            return real_create()

        start = threading.Barrier(3)
        results: list = []
        results_lock = threading.Lock()

        def creator():
            start.wait(timeout=10)
            conn = pool.get_connection(max_wait_attempts=5)
            with results_lock:
                results.append(conn)

        with mock.patch.object(
            pool, "_create_connection", side_effect=sampling_create
        ):
            threads = [threading.Thread(target=creator) for _ in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

        self.assertEqual(len(results), 3, "a creator failed or hung")
        self.assertTrue(
            all(c <= pool.max_size for c in sampled_counts),
            f"transient inflation observed: {sorted(sampled_counts)}",
        )
        self.assertEqual(
            len({id(conn) for conn in results}),
            3,
            "creators must not share one connection",
        )
        self.assertEqual(pool._created_count, 3)
        for conn in results:
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
                # Since issue #700 the creation I/O runs under the dedicated
                # _create_lock (NOT the shared pool._lock); this observer
                # reads under peak_lock only, so it never contends with
                # either pool lock.
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

        # Successful creates plus the in-flight ones must never push the
        # observed count past max_size from any observer's perspective.
        # The capacity-checked increment bounds it by construction; the
        # peak probe exists to catch any future unguarded increment path.
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
