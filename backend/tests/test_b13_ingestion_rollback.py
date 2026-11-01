"""Issue #702 final-critic regression: the ingestion rollback leg.

P02-SK2-09's ingestion half: when the ingestion lease switch is disabled at
boot, files rows owning non-terminal ingestion jobs are reset to the legacy
recovery entry state (collected BEFORE the jobs rows are settled), and the
abandoned jobs rows settle terminally with error='lease_disabled_rollback'
so they can never double-serve after a re-enable. New behavior in #702 —
this file is its executed evidence (final-critic finding 1).
"""

import json

from app.config import settings
from app.models.database import SQLiteConnectionPool, run_migrations
from app.services.background_tasks import BackgroundProcessor
from app.services.job_lease import ensure_jobs_schema


def _setting_overridden(name, value):
    from contextlib import contextmanager

    @contextmanager
    def _ctx():
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

    return _ctx()


async def test_ingestion_rollback_leg_resets_files_and_settles_jobs(tmp_path):
    db_path = str(tmp_path / "b13-ingest-rollback.db")
    run_migrations(db_path)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        with pool.connection() as conn:
            conn.row_factory = None
            cur = conn.execute(
                "INSERT INTO files (vault_id, file_path, file_name, "
                "file_size, status, phase, source) VALUES "
                "(1, 'uploads/b13-rollback.txt', 'b13-rollback.txt', 1, "
                "'processing', NULL, 'upload')"
            )
            file_id = int(cur.lastrowid)
            ensure_jobs_schema(conn)
            conn.execute(
                "INSERT INTO jobs (queue, payload_json, status, worker_id, "
                "attempts) VALUES ('ingestion', ?, 'running', "
                "'b13-dead-worker', 2)",
                (
                    json.dumps(
                        {
                            "file_id": file_id,
                            "vault_id": 1,
                            "file_path": "uploads/b13-rollback.txt",
                        }
                    ),
                ),
            )
            conn.commit()

        with _setting_overridden("ingestion_job_lease_enabled", False):
            processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
            processor._reverse_sync_disabled_queues()  # noqa: SLF001

        with pool.connection() as conn:
            conn.row_factory = None
            files_row = conn.execute(
                "SELECT status, phase, error_message FROM files WHERE id = ?",
                (file_id,),
            ).fetchone()
            jobs_rows = conn.execute(
                "SELECT id, status, error, attempts FROM jobs "
                "WHERE queue = 'ingestion'"
            ).fetchall()

        assert files_row[0] == "pending", (
            "the rollback boot must reset the processing files row to the "
            "legacy recovery entry state"
        )
        assert files_row[1] == "queued"
        assert files_row[2] is None
        assert len(jobs_rows) == 1, "the abandoned jobs row must settle, not clone"
        settled = jobs_rows[0]
        assert settled[1] == "failed"
        assert settled[2] == "lease_disabled_rollback"
        assert settled[3] == 2, "settling must not mutate the attempt ledger"

        # Re-enable semantics: the settled row is terminal (never claimable
        # again); the next lease boot mints at most ONE fresh job for the
        # reset file — single-serve, no double-serve from the stale row.
        with pool.connection() as conn:
            conn.row_factory = None
            processor._sync_missing_ingest_job_rows()  # noqa: SLF001
            claimable = conn.execute(
                "SELECT COUNT(*), MIN(attempts) FROM jobs "
                "WHERE queue = 'ingestion' AND status = 'pending'"
            ).fetchone()
        assert claimable[0] == 1, (
            "exactly one fresh claimable job may exist after re-enable "
            f"(got {claimable[0]})"
        )
        assert claimable[1] == 0, "the fresh mint must start at zero attempts"
    finally:
        pool.close_all()
