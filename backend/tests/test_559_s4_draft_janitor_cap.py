"""Issue #559 stage-4 guardrail (final-critic Gap 1): the draft janitor
enforces ``jobs_max_attempts`` through its PRODUCTION entry point.

``DraftJobProcessor._reclaim_expired_leases`` must settle a crash-looping
draft job terminally (``failed`` / ``lease_attempt_cap_exceeded``) once its
durable ``attempts`` counter reaches the cap — the same contract the shared
janitor enforces on the ``jobs``-table queues — instead of resurrecting it
to ``pending`` on every sweep. Below the cap, recovery to pending still
applies, preserving the business-aware side effects.
"""

import sqlite3
import unittest
from pathlib import Path
from tempfile import mkdtemp

from app.config import settings
from app.models.database import SQLiteConnectionPool, init_db, run_migrations
from app.services.draft_job_processor import DraftJobProcessor


def _connect(db_path):
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class TestDraftJanitorAttemptsCap(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = Path(mkdtemp(prefix="s4-janitor-cap-"))
        self.db_path = str(temp / "draft-cap.db")
        init_db(self.db_path)
        run_migrations(self.db_path)
        self.conn = _connect(self.db_path)
        self.addCleanup(self.conn.close)
        self.conn.executescript(
            """
            INSERT OR IGNORE INTO users (id, username, hashed_password)
                VALUES (1, 'cap-user', 'x');
            INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'cap-vault');
            INSERT OR IGNORE INTO drafts (id, vault_id, created_by, title, mode)
                VALUES (1, 1, 1, 'cap-draft', 'rewrite');
            """
        )
        self.conn.commit()
        self.pool = SQLiteConnectionPool(self.db_path, max_size=2)
        self.addCleanup(self.pool.close_all)
        self.processor = DraftJobProcessor(
            self.pool,
            storage=None,
            extraction=None,
            poll_interval=0.05,
        )

    def _seed_running_job(self, job_type):
        cur = self.conn.execute(
            "INSERT INTO draft_jobs (draft_id, vault_id, created_by, "
            "job_type, max_model_calls, timeout_seconds, status, "
            "heartbeat_at) VALUES (1, 1, 1, ?, 0, 60, 'running', "
            "datetime('now', '-600 seconds'))",
            (job_type,),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def _age_attempts(self, job_id, attempts):
        self.conn.execute(
            "UPDATE draft_jobs SET attempts = ? WHERE id = ?",
            (attempts, job_id),
        )
        self.conn.commit()

    async def test_at_cap_settles_failed_and_does_not_resurrect(self):
        job_id = self._seed_running_job("parse_input")
        cap = max(int(settings.jobs_max_attempts), 1)
        self._age_attempts(job_id, cap)

        total = self.processor._reclaim_expired_leases()  # noqa: SLF001

        self.assertGreaterEqual(total, 1)
        row = self.conn.execute(
            "SELECT status, error_code, worker_id FROM draft_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_code"], "lease_attempt_cap_exceeded")
        self.assertIsNone(row["worker_id"])
        # A second sweep must not resurrect the capped job.
        self.processor._reclaim_expired_leases()  # noqa: SLF001
        still = self.conn.execute(
            "SELECT status FROM draft_jobs WHERE id = ?", (job_id,)
        ).fetchone()["status"]
        self.assertEqual(still, "failed")

    async def test_below_cap_still_recovers_to_pending(self):
        job_id = self._seed_running_job("parse_input")
        cap = max(int(settings.jobs_max_attempts), 1)
        self._age_attempts(job_id, max(cap - 1, 0))

        total = self.processor._reclaim_expired_leases()  # noqa: SLF001

        self.assertGreaterEqual(total, 1)
        row = self.conn.execute(
            "SELECT status, error_code FROM draft_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["error_code"], "worker_restart")

    async def test_compile_at_cap_settles_draft_failed(self):
        # Production shape: a compile job only exists after the draft was
        # queued for compilation.
        self.conn.execute("UPDATE drafts SET status = 'queued' WHERE id = 1")
        self.conn.commit()
        job_id = self._seed_running_job("compile")
        cap = max(int(settings.jobs_max_attempts), 1)
        self._age_attempts(job_id, cap)

        self.processor._reclaim_expired_leases()  # noqa: SLF001

        row = self.conn.execute(
            "SELECT status, error_code FROM draft_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_code"], "lease_attempt_cap_exceeded")
        # The draft must not strand on 'queued' after its only compile path
        # settled terminally.
        draft_status = self.conn.execute(
            "SELECT status FROM drafts WHERE id = 1"
        ).fetchone()["status"]
        self.assertEqual(draft_status, "failed")


if __name__ == "__main__":
    unittest.main()
