"""Issue #559 acceptance check C6 (AC5, C05-proving at the lease level):
the janitor settles orphaned leases without any process restart.

Plants N=50 ``running`` rows with stale heartbeats across 2 queues (jobs whose
workers crashed after claiming — no terminal write, no renewal). WITHOUT any
process restart or boot-recovery routine, a single ``reclaim_expired()`` pass
requeues all of them, and sequential claims by fresh workers drive ALL 50 to
terminal status. The point of C05: settlement must not depend on boot-recovery
ordering — nothing in this test calls a startup/recovery routine; the plain
janitor pass is the only settlement mechanism.

Expectation: RED at the pre-change base (ModuleNotFoundError — the
``app.services.job_lease`` module does not exist yet); GREEN post-fix.
"""

import sqlite3
import unittest
from pathlib import Path
from tempfile import mkdtemp

RECLAIM_TIMEOUT_SECONDS = 30
STALE_SECONDS = 120  # well beyond the reclaim timeout
QUEUES = ("ingest-sim", "reindex-sim")
PER_QUEUE = 25
TOTAL = PER_QUEUE * len(QUEUES)


def _connect(db_path):
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class TestJanitorSettlesWithoutRestart(unittest.TestCase):
    def setUp(self):
        from app.services.job_lease import JobLease, ensure_jobs_schema

        self.db_path = str(Path(mkdtemp()) / "lease-settle.db")
        self.conn = _connect(self.db_path)
        ensure_jobs_schema(self.conn)
        self.lease = JobLease(self.conn, reclaim_timeout_seconds=RECLAIM_TIMEOUT_SECONDS)
        self.addCleanup(self.conn.close)

    def status_counts(self):
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
        ).fetchall()
        return {row["status"]: row["n"] for row in rows}

    def test_single_janitor_pass_settles_fifty_orphaned_leases_without_restart(self):
        # Plant: all 50 jobs are claimed through the REAL claim path by workers
        # that then crash — the leases end 'running' with fresh heartbeats.
        for queue_index, queue in enumerate(QUEUES):
            for i in range(PER_QUEUE):
                self.conn.execute(
                    "INSERT INTO jobs (queue, payload_json) VALUES (?, '{}')", (queue,)
                )
        self.conn.commit()

        for queue in QUEUES:
            claimed = 0
            while True:
                row = self.lease.claim(queue, f"crashed-{queue}")
                if row is None:
                    break
                claimed += 1
            self.assertEqual(claimed, PER_QUEUE)  # queue drained, all 'running'
        counts = self.status_counts()
        self.assertEqual(counts.get("running"), TOTAL)
        self.assertEqual(counts.get("pending", 0), 0)

        # Crash: every worker's heartbeat goes stale in one shot. No process
        # restart, no boot-recovery routine is (or can be) involved.
        self.conn.execute(
            "UPDATE jobs SET heartbeat_at = datetime('now', ?)",
            (f"-{STALE_SECONDS} seconds",),
        )
        self.conn.commit()

        # ONE janitor pass settles the whole orphan population.
        self.assertEqual(self.lease.reclaim_expired(), TOTAL)
        counts = self.status_counts()
        self.assertEqual(counts.get("running", 0), 0)
        self.assertEqual(counts.get("pending"), TOTAL)

        # Fresh workers drain both queues sequentially; every job completes.
        completed = 0
        for queue in QUEUES:
            while True:
                worker = f"drain-{queue}-{completed}"
                row = self.lease.claim(queue, worker)
                if row is None:
                    break
                self.assertTrue(self.lease.complete(row["id"], worker, {"drained": True}))
                completed += 1
        self.assertEqual(completed, TOTAL)

        counts = self.status_counts()
        self.assertEqual(counts.get("running", 0), 0, "no lease left running")
        self.assertEqual(counts.get("pending", 0), 0)
        self.assertEqual(counts.get("completed"), TOTAL)
