"""Issue #559 acceptance check C5 (AC6, PRESERVING): status vocabularies.

Guards that the existing job-table status CHECK vocabularies still accept and
reject exactly the same values after the #559 lease migration:

- draft_jobs          pending/running/completed/failed/cancelled (rejects 'interrupted')
- wiki_compile_jobs   pending/running/completed/failed/cancelled (rejects 'interrupted')
- kms_compile_jobs    pending/running/completed/failed/cancelled (rejects 'interrupted')
- document_reindex_jobs additionally accepts 'interrupted' (rejects 'processing')
- files.status        pending/processing/indexed/partial/error (rejects 'queued')

The real tables are created by calling the repo's real migration path
(``app.models.database.run_migrations``) on a temp DB — the same fixture
pattern as ``tests/test_wiki_compile_processor.py``. This file exercises ONLY
existing schema and must be GREEN at the pre-change base AND stay green
post-fix.
"""

import sqlite3
import unittest
from pathlib import Path
from tempfile import mkdtemp

FIVE_STATUS_VOCAB = ["pending", "running", "completed", "failed", "cancelled"]


def _wiki_kms_reindex_params(status):
    return (1, "manual", status)


def _draft_job_params(status):
    return (1, 1, 1, "parse_input", 1, 60, status)


def _file_params(status):
    return (1, f"/tmp/559c5-{status}.txt", f"559c5-{status}.txt", 10, status)


class TestJobStatusVocabulariesPreserved(unittest.TestCase):
    def setUp(self):
        from app.models.database import run_migrations

        db_path = str(Path(mkdtemp()) / "vocab.db")
        run_migrations(db_path)
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            INSERT OR IGNORE INTO users (id, username, hashed_password)
                VALUES (1, 'vocab-user', 'x');
            INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'vocab-vault');
            INSERT OR IGNORE INTO drafts (id, vault_id, created_by, title, mode)
                VALUES (1, 1, 1, 'vocab-draft', 'rewrite');
            """
        )
        self.conn.commit()
        self.addCleanup(self.conn.close)

    def assert_accepts(self, table, columns, make_params, status):
        placeholders = ", ".join("?" for _ in columns.split(","))
        cur = self.conn.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
            make_params(status),
        )
        self.conn.commit()
        stored = self.conn.execute(
            f"SELECT status FROM {table} WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        self.assertIsNotNone(stored)
        self.assertEqual(stored["status"], status)

    def assert_rejects(self, table, columns, make_params, status):
        placeholders = ", ".join("?" for _ in columns.split(","))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
                make_params(status),
            )
        self.conn.rollback()

    def test_draft_jobs_vocabulary_unchanged(self):
        columns = (
            "draft_id, vault_id, created_by, job_type, "
            "max_model_calls, timeout_seconds, status"
        )
        for status in FIVE_STATUS_VOCAB:
            with self.subTest(status=status):
                self.assert_accepts("draft_jobs", columns, _draft_job_params, status)
        self.assert_rejects("draft_jobs", columns, _draft_job_params, "interrupted")

    def test_wiki_compile_jobs_vocabulary_unchanged(self):
        columns = "vault_id, trigger_type, status"
        for status in FIVE_STATUS_VOCAB:
            with self.subTest(status=status):
                self.assert_accepts(
                    "wiki_compile_jobs", columns, _wiki_kms_reindex_params, status
                )
        self.assert_rejects(
            "wiki_compile_jobs", columns, _wiki_kms_reindex_params, "interrupted"
        )

    def test_kms_compile_jobs_vocabulary_unchanged(self):
        columns = "vault_id, trigger_type, status"
        for status in FIVE_STATUS_VOCAB:
            with self.subTest(status=status):
                self.assert_accepts(
                    "kms_compile_jobs", columns, _wiki_kms_reindex_params, status
                )
        self.assert_rejects(
            "kms_compile_jobs", columns, _wiki_kms_reindex_params, "interrupted"
        )

    def test_document_reindex_jobs_still_accepts_interrupted(self):
        columns = "vault_id, trigger_type, status"
        for status in FIVE_STATUS_VOCAB + ["interrupted"]:
            with self.subTest(status=status):
                self.assert_accepts(
                    "document_reindex_jobs", columns, _wiki_kms_reindex_params, status
                )
        self.assert_rejects(
            "document_reindex_jobs", columns, _wiki_kms_reindex_params, "processing"
        )

    def test_files_status_vocabulary_unchanged(self):
        columns = "vault_id, file_path, file_name, file_size, status"
        for status in ["pending", "processing", "indexed", "partial", "error"]:
            with self.subTest(status=status):
                self.assert_accepts("files", columns, _file_params, status)
        self.assert_rejects("files", columns, _file_params, "queued")
