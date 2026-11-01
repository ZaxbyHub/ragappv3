"""Issue #953 acceptance checks: historical (pre-#951) raw ``jobs.error``
rows must not reach vault readers through the wiki/KMS store readers, and
the legacy-table sync must not re-surface that raw text either.

PR #951 redacted the WRITE path (new failures persist stable codes like
``COMPILE_FAILED: ...``), but rows written before that deploy — and rows the
legacy sync copies forward from ``wiki_compile_jobs`` / ``kms_compile_jobs``
— still hold raw ``str(exc)`` text, and the row mappers
(``_compile_job_from_jobs_row`` in both stores) pass the column through
untouched, so ``WikiStore.list_jobs`` / ``KMSStore.list_jobs`` (and the
GET /wiki/jobs, GET /kms/jobs routes over them) return it verbatim.

Checks (lease mode on; the reader surface under test is the store reader):

- AC1: a pre-fix-shaped failed ``jobs`` row whose ``error`` column holds raw
  exception text is NOT returned with that raw text by ``WikiStore.list_jobs``
  (wiki arm) / ``KMSStore.list_jobs`` (kms arm) — the sentinel is absent and
  the reader-visible error is still non-empty (a stable-code shape).
- AC2: a legacy failed row seeded with raw error text is still copied into
  ``jobs`` by ``_sync_missing_compile_job_rows`` (the sync still creates the
  row), but the raw text is NOT reader-visible through the store readers.
- PRESERVING: a row whose ``error`` already matches the post-#951 stable-code
  shapes (``COMPILE_FAILED:`` / ``attempt_cap_exceeded:`` /
  ``PAYLOAD_MALFORMED:`` prefixes) is returned UNCHANGED — byte-identical —
  by both mappers.

RED at base for the AC1/AC2 checks (the mappers and the sync pass the raw
sentinel through verbatim); GREEN after the fix (the reader-visible value is
redacted to a stable shape while already-stable values pass through
unchanged). The PRESERVING check is GREEN at base and must stay GREEN.

Harness: temp ``run_migrations`` DBs, direct store calls with the lease
switch patched on (mirrors the frozen o02 suites and the Phase-2 probes).
"""

import os
import shutil
import sqlite3
import unittest
from pathlib import Path
from tempfile import mkdtemp
from unittest.mock import patch

os.environ.setdefault("USERS_ENABLED", "false")
os.environ.setdefault(
    "ADMIN_SECRET_TOKEN", "o02c-token-0123456789abcdef0123456789abcdef"
)

from app.config import settings  # noqa: E402

WIKI_SENTINEL = "O02C-SENTINEL-WIKI-HISTORICAL-RAW token=sk-live-abc"
KMS_SENTINEL = "O02C-SENTINEL-KMS-HISTORICAL-RAW path=/etc/shadow"
SYNC_WIKI_SENTINEL = "O02C-SENTINEL-WIKI-SYNC-RAW pw=hunter2"
SYNC_KMS_SENTINEL = "O02C-SENTINEL-KMS-SYNC-RAW pw=hunter3"

# Post-#951 stable-code shapes: these must pass through any redaction
# boundary byte-identically (PRESERVING arm).
STABLE_CODE_SHAPES = (
    "COMPILE_FAILED: wiki compile dispatch failed; see server logs",
    "COMPILE_FAILED: KMS compile dispatch failed; see server logs",
    "attempt_cap_exceeded: COMPILE_FAILED: wiki compile dispatch"
    " failed; see server logs",
    "PAYLOAD_MALFORMED: compile job payload is corrupt; see server logs",
)


def _mkdb(prefix: str) -> str:
    from app.models.database import run_migrations

    tmp_dir = mkdtemp(prefix=prefix)
    db_path = str(Path(tmp_dir) / "o02c.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'V1')")
    conn.commit()
    conn.close()
    return db_path


def _conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


class TestHistoricalErrorRedaction(unittest.TestCase):
    """AC1/AC2 (#953): historical raw error rows are not reader-visible."""

    def setUp(self):
        lease_patcher = patch.object(settings, "wiki_kms_job_lease_enabled", True)
        lease_patcher.start()
        self.addCleanup(lease_patcher.stop)
        self.tmp_dirs = []

    def _db(self, prefix: str) -> str:
        db_path = _mkdb(prefix)
        self.tmp_dirs.append(os.path.dirname(db_path))
        self.addCleanup(shutil.rmtree, os.path.dirname(db_path), True)
        return db_path

    def test_wiki_historical_failed_row_error_redacted_at_read(self):
        # check: AC1 (wiki arm) — RED at base, GREEN after the fix.
        from app.services.wiki_store import WikiStore

        db_path = self._db("o02c-hist-wiki-")
        conn = _conn(db_path)
        try:
            job = WikiStore(conn).create_job(vault_id=1, trigger_type="manual")
            conn.execute(
                "UPDATE jobs SET status = 'failed', error = ? WHERE id = ?",
                (f"RuntimeError: secret {WIKI_SENTINEL}", job.id),
            )
            conn.commit()
            rows = WikiStore(conn).list_jobs(1)
            row = next((j for j in rows if j.id == job.id), None)
            self.assertIsNotNone(row, "expected the historical row in list_jobs")
            error = row.error or ""
            self.assertNotIn(
                WIKI_SENTINEL,
                error,
                "raw pre-#951 error text must not be reader-visible (#953)",
            )
            self.assertTrue(
                error.strip(),
                "the reader-visible error must stay a non-empty stable shape",
            )
        finally:
            conn.close()

    def test_kms_historical_failed_row_error_redacted_at_read(self):
        # check: AC1 (kms arm) — RED at base, GREEN after the fix.
        from app.services.kms_store import KMSStore

        db_path = self._db("o02c-hist-kms-")
        conn = _conn(db_path)
        try:
            job = KMSStore(conn).create_job(1, "manual")
            conn.execute(
                "UPDATE jobs SET status = 'failed', error = ? WHERE id = ?",
                (f"ValueError: secret {KMS_SENTINEL}", job.id),
            )
            conn.commit()
            rows = KMSStore(conn).list_jobs(1)
            row = next((j for j in rows if j.id == job.id), None)
            self.assertIsNotNone(row, "expected the historical row in list_jobs")
            error = row.error or ""
            self.assertNotIn(
                KMS_SENTINEL,
                error,
                "raw pre-#951 error text must not be reader-visible (#953)",
            )
            self.assertTrue(
                error.strip(),
                "the reader-visible error must stay a non-empty stable shape",
            )
        finally:
            conn.close()

    def _run_sync(self, db_path: str) -> None:
        from app.models.database import SQLiteConnectionPool
        from app.services.background_tasks import BackgroundProcessor

        pool = SQLiteConnectionPool(db_path, max_size=2)
        try:
            with patch.object(settings, "reindex_job_lease_enabled", True):
                bp = BackgroundProcessor(pool=pool, retry_delay=0.05)
                bp._sync_missing_compile_job_rows()
        finally:
            pool.close_all()

    def test_legacy_sync_wiki_row_not_reader_visible_raw(self):
        # check: AC2 (wiki arm) — RED at base, GREEN after the fix.
        from app.services.wiki_store import WikiStore

        db_path = self._db("o02c-sync-wiki-")
        conn = _conn(db_path)
        legacy_id = None
        try:
            cur = conn.execute(
                "INSERT INTO wiki_compile_jobs (vault_id, trigger_type, status,"
                " error) VALUES (1, 'manual', 'failed', ?)",
                (f"RuntimeError: legacy {SYNC_WIKI_SENTINEL}",),
            )
            legacy_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()

        self._run_sync(db_path)

        conn = _conn(db_path)
        try:
            # The sync must still have created the unified row...
            row = conn.execute(
                "SELECT id FROM jobs WHERE queue = 'wiki' AND "
                "CASE WHEN json_valid(payload_json) THEN CAST(json_extract("
                "payload_json, '$.legacy_job_id') AS INTEGER) ELSE NULL END = ?",
                (legacy_id,),
            ).fetchone()
            self.assertIsNotNone(
                row, "the legacy sync must still create the unified jobs row"
            )
            # ...but the raw text must not be reader-visible through the store.
            jobs = WikiStore(conn).list_jobs(1)
            mapped = next(
                (j for j in jobs if j.id == row["id"]), None
            )
            self.assertIsNotNone(
                mapped, "the synced row must be returned by the store reader"
            )
            self.assertNotIn(
                SYNC_WIKI_SENTINEL,
                mapped.error or "",
                "the legacy sync must not surface the raw legacy error text"
                " through the unified rows (#953)",
            )
        finally:
            conn.close()

    def test_legacy_sync_kms_row_not_reader_visible_raw(self):
        # check: AC2 (kms arm) — RED at base, GREEN after the fix.
        from app.services.kms_store import KMSStore

        db_path = self._db("o02c-sync-kms-")
        conn = _conn(db_path)
        legacy_id = None
        try:
            cur = conn.execute(
                "INSERT INTO kms_compile_jobs (vault_id, trigger_type, status,"
                " error) VALUES (1, 'manual', 'failed', ?)",
                (f"ValueError: legacy {SYNC_KMS_SENTINEL}",),
            )
            legacy_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()

        self._run_sync(db_path)

        conn = _conn(db_path)
        try:
            row = conn.execute(
                "SELECT id FROM jobs WHERE queue = 'kms' AND "
                "CASE WHEN json_valid(payload_json) THEN CAST(json_extract("
                "payload_json, '$.legacy_job_id') AS INTEGER) ELSE NULL END = ?",
                (legacy_id,),
            ).fetchone()
            self.assertIsNotNone(
                row, "the legacy sync must still create the unified jobs row"
            )
            jobs = KMSStore(conn).list_jobs(1)
            mapped = next((j for j in jobs if j.id == row["id"]), None)
            self.assertIsNotNone(
                mapped, "the synced row must be returned by the store reader"
            )
            self.assertNotIn(
                SYNC_KMS_SENTINEL,
                mapped.error or "",
                "the legacy sync must not surface the raw legacy error text"
                " through the unified rows (#953)",
            )
        finally:
            conn.close()


class TestStableCodePassThrough(unittest.TestCase):
    """PRESERVING: already-stable error values pass through byte-identically.

    GREEN at base (the mappers pass the column through) and must stay GREEN
    after the fix — redaction applies to raw text only, never to values that
    already carry a post-#951 stable code.
    """

    def setUp(self):
        lease_patcher = patch.object(settings, "wiki_kms_job_lease_enabled", True)
        lease_patcher.start()
        self.addCleanup(lease_patcher.stop)
        self.db_path = _mkdb("o02c-stable-")
        self.addCleanup(
            shutil.rmtree, os.path.dirname(self.db_path), True
        )

    def test_stable_code_shapes_returned_unchanged(self):
        from app.services.kms_store import KMSStore
        from app.services.wiki_store import WikiStore

        conn = _conn(self.db_path)
        try:
            expected = {}
            for shape in STABLE_CODE_SHAPES:
                wjob = WikiStore(conn).create_job(
                    vault_id=1, trigger_type="manual"
                )
                conn.execute(
                    "UPDATE jobs SET status = 'failed', error = ? WHERE id = ?",
                    (shape, wjob.id),
                )
                expected[wjob.id] = ("wiki", shape)
                kjob = KMSStore(conn).create_job(1, "manual")
                conn.execute(
                    "UPDATE jobs SET status = 'failed', error = ? WHERE id = ?",
                    (shape, kjob.id),
                )
                expected[kjob.id] = ("kms", shape)
            conn.commit()

            for queue, store_cls in (
                ("wiki", WikiStore),
                ("kms", KMSStore),
            ):
                for mapped in store_cls(conn).list_jobs(1):
                    with self.subTest(queue=queue, job_id=mapped.id):
                        self.assertIn(mapped.id, expected)
                        want_queue, want_error = expected[mapped.id]
                        self.assertEqual(want_queue, queue)
                        self.assertEqual(
                            mapped.error,
                            want_error,
                            "an already-stable error value must be returned"
                            " byte-identically by the row mapper",
                        )
        finally:
            conn.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
