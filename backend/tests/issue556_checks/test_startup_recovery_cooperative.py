from __future__ import annotations

import asyncio
import threading

import pytest

from app.models.database import SQLiteConnectionPool
from app.services import background_tasks
from app.services.background_tasks import BackgroundProcessor


class _Cursor:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _RecoveryConnection:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.exited = threading.Event()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.exited.set()
        return False

    def execute(self, sql, _params=()):
        if "FROM ingestion_stage_states s" in sql and "s.status IN" in sql:
            return _Cursor(self.rows)
        if "FROM files f" in sql and "NOT EXISTS" in sql:
            return _Cursor()
        if "active_generation_hash" in sql:
            return _Cursor([{"active_generation_hash": "generation"}])
        return _Cursor()


class _RecoveryPool:
    def __init__(self, rows=()):
        self.connection_obj = _RecoveryConnection(rows)

    def connection(self):
        return self.connection_obj


@pytest.mark.asyncio
async def test_atom_recovery_yields_before_large_backlog_finishes(
    monkeypatch: pytest.MonkeyPatch,
):
    rows = [
        {"file_id": index, "vault_id": 1, "file_hash": f"hash-{index}"}
        for index in range(64)
    ]
    processor = BackgroundProcessor()
    processor.processor.pool = _RecoveryPool(rows)
    processor.multimodal_service = object()
    processor._should_enqueue_atom_enrichment = lambda *_args: True

    yield_seen = asyncio.Event()
    recovery_finished = asyncio.Event()
    real_sleep = asyncio.sleep

    async def observe_sleep(delay):
        if delay == 0:
            yield_seen.set()
        await real_sleep(delay)

    monkeypatch.setattr(background_tasks.asyncio, "sleep", observe_sleep)
    monkeypatch.setattr(
        background_tasks, "RECOVERY_COOPERATIVE_YIELD_EVERY", 4
    )

    async def recover():
        try:
            await processor._resume_pending_atom_enrichment()
        finally:
            recovery_finished.set()

    recovery = asyncio.create_task(recover())
    await asyncio.wait_for(yield_seen.wait(), timeout=1.0)
    assert not recovery_finished.is_set()
    await recovery
    assert processor.atom_enrichment_queue.qsize() == len(rows)


@pytest.mark.asyncio
async def test_artifact_recovery_offloads_synchronous_sweep(
    monkeypatch: pytest.MonkeyPatch,
):
    processor = BackgroundProcessor()
    processor.processor.pool = _RecoveryPool()
    sweep_started = threading.Event()
    sweep_finished = threading.Event()
    release_sweep = threading.Event()

    def slow_sweep(_conn):
        sweep_started.set()
        try:
            assert release_sweep.wait(timeout=2.0)
            return 0, 0
        finally:
            sweep_finished.set()

    monkeypatch.setattr(
        "app.services.artifact_store.sweep_pending_asset_deletes", slow_sweep
    )

    sweep = asyncio.create_task(processor.sweep_pending_artifact_deletes())
    assert await asyncio.to_thread(sweep_started.wait, 1.0)
    heartbeat = asyncio.Event()
    asyncio.get_running_loop().call_soon(heartbeat.set)
    await asyncio.wait_for(heartbeat.wait(), timeout=1.0)
    assert not sweep.done()

    sweep.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sweep
    assert not processor.processor.pool.connection_obj.exited.is_set()

    release_sweep.set()
    assert await asyncio.to_thread(sweep_finished.wait, 1.0)
    assert processor.processor.pool.connection_obj.exited.is_set()


@pytest.mark.asyncio
async def test_cancelled_artifact_sweep_does_not_hold_real_pool_connection(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
):
    pool = SQLiteConnectionPool(str(tmp_path / "artifact-sweep.db"), max_size=1)
    processor = BackgroundProcessor(pool=pool)
    sweep_started = threading.Event()
    sweep_finished = threading.Event()
    release_sweep = threading.Event()

    def slow_sweep(_conn):
        sweep_started.set()
        try:
            assert release_sweep.wait(timeout=2.0)
            return 0, 0
        finally:
            sweep_finished.set()

    monkeypatch.setattr(
        "app.services.artifact_store.sweep_pending_asset_deletes", slow_sweep
    )

    sweep = asyncio.create_task(processor.sweep_pending_artifact_deletes())
    tracked_workers = ()
    try:
        assert await asyncio.to_thread(sweep_started.wait, 1.0)
        sweep.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sweep

        # The outer coroutine is cancelled while the thread remains blocked;
        # closing the application pool must not close the worker's connection.
        tracked_workers = tuple(processor._artifact_sweep_tasks)
        assert len(tracked_workers) == 1
        pool.close_all()
    finally:
        release_sweep.set()
        assert await asyncio.to_thread(sweep_finished.wait, 1.0)
        if tracked_workers:
            await asyncio.gather(*tracked_workers, return_exceptions=True)

    assert all(worker.done() for worker in tracked_workers)


@pytest.mark.asyncio
async def test_stop_drains_tracked_artifact_sweep_tasks_without_waiting_for_thread(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
):
    pool = SQLiteConnectionPool(str(tmp_path / "artifact-stop.db"), max_size=1)
    processor = BackgroundProcessor(pool=pool)
    sweep_started = threading.Event()
    sweep_finished = threading.Event()
    release_sweep = threading.Event()

    def slow_sweep(_conn):
        sweep_started.set()
        try:
            assert release_sweep.wait(timeout=2.0)
            return 0, 0
        finally:
            sweep_finished.set()

    monkeypatch.setattr(
        "app.services.artifact_store.sweep_pending_asset_deletes", slow_sweep
    )

    sweep = asyncio.create_task(processor.sweep_pending_artifact_deletes())
    try:
        assert await asyncio.to_thread(sweep_started.wait, 1.0)
        processor._running = True
        await asyncio.wait_for(processor.stop(timeout=0.2), timeout=1.0)
        assert sweep.done()
        assert not processor._artifact_sweep_tasks
    finally:
        release_sweep.set()
        assert await asyncio.to_thread(sweep_finished.wait, 1.0)
        await asyncio.gather(sweep, return_exceptions=True)
        pool.close_all()
