"""Issue #559 cross-stage acceptance check C7 (DISCRIMINATING): boot-time
migration + rollback reverse sync.

C7 contract under test (issue #559, cross-stage migration):

- boot-time in-flight migration: pending/running rows in ``wiki_compile_jobs``,
  ``kms_compile_jobs`` and ``document_reindex_jobs`` are copied into lease
  ownership in the shared ``jobs`` table, idempotently (running the migration
  twice does not duplicate);
- with the lease path enabled, the workers/janitor do not claim before the
  migration barrier completes (no window where the new path serves rows the
  migration has not copied);
- reverse sync for rollback: with the lease path DISABLED at boot, pending
  jobs that exist only in the shared ``jobs`` table are synced back so the
  legacy claim path can serve them (non-lossy rollback).

Fixture strategy mirrors ``tests/test_559_lease_integration.py`` (processor +
pool on a temp DB).

Expectation: RED at the pre-stage-3/4 base — no wiki/kms/reindex boot
migration, no reverse sync, and no reindex lease claim (NEW-SURFACE
AttributeError for ``_claim_next_reindex_job`` raised inside the test body).
GREEN post-fix.
"""

import asyncio
import json
import sqlite3
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import mkdtemp

from app.config import settings
from app.models.database import SQLiteConnectionPool, run_migrations
from app.services.background_tasks import BackgroundProcessor
from app.services.job_lease import ensure_jobs_schema


@contextmanager
def _setting_overridden(name, value):
    """Toggle a settings field; a no-op while the field does not exist.

    pydantic models reject setattr of unknown fields, so at the pre-change
    base (fields absent) this yields without touching anything — the
    surrounding check then fails for its own behavioral reason.
    """
    sentinel = object()
    previous = getattr(settings, name, sentinel)
    if previous is sentinel:
        yield
        return
    setattr(settings, name, value)
    try:
        yield
    finally:
        setattr(settings, name, previous)


REINDEX_QUEUE = "reindex"
WIKI_QUEUE = "wiki"
KMS_QUEUE = "kms"


def _connect(db_path):
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _make_db(prefix):
    db_path = str(Path(mkdtemp(prefix=prefix)) / "s234.db")
    run_migrations(db_path)
    conn = _connect(db_path)
    conn.execute(
        "INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 's234-vault')"
    )
    conn.commit()
    return db_path, conn


def _insert_legacy_job(conn, table, status):
    cur = conn.execute(
        f"INSERT INTO {table} (vault_id, trigger_type, status) "  # nosec B608
        "VALUES (1, 'manual', ?)",
        (status,),
    )
    conn.commit()
    return int(cur.lastrowid)


def _plant_jobs_row(conn, queue, payload, status="pending"):
    """Plant one row in the shared jobs table (fixture setup only)."""
    ensure_jobs_schema(conn)
    cur = conn.execute(
        "INSERT INTO jobs (queue, payload_json, status) VALUES (?, ?, ?)",
        (queue, json.dumps(payload), status),
    )
    conn.commit()
    return int(cur.lastrowid)


def _queue_count(conn, queue):
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE queue = ?", (queue,)
        ).fetchone()[0]
    )


class _ProcessorHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db_path, self.conn = _make_db("s234-")
        self.addCleanup(self.conn.close)
        self.pool = SQLiteConnectionPool(self.db_path, max_size=4)
        self.addCleanup(self.pool.close_all)
        self.processor = BackgroundProcessor(pool=self.pool, retry_delay=0.05)


class TestBootMigrationIntoLeaseOwnership(_ProcessorHarness):
    # check: C7 (DISCRIMINATING) — pending/running rows of the three legacy
    # job tables are copied into lease ownership in the shared jobs table,
    # idempotently.

    async def test_pending_rows_from_all_three_tables_copied_idempotently(self):
        _insert_legacy_job(self.conn, "wiki_compile_jobs", "pending")
        _insert_legacy_job(self.conn, "kms_compile_jobs", "pending")
        _insert_legacy_job(self.conn, "document_reindex_jobs", "pending")

        await self.processor._run_jobs_migration()  # noqa: SLF001

        for queue in (WIKI_QUEUE, KMS_QUEUE, REINDEX_QUEUE):
            self.assertEqual(
                _queue_count(self.conn, queue),
                1,
                f"boot migration must copy the pending legacy row into "
                f"lease ownership (queue {queue!r})",
            )
        copied = self.conn.execute(
            "SELECT status, payload_json FROM jobs WHERE queue = ?",
            (WIKI_QUEUE,),
        ).fetchone()
        self.assertEqual(copied["status"], "pending", "still claimable")
        payload = json.loads(copied["payload_json"])
        self.assertEqual(payload.get("vault_id"), 1)
        self.assertEqual(payload.get("trigger_type"), "manual")

        # Idempotent: a second boot migration run must not duplicate rows.
        await self.processor._run_jobs_migration()  # noqa: SLF001
        for queue in (WIKI_QUEUE, KMS_QUEUE, REINDEX_QUEUE):
            self.assertEqual(
                _queue_count(self.conn, queue),
                1,
                f"running the migration twice must not duplicate queue "
                f"{queue!r} rows",
            )

    async def test_running_legacy_rows_are_also_copied(self):
        _insert_legacy_job(self.conn, "wiki_compile_jobs", "running")

        await self.processor._run_jobs_migration()  # noqa: SLF001

        self.assertEqual(
            _queue_count(self.conn, WIKI_QUEUE),
            1,
            "a crashed-mid-run (running) legacy row must also gain lease "
            "ownership so the janitor — not a restart — settles it",
        )
        copied = self.conn.execute(
            "SELECT status FROM jobs WHERE queue = ?", (WIKI_QUEUE,)
        ).fetchone()
        self.assertIn(copied["status"], ("pending", "running"))


class TestMigrationBarrier(_ProcessorHarness):
    # check: C7 (DISCRIMINATING) — with the lease path enabled, the new claim
    # path does not serve rows before the migration barrier completes.
    # NEW-SURFACE: pins ``BackgroundProcessor._claim_next_reindex_job`` (the
    # reindex lease claim, mirroring ``_claim_next_ingest_job``), which cannot
    # exist at the base — clean AttributeError inside the test body.

    async def test_claim_does_not_serve_reindex_rows_before_the_barrier(self):
        _plant_jobs_row(
            self.conn, REINDEX_QUEUE, {"vault_id": None, "legacy_job_id": 1}
        )

        # The processor was constructed but not started: the boot migration
        # barrier is still closed, so the claim must not serve the row.
        try:
            claimed = await asyncio.wait_for(
                self.processor._claim_next_reindex_job("early-worker"),  # noqa: SLF001
                timeout=2.0,
            )
        except asyncio.TimeoutError:
            claimed = None  # a barrier-blocked claim is equally correct
        self.assertIsNone(
            claimed,
            "the reindex lease claim must not serve rows before the boot "
            "migration barrier completes",
        )

        self.processor._jobs_barrier.set()  # noqa: SLF001
        result = self.processor._claim_next_reindex_job("late-worker")  # noqa: SLF001
        if asyncio.iscoroutine(result):
            result = await result
        self.assertIsNotNone(result, "after the barrier the row is claimable")
        self.assertEqual(result["status"], "running")


class TestReverseSyncForRollback(_ProcessorHarness):
    # check: C7 (DISCRIMINATING) — with the lease path DISABLED at boot,
    # pending jobs that exist only in the shared jobs table are synced back
    # into the legacy tables so the legacy claim path can serve them.

    async def test_lease_disabled_boot_syncs_shared_pending_rows_back(self):
        # Pending work that exists ONLY in the shared jobs table (the state a
        # flag-flip rollback starts from).
        _plant_jobs_row(self.conn, REINDEX_QUEUE, {"vault_id": None})
        _plant_jobs_row(
            self.conn,
            WIKI_QUEUE,
            {"vault_id": 1, "trigger_type": "manual", "input_json": {}},
        )

        with _setting_overridden("reindex_job_lease_enabled", False), _setting_overridden(
            "wiki_kms_job_lease_enabled", False
        ):
            rollback_processor = BackgroundProcessor(
                pool=self.pool, retry_delay=0.05
            )
            await asyncio.wait_for(rollback_processor.start(), timeout=10)
            try:
                startup_task = getattr(
                    rollback_processor, "_startup_recovery_task", None
                )
                if startup_task is not None:
                    await asyncio.wait_for(
                        asyncio.shield(startup_task), timeout=5
                    )
                # Boot-time reverse sync is detached; poll briefly for it.
                deadline = asyncio.get_running_loop().time() + 3.0
                while asyncio.get_running_loop().time() < deadline:
                    if (
                        self.conn.execute(
                            "SELECT COUNT(*) FROM document_reindex_jobs"
                        ).fetchone()[0]
                        >= 1
                        and self.conn.execute(
                            "SELECT COUNT(*) FROM wiki_compile_jobs"
                        ).fetchone()[0]
                        >= 1
                    ):
                        break
                    await asyncio.sleep(0.05)
            finally:
                await asyncio.wait_for(
                    rollback_processor.stop(timeout=3), timeout=15
                )

        self.assertGreaterEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM document_reindex_jobs"
            ).fetchone()[0],
            1,
            "a lease-disabled boot must sync shared pending reindex rows "
            "back into document_reindex_jobs so the legacy claim path can "
            "serve them",
        )
        self.assertGreaterEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM wiki_compile_jobs"
            ).fetchone()[0],
            1,
            "a lease-disabled boot must sync shared pending wiki rows back "
            "into wiki_compile_jobs so the legacy claim path can serve them",
        )


if __name__ == "__main__":
    unittest.main()
