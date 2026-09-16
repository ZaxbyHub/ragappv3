"""Issue #559 acceptance check C3: lease lifecycle contract edges.

Pins the primitive-level edges of the ``app.services.job_lease`` contract:
(a) claim on an empty queue returns ``None``; (b) a heartbeat renewed inside
the lease window PREVENTS reclaim; (c) ``reclaim_expired(queue=...)`` only
touches the named queue; (d) terminal rows (completed/failed) are never
reclaimed.

Behaviorally these are preserving-style contract edges of the new primitive,
but the file is RED at the pre-change base along with the rest of the set
(ModuleNotFoundError — ``app.services.job_lease`` does not exist yet) and
GREEN post-fix. The discriminating weight of the check set lives in C2/C4.
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


class TestLeaseLifecycleEdges(unittest.TestCase):
    def setUp(self):
        from app.services.job_lease import JobLease, ensure_jobs_schema

        self.db_path = str(Path(mkdtemp()) / "lease-lifecycle.db")
        self.conn = _connect(self.db_path)
        ensure_jobs_schema(self.conn)
        self.lease = JobLease(self.conn, reclaim_timeout_seconds=RECLAIM_TIMEOUT_SECONDS)
        self.addCleanup(self.conn.close)

    def insert_pending(self, queue="q1"):
        cur = self.conn.execute(
            "INSERT INTO jobs (queue, payload_json) VALUES (?, '{}')", (queue,)
        )
        self.conn.commit()
        return cur.lastrowid

    def fetch_status(self, job_id):
        row = self.conn.execute(
            "SELECT status FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        self.assertIsNotNone(row)
        return row["status"]

    def age_heartbeat(self, job_id, seconds=STALE_SECONDS):
        self.conn.execute(
            "UPDATE jobs SET heartbeat_at = datetime('now', ?) WHERE id = ?",
            (f"-{seconds} seconds", job_id),
        )
        self.conn.commit()

    def test_claim_on_empty_queue_returns_none(self):
        self.insert_pending("other-queue")
        self.assertIsNone(self.lease.claim("q1", "worker-1"))

    def test_heartbeat_renewal_inside_lease_window_prevents_reclaim(self):
        job_id = self.insert_pending()
        row = self.lease.claim("q1", "worker-1")
        self.assertIsNotNone(row)

        # Renewed heartbeat -> fresh timestamp -> the janitor must not reclaim.
        self.assertTrue(self.lease.heartbeat(job_id, "worker-1"))
        self.assertEqual(self.lease.reclaim_expired(), 0)
        self.assertEqual(self.fetch_status(job_id), "running")

    def test_reclaim_queue_filter_leaves_other_queues_alone(self):
        job_qa = self.insert_pending("qa")
        job_qb = self.insert_pending("qb")
        self.assertIsNotNone(self.lease.claim("qa", "worker-qa"))
        self.assertIsNotNone(self.lease.claim("qb", "worker-qb"))
        self.age_heartbeat(job_qa)
        self.age_heartbeat(job_qb)

        self.assertEqual(self.lease.reclaim_expired(queue="qa"), 1)
        self.assertEqual(self.fetch_status(job_qa), "pending")
        # qb's lease is equally expired but was not touched by the qa pass.
        self.assertEqual(self.fetch_status(job_qb), "running")

        self.assertEqual(self.lease.reclaim_expired(queue="qb"), 1)
        self.assertEqual(self.fetch_status(job_qb), "pending")

    def test_terminal_rows_are_never_reclaimed(self):
        job_ok = self.insert_pending()
        job_bad = self.insert_pending()
        row_ok = self.lease.claim("q1", "worker-1")
        row_bad = self.lease.claim("q1", "worker-1")
        self.assertIsNotNone(row_ok)
        self.assertIsNotNone(row_bad)
        self.assertTrue(self.lease.complete(job_ok, "worker-1", {"ok": True}))
        self.assertTrue(self.lease.fail(job_bad, "worker-1", "boom"))

        # Terminal rows stay terminal even with stale heartbeats.
        self.age_heartbeat(job_ok)
        self.age_heartbeat(job_bad)
        self.assertEqual(self.lease.reclaim_expired(), 0)
        self.assertEqual(self.fetch_status(job_ok), "completed")
        self.assertEqual(self.fetch_status(job_bad), "failed")
