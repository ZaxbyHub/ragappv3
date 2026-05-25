"""Tests for KMSRetrievalService (Phase 2 RAG integration).

Verifies vault scoping, archived-status exclusion, kms_enabled gating, FTS
matching against title/body, and KMSEvidence labelling/serialization.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from queue import Empty, Queue

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import settings
from app.models.database import init_db, run_migrations
from app.services.kms_retrieval import (
    KMSEvidence,
    KMSRetrievalService,
    build_kms_fts_query,
)


class _Pool:
    """Minimal connection pool exposing get_connection/release_connection."""

    def __init__(self, db_path):
        self.db_path = db_path
        self._q = Queue(maxsize=5)

    def get_connection(self):
        try:
            return self._q.get_nowait()
        except Empty:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            return conn

    def release_connection(self, conn):
        try:
            self._q.put_nowait(conn)
        except Exception:
            conn.close()

    def close_all(self):
        while True:
            try:
                self._q.get_nowait().close()
            except Empty:
                break


class TestKMSRetrieval(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._db = str(Path(self._tmp) / "app.db")
        init_db(self._db)
        run_migrations(self._db)
        self._pool = _Pool(self._db)
        self.service = KMSRetrievalService(pool=self._pool)
        self._kms_enabled_original = settings.kms_enabled
        settings.kms_enabled = True
        # Seed a vault and entries.
        conn = sqlite3.connect(self._db)
        try:
            conn.execute("INSERT INTO vaults (id, name) VALUES (1, 'V1')")
            conn.execute("INSERT INTO vaults (id, name) VALUES (2, 'V2')")
            self._insert(conn, 1, "Onboarding Guide", "How to set up zlorptanium access.", "published")
            self._insert(conn, 1, "Archived Note", "Old zlorptanium info.", "archived")
            self._insert(conn, 1, "Draft Note", "Draft zlorptanium details.", "draft")
            self._insert(conn, 2, "Other Vault", "Different zlorptanium entry.", "published")
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        settings.kms_enabled = self._kms_enabled_original
        self._pool.close_all()
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def _insert(self, conn, vault_id, title, body, status):
        conn.execute(
            "INSERT INTO kms_entries (vault_id, slug, title, body, summary, tags_json, "
            "source_type, status) VALUES (?, ?, ?, ?, '', '[]', 'manual', ?)",
            (vault_id, title.lower().replace(" ", "-"), title, body, status),
        )

    def test_returns_published_and_draft_excludes_archived(self):
        results = self.service.retrieve("zlorptanium", vault_id=1)
        titles = {r.title for r in results}
        self.assertIn("Onboarding Guide", titles)
        self.assertIn("Draft Note", titles)
        self.assertNotIn("Archived Note", titles)
        # Cross-vault entry must not leak.
        self.assertNotIn("Other Vault", titles)

    def test_labels_assigned_sequentially(self):
        results = self.service.retrieve("zlorptanium", vault_id=1)
        self.assertTrue(results)
        self.assertEqual(results[0].label_placeholder, "K1")
        for i, r in enumerate(results, 1):
            self.assertEqual(r.label_placeholder, f"K{i}")
            self.assertIsInstance(r, KMSEvidence)

    def test_vault_none_returns_empty(self):
        self.assertEqual(self.service.retrieve("zlorptanium", vault_id=None), [])

    def test_disabled_returns_empty(self):
        settings.kms_enabled = False
        self.assertEqual(self.service.retrieve("zlorptanium", vault_id=1), [])

    def test_no_match_returns_empty(self):
        self.assertEqual(self.service.retrieve("nonexistentterm", vault_id=1), [])

    def test_empty_query_returns_empty(self):
        self.assertEqual(self.service.retrieve("!!! ???", vault_id=1), [])

    def test_to_dict_shape(self):
        results = self.service.retrieve("zlorptanium", vault_id=1)
        d = results[0].to_dict()
        for key in ("kms_label", "entry_id", "title", "excerpt", "score", "score_type"):
            self.assertIn(key, d)


class TestBuildKmsFtsQuery(unittest.TestCase):
    def test_strips_punctuation_and_prefixes(self):
        self.assertEqual(build_kms_fts_query("Quarterly-Report!"), "quarterly* report*")

    def test_empty_on_no_tokens(self):
        self.assertEqual(build_kms_fts_query("!!! ???"), "")


if __name__ == "__main__":
    unittest.main()
