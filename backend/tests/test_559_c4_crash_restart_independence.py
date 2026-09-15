"""Issue #559 acceptance check C4 (AC4, DISCRIMINATING at the lease level):
crash-restart independence.

A worker claims a job and dies (no terminal write, no further heartbeats —
the connection is closed, simulating a hard kill). The janitor reclaims, a
second worker claims and dies too, the janitor reclaims again, and a third
worker drives the job to ``completed``. Exactly one completion exists, the
lease generation strictly increased across every reclaim cycle, and a
duplicate terminal write by the finishing worker is idempotently fenced off.

A second scenario reclaims MAX times before completing: the job reaches a
terminal status regardless of how many reclaim (restart) cycles occurred.

Expectation: RED at the pre-change base (ModuleNotFoundError — the
``app.services.job_lease`` module does not exist yet); GREEN post-fix.
"""

import sqlite3
import unittest
from pathlib import Path
from tempfile import mkdtemp

RECLAIM_TIMEOUT_SECONDS = 30
STALE_SECONDS = 120  # well beyond the reclaim timeout
MAX_RECLAIM_CYCLES = 5


def _connect(db_path):
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class TestCrashRestartIndependence(unittest.TestCase):
    def setUp(self):
        from app.services.job_lease import JobLease, ensure_jobs_schema

        self.db_path = str(Path(mkdtemp()) / "lease-crash.db")
        self.conn = _connect(self.db_path)
        ensure_jobs_schema(self.conn)
        self.janitor = JobLease(self.conn, reclaim_timeout_seconds=RECLAIM_TIMEOUT_SECONDS)
        self.addCleanup(self.conn.close)

    def insert_pending(self, queue="q1"):
        cur = self.conn.execute(
            "INSERT INTO jobs (queue, payload_json) VALUES (?, '{}')", (queue,)
        )
        self.conn.commit()
        return cur.lastrowid

    def fetch(self, job_id):
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        self.assertIsNotNone(row)
        return row

    def age_heartbeat(self, job_id, seconds=STALE_SECONDS):
        self.conn.execute(
            "UPDATE jobs SET heartbeat_at = datetime('now', ?) WHERE id = ?",
            (f"-{seconds} seconds", job_id),
        )
        self.conn.commit()

    def crash_claim(self, worker_id):
        """A fresh process claims, then dies: connection closed, no write."""
        from app.services.job_lease import JobLease

        conn = _connect(self.db_path)
        lease = JobLease(conn, reclaim_timeout_seconds=RECLAIM_TIMEOUT_SECONDS)
        row = lease.claim("q1", worker_id)
        conn.close()  # hard kill — no terminal write, no further heartbeats
        return row

    def test_two_crashes_then_success_reaches_exactly_one_completion(self):
        from app.services.job_lease import JobLease

        job_id = self.insert_pending()

        # Cycle 0: worker A claims and crashes.
        row_a = self.crash_claim("worker-A")
        self.assertIsNotNone(row_a)
        generation_a = row_a["lease_generation"]
        self.age_heartbeat(job_id)
        self.assertEqual(self.janitor.reclaim_expired(), 1)

        # Cycle 1: worker B claims and crashes too.
        row_b = self.crash_claim("worker-B")
        self.assertIsNotNone(row_b)
        generation_b = row_b["lease_generation"]
        self.assertGreater(generation_b, generation_a)
        self.age_heartbeat(job_id)
        self.assertEqual(self.janitor.reclaim_expired(), 1)

        # Cycle 2: worker C claims and completes this time.
        conn_c = _connect(self.db_path)
        self.addCleanup(conn_c.close)
        lease_c = JobLease(conn_c, reclaim_timeout_seconds=RECLAIM_TIMEOUT_SECONDS)
        row_c = lease_c.claim("q1", "worker-C")
        self.assertIsNotNone(row_c)
        generation_c = row_c["lease_generation"]
        self.assertGreater(generation_c, generation_b)

        self.assertTrue(lease_c.complete(job_id, "worker-C", {"cycle": "final"}))
        # A duplicate terminal write by the same (already-terminal) worker is
        # idempotently fenced: it returns False and does not overwrite.
        self.assertFalse(lease_c.complete(job_id, "worker-C", {"cycle": "duplicate"}))

        final = self.fetch(job_id)
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["worker_id"], "worker-C")
        self.assertGreaterEqual(final["lease_generation"], generation_c)
        self.assertNotIn("duplicate", final["result_json"])
        self.assertIn("final", final["result_json"])
        completed_rows = self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE id = ? AND status = 'completed'",
            (job_id,),
        ).fetchone()[0]
        self.assertEqual(completed_rows, 1)

    def test_job_reaches_terminal_status_after_max_reclaim_cycles(self):
        job_id = self.insert_pending()
        generations = []
        for cycle in range(MAX_RECLAIM_CYCLES):
            row = self.crash_claim(f"crasher-{cycle}")
            self.assertIsNotNone(row, f"cycle {cycle} must be able to re-claim")
            if generations:
                self.assertGreater(row["lease_generation"], generations[-1])
            generations.append(row["lease_generation"])
            self.age_heartbeat(row["id"])
            self.assertEqual(self.janitor.reclaim_expired(), 1)

        from app.services.job_lease import JobLease

        conn = _connect(self.db_path)
        self.addCleanup(conn.close)
        lease = JobLease(conn, reclaim_timeout_seconds=RECLAIM_TIMEOUT_SECONDS)
        row = lease.claim("q1", "worker-final")
        self.assertIsNotNone(row)
        self.assertTrue(lease.complete(row["id"], "worker-final", {"after": "max-cycles"}))

        final = self.fetch(row["id"])
        self.assertEqual(final["status"], "completed")
        self.assertGreater(final["lease_generation"], generations[-1])
        self.assertGreaterEqual(final["lease_generation"], MAX_RECLAIM_CYCLES)
