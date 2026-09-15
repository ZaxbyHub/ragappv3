"""Issue #559 acceptance check C2 (AC3, DISCRIMINATING): janitor + fencing.

Worker A claims a job and stalls (heartbeat pushed far past the reclaim
timeout). The janitor reclaims the lease — row back to ``pending``, lease
generation bumped, worker cleared — and worker B re-claims it. Every late
write by the stale worker A (``complete``/``fail``/``heartbeat``) must be
rejected (return ``False``) and leave the row byte-for-byte unmutated, while
B's writes succeed. A heartbeat-renewal design WITHOUT a fencing token fails
this check.

Expectation: RED at the pre-change base (ModuleNotFoundError — the
``app.services.job_lease`` module does not exist yet); GREEN post-fix.
"""

import sqlite3
import unittest
from pathlib import Path
from tempfile import mkdtemp

RECLAIM_TIMEOUT_SECONDS = 30
STALE_SECONDS = 120  # well beyond the reclaim timeout


def _connect(db_path):
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class TestLeaseFencingAndJanitor(unittest.TestCase):
    def setUp(self):
        from app.services.job_lease import ensure_jobs_schema

        self.db_path = str(Path(mkdtemp()) / "lease-fence.db")
        self.conn = _connect(self.db_path)
        ensure_jobs_schema(self.conn)
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
        # Tests control time by pushing heartbeat_at directly into the past.
        self.conn.execute(
            "UPDATE jobs SET heartbeat_at = datetime('now', ?) WHERE id = ?",
            (f"-{seconds} seconds", job_id),
        )
        self.conn.commit()

    def test_stale_worker_late_writes_are_fenced_off_after_reclaim(self):
        from app.services.job_lease import JobLease

        job_id = self.insert_pending()

        # Worker A holds the lease.
        conn_a = _connect(self.db_path)
        self.addCleanup(conn_a.close)
        lease_a = JobLease(conn_a, reclaim_timeout_seconds=RECLAIM_TIMEOUT_SECONDS)
        row_a = lease_a.claim("q1", "worker-A")
        self.assertIsNotNone(row_a)
        generation_a = row_a["lease_generation"]

        # Simulated stall: A stops heartbeating far past the reclaim timeout.
        self.age_heartbeat(job_id)

        # Janitor reclaims the expired lease.
        janitor = JobLease(self.conn, reclaim_timeout_seconds=RECLAIM_TIMEOUT_SECONDS)
        self.assertEqual(janitor.reclaim_expired(), 1)
        reclaimed = self.fetch(job_id)
        self.assertEqual(reclaimed["status"], "pending")
        self.assertIsNone(reclaimed["worker_id"])
        self.assertGreater(reclaimed["lease_generation"], generation_a)

        # Worker B picks the job up and now holds the fresher generation.
        conn_b = _connect(self.db_path)
        self.addCleanup(conn_b.close)
        lease_b = JobLease(conn_b, reclaim_timeout_seconds=RECLAIM_TIMEOUT_SECONDS)
        row_b = lease_b.claim("q1", "worker-B")
        self.assertIsNotNone(row_b)
        self.assertEqual(row_b["worker_id"], "worker-B")
        generation_b = row_b["lease_generation"]
        self.assertGreater(generation_b, generation_a)

        snapshot = dict(self.fetch(job_id))

        # The stale worker's late writes are ALL rejected — and mutate nothing.
        self.assertFalse(
            lease_a.complete(job_id, "worker-A", {"winner": "A-stale"})
        )
        self.assertFalse(lease_a.fail(job_id, "worker-A", "stale worker error"))
        self.assertFalse(lease_a.heartbeat(job_id, "worker-A"))
        self.assertEqual(dict(self.fetch(job_id)), snapshot)

        # The current holder's writes succeed.
        self.assertTrue(lease_b.heartbeat(job_id, "worker-B"))
        self.assertTrue(lease_b.complete(job_id, "worker-B", {"winner": "B"}))

        final = self.fetch(job_id)
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["worker_id"], "worker-B")
        self.assertGreaterEqual(final["lease_generation"], generation_b)
        self.assertNotIn("A-stale", final["result_json"])
        self.assertIn("B", final["result_json"])
