"""Issue #559 lease-mode integration coverage (PR-review PRR-002/003/004/005/
006/008 + PRR-007 disposition).

Covers the behaviors the frozen acceptance checks and C05 do not reach:
- graceful shutdown releases a held (unsettled) lease and skips deferred rows
- cancel_pending_jobs cancels durable pending rows in lease mode
- admission rejection in lease mode requeues and is bounded by the cap
- the successful (real file) enqueue -> claim -> complete -> indexed flow
- fenced settlement at the processor level (non-owner write is a no-op)
- boot migration idempotency (second run creates zero rows)
"""

import asyncio
import sqlite3
from pathlib import Path

import pytest

from app.config import settings
from app.models.database import SQLiteConnectionPool, run_migrations
from app.services import background_tasks as bt
from app.services.admission import AdmissionRejected
from app.services.background_tasks import BackgroundProcessor


def _seed(tmp_path: Path) -> tuple[str, sqlite3.Connection]:
    db_path = tmp_path / "lease-int.db"
    run_migrations(str(db_path))
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return str(db_path), conn


def _job_rows(conn: sqlite3.Connection) -> dict[int, dict]:
    return {
        int(row["id"]): dict(row)
        for row in conn.execute(
            "SELECT id, status, worker_id, lease_generation, attempts, "
            "run_after, error FROM jobs"
        ).fetchall()
    }


@pytest.mark.asyncio
async def test_shutdown_releases_running_lease_and_skips_deferred(tmp_path):
    """A held (unsettled) lease is released by shutdown; deferred rows are
    not counted as outstanding work and survive for the next boot."""
    db_path, conn = _seed(tmp_path)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        # A crash-orphan running lease with a FRESH heartbeat: within this
        # test's window nobody settles it (its worker is gone) and the
        # janitor cannot reclaim it (heartbeat is not stale).
        conn.execute(
            "INSERT INTO jobs (queue, payload_json, status, worker_id, "
            "lease_generation, attempts, heartbeat_at, started_at) VALUES "
            "('ingestion', '{}', 'running', 'ghost', 5, 1, "
            "datetime('now'), datetime('now'))"
        )
        conn.execute(
            "INSERT INTO jobs (queue, payload_json, status, run_after) "
            "VALUES ('ingestion', '{}', 'pending', "
            "datetime('now', '+3600 seconds'))"
        )
        conn.commit()
        running_id = int(
            conn.execute(
                "SELECT id FROM jobs WHERE status = 'running'"
            ).fetchone()[0]
        )
        deferred_id = int(
            conn.execute(
                "SELECT id FROM jobs WHERE status = 'pending'"
            ).fetchone()[0]
        )

        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        await asyncio.wait_for(processor.start(), timeout=10)
        # The held lease cannot settle, so the quiescence wait must time out
        # at `timeout` (NOT wait the deferred hour away) and shutdown must
        # proceed; the release sweep then hands the lease back.
        await asyncio.wait_for(processor.stop(timeout=3), timeout=15)

        rows = _job_rows(conn)
        assert rows[running_id]["status"] == "pending"
        assert rows[running_id]["worker_id"] is None
        assert rows[running_id]["lease_generation"] >= 6, (
            "release sweep must bump the generation"
        )
        assert rows[deferred_id]["status"] == "pending"
        assert rows[deferred_id]["run_after"] is not None
    finally:
        pool.close_all()


@pytest.mark.asyncio
async def test_cancel_pending_jobs_cancels_durable_rows(tmp_path):
    """Lease-mode cancellation targets the durable pending rows."""
    db_path, conn = _seed(tmp_path)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        await processor.enqueue(str(tmp_path / "a.pdf"), 1, file_id=101)
        await processor.enqueue(str(tmp_path / "b.pdf"), 1, file_id=102)

        cancelled = processor.cancel_pending_jobs(file_id=101)
        assert cancelled == 1

        rows = {
            int(r["fid"]): r["status"]
            for r in conn.execute(
                "SELECT CAST(json_extract(payload_json, '$.file_id') AS "
                "INTEGER) AS fid, status FROM jobs"
            ).fetchall()
        }
        assert rows[101] == "cancelled"
        assert rows[102] == "pending"
    finally:
        pool.close_all()


@pytest.mark.asyncio
async def test_lease_mode_admission_rejection_requeues_and_cap_settles(
    tmp_path, monkeypatch
):
    """Admission rejection in lease mode must requeue the durable row; the
    bounded retry loop then terminates it at the attempts cap instead of
    leaving a 'running' row stuck under a fresh heartbeat forever."""

    class _RejectingAdmit:
        async def __aenter__(self):
            raise AdmissionRejected("queue_full")

        async def __aexit__(self, *exc_info):
            return False

    class _RejectingController:
        def admit(self, *args, **kwargs):
            return _RejectingAdmit()

    monkeypatch.setattr(bt, "get_admission_controller", lambda: _RejectingController())

    db_path, conn = _seed(tmp_path)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        real_file = tmp_path / "rejected.txt"
        real_file.write_text("data")
        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        await asyncio.wait_for(processor.start(), timeout=10)
        await processor.enqueue(str(real_file), 1, file_id=7)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + 60

        def _terminal():
            row = conn.execute(
                "SELECT status, attempts, error FROM jobs WHERE queue = "
                "'ingestion' AND json_extract(payload_json, '$.file_id') = 7"
            ).fetchone()
            return row is not None and row["status"] == "failed"

        while loop.time() < deadline and not _terminal():
            await asyncio.sleep(0.25)
        assert _terminal(), "admission-rejected job never settled terminally"

        row = conn.execute(
            "SELECT status, attempts, error, worker_id FROM jobs WHERE "
            "json_extract(payload_json, '$.file_id') = 7"
        ).fetchone()
        assert row["attempts"] == settings.jobs_max_attempts
        # terminal fail keeps the failing worker's id (audit trail); only
        # requeue/release/reclaim clear it
        assert row["worker_id"] is not None
        assert row["error"] is not None
        assert row["error"].startswith("attempt_cap_exceeded")
        await asyncio.wait_for(processor.stop(timeout=10), timeout=20)
    finally:
        pool.close_all()


@pytest.mark.asyncio
async def test_lease_mode_successful_file_flows_to_completed(tmp_path, monkeypatch):
    """The success path in lease mode: enqueue -> claim -> process -> files
    'indexed' -> jobs 'completed' (C05 only covers the failing path)."""
    db_path, conn = _seed(tmp_path)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        real_file = tmp_path / "real.txt"
        real_file.write_text("hello")
        conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_size, "
            "status, phase, source) VALUES (1, ?, 'real.txt', 5, 'pending', "
            "'queued', 'upload')",
            (str(real_file),),
        )
        conn.commit()
        file_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        # Enqueue BEFORE start(): the boot migration must see the pending
        # jobs row and NOT create a second row for the same file (its NOT-IN
        # predicate), so the file is claimed and processed exactly once.
        await processor.enqueue(str(real_file), 1, file_id=file_id)
        calls: list[int] = []

        async def fake_process_existing(file_id, file_path, vault_id, **kwargs):
            calls.append(file_id)
            conn.execute(
                "UPDATE files SET status = 'indexed', phase = NULL "
                "WHERE id = ?",
                (file_id,),
            )
            conn.commit()
            return None

        monkeypatch.setattr(
            processor.processor, "process_existing_file", fake_process_existing
        )
        await asyncio.wait_for(processor.start(), timeout=10)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + 60

        def _completed():
            row = conn.execute(
                "SELECT status FROM jobs WHERE id = (SELECT MAX(id) FROM jobs)"
            ).fetchone()
            return row is not None and row["status"] == "completed"

        while loop.time() < deadline and not _completed():
            await asyncio.sleep(0.25)
        row = conn.execute(
            "SELECT status, attempts, worker_id FROM jobs WHERE id = "
            "(SELECT MAX(id) FROM jobs)"
        ).fetchone()
        assert row["status"] == "completed"
        assert row["attempts"] == 1
        assert row["worker_id"] is not None  # completed by its worker
        assert calls == [file_id]
        files_status = conn.execute(
            "SELECT status FROM files WHERE id = ?", (file_id,)
        ).fetchone()["status"]
        assert files_status == "indexed"
        await asyncio.wait_for(processor.stop(timeout=10), timeout=20)
    finally:
        pool.close_all()


@pytest.mark.asyncio
async def test_settle_ingest_job_fenced_off_for_non_owner(tmp_path):
    """Processor-level fencing: a non-owner settlement is a no-op."""
    db_path, conn = _seed(tmp_path)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        processor = BackgroundProcessor(pool=pool)
        conn.execute(
            "INSERT INTO jobs (queue, payload_json, status, worker_id, "
            "lease_generation, attempts, heartbeat_at) VALUES "
            "('ingestion', '{}', 'running', 'owner-a', 4, 1, datetime('now'))"
        )
        conn.commit()
        job_id = int(
            conn.execute("SELECT id FROM jobs").fetchone()[0]
        )
        settled = await processor._settle_ingest_job(job_id, "owner-b", "complete")
        assert settled is False
        row = conn.execute(
            "SELECT status, worker_id, result_json FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        assert row["status"] == "running"
        assert row["worker_id"] == "owner-a"
        assert row["result_json"] == "{}"
    finally:
        pool.close_all()


@pytest.mark.asyncio
async def test_jobs_migration_second_run_creates_zero_rows(tmp_path):
    """Boot migration idempotency: a completed migration re-runs clean
    (structural guarantee backing the crash-resume claim)."""
    db_path, conn = _seed(tmp_path)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        for i in range(2):
            conn.execute(
                "INSERT INTO files (vault_id, file_path, file_name, "
                "file_size, status, phase, source) VALUES (1, ?, ?, 1, "
                "'pending', 'queued', 'upload')",
                (str(tmp_path / f"f{i}.pdf"), f"f{i}.pdf"),
            )
        conn.commit()
        processor = BackgroundProcessor(pool=pool)
        first = await asyncio.to_thread(processor._sync_missing_ingest_job_rows)
        assert first == 2
        second = await asyncio.to_thread(processor._sync_missing_ingest_job_rows)
        assert second == 0
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
    finally:
        pool.close_all()
