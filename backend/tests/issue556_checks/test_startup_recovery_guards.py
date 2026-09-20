from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from app.models.database import SQLiteConnectionPool, init_db
from app.services import enrichment_state as est
from app.services.background_tasks import BackgroundProcessor, ReindexTaskItem


class _Cursor:
    def __init__(self, rows=(), *, rowcount=0):
        self._rows = list(rows)
        self.rowcount = rowcount

    def fetchall(self):
        return list(self._rows)


class _Connection:
    def __init__(self, pending=(), processing=()):
        self.pending = list(pending)
        self.processing = list(processing)
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        if "status = 'pending'" in sql:
            return _Cursor(self.pending)
        if "status = 'processing'" in sql:
            return _Cursor(self.processing)
        if "document_reindex_jobs" in sql and sql.lstrip().upper().startswith("UPDATE"):
            return _Cursor(rowcount=1)
        if "document_reindex_jobs" in sql:
            return _Cursor()
        return _Cursor()

    def commit(self):
        return None


class _Pool:
    def __init__(self, *, pending=(), processing=()):
        self.connection_obj = _Connection(pending, processing)

    def connection(self):
        return self.connection_obj

    # #645: the recovery sweeps check out via the async CM.
    def connection_async(self):
        return self.connection_obj


async def _stop(processor: BackgroundProcessor) -> None:
    await processor.stop(timeout=0.2)
    tasks = list(processor._worker_tasks)
    for name in (
        "_enrichment_worker_task",
        "_atom_enrichment_worker_task",
        "_reindex_worker_task",
        "_retry_scheduler_task",
        "_startup_recovery_task",
        "_vector_delete_sweep_task",
        "_artifact_delete_sweep_task",
        "_orphan_rescan_task",
    ):
        task = getattr(processor, name, None)
        if isinstance(task, asyncio.Task):
            tasks.append(task)
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_boot_recovery_does_not_steal_row_claimed_after_select(tmp_path: Path):
    file_path = tmp_path / "live.txt"
    file_path.write_text("payload", encoding="utf-8")
    processor = BackgroundProcessor()
    processor.processor.pool = _Pool(
        pending=[(11, str(file_path), 1, "upload")],
    )
    claim_gate = asyncio.Event()
    original_claim = processor._claim_recovery_file
    enqueued = []

    async def gated_claim(file_id):
        await claim_gate.wait()
        return await original_claim(file_id)

    async def record_enqueue(**kwargs):
        enqueued.append(kwargs)

    processor._claim_recovery_file = gated_claim
    processor.enqueue = record_enqueue
    recovery = asyncio.create_task(
        processor._recover_stranded_pending_rows(require_older_than_minutes=None)
    )
    await asyncio.sleep(0)
    async with processor._active_file_ids_lock:
        processor._active_file_ids.add(11)
    claim_gate.set()
    await recovery

    assert enqueued == []
    await _stop(processor)


@pytest.mark.asyncio
async def test_reindex_worker_waits_for_startup_recovery_gate():
    processor = BackgroundProcessor()
    processor._reindex_start_gate.clear()
    processed = []
    processed_event = asyncio.Event()

    async def record_reindex(job_id):
        processed.append(job_id)
        processed_event.set()

    processor._process_reindex_job = record_reindex
    await processor.reindex_queue.put(ReindexTaskItem(job_id=19))
    worker = asyncio.create_task(processor._reindex_worker_loop())
    await asyncio.sleep(0)
    assert not worker.done()
    assert processed == []

    processor._reindex_start_gate.set()
    await asyncio.wait_for(processed_event.wait(), timeout=1.0)
    assert processed == [19]
    processor.shutdown_event.set()
    await asyncio.wait_for(worker, timeout=1.0)


@pytest.mark.asyncio
async def test_cancelled_startup_recovery_opens_reindex_gate():
    processor = BackgroundProcessor()
    processor._reindex_start_gate.clear()
    phase_started = asyncio.Event()

    async def blocked_phase(*_args, **_kwargs):
        phase_started.set()
        await asyncio.Event().wait()

    processor._recover_stranded_pending_rows = blocked_phase
    recovery = asyncio.create_task(processor._run_startup_recovery())
    await asyncio.wait_for(phase_started.wait(), timeout=1.0)
    assert not processor._reindex_start_gate.is_set()

    recovery.cancel()
    with pytest.raises(asyncio.CancelledError):
        await recovery
    assert processor._reindex_start_gate.is_set()


@pytest.mark.asyncio
async def test_duplicate_file_queue_ownership_is_ref_counted():
    processor = BackgroundProcessor()

    async def no_op(_task):
        return None

    processor._process_task = no_op
    assert await processor.enqueue("same.txt", 1, file_id=7)
    assert await processor.enqueue("same.txt", 1, file_id=7)
    assert processor._queued_file_ids == {7: 2}
    assert not await processor._claim_recovery_file(7)

    await processor._process_task_wrapper(await processor.queue.get())
    assert processor._queued_file_ids == {7: 1}
    assert not await processor._claim_recovery_file(7)
    await processor._process_task_wrapper(await processor.queue.get())
    assert processor._queued_file_ids == {}
    await _stop(processor)


@pytest.mark.asyncio
async def test_enqueue_reports_recovery_reservation_noop():
    processor = BackgroundProcessor()
    assert await processor._claim_recovery_file(7)
    try:
        assert await processor.enqueue("same.txt", 1, file_id=7) is False
        assert processor.queue.qsize() == 0
    finally:
        await processor._release_recovery_file(7)
        await _stop(processor)


@pytest.mark.asyncio
async def test_reindex_enqueue_reserves_job_ids():
    processor = BackgroundProcessor()
    await processor.enqueue_reindex(19)
    await processor.enqueue_reindex(19)
    assert processor.reindex_queue.qsize() == 1
    assert processor._reindex_job_ids == {19}
    processor.reindex_queue.get_nowait()
    processor.reindex_queue.task_done()
    processor._reindex_job_ids.clear()
    await _stop(processor)


def _seed_recovery_db(tmp_path: Path) -> tuple[SQLiteConnectionPool, sqlite3.Connection]:
    db_path = tmp_path / "recovery.db"
    init_db(str(db_path))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("INSERT INTO vaults (name) VALUES ('v')")
    conn.commit()
    return SQLiteConnectionPool(str(db_path), max_size=2), conn


@pytest.mark.asyncio
async def test_startup_cutoff_excludes_new_enrichment_rows(tmp_path: Path):
    pool, conn = _seed_recovery_db(tmp_path)
    try:
        conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_size, status, "
            "enrichment_status, enrichment_updated_at) VALUES (1, 'old', 'old', 1, "
            "'indexed', 'processing', '2000-01-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_size, status, "
            "enrichment_status, enrichment_updated_at) VALUES (1, 'new', 'new', 1, "
            "'indexed', 'processing', '2999-01-01T00:00:00+00:00')"
        )
        conn.commit()
        processor = BackgroundProcessor(pool=pool)
        await processor._recover_stranded_enrichment_rows(
            startup_cutoff="2025-01-01T00:00:00+00:00"
        )
        rows = conn.execute(
            "SELECT file_path, enrichment_status FROM files ORDER BY id"
        ).fetchall()
        assert [(row["file_path"], row["enrichment_status"]) for row in rows] == [
            ("old", "error"),
            ("new", "processing"),
        ]
    finally:
        conn.close()
        pool.close_all()


@pytest.mark.asyncio
async def test_atom_recovery_cutoff_preserves_current_running_claim(tmp_path: Path):
    pool, conn = _seed_recovery_db(tmp_path)
    try:
        for file_name, generation, started_at in (
            ("old", "old-gen", "2000-01-01T00:00:00+00:00"),
            ("new", "new-gen", "2999-01-01T00:00:00+00:00"),
        ):
            conn.execute(
                "INSERT INTO files (vault_id, file_path, file_name, file_size, status, "
                "active_generation_hash) VALUES (1, ?, ?, 1, 'indexed', ?)",
                (file_name, file_name, generation),
            )
            file_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            atom_id = conn.execute(
                "INSERT INTO document_atoms (atom_id, schema_version, file_id, "
                "generation_hash, ordinal, kind) VALUES (?, 1, ?, ?, 0, 'image') "
                "RETURNING id",
                (f"{file_name}-atom", file_id, generation),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO ingestion_stage_states (file_id, atom_id, generation_hash, "
                "stage, status, input_fingerprint, started_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (file_id, atom_id, generation, est.ENRICH_STAGE, est.RUNNING, "fp", started_at),
            )
        conn.commit()
        processor = BackgroundProcessor(pool=pool)
        await processor._recover_stranded_atom_enrichment_rows(
            startup_cutoff="2025-01-01T00:00:00+00:00"
        )
        rows = conn.execute(
            "SELECT f.file_name, s.status FROM files f JOIN ingestion_stage_states s "
            "ON s.file_id = f.id ORDER BY f.id"
        ).fetchall()
        assert [(row["file_name"], row["status"]) for row in rows] == [
            ("old", est.PENDING),
            ("new", est.RUNNING),
        ]
    finally:
        conn.close()
        pool.close_all()


@pytest.mark.asyncio
async def test_missing_file_recovery_yields_to_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    missing = [
        (index, str(tmp_path / f"missing-{index}.txt"), 1, "upload")
        for index in range(64)
    ]
    processor = BackgroundProcessor()
    processor.processor.pool = _Pool(processing=missing)
    heartbeat = asyncio.Event()
    recovery_finished = asyncio.Event()
    recovery_yielded = asyncio.Event()
    release_yield = asyncio.Event()

    real_sleep = asyncio.sleep

    async def observe_sleep(delay):
        if delay == 0:
            recovery_yielded.set()
        await real_sleep(delay)
        if delay == 0:
            await release_yield.wait()

    monkeypatch.setattr("app.services.background_tasks.asyncio.sleep", observe_sleep)

    async def observe():
        await recovery_yielded.wait()
        heartbeat.set()

    async def recover():
        try:
            await processor._recover_stranded_pending_rows(
                require_older_than_minutes=None
            )
        finally:
            recovery_finished.set()

    recovery = asyncio.create_task(recover())
    observer = asyncio.create_task(observe())
    await asyncio.wait_for(heartbeat.wait(), timeout=1.0)
    assert not recovery_finished.is_set(), (
        "heartbeat must be observed at a cooperative recovery yield, before "
        "the missing-file sweep completes"
    )
    release_yield.set()
    await recovery
    await observer

    assert heartbeat.is_set()
    await _stop(processor)
