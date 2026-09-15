"""Issue #559 acceptance check C1 (AC2, DISCRIMINATING): lease claim contention.

Two workers race the single-statement DB claim in ``app.services.job_lease``
against ONE shared temp-file SQLite database. Exactly one claimant may receive
the row; the loser must receive ``None``. With five pending rows and two
concurrent draining workers, no row may ever be claimed twice.

Mirrors the two-thread ``threading.Barrier`` pattern of
``tests/draft_room/test_draft_job_processor.py::TestClaimAtomicity``.

Expectation: RED at the pre-change base (ModuleNotFoundError — the
``app.services.job_lease`` module does not exist yet); GREEN post-fix.
"""

import sqlite3
import threading
import unittest
from pathlib import Path
from tempfile import mkdtemp

RECLAIM_TIMEOUT_SECONDS = 30


def _connect(db_path):
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class TestLeaseClaimContention(unittest.TestCase):
    def setUp(self):
        from app.services.job_lease import ensure_jobs_schema

        self.db_path = str(Path(mkdtemp()) / "lease-contention.db")
        self.conn = _connect(self.db_path)
        ensure_jobs_schema(self.conn)
        self.addCleanup(self.conn.close)

    def insert_pending(self, queue, count=1):
        ids = []
        for _ in range(count):
            cur = self.conn.execute(
                "INSERT INTO jobs (queue, payload_json) VALUES (?, '{}')", (queue,)
            )
            ids.append(cur.lastrowid)
        self.conn.commit()
        return ids

    def test_two_workers_race_one_pending_row_exactly_one_wins(self):
        job_id = self.insert_pending("q1", count=1)[0]

        results = [None, None]
        barrier = threading.Barrier(2)

        def claim(slot):
            from app.services.job_lease import JobLease

            conn = _connect(self.db_path)
            try:
                lease = JobLease(conn, reclaim_timeout_seconds=RECLAIM_TIMEOUT_SECONDS)
                barrier.wait(timeout=5)
                results[slot] = lease.claim("q1", f"worker-{slot}")
            finally:
                conn.close()

        threads = [
            threading.Thread(target=claim, args=(slot,)) for slot in (0, 1)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))

        claimed = [row for row in results if row is not None]
        rejected = [row for row in results if row is None]
        self.assertEqual(
            len(claimed), 1, f"exactly one claimant wins the single row: {results!r}"
        )
        self.assertEqual(len(rejected), 1)

        winner = claimed[0]
        self.assertEqual(winner["id"], job_id)
        self.assertEqual(winner["status"], "running")
        self.assertIn(winner["worker_id"], ("worker-0", "worker-1"))

        stored = self.conn.execute(
            "SELECT id, status, worker_id FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        self.assertEqual(stored["status"], "running")
        self.assertEqual(stored["worker_id"], winner["worker_id"])

    def test_two_draining_workers_never_claim_the_same_row_twice(self):
        expected = set(self.insert_pending("q1", count=5))

        claimed = [[], []]
        barrier = threading.Barrier(2)

        def drain(slot):
            from app.services.job_lease import JobLease

            conn = _connect(self.db_path)
            try:
                lease = JobLease(conn, reclaim_timeout_seconds=RECLAIM_TIMEOUT_SECONDS)
                barrier.wait(timeout=5)
                # Bounded attempts: the queue holds 5 rows, so 10 attempts per
                # thread is far more than needed to observe a drained queue.
                for _ in range(10):
                    row = lease.claim("q1", f"drainer-{slot}")
                    if row is None:
                        break
                    claimed[slot].append(row["id"])
            finally:
                conn.close()

        threads = [
            threading.Thread(target=drain, args=(slot,)) for slot in (0, 1)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertFalse(any(thread.is_alive() for thread in threads))

        all_claimed = claimed[0] + claimed[1]
        self.assertEqual(
            len(all_claimed),
            len(set(all_claimed)),
            f"a row was claimed twice: {claimed!r}",
        )
        self.assertEqual(set(all_claimed), expected)

        still_pending = self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE queue = 'q1' AND status = 'pending'"
        ).fetchone()[0]
        self.assertEqual(still_pending, 0)
