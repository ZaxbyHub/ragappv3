"""Issue #956 pin: a manual retry grants a full fresh attempt budget.

The lease-mode ``retry_job`` in WikiStore and KMSStore resets the failed
row to pending. Before #956 the SET clause left ``attempts`` untouched, so
a job that had reached ``jobs_max_attempts`` got exactly one claim after a
manual retry (``JobLease.claim`` pre-increments attempts, and the cap
settle fires on ``attempts >= jobs_max_attempts``) before settling failed
again — the operator's retry button was a no-op budget-wise.

Chosen semantics (recorded on #956): an operator-initiated retry is an
explicit decision to try again, so ``attempts`` resets to 0 and the next
``jobs_max_attempts`` claims are real attempts. The janitor's reclaim
accounting is a separate SQL path and is unchanged.
"""

import os
import shutil
import sqlite3
import unittest
from pathlib import Path
from tempfile import mkdtemp

os.environ.setdefault("USERS_ENABLED", "false")
os.environ.setdefault(
    "ADMIN_SECRET_TOKEN", "o02i-token-0123456789abcdef0123456789abcdef"
)

from app.config import settings  # noqa: E402


def _mkdb(prefix: str) -> str:
    from app.models.database import run_migrations

    tmp_dir = mkdtemp(prefix=prefix)
    db_path = str(Path(tmp_dir) / "o02i.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'V1')")
    conn.commit()
    conn.close()
    return db_path


class TestManualRetryBudget(unittest.TestCase):
    """retry_job on a capped failed row resets attempts to a fresh budget."""

    def _capped_job(self, db_path: str, queue: str) -> tuple:
        from app.services.job_lease import ensure_jobs_schema

        cap = settings.jobs_max_attempts
        ensure_jobs_schema(sqlite3.connect(db_path, timeout=10))
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            payload = '{"vault_id": 1, "trigger_type": "manual"}'
            cur = conn.execute(
                "INSERT INTO jobs (queue, status, payload_json, attempts,"
                " lease_generation, error) VALUES (?, 'failed', ?, ?, 2, ?)",
                (queue, payload, cap, "COMPILE_FAILED: wiki compile dispatch failed; see server logs"),
            )
            job_id = int(cur.lastrowid)
            conn.commit()
            return job_id, cap
        finally:
            conn.close()

    def _row(self, db_path: str, job_id: int) -> sqlite3.Row:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(
                "SELECT status, attempts, lease_generation, error, run_after"
                " FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        finally:
            conn.close()

    def test_wiki_retry_resets_capped_attempts(self):
        from app.services.wiki_store import WikiStore

        db_path = _mkdb("o02i-wiki-")
        self.addCleanup(shutil.rmtree, os.path.dirname(db_path), True)
        job_id, cap = self._capped_job(db_path, "wiki")
        self.assertEqual(
            self._row(db_path, job_id)["attempts"],
            cap,
            "fixture sanity: the row starts at the attempt cap",
        )

        from unittest.mock import patch

        with patch.object(settings, "wiki_kms_job_lease_enabled", True):
            conn = sqlite3.connect(db_path, timeout=10)
            try:
                job = WikiStore(conn).retry_job(job_id, 1)
            finally:
                conn.close()

        self.assertIsNotNone(job, "the capped failed row must be retryable")
        row = self._row(db_path, job_id)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(
            row["attempts"],
            0,
            "manual retry must reset the attempt budget (#956): without"
            " the reset, the next claim pre-increments to cap+1 and the"
            " cap settle fires immediately",
        )
        self.assertEqual(row["lease_generation"], 3)
        self.assertIsNone(row["error"])
        self.assertIsNone(row["run_after"])

    def test_kms_retry_resets_capped_attempts(self):
        from app.services.kms_store import KMSStore

        db_path = _mkdb("o02i-kms-")
        self.addCleanup(shutil.rmtree, os.path.dirname(db_path), True)
        job_id, cap = self._capped_job(db_path, "kms")
        self.assertEqual(
            self._row(db_path, job_id)["attempts"],
            cap,
            "fixture sanity: the row starts at the attempt cap",
        )

        from unittest.mock import patch

        with patch.object(settings, "wiki_kms_job_lease_enabled", True):
            conn = sqlite3.connect(db_path, timeout=10)
            try:
                job = KMSStore(conn).retry_job(job_id, 1)
            finally:
                conn.close()

        self.assertIsNotNone(job, "the capped failed row must be retryable")
        row = self._row(db_path, job_id)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(
            row["attempts"],
            0,
            "manual retry must reset the attempt budget on the KMS queue"
            " too (#956 store parity)",
        )
        self.assertEqual(row["lease_generation"], 3)
        self.assertIsNone(row["error"])
        self.assertIsNone(row["run_after"])
