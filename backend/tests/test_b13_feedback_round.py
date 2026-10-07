"""PR #858 feedback-round regressions (swarm-pr-review PRR-* + out-of-band F-*).

Covers the two probe-proven blockers and the review-identified coverage gaps:
- cap guards key on terminal-at-cap (attempts >= jobs_max_attempts), not the
  error literal (the marker is only written when the error column was empty);
- a narrowed file-scoped retry refuses the staged index rebuild;
- stop()'s backlog-drain leg, both shutdown sweeps' refunds, the
  ReindexOperatorGuidance verbatim-persist boundary, the /health route leg,
  the boot-sync terminal files settlement, AC11's single-job invariant,
  AC3's failed-file-retried half, janitor mid-pass rollback, and the
  cancel-preserving dedupe.

Harness idioms mirror tests/test_b13_lease_reindex_durability.py and
tests/test_b02_reindex_vault_scope_guard.py.
"""

import asyncio
import inspect
import json
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

from app.config import settings
from app.models.database import SQLiteConnectionPool, run_migrations
from app.services.background_tasks import (
    BackgroundProcessor,
    ReindexOperatorGuidance,
    TaskItem,
)
from app.services.job_lease import JobLease, ensure_jobs_schema

INGESTION_QUEUE = "ingestion"
REINDEX_QUEUE = "reindex"
STALE_SECONDS = 3600


@contextmanager
def _setting_overridden(name, value):
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


def _connect(db_path):
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _seed(tmp_path, name="b13fb.db"):
    db_path = str(tmp_path / name)
    run_migrations(db_path)
    return db_path, _connect(db_path)


def _insert_file(conn, *, status, vault_id=1, phase="queued", name="b13fb-file.txt"):
    cur = conn.execute(
        "INSERT INTO files (vault_id, file_path, file_name, file_size, "
        "status, phase, source) VALUES (?, ?, ?, 1, ?, ?, 'upload')",
        (vault_id, f"uploads/{name}", name, status, phase),
    )
    conn.commit()
    return int(cur.lastrowid)


def _insert_job(
    conn,
    queue,
    payload,
    *,
    status,
    error=None,
    worker_id=None,
    attempts=0,
    heartbeat_age_seconds=None,
):
    ensure_jobs_schema(conn)
    cur = conn.execute(
        "INSERT INTO jobs (queue, payload_json, status, error, worker_id, "
        "attempts) VALUES (?, ?, ?, ?, ?, ?)",
        (queue, json.dumps(payload), status, error, worker_id, attempts),
    )
    job_id = int(cur.lastrowid)
    if heartbeat_age_seconds is not None:
        conn.execute(
            "UPDATE jobs SET heartbeat_at = datetime('now', ?) WHERE id = ?",
            (f"-{heartbeat_age_seconds} seconds", job_id),
        )
    conn.commit()
    return job_id


def _files_row(conn, file_id):
    return conn.execute(
        "SELECT status, phase, error_message FROM files WHERE id = ?",
        (file_id,),
    ).fetchone()


async def test_boot_sync_cap_guard_keys_on_attempts(tmp_path):
    """F-001 (PRR-001 / OOB F-002): the boot sync's cap exclusion must fire
    for a capped job whose error column carries OLD text (requeue-then-crash
    shape), not only for the literal marker."""
    db_path, conn = _seed(tmp_path)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        cap = int(settings.jobs_max_attempts)
        file_id = _insert_file(conn, status="processing", phase=None)
        _insert_job(
            conn,
            INGESTION_QUEUE,
            {"file_id": file_id, "vault_id": 1, "file_path": "uploads/b13fb-file.txt"},
            status="failed",
            error="boom: the old requeue error text",
            attempts=cap,
        )
        conn.commit()

        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        processor._sync_missing_ingest_job_rows()  # noqa: SLF001

        pending = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE queue = ? AND status = 'pending'",
            (INGESTION_QUEUE,),
        ).fetchone()[0]
        assert pending == 0, (
            "a file whose only job is terminally failed at the attempts cap "
            "(old error text, no literal marker) must not get a fresh "
            "zero-attempt job from the boot sync"
        )
        row = _files_row(conn, file_id)
        assert row[0] == "error", (
            "the cap-excluded file's processing row must settle terminally"
        )
        assert row[2] == "lease_attempt_cap_exceeded"
    finally:
        pool.close_all()


async def test_janitor_resync_keys_on_attempts(tmp_path):
    """F-001 janitor leg: a lease reclaimed at the cap with prior error text
    must still have its files row failed by the sweep's newly-capped resync."""
    db_path, conn = _seed(tmp_path)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        cap = int(settings.jobs_max_attempts)
        file_id = _insert_file(conn, status="processing", phase=None)
        _insert_job(
            conn,
            INGESTION_QUEUE,
            {"file_id": file_id, "vault_id": 1, "file_path": "uploads/b13fb-file.txt"},
            status="running",
            worker_id="b13fb-dead-worker",
            attempts=cap,
            error="boom: set by the previous requeue",
            heartbeat_age_seconds=STALE_SECONDS,
        )
        conn.commit()

        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        await processor._janitor_sweep_ingestion()  # noqa: SLF001

        row = _files_row(conn, file_id)
        assert row[0] == "error", (
            "the janitor must fail the files row of a job it reclaimed at "
            "the cap even when the row's error column carries old text: "
            f"got {row!r}"
        )
        job = conn.execute(
            "SELECT status, attempts FROM jobs WHERE queue = ?",
            (INGESTION_QUEUE,),
        ).fetchone()
        assert job[0] == "failed" and job[1] >= cap
    finally:
        pool.close_all()


async def test_narrowed_retry_refuses_staged_rebuild(tmp_path):
    """F-002 (PRR-002 / OOB F-001): a narrowed file-scoped retry must never
    enter the staged dimension rebuild — the commit would swap the GLOBAL
    chunks table with only the failed files staged."""
    from app.models.database import SQLiteConnectionPool as _P  # noqa: F401

    db_path, conn = _seed(tmp_path, "b13fb-staged.db")
    pool = SQLiteConnectionPool(db_path, max_size=2)

    class _RecordingStore:
        def __init__(self):
            self.begin_attempts = 0

        async def get_live_embedding_dim(self):
            return 4

        async def begin_dimension_rebuild(self, new_dim):  # pragma: no cover
            self.begin_attempts += 1
            raise AssertionError(f"narrowed retry opened a rebuild ({new_dim})")

    class _ProbeEmbeddingService:
        async def embed_batch(self, texts, batch_size=None, fail_fast=True):
            return [[6.0, 6.0, 6.0, 6.0, 6.0, 6.0] for _ in texts]

    try:
        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        file_id = _insert_file(conn, status="indexed", name="b13fb-staged.txt")
        conn.commit()
        processor.processor.embedding_service = _ProbeEmbeddingService()
        store = _RecordingStore()
        processor.processor.vector_store = store

        status, result, error = await processor._reindex_embed_all(  # noqa: SLF001
            1, None, retry_file_ids=[file_id]
        )

        assert status == "failed"
        assert isinstance(error, ReindexOperatorGuidance), (
            "a narrowed retry on a dimension change must fail with operator "
            f"guidance, got {error!r}"
        )
        assert "full reindex" in str(error)
        assert store.begin_attempts == 0, (
            "the staged rebuild must never open for a narrowed retry"
        )
    finally:
        pool.close_all()


async def test_stop_drain_resets_parked_ticket(tmp_path):
    """F-011 (PRR-013): the stop() backlog-drain leg must reset parked
    ingestion tickets that never reached the retry scheduler."""
    db_path, conn = _seed(tmp_path, "b13fb-drain.db")
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        file_id = _insert_file(conn, status="processing", phase=None)
        with (
            _setting_overridden("ingestion_job_lease_enabled", False),
            _setting_overridden("reindex_job_lease_enabled", False),
            _setting_overridden("wiki_kms_job_lease_enabled", False),
        ):
            processor = BackgroundProcessor(pool=pool, retry_delay=60.0)
            processor._running = True  # noqa: SLF001
            # Suppress the scheduler so the ticket is still IN the backlog
            # when stop() drains it (the complement of the frozen AC10,
            # which exercises the scheduler-cancellation leg).
            monkey_noop = lambda: None  # noqa: E731
            processor._ensure_retry_scheduler = monkey_noop  # noqa: SLF001
            await processor._handle_failure(  # noqa: SLF001
                TaskItem(
                    file_path="uploads/b13fb-file.txt",
                    vault_id=1,
                    attempt=1,
                    file_id=file_id,
                ),
                RuntimeError("b13fb transient"),
            )
            assert processor._retry_backlog.qsize() == 1  # noqa: SLF001
            await processor.stop(timeout=1.0)

        row = _files_row(conn, file_id)
        assert row[0] == "pending", (
            "the drain leg must reset a still-parked ingestion ticket's "
            f"files row to pending, got {row!r}"
        )
    finally:
        pool.close_all()


async def test_release_sweeps_refund_attempts(tmp_path):
    """F-012 (PRR-015): both shutdown release sweeps refund the claimed
    attempt, exactly like JobLease.release."""
    db_path, conn = _seed(tmp_path, "b13fb-sweeps.db")
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        ingest_fid = _insert_job(
            conn,
            INGESTION_QUEUE,
            {"file_id": 1},
            status="running",
            worker_id="w1",
            attempts=2,
        )
        reindex_fid = _insert_job(
            conn,
            REINDEX_QUEUE,
            {"vault_id": 1},
            status="running",
            worker_id="w2",
            attempts=3,
        )
        conn.commit()
        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        assert processor._release_all_ingest_leases() == 1  # noqa: SLF001
        assert processor._release_reindex_leases() == 1  # noqa: SLF001
        assert conn.execute(
            "SELECT attempts FROM jobs WHERE id = ?", (ingest_fid,)
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT attempts FROM jobs WHERE id = ?", (reindex_fid,)
        ).fetchone()[0] == 2
    finally:
        pool.close_all()


async def test_guidance_persists_verbatim_at_cap(tmp_path, monkeypatch):
    """F-013 (PRR-016): the ReindexOperatorGuidance verbatim-persist branch
    at the lease settle boundary."""
    db_path, conn = _seed(tmp_path, "b13fb-guidance.db")
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        cap = int(settings.jobs_max_attempts)
        job_id = _insert_job(
            conn, REINDEX_QUEUE, {"vault_id": 1}, status="pending"
        )
        conn.execute(
            "UPDATE jobs SET attempts = ? WHERE id = ?", (cap - 1, job_id)
        )
        conn.commit()
        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        lease = JobLease(
            conn,
            reclaim_timeout_seconds=settings.jobs_lease_reclaim_timeout_seconds,
            max_attempts=cap,
        )
        row = lease.claim(REINDEX_QUEUE, "b13fb-guidance-worker")
        assert row is not None and int(row["attempts"]) == cap

        async def fake_embed(job_id_arg, vault_id):  # noqa: ARG001
            return (
                "failed",
                {},
                ReindexOperatorGuidance(
                    "GUIDANCE-VERBATIM-42 run a full reindex instead"
                ),
            )

        monkeypatch.setattr(processor, "_reindex_embed_all", fake_embed)
        await processor._run_reindex_job_row(  # noqa: SLF001
            row, "b13fb-guidance-worker"
        )

        error = conn.execute(
            "SELECT error FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()["error"]
        assert error is not None
        tail = error.split("attempt_cap_exceeded: ", 1)[-1]
        assert "GUIDANCE-VERBATIM-42" in tail, (
            "operator guidance must persist verbatim at the cap, got "
            f"{error!r}"
        )
    finally:
        pool.close_all()


async def test_health_reports_durable_backlog(tmp_path, monkeypatch):
    """F-014 (PRR-017): the shallow /health route reports the durable
    backlog through the off-loop queue_size probe."""
    from app.api.routes import health

    db_path, conn = _seed(tmp_path, "b13fb-health.db")
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        for i in range(2):
            _insert_job(
                conn,
                INGESTION_QUEUE,
                {"file_id": 100 + i},
                status="pending",
            )
        conn.commit()
        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        assert processor._ingest_lease_enabled  # noqa: SLF001

        monkeypatch.setattr(health, "_spawn_refresh_task", lambda *a, **k: None)
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(background_processor=processor))
        )
        route = inspect.unwrap(health.health_check)
        result = await route(
            request,
            deep=False,
            llm_checker=None,
            model_checker=None,
            user=None,
        )
        assert result["ingestion_queue_size"] == 2, (
            "the shallow /health route must report the durable lease backlog"
        )
    finally:
        pool.close_all()


async def test_boot_sync_settles_cap_excluded_files_row(tmp_path):
    """F-015 (PRR-018): the boot-sync terminal settlement of cap-excluded
    files rows (the half of P02-SK2-08 the frozen C7 does not assert)."""
    db_path, conn = _seed(tmp_path, "b13fb-settle.db")
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        cap = int(settings.jobs_max_attempts)
        file_id = _insert_file(conn, status="processing", phase=None)
        _insert_job(
            conn,
            INGESTION_QUEUE,
            {"file_id": file_id, "vault_id": 1, "file_path": "uploads/b13fb-file.txt"},
            status="failed",
            attempts=cap,
        )
        conn.commit()
        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        processor._sync_missing_ingest_job_rows()  # noqa: SLF001
        row = _files_row(conn, file_id)
        assert (row[0], row[1], row[2]) == (
            "error",
            "error",
            "lease_attempt_cap_exceeded",
        ), f"cap-excluded file must settle terminally, got {row!r}"
    finally:
        pool.close_all()


async def test_double_retry_holds_single_pending_job(tmp_path, monkeypatch):
    """F-016 (PRR-020): the durable half of AC11 — a double admin retry in
    lease mode holds exactly ONE pending ingestion job for the file."""
    from app.api.routes import documents

    db_path, conn = _seed(tmp_path, "b13fb-retry.db")
    pool = SQLiteConnectionPool(db_path, max_size=2)
    route_conn = _connect(db_path)
    try:
        file_id = _insert_file(conn, status="error", name="b13fb-retry.txt")
        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        assert processor._ingest_lease_enabled  # noqa: SLF001
        processor._running = True  # noqa: SLF001
        monkeypatch.setattr(
            documents, "_record_document_action", lambda *_a, **_k: None
        )
        route = inspect.unwrap(documents.retry_document)
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(secret_manager=None))
        )

        async def call():
            return await route(
                file_id,
                request,
                route_conn,
                {},
                "csrf",
                None,
                processor,
                {"id": 7},
            )

        first = await call()
        second = await call()
        assert first["status"] == "scheduled"
        assert second["status"] == "already_in_progress"
        pending = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE queue = ? AND status = 'pending' "
            "AND CAST(json_extract(payload_json, '$.file_id') AS INTEGER) = ?",
            (INGESTION_QUEUE, file_id),
        ).fetchone()[0]
        assert pending == 1, (
            f"a double retry must hold exactly one pending job, got {pending}"
        )
    finally:
        route_conn.close()
        pool.close_all()


async def test_retry_reembeds_the_failed_files(tmp_path, monkeypatch):
    """F-017 (PRR-023): AC3's complement — the narrowed retry must actually
    re-embed the failed files, not silently retry nothing."""
    db_path, conn = _seed(tmp_path, "b13fb-ac3b.db")
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        bad = _insert_file(conn, status="indexed", name="b13fb-bad.txt")
        _insert_job(conn, REINDEX_QUEUE, {"vault_id": 1}, status="pending")
        calls: list[int] = []

        async def counting(existing_id, file_path, vault_id, **kwargs):  # noqa: ARG001
            calls.append(int(existing_id))
            if int(existing_id) == bad:
                raise RuntimeError("b13fb permanently bad")
            return None

        monkeypatch.setattr(
            processor.processor, "process_existing_file", counting
        )
        lease = JobLease(
            conn,
            reclaim_timeout_seconds=settings.jobs_lease_reclaim_timeout_seconds,
            max_attempts=int(settings.jobs_max_attempts),
        )
        row = lease.claim(REINDEX_QUEUE, "b13fb-ac3b-worker")
        assert row is not None
        await processor._run_reindex_job_row(row, "b13fb-ac3b-worker")  # noqa: SLF001
        conn.execute(
            "UPDATE jobs SET run_after = datetime('now', '-1 seconds') "
            "WHERE queue = ?",
            (REINDEX_QUEUE,),
        )
        conn.commit()
        row = lease.claim(REINDEX_QUEUE, "b13fb-ac3b-worker")
        assert row is not None
        await processor._run_reindex_job_row(row, "b13fb-ac3b-worker")  # noqa: SLF001

        assert calls.count(bad) == 2, (
            "the failed file must be re-embedded on BOTH attempts (attempt 1 "
            f"full scope + narrowed retry), calls={calls!r}"
        )
    finally:
        pool.close_all()


async def test_janitor_sweep_rolls_back_on_midpass_failure(tmp_path, monkeypatch):
    """F-018 (PRR-019): a failure between the reclaim and the files resync
    rolls the WHOLE sweep back — no partial cap settlement."""
    db_path, conn = _seed(tmp_path, "b13fb-txn.db")
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        cap = int(settings.jobs_max_attempts)
        file_id = _insert_file(conn, status="processing", phase=None)
        _insert_job(
            conn,
            INGESTION_QUEUE,
            {"file_id": file_id, "vault_id": 1, "file_path": "uploads/b13fb-file.txt"},
            status="running",
            worker_id="b13fb-dead",
            attempts=cap,
            heartbeat_age_seconds=STALE_SECONDS,
        )
        conn.commit()
        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)

        def explode(self, queue=None):  # noqa: ARG001
            raise RuntimeError("b13fb mid-pass failure")

        monkeypatch.setattr(JobLease, "reclaim_expired", explode)
        try:
            await processor._janitor_sweep_ingestion()  # noqa: SLF001
            raised = False
        except RuntimeError:
            raised = True
        assert raised, "the mid-pass failure must propagate out of the sweep"

        job = conn.execute(
            "SELECT status FROM jobs WHERE queue = ?", (INGESTION_QUEUE,)
        ).fetchone()
        row = _files_row(conn, file_id)
        assert job[0] == "running", "the reclaim must roll back with the sweep"
        assert row[0] == "processing", (
            "no files-row settlement may survive a rolled-back sweep"
        )
    finally:
        pool.close_all()
        monkeypatch.undo()


async def test_dedupe_preserves_pending_cancel(tmp_path):
    """F-007 (out-of-band): an already_in_progress retry must not discard a
    pending #783 cancellation; a fresh accepted enqueue still clears it."""
    db_path, conn = _seed(tmp_path, "b13fb-cancel.db")
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        file_id = _insert_file(conn, status="processing", phase=None)
        _insert_job(
            conn,
            INGESTION_QUEUE,
            {"file_id": file_id, "vault_id": 1, "file_path": "uploads/b13fb-file.txt"},
            status="pending",
        )
        conn.commit()
        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        processor._running = True  # noqa: SLF001
        processor.processor.request_cancel(file_id)
        assert processor.processor.is_cancel_requested(file_id)

        enqueued = await processor.enqueue(
            "uploads/b13fb-file.txt", 1, file_id=file_id, _dedupe_existing_job=True
        )
        assert enqueued is False
        assert processor.processor.is_cancel_requested(file_id), (
            "an already_in_progress answer must not discard the pending cancel"
        )

        conn.execute("DELETE FROM jobs WHERE queue = ?", (INGESTION_QUEUE,))
        conn.commit()
        enqueued = await processor.enqueue(
            "uploads/b13fb-file.txt", 1, file_id=file_id
        )
        assert enqueued is True
        assert not processor.processor.is_cancel_requested(file_id), (
            "an accepted enqueue must discard the stale cancel"
        )
    finally:
        pool.close_all()
