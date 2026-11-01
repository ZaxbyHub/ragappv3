"""Issue #954 acceptance checks: the legacy-sync anti-join must survive a
malformed ``payload_json`` row on the wiki/kms queues.

``_sync_missing_compile_job_rows`` dedupes copied legacy rows with

    WHERE j.id NOT IN (
        SELECT CAST(json_extract(j2.payload_json, '$.legacy_job_id') ...)
        FROM jobs j2 WHERE j2.queue = ? AND json_extract(...) IS NOT NULL)

— and SQLite evaluates ``json_extract`` on every same-queue row with no
``json_valid`` guard, so ONE ``payload_json = '{not json'`` row raises
``sqlite3.OperationalError: malformed JSON`` inside the sync's BEGIN
IMMEDIATE transaction and the whole sync (all queues, every vault) aborts
and rolls back. The #951 guards covered the store-filter sites only.

Checks:

- AC4: with a ``queue='wiki'`` malformed row AND a well-formed pending
  legacy ``wiki_compile_jobs`` row, the sync completes WITHOUT raising and
  still copies the well-formed legacy row.
- AC5: the same for ``queue='kms'`` + a legacy ``kms_compile_jobs`` row.
- AC6 (PRESERVING): with well-formed rows only, the anti-join result is
  unchanged — the first sync copies the legacy rows and a second sync run
  creates 0 new rows (idempotence).

RED at base for AC4/AC5 (the sync raises OperationalError on the malformed
row); GREEN after the fix (the anti-join guards the extract, the malformed
row is excluded from the dedupe set, and the good legacy row still copies).
The AC6 check is GREEN at base and must stay GREEN.

Harness: temp ``run_migrations`` DBs behind a SQLiteConnectionPool with both
lease switches patched on, mirroring the frozen o02 suites and the Phase-2
probes.
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
    "ADMIN_SECRET_TOKEN", "o02d-token-0123456789abcdef0123456789abcdef"
)

from app.config import settings  # noqa: E402

MALFORMED_PAYLOAD = "{not json"


def _mkdb(prefix: str) -> str:
    from app.models.database import run_migrations

    tmp_dir = mkdtemp(prefix=prefix)
    db_path = str(Path(tmp_dir) / "o02d.db")
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


_LEGACY_TABLES = {"wiki": "wiki_compile_jobs", "kms": "kms_compile_jobs"}


def _seed_malformed_plus_good(db_path: str, queue: str) -> int:
    """One malformed jobs row + one well-formed pending legacy row on `queue`."""
    conn = _conn(db_path)
    try:
        conn.execute(
            "INSERT INTO jobs (queue, payload_json, status) "
            "VALUES (?, ?, 'pending')",
            (queue, MALFORMED_PAYLOAD),
        )
        cur = conn.execute(
            f"INSERT INTO {_LEGACY_TABLES[queue]} "  # nosec B608 - dict literal
            "(vault_id, trigger_type, status) VALUES (1, 'manual', 'pending')"
        )
        legacy_id = int(cur.lastrowid)
        conn.commit()
        return legacy_id
    finally:
        conn.close()


def _synced_row(conn: sqlite3.Connection, queue: str, legacy_id: int):
    return conn.execute(
        "SELECT id, status FROM jobs WHERE queue = ? AND "
        "CASE WHEN json_valid(payload_json) THEN CAST(json_extract("
        "payload_json, '$.legacy_job_id') AS INTEGER) ELSE NULL END = ?",
        (queue, legacy_id),
    ).fetchone()


def _run_sync(db_path: str) -> int:
    from app.models.database import SQLiteConnectionPool
    from app.services.background_tasks import BackgroundProcessor

    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        with patch.object(settings, "reindex_job_lease_enabled", True):
            bp = BackgroundProcessor(pool=pool, retry_delay=0.05)
            return bp._sync_missing_compile_job_rows()
    finally:
        pool.close_all()


class TestSyncAntijoinMalformedGuard(unittest.TestCase):
    """AC4/AC5 (#954): the sync survives a malformed same-queue row."""

    def setUp(self):
        lease_patcher = patch.object(settings, "wiki_kms_job_lease_enabled", True)
        lease_patcher.start()
        self.addCleanup(lease_patcher.stop)

    def _db(self, prefix: str) -> str:
        db_path = _mkdb(prefix)
        self.addCleanup(shutil.rmtree, os.path.dirname(db_path), True)
        return db_path

    def _check(self, queue: str):
        db_path = self._db(f"o02d-{queue}-")
        legacy_id = _seed_malformed_plus_good(db_path, queue)
        try:
            _run_sync(db_path)
        except Exception as exc:  # noqa: BLE001 — the AC is "without raising"
            self.fail(
                f"legacy sync raised {type(exc).__name__}: {exc} on the"
                f" malformed {queue}-queue payload row (#954)"
            )
        conn = _conn(db_path)
        try:
            row = _synced_row(conn, queue, legacy_id)
            self.assertIsNotNone(
                row,
                "the well-formed legacy row must still be copied by the sync"
                " despite the sibling malformed payload row (#954)",
            )
            self.assertEqual(row["status"], "pending")
        finally:
            conn.close()

    def test_sync_completes_and_copies_with_malformed_wiki_row(self):
        # check: AC4 — RED at base, GREEN after the fix.
        self._check("wiki")

    def test_sync_completes_and_copies_with_malformed_kms_row(self):
        # check: AC5 — RED at base, GREEN after the fix.
        self._check("kms")


class TestSyncAntijoinIdempotent(unittest.TestCase):
    """AC6 (PRESERVING): well-formed anti-join behavior is unchanged.

    GREEN at base and must stay GREEN — the guard may only EXCLUDE malformed
    rows from the dedupe set; well-formed rows keep their exact copy-once
    semantics (first sync copies, second sync creates 0 new rows).
    """

    def test_second_sync_run_creates_no_new_rows(self):
        from app.models.database import SQLiteConnectionPool, run_migrations
        from app.services.background_tasks import BackgroundProcessor

        tmp_dir = mkdtemp(prefix="o02d-idem-")
        self.addCleanup(shutil.rmtree, tmp_dir, True)
        db_path = str(Path(tmp_dir) / "o02d-idem.db")
        run_migrations(db_path)
        conn = _conn(db_path)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'V1')"
            )
            conn.execute(
                "INSERT INTO wiki_compile_jobs (vault_id, trigger_type, status)"
                " VALUES (1, 'manual', 'pending')"
            )
            conn.execute(
                "INSERT INTO kms_compile_jobs (vault_id, trigger_type, status)"
                " VALUES (1, 'manual', 'pending')"
            )
            conn.commit()
        finally:
            conn.close()

        pool = SQLiteConnectionPool(db_path, max_size=2)
        self.addCleanup(pool.close_all)
        with patch.object(settings, "wiki_kms_job_lease_enabled", True), \
                patch.object(settings, "reindex_job_lease_enabled", True):
            bp = BackgroundProcessor(pool=pool, retry_delay=0.05)
            first = bp._sync_missing_compile_job_rows()
            second = bp._sync_missing_compile_job_rows()

        self.assertGreaterEqual(
            first, 1, "the first sync must copy the well-formed legacy rows"
        )
        self.assertEqual(
            second,
            0,
            "the anti-join must stay idempotent for well-formed rows: a"
            " second sync run creates 0 new jobs rows",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
