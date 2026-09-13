from __future__ import annotations

import asyncio
import contextlib
import inspect
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.lifespan import _safe_await
from app.services.background_tasks import BackgroundProcessor, TaskItem
from app.services.document_processor import DocumentProcessingError

_PERIODIC = (
    "_vector_delete_sweep_task",
    "_artifact_delete_sweep_task",
    "_orphan_rescan_task",
)
_RECOVERY = (
    "_recover_stranded_pending_rows",
    "_recover_interrupted_reindex_jobs",
    "_recover_stranded_enrichment_rows",
    "_recover_stranded_atom_enrichment_rows",
    "_resume_pending_atom_enrichment",
    "retry_pending_vector_deletes",
    "sweep_pending_artifact_deletes",
)


def _owned_tasks(processor: BackgroundProcessor) -> list[asyncio.Task]:
    tasks = list(processor._worker_tasks)
    for name in (
        "_enrichment_worker_task",
        "_atom_enrichment_worker_task",
        "_reindex_worker_task",
        "_retry_scheduler_task",
        "_startup_recovery_task",
        *_PERIODIC,
    ):
        task = getattr(processor, name, None)
        if isinstance(task, asyncio.Task):
            tasks.append(task)
    return tasks


async def _stop(processor: BackgroundProcessor) -> None:
    with contextlib.suppress(Exception):
        await asyncio.wait_for(processor.stop(timeout=0.2), timeout=2.0)
    tasks = _owned_tasks(processor)
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _no_op(*_args, **_kwargs):
    return None


def _disable_other_recovery(processor: BackgroundProcessor, first=None) -> None:
    for name in _RECOVERY[1:]:
        setattr(processor, name, _no_op)
    if first is not None:
        setattr(processor, _RECOVERY[0], first)


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return list(self.rows)


class _Connection:
    def __init__(self, pending):
        self.pending = pending

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, *_args):
        return _Cursor(self.pending if "status = 'pending'" in sql else [])


class _Pool:
    def __init__(self, pending):
        self.connection_obj = _Connection(pending)

    def connection(self):
        return self.connection_obj


@pytest.mark.asyncio
async def test_lifespan_safe_await_timeout_is_nonfatal():
    """The lifespan wrapper must swallow a timed-out processor start."""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow_start():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    await _safe_await(slow_start(), "Background processor start", timeout=0.01)

    assert started.is_set()
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_start_returns_within_10s_while_boot_recovery_remains_active():
    processor = BackgroundProcessor(max_retries=0, retry_delay=0.01)
    started, finished, cancelled, release = (asyncio.Event() for _ in range(4))

    async def blocked(*_args, **_kwargs):
        started.set()
        try:
            await release.wait()
            finished.set()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    _disable_other_recovery(processor, blocked)
    try:
        returned = True
        try:
            await asyncio.wait_for(processor.start(), timeout=10.0)
        except asyncio.TimeoutError:
            returned = False
        await asyncio.wait_for(started.wait(), timeout=1.0)
        assert returned, "AC1: start timed out instead of returning during boot recovery"
        assert not finished.is_set(), "AC1: boot recovery unexpectedly finished before start returned"
    finally:
        release.set()
        await _stop(processor)
        assert cancelled.is_set() or finished.is_set()
        assert all(task.done() for task in _owned_tasks(processor))


@pytest.mark.asyncio
async def test_periodic_sweeps_exist_before_boot_recovery_completes():
    processor = BackgroundProcessor(max_retries=0, retry_delay=0.01)
    started, finished, release = (asyncio.Event() for _ in range(3))

    async def blocked(*_args, **_kwargs):
        started.set()
        await release.wait()
        finished.set()

    _disable_other_recovery(processor, blocked)
    start_task = asyncio.create_task(processor.start())
    try:
        await asyncio.wait_for(started.wait(), timeout=1.0)
        await asyncio.sleep(0)
        missing = [name for name in _PERIODIC if not isinstance(getattr(processor, name, None), asyncio.Task)]
        assert not missing, "AC2: periodic sweep tasks were not published before recovery completed"
        assert not finished.is_set(), "AC2: recovery fixture unexpectedly finished before task publication"
    finally:
        release.set()
        if not start_task.done():
            start_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await start_task
        await _stop(processor)
        assert all(task.done() for task in _owned_tasks(processor))


@pytest.mark.asyncio
async def test_workers_precede_recovery_and_drain_oversized_backlog(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "ingestion_queue_max_size", 2)
    monkeypatch.setattr(settings, "ingestion_worker_count", 1)
    processor = BackgroundProcessor(max_retries=0, retry_delay=0.01)
    workers_seen, processed, drained = [], [], asyncio.Event()
    with tempfile.TemporaryDirectory() as temp_dir:
        paths = [str(Path(temp_dir) / f"stranded-{i}") for i in range(4)]

        async def process_file(file_path, **_kwargs):
            processed.append(file_path)
            if len(processed) == len(paths):
                drained.set()

        async def enqueue_backlog(*_args, **_kwargs):
            workers_seen.append(len(processor._worker_tasks))
            for path in paths:
                await processor.queue.put(TaskItem(file_path=path, vault_id=1))

        processor.processor.process_file = process_file
        _disable_other_recovery(processor, enqueue_backlog)
        try:
            await processor.start()
            await asyncio.wait_for(drained.wait(), timeout=1.0)
            assert workers_seen == [1], "AC3: recovery ran before the worker was created"
            assert processed == paths, "AC3: worker did not drain the oversized backlog in order"
        finally:
            await _stop(processor)
            assert all(task.done() for task in _owned_tasks(processor))


@pytest.mark.asyncio
async def test_recovery_reads_maintenance_once_and_enqueue_stays_gated():
    pending = []
    with tempfile.TemporaryDirectory() as temp_dir:
        for index in range(3):
            path = Path(temp_dir) / f"pending-{index}.txt"
            path.write_text("x", encoding="utf-8")
            pending.append((index + 1, str(path), 1, "upload"))

        class Maintenance:
            enabled = False
            calls = 0

            def get_flag(self):
                self.calls += 1
                return SimpleNamespace(enabled=self.enabled)

        maintenance = Maintenance()
        processor = BackgroundProcessor(maintenance_service=maintenance)
        processor.processor.pool = _Pool(pending)
        enqueued = []
        real_enqueue = processor.enqueue

        async def record_enqueue(*args, **kwargs):
            enqueued.append((args, kwargs))
            return await real_enqueue(*args, **kwargs)

        processor.enqueue = record_enqueue
        try:
            await processor._recover_stranded_pending_rows(
                require_older_than_minutes=None, ignore_active=set()
            )
            assert maintenance.calls == 1, "AC5: recovery read the maintenance flag more than once"
            assert [call[1]["file_id"] for call in enqueued] == [1, 2, 3]
            assert all(call[1]["_maintenance_checked"] for call in enqueued)

            maintenance.enabled = True
            await processor._recover_stranded_pending_rows(
                require_older_than_minutes=None, ignore_active=set()
            )
            assert maintenance.calls == 2, "AC5: fresh recovery sweep did not read maintenance once"
            assert len(enqueued) == 3, "AC5: maintenance-enabled recovery enqueued stranded rows"

            with pytest.raises(DocumentProcessingError):
                await processor.enqueue(file_path="ordinary.txt", vault_id=1)
            assert maintenance.calls == 3, "AC5: ordinary enqueue bypassed its maintenance gate"
        finally:
            await _stop(processor)
            assert all(task.done() for task in _owned_tasks(processor))


class _GuardedProcessor:
    def __init__(self):
        self.is_running = False
        self.start_calls = 0

    async def start(self):
        self.start_calls += 1
        self.is_running = True

    async def enqueue(self, *_args, **_kwargs):
        return None

    async def enqueue_reindex(self, _job_id):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("caller_name", ("retry_document", "scan_directories", "reindex_documents"), ids=("retry", "scan", "reindex"))
async def test_ac4_documents_guards_and_repeated_start_are_idempotent(caller_name, monkeypatch):
    from app.api.routes import documents

    processor = _GuardedProcessor()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(secret_manager=None)))
    route = inspect.unwrap(getattr(documents, caller_name))
    if caller_name == "retry_document":
        class Cursor:
            def fetchone(self):
                return {"file_path": "ordinary.txt", "vault_id": 1}

        class Connection:
            def execute(self, *_args):
                return Cursor()

            def commit(self):
                pass

        monkeypatch.setattr(documents, "_record_document_action", lambda *_args, **_kwargs: None)
        def call():
            return route(1, request, Connection(), {}, "csrf", None, processor, {"id": 7})
    elif caller_name == "scan_directories":
        class Watcher:
            def __init__(self, *_args, **_kwargs):
                pass

            async def scan_once(self):
                return 0

        monkeypatch.setattr("app.services.file_watcher.FileWatcher", Watcher)
        def call():
            return route(request, processor, object(), {}, "csrf")
    else:
        class Cursor:
            lastrowid = 17

        class Connection:
            def execute(self, *_args):
                return Cursor()

            def commit(self):
                pass

        def call():
            return route(request, documents.ReindexRequest(vault_id=None), Connection(), {}, "csrf", processor)
    try:
        await call()
        await call()
        assert processor.start_calls == 1, (
            f"AC4: {caller_name} invoked start more than once while still running"
        )
        assert processor.is_running
    finally:
        processor.is_running = False


@pytest.mark.asyncio
async def test_ac4_start_is_idempotent_without_duplicate_workers_or_sweeps():
    processor = BackgroundProcessor(max_retries=0, retry_delay=0.01)
    _disable_other_recovery(processor)
    try:
        await processor.start()
        first = _owned_tasks(processor)
        await processor.start()
        assert _owned_tasks(processor) == first
    finally:
        await _stop(processor)
        assert all(task.done() for task in _owned_tasks(processor))


@pytest.mark.asyncio
async def test_failed_boot_recovery_continues_later_steps():
    processor = BackgroundProcessor(max_retries=0, retry_delay=0.01)
    calls, completed = [], asyncio.Event()

    async def step(name):
        calls.append(name)
        if name == _RECOVERY[0]:
            raise RuntimeError("synthetic recovery failure")
        if len(calls) == len(_RECOVERY):
            completed.set()

    for name in _RECOVERY:
        setattr(processor, name, lambda *args, _name=name, **kwargs: step(_name))
    try:
        await processor.start()
        recovery_task = getattr(processor, "_startup_recovery_task", None)
        assert isinstance(recovery_task, asyncio.Task), (
            "AC6: start did not retain an owned boot recovery task"
        )
        await asyncio.wait_for(completed.wait(), timeout=1.0)
        await asyncio.wait_for(recovery_task, timeout=1.0)
        assert calls == list(_RECOVERY)
    finally:
        await _stop(processor)
        assert all(task.done() for task in _owned_tasks(processor))


@pytest.mark.asyncio
async def test_stop_cancels_detached_boot_recovery_and_resets_running_state():
    processor = BackgroundProcessor(max_retries=0, retry_delay=0.01)
    started, cancelled, release = (asyncio.Event() for _ in range(3))

    async def blocked(*_args, **_kwargs):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    _disable_other_recovery(processor, blocked)
    start_task = asyncio.create_task(processor.start())
    try:
        await asyncio.wait_for(started.wait(), timeout=1.0)
        await _stop(processor)
        assert start_task.done(), "AC7: start did not detach recovery before stop"
        assert cancelled.is_set(), "AC7: stop did not cancel owned startup recovery"
        assert not processor._running, "AC7: stop did not reset the running state"
    finally:
        release.set()
        if not start_task.done():
            start_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await start_task
        await _stop(processor)
        assert all(task.done() for task in _owned_tasks(processor))


@pytest.mark.asyncio
async def test_publication_failure_closes_rejected_owned_coroutine():
    """The production ownership wrapper, not this test double, closes the coro."""
    processor = BackgroundProcessor(max_retries=0, retry_delay=0.01)
    rejected = []
    error = RuntimeError("synthetic publication failure")

    def fail_publication(coro, **_kwargs):
        rejected.append(coro)
        raise error

    _disable_other_recovery(processor)
    with patch.object(asyncio, "create_task", side_effect=fail_publication):
        with pytest.raises(RuntimeError, match="synthetic publication failure") as raised:
            await processor.start()
    assert raised.value is error
    assert len(rejected) == 1
    assert rejected[0].cr_frame is None
    assert not processor._running
    assert not processor._starting
