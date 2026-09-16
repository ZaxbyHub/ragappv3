"""Issue #559 stage-2 acceptance check C3 (DISCRIMINATING).

Heartbeat fencing + janitor for the wiki and KMS queues: a worker holds a
claimed job, its heartbeat ages past the reclaim timeout, and the janitor
reclaims the row (back to ``pending`` with a bumped lease generation, or
terminally ``failed`` once the attempts cap is hit) with NO process restart
involved. The stale worker's late ``complete``/``fail``/``heartbeat``/
``requeue`` writes are all rejected (return ``False``, mutate nothing) and a
second worker can then claim and complete the job.

Expectation: RED at the stage-2 base — the stores still write their legacy
tables, so the shared lease claim finds nothing to fence. GREEN
post-migration.

Time control: the janitor is driven by backdating ``jobs.heartbeat_at``
directly (the stage-1 C2/C3 harness pattern), so no test sleeps past a
barrier wait; the attempts cap is driven by a small ``max_attempts`` value
honored by the shared lease constructor.
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


def _make_db():
    from app.models.database import run_migrations

    db_path = str(Path(mkdtemp()) / "s2-fence.db")
    run_migrations(db_path)  # creates legacy tables AND the shared jobs table
    conn = _connect(db_path)
    conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'V1')")
    conn.commit()
    return db_path, conn


class TestC3WikiKmsFenceAndJanitor(unittest.TestCase):
    # check: C3 (DISCRIMINATING) — an expired lease on a wiki/KMS job is
    # reclaimed by the janitor alone (no process restart, no startup orphan
    # sweep): row back to pending with a bumped generation, or terminally
    # failed at the attempts cap; the stale worker's late writes are fenced
    # off; a second worker claims and completes the job.

    def setUp(self):
        from app.services.kms_store import KMSStore
        from app.services.wiki_store import WikiStore

        self.db_path, self.conn = _make_db()
        self.wiki_store = WikiStore(self.conn)
        self.kms_store = KMSStore(self.conn)
        self.addCleanup(self.conn.close)

    def _lease(self, conn, **kwargs):
        from app.services.job_lease import JobLease

        return JobLease(
            conn, reclaim_timeout_seconds=RECLAIM_TIMEOUT_SECONDS, **kwargs
        )

    def fetch(self, job_id):
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        self.assertIsNotNone(row)
        return row

    def age_heartbeat(self, job_id, seconds=STALE_SECONDS):
        # Tests control time by pushing heartbeat_at into the past (the
        # stage-1 harness pattern) instead of sleeping out the timeout.
        self.conn.execute(
            "UPDATE jobs SET heartbeat_at = datetime('now', ?) WHERE id = ?",
            (f"-{seconds} seconds", job_id),
        )
        self.conn.commit()

    def _assert_fenced_reclaim_scenario(self, queue, plant):
        plant()

        # Worker A holds the lease.
        conn_a = _connect(self.db_path)
        self.addCleanup(conn_a.close)
        lease_a = self._lease(conn_a)
        row_a = lease_a.claim(queue, "worker-A")
        self.assertIsNotNone(
            row_a, f"the shared lease claim must see the {queue} job"
        )
        job_id = row_a["id"]
        generation_a = row_a["lease_generation"]

        # Simulated stall: A stops heartbeating far past the reclaim timeout.
        self.age_heartbeat(job_id)

        # The JANITOR alone reclaims the expired lease — no process restart,
        # no startup orphan reset is invoked anywhere in this test.
        janitor = self._lease(self.conn)
        self.assertEqual(janitor.reclaim_expired(queue=queue), 1)
        reclaimed = self.fetch(job_id)
        self.assertEqual(reclaimed["status"], "pending")
        self.assertIsNone(reclaimed["worker_id"])
        self.assertGreater(reclaimed["lease_generation"], generation_a)

        # Worker B picks the job up under the fresher generation.
        conn_b = _connect(self.db_path)
        self.addCleanup(conn_b.close)
        lease_b = self._lease(conn_b)
        row_b = lease_b.claim(queue, "worker-B")
        self.assertIsNotNone(row_b)
        self.assertEqual(row_b["worker_id"], "worker-B")
        self.assertGreater(row_b["lease_generation"], generation_a)

        # Every late write by the stale worker A is rejected — and mutates
        # nothing (complete / fail / progress heartbeat / retryable requeue).
        snapshot = dict(self.fetch(job_id))
        self.assertFalse(lease_a.complete(job_id, "worker-A", {"winner": "A-stale"}))
        self.assertFalse(lease_a.fail(job_id, "worker-A", "stale worker error"))
        self.assertFalse(lease_a.heartbeat(job_id, "worker-A"))
        self.assertFalse(
            lease_a.requeue(job_id, "worker-A", delay_seconds=1.0, error="stale")
        )
        self.assertEqual(dict(self.fetch(job_id)), snapshot)

        # The current holder's writes succeed and settle the job.
        self.assertTrue(lease_b.heartbeat(job_id, "worker-B"))
        self.assertTrue(lease_b.complete(job_id, "worker-B", {"winner": "B"}))
        final = self.fetch(job_id)
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["worker_id"], "worker-B")
        self.assertNotIn("A-stale", final["result_json"])
        self.assertIn("B", final["result_json"])

    def test_wiki_stale_worker_late_writes_are_fenced_after_janitor_reclaim(self):
        self._assert_fenced_reclaim_scenario(
            "wiki",
            lambda: self.wiki_store.create_job(
                vault_id=1,
                trigger_type="ingest",
                trigger_id="file:7",
                input_json={"file_id": 7},
            ),
        )

    def test_kms_stale_worker_late_writes_are_fenced_after_janitor_reclaim(self):
        self._assert_fenced_reclaim_scenario(
            "kms",
            lambda: self.kms_store.create_job(
                vault_id=1,
                trigger_type="manual",
                trigger_id="vault:1",
                input_json={"vault_id": 1},
            ),
        )

    def _assert_cap_scenario(self, queue, plant, max_attempts, stall_cycles):
        """Repeated lease expiry up to the cap must settle the row terminally."""
        plant()
        conn_a = _connect(self.db_path)
        self.addCleanup(conn_a.close)
        lease_a = self._lease(conn_a, max_attempts=max_attempts)
        janitor = self._lease(self.conn, max_attempts=max_attempts)

        job_id = None
        for cycle in range(stall_cycles):
            row = lease_a.claim(queue, f"worker-A-{cycle}")
            self.assertIsNotNone(row, f"re-claim {cycle} must see the {queue} job")
            job_id = row["id"]
            self.age_heartbeat(job_id)
            self.assertEqual(janitor.reclaim_expired(queue=queue), 1)
            if cycle < stall_cycles - 1:
                # Below the cap the janitor requeues for another attempt.
                self.assertEqual(
                    self.fetch(job_id)["status"],
                    "pending",
                    "below the attempts cap the janitor must requeue",
                )

        settled = self.fetch(job_id)
        self.assertEqual(
            settled["status"], "failed", "at the cap the janitor must settle failed"
        )
        self.assertIsNotNone(settled["error"])
        self.assertIn(
            "attempt_cap",
            settled["error"],
            f"distinctive cap error expected, got {settled['error']!r}",
        )

        # Terminally settled: the stale worker cannot complete it, the row is
        # never requeued, and no later worker can claim it again.
        self.assertFalse(lease_a.complete(job_id, "worker-A-0", {"late": True}))
        self.assertEqual(janitor.reclaim_expired(queue=queue), 0)
        conn_c = _connect(self.db_path)
        self.addCleanup(conn_c.close)
        self.assertIsNone(self._lease(conn_c).claim(queue, "worker-C"))
        self.assertEqual(self.fetch(job_id)["status"], "failed")

    def test_wiki_attempts_cap_settles_failed_and_is_not_requeued(self):
        # attempts 1 -> requeued; attempts 2 (== cap) -> terminally failed.
        self._assert_cap_scenario(
            "wiki",
            lambda: self.wiki_store.create_job(
                vault_id=1,
                trigger_type="ingest",
                trigger_id="file:8",
                input_json={"file_id": 8},
            ),
            max_attempts=2,
            stall_cycles=2,
        )

    def test_kms_attempts_cap_settles_failed_and_is_not_requeued(self):
        # attempts 1 (== cap) -> terminally failed on the first expiry.
        self._assert_cap_scenario(
            "kms",
            lambda: self.kms_store.create_job(
                vault_id=1,
                trigger_type="manual",
                trigger_id="vault:1",
                input_json={"vault_id": 1},
            ),
            max_attempts=1,
            stall_cycles=1,
        )


if __name__ == "__main__":
    unittest.main()
