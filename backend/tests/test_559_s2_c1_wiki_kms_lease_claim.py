"""Issue #559 stage-2 acceptance check C1 (DISCRIMINATING).

Stage 2 migrates the wiki and KMS compile-job queues onto the shared
DB-claimed job lease that stage 1 built for ingestion. This check pins the
enqueue level of that migration: creating a wiki compile job via
``WikiStore.create_job`` stores the work in the shared ``jobs`` table with
``queue='wiki'`` (payload carrying vault_id / trigger_type / trigger_id /
input_json); the same for KMS with ``queue='kms'``; and the shared lease
claim serves each queue in isolation.

Expectation: RED at the stage-2 base — the stores still write their legacy
``wiki_compile_jobs`` / ``kms_compile_jobs`` tables, so no ``jobs`` row with
queue='wiki'/'kms' ever appears and the shared lease claim finds nothing.
Every failure below is a clean assertion failure inside the test body (all
referenced symbols already exist at base). GREEN post-migration.
"""

import json
import sqlite3
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

    db_path = str(Path(mkdtemp()) / "s2-lease.db")
    run_migrations(db_path)  # creates legacy tables AND the shared jobs table
    conn = _connect(db_path)
    conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'V1')")
    conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (2, 'V2')")
    conn.commit()
    return db_path, conn


def _payload(row):
    return json.loads(row["payload_json"])


def _carried_input(payload):
    """The input_json the enqueuer handed the store, dict or JSON-string form."""
    raw = payload.get("input_json")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return raw if isinstance(raw, dict) else {}


class TestC1WikiKmsLeaseClaim(unittest.TestCase):
    # check: C1 (DISCRIMINATING) — WikiStore/KMSStore.create_job store the
    # work in the shared jobs table under queue='wiki'/'kms' with the
    # documented payload, and the shared lease claim path races
    # exactly-one-winner on those queues.

    def setUp(self):
        from app.services.kms_store import KMSStore
        from app.services.wiki_store import WikiStore

        self.db_path, self.conn = _make_db()
        self.wiki_store = WikiStore(self.conn)
        self.kms_store = KMSStore(self.conn)
        self.addCleanup(self.conn.close)

    def _queue_rows(self, queue):
        return self.conn.execute(
            "SELECT * FROM jobs WHERE queue = ?", (queue,)
        ).fetchall()

    def _lease(self, conn, **kwargs):
        from app.services.job_lease import JobLease

        return JobLease(
            conn, reclaim_timeout_seconds=RECLAIM_TIMEOUT_SECONDS, **kwargs
        )

    # -- enqueue lands in the unified store ---------------------------------

    def test_wiki_create_job_stores_row_in_shared_jobs_queue(self):
        job = self.wiki_store.create_job(
            vault_id=1,
            trigger_type="ingest",
            trigger_id="file:7",
            input_json={"file_id": 7, "vault_id": 1},
        )
        rows = self._queue_rows("wiki")
        self.assertEqual(
            len(rows), 1, f"expected one queue='wiki' row, found {len(rows)}"
        )
        row = rows[0]
        self.assertEqual(row["status"], "pending")
        self.assertEqual(
            row["id"],
            job.id,
            "the created job's id must address the unified jobs row",
        )
        payload = _payload(row)
        self.assertEqual(payload["vault_id"], 1)
        self.assertEqual(payload["trigger_type"], "ingest")
        self.assertEqual(payload["trigger_id"], "file:7")
        self.assertEqual(_carried_input(payload).get("file_id"), 7)

    def test_kms_create_job_stores_row_in_shared_jobs_queue(self):
        job = self.kms_store.create_job(
            vault_id=1,
            trigger_type="manual",
            trigger_id="vault:1",
            input_json={"vault_id": 1},
        )
        rows = self._queue_rows("kms")
        self.assertEqual(
            len(rows), 1, f"expected one queue='kms' row, found {len(rows)}"
        )
        row = rows[0]
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["id"], job.id)
        payload = _payload(row)
        self.assertEqual(payload["vault_id"], 1)
        self.assertEqual(payload["trigger_type"], "manual")
        self.assertEqual(payload["trigger_id"], "vault:1")
        self.assertEqual(_carried_input(payload).get("vault_id"), 1)

    # -- the shared lease claim serves each queue in isolation ---------------

    def test_wiki_and_kms_queues_are_claimed_in_isolation(self):
        self.wiki_store.create_job(
            vault_id=1,
            trigger_type="ingest",
            trigger_id="file:7",
            input_json={"file_id": 7},
        )
        self.kms_store.create_job(
            vault_id=1,
            trigger_type="manual",
            trigger_id="vault:1",
            input_json={"vault_id": 1},
        )
        lease = self._lease(self.conn)

        kms_row = lease.claim("kms", "kms-worker")
        self.assertIsNotNone(kms_row, "the shared claim must see the kms job")
        self.assertEqual(_payload(kms_row)["trigger_type"], "manual")

        wiki_row = lease.claim("wiki", "wiki-worker")
        self.assertIsNotNone(wiki_row, "the shared claim must see the wiki job")
        self.assertEqual(_payload(wiki_row)["trigger_id"], "file:7")

        self.assertIsNone(lease.claim("kms", "kms-worker-2"))
        self.assertIsNone(lease.claim("wiki", "wiki-worker-2"))


if __name__ == "__main__":
    unittest.main()
