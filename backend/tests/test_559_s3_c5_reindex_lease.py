"""Issue #559 stage-3 acceptance check C5 (DISCRIMINATING): the reindex queue
migrates onto the shared DB-claimed lease.

Stage-3 contract under test (issue #559, stages 3/4 + cross-stage migration):

- a reindex job is claimed through the shared lease path (single-statement
  claim on the shared ``jobs`` table / the shared ``JobLease`` primitive), so
  two workers racing for one pending reindex job never both claim it;
- a crashed reindex job (lease expired past the reclaim timeout) is reclaimed
  by the janitor WITHOUT a restart and retried, with retries bounded by the
  attempts cap (at the cap: terminally ``failed`` with the distinctive
  ``lease_attempt_cap_exceeded`` error);
- the ``interrupted`` external status is mapped away: the reindex job status
  endpoint never returns ``interrupted`` — historical ``document_reindex_jobs``
  rows stored as ``interrupted`` are presented as terminal ``failed`` (error
  preserved) after migration;
- the startup routine that marks running reindex jobs ``interrupted`` and the
  shutdown equivalent no longer run when the lease path is enabled.

Fixture strategy mirrors ``tests/test_559_lease_integration.py`` (real schema
via ``run_migrations`` on a temp DB + ``SQLiteConnectionPool`` + a
``BackgroundProcessor`` whose claim/janitor/migration entry points are invoked
directly) and ``tests/test_559_c4_crash_restart_independence.py`` (crashes are
simulated by abandoning worker state and backdating ``heartbeat_at`` — never
by killing processes or sleeping past a timeout).

Expectation: RED at the pre-stage-3 base — the reindex claim is a non-atomic
UPDATE+SELECT inside ``_process_reindex_job``, the janitor only sweeps the
``ingestion`` queue, no reindex boot migration exists, and the status endpoint
returns ``interrupted`` verbatim. GREEN post-fix.
"""

import asyncio
import sqlite3
import unittest
from pathlib import Path
from tempfile import mkdtemp

from app.config import settings
from app.models.database import SQLiteConnectionPool, run_migrations
from app.services.background_tasks import BackgroundProcessor
from app.services.job_lease import JobLease, ensure_jobs_schema

RECLAIM_TIMEOUT_SECONDS = 30
STALE_SECONDS = 120  # well beyond the reclaim timeout
REINDEX_QUEUE = "reindex"


def _connect(db_path):
    # check_same_thread=False: processor helpers execute on the connection via
    # asyncio.to_thread, like the production pool connections they use.
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _make_db(prefix):
    db_path = str(Path(mkdtemp(prefix=prefix)) / "s3-reindex.db")
    run_migrations(db_path)
    return db_path


def _insert_reindex_job(conn, status="pending", error=None, vault_id=None):
    cur = conn.execute(
        "INSERT INTO document_reindex_jobs (vault_id, trigger_type, status, "
        "error, input_json) VALUES (?, 'api', ?, ?, '{}')",
        (vault_id, status, error),
    )
    conn.commit()
    return int(cur.lastrowid)


def _field(row, name):
    """Tolerant field read: sqlite3.Row / mapping / attribute object."""
    try:
        return row[name]
    except (TypeError, KeyError, IndexError):
        return getattr(row, name)


async def _call_claim(processor, worker_id):
    """Invoke the reindex lease claim whether it is sync or async."""
    result = processor._claim_next_reindex_job(worker_id)  # noqa: SLF001
    if asyncio.iscoroutine(result):
        result = await result
    return result


class _ProcessorHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db_path = _make_db("s3c4-")
        self.conn = _connect(self.db_path)
        self.addCleanup(self.conn.close)
        self.pool = SQLiteConnectionPool(self.db_path, max_size=4)
        self.addCleanup(self.pool.close_all)
        self.processor = BackgroundProcessor(pool=self.pool, retry_delay=0.05)


class TestReindexLeaseClaim(_ProcessorHarness):
    # check: C5 (DISCRIMINATING) — the reindex claim is the shared lease's
    # single-statement claim on the shared jobs table: two racing workers
    # never both claim one pending reindex job.

    async def test_pending_reindex_row_claimed_via_shared_lease_exactly_once(self):
        _insert_reindex_job(self.conn)  # pending, as the POST /reindex route seeds it

        # Boot migration first: the pending legacy row must land in the shared
        # jobs table as lease ownership (queue 'reindex', still claimable).
        await self.processor._run_jobs_migration()  # noqa: SLF001
        rows = self.conn.execute(
            "SELECT id, status FROM jobs WHERE queue = ?", (REINDEX_QUEUE,)
        ).fetchall()
        self.assertEqual(
            len(rows),
            1,
            "boot migration must copy the pending reindex row into shared "
            "lease ownership (queue 'reindex')",
        )
        self.assertEqual(rows[0]["status"], "pending")
        migrated_id = int(rows[0]["id"])

        # Two workers race for the single migrated row. Both claims run
        # concurrently (the claim delegates to a worker thread, exactly like
        # the ingestion lease claim), so the single-statement UPDATE...
        # RETURNING is exercised as a real race.
        claims = await asyncio.gather(
            _call_claim(self.processor, "claimant-a"),
            _call_claim(self.processor, "claimant-b"),
        )
        winners = [row for row in claims if row is not None]
        losers = [row for row in claims if row is None]
        self.assertEqual(
            len(winners), 1, f"exactly one racing claimant may win: {claims!r}"
        )
        self.assertEqual(len(losers), 1)
        self.assertEqual(int(_field(winners[0], "id")), migrated_id)
        self.assertEqual(_field(winners[0], "status"), "running")
        self.assertIn(_field(winners[0], "worker_id"), ("claimant-a", "claimant-b"))

        stored = self.conn.execute(
            "SELECT status, worker_id FROM jobs WHERE id = ?", (migrated_id,)
        ).fetchone()
        self.assertEqual(stored["status"], "running")
        self.assertEqual(stored["worker_id"], _field(winners[0], "worker_id"))


class TestReindexCrashReclaimedWithoutRestart(_ProcessorHarness):
    # check: C5 (DISCRIMINATING) — a crashed reindex lease (heartbeat expired
    # past the reclaim timeout) is reclaimed by the janitor without a restart,
    # retried, and bounded by the attempts cap.

    def _enqueue_reindex_lease(self):
        ensure_jobs_schema(self.conn)
        lease = JobLease(self.conn, reclaim_timeout_seconds=RECLAIM_TIMEOUT_SECONDS)
        return lease.enqueue(REINDEX_QUEUE, {"vault_id": None})

    def _crash_claim(self, worker_id):
        """A fresh process claims, then dies: connection closed, no write."""
        conn = _connect(self.db_path)
        lease = JobLease(conn, reclaim_timeout_seconds=RECLAIM_TIMEOUT_SECONDS)
        row = lease.claim(REINDEX_QUEUE, worker_id)
        generation = row["lease_generation"] if row is not None else None
        conn.close()  # hard kill — no terminal write, no further heartbeats
        return row, generation

    def _age_heartbeat(self, job_id, seconds=STALE_SECONDS):
        self.conn.execute(
            "UPDATE jobs SET heartbeat_at = datetime('now', ?) WHERE id = ?",
            (f"-{seconds} seconds", job_id),
        )
        self.conn.commit()

    async def test_crashed_reindex_lease_reclaimed_by_janitor_and_retried(self):
        job_id = self._enqueue_reindex_lease()
        row, generation = self._crash_claim("reindex-crasher")
        self.assertIsNotNone(row)
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()["status"],
            "running",
        )

        self._age_heartbeat(job_id)
        # The janitor — not a process restart — must settle the expired lease.
        # The processor's sweep derives its cutoff from settings; pin it to
        # the file's short reclaim constant so the backdated heartbeat is
        # unambiguously expired.
        previous_timeout = settings.jobs_lease_reclaim_timeout_seconds
        settings.jobs_lease_reclaim_timeout_seconds = RECLAIM_TIMEOUT_SECONDS
        try:
            await self.processor._janitor_sweep_once()  # noqa: SLF001
        finally:
            settings.jobs_lease_reclaim_timeout_seconds = previous_timeout

        stored = self.conn.execute(
            "SELECT status, worker_id, attempts, lease_generation FROM jobs "
            "WHERE id = ?",
            (job_id,),
        ).fetchone()
        self.assertEqual(
            stored["status"],
            "pending",
            "the janitor sweep must reclaim the crashed reindex lease back to "
            "pending (retry) without a restart",
        )
        self.assertIsNone(stored["worker_id"])
        self.assertGreaterEqual(stored["attempts"], 1)
        self.assertGreater(stored["lease_generation"], generation)

        # The reclaimed row is retried: a new worker can claim it again.
        retry_conn = _connect(self.db_path)
        self.addCleanup(retry_conn.close)
        retry_lease = JobLease(
            retry_conn, reclaim_timeout_seconds=RECLAIM_TIMEOUT_SECONDS
        )
        retry_row = retry_lease.claim(REINDEX_QUEUE, "reindex-retryer")
        self.assertIsNotNone(retry_row)
        self.assertEqual(int(retry_row["id"]), job_id)
        self.assertEqual(retry_row["status"], "running")

    async def test_reindex_retries_bounded_by_attempts_cap_terminal_failure(self):
        cap = int(settings.jobs_max_attempts)
        self.assertGreaterEqual(cap, 1)
        job_id = self._enqueue_reindex_lease()

        for attempt in range(1, cap + 1):
            row, _generation = self._crash_claim(f"reindex-crasher-{attempt}")
            self.assertIsNotNone(row, f"attempt {attempt} must be claimable")
            self._age_heartbeat(row["id"])
            # Sweep with the file's short reclaim timeout (see the first
            # janitor test for the settings-pinning rationale).
            previous_timeout = settings.jobs_lease_reclaim_timeout_seconds
            settings.jobs_lease_reclaim_timeout_seconds = RECLAIM_TIMEOUT_SECONDS
            try:
                await self.processor._janitor_sweep_once()  # noqa: SLF001
            finally:
                settings.jobs_lease_reclaim_timeout_seconds = previous_timeout
            status = self.conn.execute(
                "SELECT status FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()["status"]
            if attempt < cap:
                self.assertEqual(
                    status,
                    "pending",
                    f"reclaim below the attempts cap must retry (attempt "
                    f"{attempt}/{cap})",
                )
            else:
                self.assertEqual(status, "failed")

        final = self.conn.execute(
            "SELECT status, error, attempts FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        self.assertEqual(final["status"], "failed")
        self.assertEqual(
            final["error"],
            "lease_attempt_cap_exceeded",
            "the cap must settle the reindex job terminally with the "
            "distinctive error",
        )
        self.assertGreaterEqual(final["attempts"], cap)

        # Terminal: no further claim, no further reclaim resurrection.
        terminal_conn = _connect(self.db_path)
        self.addCleanup(terminal_conn.close)
        self.assertIsNone(
            JobLease(
                terminal_conn, reclaim_timeout_seconds=RECLAIM_TIMEOUT_SECONDS
            ).claim(REINDEX_QUEUE, "post-cap-worker")
        )
        await self.processor._janitor_sweep_once()  # noqa: SLF001
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()["status"],
            "failed",
        )


class TestReindexInterruptedMappedAway(_ProcessorHarness):
    # check: C5 (DISCRIMINATING) — `interrupted` is mapped away: the status
    # endpoint never returns it, and the startup/shutdown routines that wrote
    # it no longer run when the lease path is enabled.

    async def test_status_endpoint_presents_historical_interrupted_row_as_failed(
        self,
    ):
        from app.api.routes.documents import get_reindex_job_status

        job_id = _insert_reindex_job(
            self.conn,
            status="interrupted",
            error="Interrupted by process restart",
        )
        # "after migration": run the boot migration first so either a read-time
        # mapping or a migration rewrite satisfies the contract.
        await self.processor._run_jobs_migration()  # noqa: SLF001

        route_conn = _connect(self.db_path)
        self.addCleanup(route_conn.close)
        response = await get_reindex_job_status(
            job_id=job_id,
            conn=route_conn,
            user={"id": 1, "username": "checker", "role": "superadmin"},
        )
        self.assertNotEqual(
            response.status, "interrupted", "the endpoint must never expose it"
        )
        self.assertEqual(response.status, "failed")
        self.assertEqual(response.error, "Interrupted by process restart")

    async def test_startup_recovery_does_not_mark_running_reindex_interrupted(self):
        _insert_reindex_job(self.conn, status="running")
        # Run the migration first (it opens the barrier the lease-mode recovery
        # phases wait on), then the startup recovery routine directly.
        await self.processor._run_jobs_migration()  # noqa: SLF001
        await self.processor._run_startup_recovery()  # noqa: SLF001

        status = self.conn.execute(
            "SELECT status FROM document_reindex_jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()["status"]
        self.assertNotEqual(
            status,
            "interrupted",
            "with the lease path enabled, startup must not run the "
            "marks-interrupted routine (bt._recover_interrupted_reindex_jobs); "
            "the janitor owns settlement",
        )

    async def test_shutdown_does_not_mark_running_reindex_interrupted(self):
        _insert_reindex_job(self.conn, status="running")
        await asyncio.wait_for(self.processor.start(), timeout=10)
        await asyncio.wait_for(self.processor.stop(timeout=3), timeout=15)

        status = self.conn.execute(
            "SELECT status FROM document_reindex_jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()["status"]
        self.assertNotEqual(
            status,
            "interrupted",
            "with the lease path enabled, shutdown must not run the "
            "marks-interrupted routine (bt._mark_running_reindex_jobs_"
            "interrupted); lease release/reclaim owns settlement",
        )


if __name__ == "__main__":
    unittest.main()
