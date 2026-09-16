"""Issue #559 stage-2 acceptance check C2 (DISCRIMINATING).

Claim contention on the shared lease for the wiki and KMS queues: two
workers racing the shared lease claim for one pending wiki (or kms) row see
exactly one winner and one empty hand — the single-statement claim
(``UPDATE ... WHERE status='pending' ... RETURNING`` semantics) the issue
standardizes on must never let both racers take the row.

Expectation: RED at the stage-2 base — the stores still write their legacy
``wiki_compile_jobs`` / ``kms_compile_jobs`` tables, so the shared lease
claim finds no wiki/kms row at all (``[None, None]`` instead of one winner).
Every failure is a clean assertion failure inside the test body. GREEN
post-migration.
"""

import json
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


def _make_db():
    from app.models.database import run_migrations

    db_path = str(Path(mkdtemp()) / "s2-contention.db")
    run_migrations(db_path)  # creates legacy tables AND the shared jobs table
    conn = _connect(db_path)
    conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'V1')")
    conn.commit()
    return db_path, conn


def _payload(row):
    return json.loads(row["payload_json"])


class TestC2WikiKmsLeaseContention(unittest.TestCase):
    # check: C2 (DISCRIMINATING) — two workers racing the shared lease claim
    # for one pending wiki/kms row: exactly one claim returns the row, the
    # other returns nothing.

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

    def _race_claim(self, queue, plant):
        plant()
        results = [None, None]
        barrier = threading.Barrier(2)

        def claim(slot):
            conn = _connect(self.db_path)
            try:
                lease = self._lease(conn)
                barrier.wait(timeout=5)
                results[slot] = lease.claim(queue, f"worker-{slot}")
            finally:
                conn.close()

        threads = [threading.Thread(target=claim, args=(slot,)) for slot in (0, 1)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        return results

    def test_two_workers_race_wiki_lease_claim_exactly_one_wins(self):
        results = self._race_claim(
            "wiki",
            lambda: self.wiki_store.create_job(
                vault_id=1,
                trigger_type="ingest",
                trigger_id="file:7",
                input_json={"file_id": 7},
            ),
        )
        claimed = [row for row in results if row is not None]
        rejected = [row for row in results if row is None]
        self.assertEqual(
            len(claimed), 1, f"exactly one claimant wins the wiki row: {results!r}"
        )
        self.assertEqual(len(rejected), 1)
        winner = claimed[0]
        self.assertEqual(winner["status"], "running")
        self.assertEqual(_payload(winner)["trigger_id"], "file:7")

    def test_two_workers_race_kms_lease_claim_exactly_one_wins(self):
        results = self._race_claim(
            "kms",
            lambda: self.kms_store.create_job(
                vault_id=1,
                trigger_type="manual",
                trigger_id="vault:1",
                input_json={"vault_id": 1},
            ),
        )
        claimed = [row for row in results if row is not None]
        rejected = [row for row in results if row is None]
        self.assertEqual(
            len(claimed), 1, f"exactly one claimant wins the kms row: {results!r}"
        )
        self.assertEqual(len(rejected), 1)
        self.assertEqual(claimed[0]["status"], "running")


if __name__ == "__main__":
    unittest.main()
