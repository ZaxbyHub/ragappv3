"""Issue #820 acceptance checks: an ingest refused by the #691 dimension
probe (``EmbeddingDimensionChangedError`` -> classify_ingest_error ==
``DIMENSION_CHANGED``) is deterministically non-retryable — the live index
dimension cannot change back between attempts — so BOTH transports must
settle it to terminal error on the FIRST attempt instead of burning the
generic retry ladder (each retry re-parses and re-embeds the whole document
before landing on the same refusal).

Checks:

- AC8a (DISCRIMINATING): the legacy transport —
  ``BackgroundProcessor._handle_failure`` with ``TaskItem(attempt=1)`` and
  an ``EmbeddingDimensionChangedError`` — must schedule NO retry (queue
  empty after the retry-scheduler beat, the probe_phase2 drain idiom) AND
  take the permanent-failure path (files row terminal 'error' with the
  'DIMENSION_CHANGED:' stable code, spy on _mark_task_permanently_failed).
  At base the ladder keys on attempt count only and requeues for attempt 2.
- AC8b (DISCRIMINATING): the lease transport —
  ``BackgroundProcessor._process_ingest_job_row`` with
  ``_run_task_processing`` doubled to raise the same refusal — must settle
  the jobs row 'failed' on the FIRST attempt (no requeue to 'pending') and
  mark the files row terminal with the same code. At base the row requeues.
- AC9 (PRESERVING, NEW pin, green at base): a genuinely transient failure
  (generic ``EmbeddingError``) at attempt=1 < max DOES schedule a retry and
  does NOT terminally mark the file — the ladder must survive the
  #820 short-circuit.

Harness: temp ``run_migrations`` DBs behind ``SQLiteConnectionPool``; the
lease leg drives the production ``_process_ingest_job_row`` against a real
JobLease-claimed ingestion row (the test_incident_resilience_support idiom);
the legacy leg drives the production ``_handle_failure`` with only the
permanent-failure writer spied (wrapped, not replaced).
"""

import asyncio
import json
import sqlite3

from app.config import settings
from app.models.database import SQLiteConnectionPool, run_migrations
from app.services.background_tasks import BackgroundProcessor, TaskItem
from app.services.document_processor import (
    EmbeddingDimensionChangedError,
    classify_ingest_error,
)
from app.services.embeddings import EmbeddingError
from app.services.job_lease import JobLease, ensure_jobs_schema

INGESTION_QUEUE = "ingestion"
DIMENSION_MSG = "embedding dimension changed: live=384 configured=768"


def _connect(db_path):
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _seed(tmp_path, name="ac820.db"):
    db_path = str(tmp_path / name)
    run_migrations(db_path)
    return db_path, _connect(db_path)


def _insert_processing_file(conn, *, status="processing", phase="embedding"):
    cur = conn.execute(
        "INSERT INTO files (vault_id, file_path, file_name, file_size, status,"
        " phase, source) VALUES (1, ?, ?, 1, ?, ?, 'upload')",
        ("uploads/ac820-doc.pdf", "ac820-doc.pdf", status, phase),
    )
    conn.commit()
    return int(cur.lastrowid)


async def _drain_queue_after_scheduler_beat(processor):
    """Drain the retry requeue after the scheduler timer fires (the
    probe_phase2 820-leg idiom: the deferred retry rides a timer handle,
    so give the loop a beat well past the 0.01s test delay)."""
    requeued = []
    try:
        while True:
            requeued.append(processor.queue.get_nowait())
    except asyncio.QueueEmpty:
        pass
    await asyncio.sleep(0.05)
    try:
        while True:
            requeued.append(processor.queue.get_nowait())
    except asyncio.QueueEmpty:
        pass
    return requeued


# ---------------------------------------------------------------------------
# AC8a — legacy transport
# ---------------------------------------------------------------------------


async def test_legacy_dimension_refusal_settles_terminal_first_attempt(
    tmp_path, monkeypatch
):
    """AC8a: a DIMENSION_CHANGED refusal on the legacy transport must not be
    requeued and must terminally mark the file on attempt 1."""
    db_path, conn = _seed(tmp_path, name="ac820-legacy.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    try:
        file_id = _insert_processing_file(conn)
        processor = BackgroundProcessor(
            max_retries=3, retry_delay=0.01, pool=pool
        )

        permanent_calls = []
        original = processor._mark_task_permanently_failed  # noqa: SLF001

        def _spy(task, error):
            permanent_calls.append((task, error))
            return original(task, error)

        monkeypatch.setattr(processor, "_mark_task_permanently_failed", _spy)

        task = TaskItem(
            file_path="uploads/ac820-doc.pdf",
            attempt=1,
            source="upload",
            vault_id=1,
            file_id=file_id,
            file_hash="ac820-hash",
        )
        await processor._handle_failure(  # noqa: SLF001
            task, EmbeddingDimensionChangedError(DIMENSION_MSG)
        )
        requeued = await _drain_queue_after_scheduler_beat(processor)

        assert requeued == [], (
            "a DIMENSION_CHANGED refusal is deterministic — the live index"
            " dimension cannot change back between attempts — so the legacy"
            " ladder must NOT requeue it for attempt 2; requeued="
            f"{[t.attempt for t in requeued]!r} (classify_ingest_error maps"
            " the refusal to DIMENSION_CHANGED, but the ladder never"
            " consults the code — issue #820)"
        )
        assert len(permanent_calls) == 1, (
            "the permanent-failure path must be taken on the first attempt,"
            f" spy calls={len(permanent_calls)}"
        )
        row = conn.execute(
            "SELECT status, error_message FROM files WHERE id = ?", (file_id,)
        ).fetchone()
        assert row["status"] == "error" and (
            row["error_message"] or ""
        ).startswith("DIMENSION_CHANGED:"), (
            "the file row must settle to terminal error carrying the"
            f" DIMENSION_CHANGED stable code on attempt 1, got"
            f" status={row['status']!r} error_message={row['error_message']!r}"
            " (issue #820, AC8)"
        )
    finally:
        pool.close_all()
        conn.close()


# ---------------------------------------------------------------------------
# AC8b — lease transport
# ---------------------------------------------------------------------------


async def test_lease_dimension_refusal_settles_failed_first_attempt(
    tmp_path, monkeypatch
):
    """AC8b: a DIMENSION_CHANGED refusal on the lease transport must settle
    the jobs row terminally 'failed' on the FIRST attempt (no requeue) and
    mark the file row terminal with the same code."""
    db_path, conn = _seed(tmp_path, name="ac820-lease.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    lease_conn = _connect(db_path)
    try:
        file_id = _insert_processing_file(conn)
        ensure_jobs_schema(conn)
        conn.execute(
            "INSERT INTO jobs (queue, payload_json, status) VALUES (?, ?,"
            " 'pending')",
            (
                INGESTION_QUEUE,
                json.dumps(
                    {
                        "file_id": file_id,
                        "vault_id": 1,
                        "file_path": "uploads/ac820-doc.pdf",
                    }
                ),
            ),
        )
        conn.commit()

        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        worker_id = "ac820-lease-worker"
        lease = JobLease(lease_conn, reclaim_timeout_seconds=300.0, max_attempts=3)
        monkeypatch.setattr(settings, "jobs_max_attempts", 3)

        async def _refuse(task):  # noqa: ARG001
            raise EmbeddingDimensionChangedError(DIMENSION_MSG)

        monkeypatch.setattr(processor, "_run_task_processing", _refuse)

        row = lease.claim(INGESTION_QUEUE, worker_id)
        assert row is not None, "harness: the first claim must succeed"
        await processor._process_ingest_job_row(row, worker_id)  # noqa: SLF001

        jobs_row = conn.execute(
            "SELECT status, error FROM jobs WHERE queue = ? ORDER BY id DESC"
            " LIMIT 1",
            (INGESTION_QUEUE,),
        ).fetchone()
        assert jobs_row["status"] == "failed", (
            "a DIMENSION_CHANGED refusal must settle the jobs row"
            f" terminally 'failed' on the FIRST attempt, got"
            f" {jobs_row['status']!r} — the lease retry predicate keys on"
            " attempt count only and requeues a deterministic refusal"
            f" (error={jobs_row['error']!r}; classify_ingest_error maps the"
            " refusal to DIMENSION_CHANGED — issue #820, AC8)"
        )
        assert "DIMENSION_CHANGED" in (jobs_row["error"] or ""), (
            "the terminal settle must carry the DIMENSION_CHANGED stable"
            f" code, got {jobs_row['error']!r}"
        )
        # Round-2 amendment (critic F7): a FIRST-attempt refusal must be
        # unprefixed — 'attempt_cap_exceeded' would mean the ladder burned
        # attempts before settling.
        assert "attempt_cap_exceeded" not in (jobs_row["error"] or ""), (
            "a dimension refusal settled on the FIRST attempt must not"
            " carry the attempt_cap_exceeded composition (the cap was"
            f" never reached), got {jobs_row['error']!r}"
        )
        frow = conn.execute(
            "SELECT status, error_message FROM files WHERE id = ?", (file_id,)
        ).fetchone()
        assert frow["status"] == "error" and (
            frow["error_message"] or ""
        ).startswith("DIMENSION_CHANGED:"), (
            "the file row must settle to terminal error carrying the"
            f" DIMENSION_CHANGED stable code on attempt 1, got"
            f" status={frow['status']!r}"
            f" error_message={frow['error_message']!r} (issue #820, AC8)"
        )
    finally:
        lease_conn.close()
        pool.close_all()
        conn.close()


# ---------------------------------------------------------------------------
# AC9 — transient failures keep the ladder (PRESERVING, green at base)
# ---------------------------------------------------------------------------


async def test_transient_embedding_error_keeps_retry_ladder(tmp_path, monkeypatch):
    """AC9: a generic EmbeddingError (a genuinely transient provider
    failure) at attempt=1 < max_retries MUST still schedule a retry and must
    NOT terminally mark the file — on BOTH transports (round-2 amendment,
    critic F7: the lease leg requeues its jobs row) — the #820
    short-circuit is keyed to DIMENSION_CHANGED, not to embedding failures
    at large."""
    db_path, conn = _seed(tmp_path, name="ac820-transient.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    try:
        file_id = _insert_processing_file(conn)
        processor = BackgroundProcessor(
            max_retries=3, retry_delay=0.01, pool=pool
        )

        task = TaskItem(
            file_path="uploads/ac820-doc.pdf",
            attempt=1,
            source="upload",
            vault_id=1,
            file_id=file_id,
            file_hash="ac820-hash",
        )
        assert (
            classify_ingest_error(EmbeddingError("provider 503"))
            == "EMBEDDING_FAILED"
        ), "harness: a generic EmbeddingError classifies as a transient code"
        await processor._handle_failure(  # noqa: SLF001
            task, EmbeddingError("provider 503")
        )
        requeued = await _drain_queue_after_scheduler_beat(processor)

        assert len(requeued) == 1 and requeued[0].attempt == 2, (
            "a transient EmbeddingError below the cap must be requeued for"
            f" attempt 2, requeued={[t.attempt for t in requeued]!r}"
            " (issue #820, AC9 — the retry ladder survives)"
        )
        row = conn.execute(
            "SELECT status FROM files WHERE id = ?", (file_id,)
        ).fetchone()
        assert row["status"] != "error", (
            "a transient failure below the cap must not terminally mark the"
            f" file, got status={row['status']!r} (issue #820, AC9)"
        )
    finally:
        pool.close_all()
        conn.close()

    # --- Lease leg (round-2 amendment, critic F7): the same transient ---
    # failure through _process_ingest_job_row must REQUEUE the jobs row. ---
    db_path, conn = _seed(tmp_path, name="ac820-transient-lease.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    lease_conn = _connect(db_path)
    try:
        file_id = _insert_processing_file(conn, status="pending",
                                          phase="queued")
        ensure_jobs_schema(conn)
        conn.execute(
            "INSERT INTO jobs (queue, payload_json, status) VALUES (?, ?,"
            " 'pending')",
            (
                INGESTION_QUEUE,
                json.dumps(
                    {
                        "file_id": file_id,
                        "vault_id": 1,
                        "file_path": "uploads/ac820-doc.pdf",
                    }
                ),
            ),
        )
        conn.commit()

        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        worker_id = "ac820-transient-lease-worker"
        lease = JobLease(
            lease_conn, reclaim_timeout_seconds=300.0, max_attempts=3
        )
        monkeypatch.setattr(settings, "jobs_max_attempts", 3)

        async def _transient(task):  # noqa: ARG001
            raise EmbeddingError("provider 503 (transient lease leg)")

        monkeypatch.setattr(processor, "_run_task_processing", _transient)

        claimed = lease.claim(INGESTION_QUEUE, worker_id)
        assert claimed is not None, "harness: the first claim must succeed"
        await processor._process_ingest_job_row(claimed, worker_id)  # noqa: SLF001

        jobs_row = conn.execute(
            "SELECT status, error FROM jobs WHERE queue = ? ORDER BY id DESC"
            " LIMIT 1",
            (INGESTION_QUEUE,),
        ).fetchone()
        assert jobs_row["status"] == "pending", (
            "a transient EmbeddingError at attempt 1 < cap must REQUEUE the"
            f" lease row (back to pending), got {jobs_row['status']!r}"
            " — the #820 short-circuit must stay keyed to DIMENSION_CHANGED"
            f" (error={jobs_row['error']!r}; issue #820, AC9)"
        )
        assert (jobs_row["error"] or "").startswith("EMBEDDING_FAILED"), (
            "the requeued row must carry the transient stable code, got"
            f" {jobs_row['error']!r}"
        )
    finally:
        lease_conn.close()
        pool.close_all()
        conn.close()
