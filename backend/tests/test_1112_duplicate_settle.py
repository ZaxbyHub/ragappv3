"""Issue #1112 acceptance battery: a same-hash retry/reindex whose files-row
flip collides with another indexed/partial row in the same vault is a
DETERMINISTIC duplicate — classified DUPLICATE_FILE, stopped BEFORE any vector
write, settled terminal on the FIRST attempt on both transports, excluded from
reindex retry scopes, and never a PARSE_FAILED.

Mirrors test_820_dimension_no_retry's harness (temp run_migrations DBs,
production _handle_failure / _process_ingest_job_row, spied writer) for the
transport legs; the guard legs drive the REAL process_existing_file against a
real DB with a recording vector-store stub.
"""

import asyncio
import hashlib
import json
import sqlite3

import pytest

from app.config import settings
from app.models.database import SQLiteConnectionPool, run_migrations
from app.services.background_tasks import BackgroundProcessor, TaskItem
from app.services.document_processor import (
    INGEST_ERROR_DUPLICATE_FILE,
    DocumentProcessor,
    DuplicateFileError,
    classify_ingest_error,
)
from app.services.job_lease import JobLease, ensure_jobs_schema

INGESTION_QUEUE = "ingestion"
REINDEX_QUEUE = "reindex"


def _connect(db_path):
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _seed(tmp_path, name):
    db_path = str(tmp_path / name)
    run_migrations(db_path)
    return db_path, _connect(db_path)


def _insert_file(
    conn,
    *,
    file_hash,
    status,
    path="uploads/doc.pdf",
    vault_id=1,
):
    cur = conn.execute(
        "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size,"
        " status, phase, source) VALUES (?, ?, ?, ?, 1, ?, 'queued', 'upload')",
        (vault_id, path, path.rsplit("/", 1)[-1], file_hash, status),
    )
    conn.commit()
    return int(cur.lastrowid)


class _RecordingVectorStore:
    """Vector-store stub: every mutation call is recorded (and returns the
    shape the pipeline reads); nothing executes."""

    def __init__(self):
        self.calls = []

    async def init_table(self, *a, **k):
        self.calls.append("init_table")

    async def add_chunks(self, *a, **k):
        self.calls.append("add_chunks")
        return {}

    async def delete_by_file(self, *a, **k):
        self.calls.append("delete_by_file")

    async def delete_old_generation_by_file(self, *a, **k):
        self.calls.append("delete_old_generation_by_file")
        return 0

    async def count_by_file(self, *a, **k):
        # visibility verification: report the just-written count
        return len(self.calls)

    async def get_live_embedding_dim(self, *a, **k):
        # dimension probe (reindex pre-loop); matches the embed stub's dim
        return 2


def _real_file(tmp_path, content=b"duplicate-hash-bytes-1112") -> tuple[str, str]:
    path = tmp_path / "doc.pdf"
    path.write_bytes(content)
    return str(path), hashlib.sha256(content).hexdigest()


# ---------------------------------------------------------------------------
# Guard legs (tests 1, 2, 4, 12): the REAL process_existing_file
# ---------------------------------------------------------------------------


async def test_guard_indexed_owner_settles_error_no_vectors(tmp_path):
    """A duplicate whose owner is indexed: the pre-vector guard raises, the
    row settles error with the DUPLICATE_FILE stable code in BOTH
    user-facing columns, and no vector call ever executes."""
    db_path, conn = _seed(tmp_path, "g1.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    try:
        file_path, file_hash = _real_file(tmp_path)
        owner_id = _insert_file(conn, file_hash=file_hash, status="indexed")
        subject_id = _insert_file(
            conn, file_hash=file_hash, status="pending", path="uploads/doc2.pdf"
        )
        assert owner_id != subject_id
        stub = _RecordingVectorStore()
        processor = DocumentProcessor(pool=pool, vector_store=stub)
        with pytest.raises(DuplicateFileError):
            await processor.process_existing_file(
                subject_id, file_path, 1, file_hash=file_hash
            )
        assert stub.calls == [], (
            "the duplicate guard fires BEFORE any vector mutation; got "
            f"{stub.calls}"
        )
        row = conn.execute(
            "SELECT status, error_message, phase, phase_message FROM files"
            " WHERE id = ?",
            (subject_id,),
        ).fetchone()
        assert row["status"] == "error"
        assert (row["error_message"] or "").startswith("DUPLICATE_FILE:")
        assert (row["phase_message"] or "").startswith("DUPLICATE_FILE:")
        assert row["phase"] == "error"
    finally:
        pool.close_all()
        conn.close()


async def test_guard_partial_owner(tmp_path):
    """A duplicate whose owner is 'partial' trips the guard identically."""
    db_path, conn = _seed(tmp_path, "g2.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    try:
        file_path, file_hash = _real_file(tmp_path)
        _insert_file(conn, file_hash=file_hash, status="partial")
        subject_id = _insert_file(
            conn, file_hash=file_hash, status="pending", path="uploads/doc2.pdf"
        )
        stub = _RecordingVectorStore()
        processor = DocumentProcessor(pool=pool, vector_store=stub)
        with pytest.raises(DuplicateFileError):
            await processor.process_existing_file(
                subject_id, file_path, 1, file_hash=file_hash
            )
        assert stub.calls == []
        row = conn.execute(
            "SELECT status, error_message FROM files WHERE id = ?", (subject_id,)
        ).fetchone()
        assert row["status"] == "error"
        assert (row["error_message"] or "").startswith("DUPLICATE_FILE:")
    finally:
        pool.close_all()
        conn.close()


async def test_guard_own_row_reindex_control(tmp_path):
    """A healthy same-file reprocess (own row already indexed) must NOT trip
    the guard — the status flip to 'processing' (before the guard) excludes
    the own row via the status filter; the ``AND id != ?`` clause is
    belt-and-suspenders on top of it (review TF-002: the clause itself is
    not independently discriminated by this control)."""
    db_path, conn = _seed(tmp_path, "g3.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    try:
        file_path, file_hash = _real_file(tmp_path)
        own_id = _insert_file(conn, file_hash=file_hash, status="indexed")
        stub = _RecordingVectorStore()
        processor = DocumentProcessor(pool=pool, vector_store=stub)
        # The guard must not raise; processing continues past it (the parse
        # pipeline will fail later on the stub store — the assertion here is
        # only that the DUPLICATE guard did not fire).
        try:
            await processor.process_existing_file(
                own_id, file_path, 1, file_hash=file_hash, retry_context="reindex"
            )
        except DuplicateFileError:
            pytest.fail("own-row reprocess tripped the duplicate guard")
        except Exception:
            pass  # downstream parse/vector failure on the stub — fine here
        row = conn.execute(
            "SELECT error_message FROM files WHERE id = ?", (own_id,)
        ).fetchone()
        assert not (row["error_message"] or "").startswith("DUPLICATE_FILE:")
    finally:
        pool.close_all()
        conn.close()


async def test_guard_bytes_changed_row_keeps_indexed_restore(tmp_path):
    """The round-4 critic shape: an indexed row whose stored hash is OLD but
    whose current bytes hash to a hash owned by ANOTHER indexed row. The
    guard fires, but the #704 restore contract is authoritative — the
    undisturbed row keeps its indexed status (prior generation stays
    searchable); no error write."""
    db_path, conn = _seed(tmp_path, "g4.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    try:
        file_path, new_hash = _real_file(tmp_path, b"new-generation-bytes")
        row1 = _insert_file(
            conn, file_hash="0" * 64, status="indexed", path="uploads/row1.pdf"
        )
        _insert_file(conn, file_hash=new_hash, status="indexed", path="uploads/row2.pdf")
        stub = _RecordingVectorStore()
        processor = DocumentProcessor(pool=pool, vector_store=stub)
        with pytest.raises(DuplicateFileError):
            await processor.process_existing_file(
                row1, file_path, 1, retry_context="reindex"
            )
        assert stub.calls == []
        row = conn.execute(
            "SELECT status, error_message FROM files WHERE id = ?", (row1,)
        ).fetchone()
        assert row["status"] == "indexed", (
            "an undisturbed indexed row keeps its prior status (#704 restore "
            f"intent); got {row['status']!r} / {row['error_message']!r}"
        )
        assert classify_ingest_error(DuplicateFileError("dup")) == (
            INGEST_ERROR_DUPLICATE_FILE
        )
    finally:
        pool.close_all()
        conn.close()


# ---------------------------------------------------------------------------
# Transport legs (tests 5, 6, 7): mirror test_820_dimension_no_retry
# ---------------------------------------------------------------------------


async def test_legacy_transport_duplicate_settles_first_attempt(
    tmp_path, monkeypatch
):
    db_path, conn = _seed(tmp_path, "legacy.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    try:
        file_id = _insert_file(conn, file_hash="h" * 64, status="processing")
        processor = BackgroundProcessor(max_retries=3, retry_delay=0.01, pool=pool)
        permanent_calls = []
        original = processor._mark_task_permanently_failed  # noqa: SLF001

        def _spy(task, error):
            permanent_calls.append((task, error))
            return original(task, error)

        monkeypatch.setattr(processor, "_mark_task_permanently_failed", _spy)
        task = TaskItem(
            file_path="uploads/doc.pdf",
            attempt=1,
            source="upload",
            vault_id=1,
            file_id=file_id,
            file_hash="h" * 64,
        )
        await processor._handle_failure(  # noqa: SLF001
            task,
            DuplicateFileError("file with hash h already indexed in vault 1"),
        )
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
        assert requeued == [], (
            "a duplicate is deterministic — the legacy ladder must NOT "
            f"requeue it; got {[t.attempt for t in requeued]!r}"
        )
        assert len(permanent_calls) == 1
        row = conn.execute(
            "SELECT status, error_message, phase_message FROM files WHERE id = ?",
            (file_id,),
        ).fetchone()
        assert row["status"] == "error"
        assert (row["error_message"] or "").startswith("DUPLICATE_FILE:")
        assert (row["phase_message"] or "").startswith("DUPLICATE_FILE:")
    finally:
        pool.close_all()
        conn.close()


async def test_lease_transport_duplicate_settles_first_attempt(tmp_path, monkeypatch):
    db_path, conn = _seed(tmp_path, "lease.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    lease_conn = _connect(db_path)
    try:
        file_id = _insert_file(conn, file_hash="h" * 64, status="processing")
        ensure_jobs_schema(conn)
        conn.execute(
            "INSERT INTO jobs (queue, payload_json, status) VALUES (?, ?, 'pending')",
            (
                INGESTION_QUEUE,
                json.dumps(
                    {
                        "file_id": file_id,
                        "vault_id": 1,
                        "file_path": "uploads/doc.pdf",
                    }
                ),
            ),
        )
        conn.commit()
        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        worker_id = "dup-lease-worker"
        lease = JobLease(lease_conn, reclaim_timeout_seconds=300.0, max_attempts=3)
        monkeypatch.setattr(settings, "jobs_max_attempts", 3)

        async def _duplicate(task):  # noqa: ARG001
            raise DuplicateFileError("file with hash h already indexed in vault 1")

        monkeypatch.setattr(processor, "_run_task_processing", _duplicate)
        row = lease.claim(INGESTION_QUEUE, worker_id)
        assert row is not None
        await processor._process_ingest_job_row(row, worker_id)  # noqa: SLF001
        jobs_row = conn.execute(
            "SELECT status, error FROM jobs WHERE queue = ? ORDER BY id DESC LIMIT 1",
            (INGESTION_QUEUE,),
        ).fetchone()
        assert jobs_row["status"] == "failed", (
            "a duplicate must settle the jobs row terminally 'failed' on the "
            f"FIRST attempt; got {jobs_row['status']!r}"
        )
        assert "DUPLICATE_FILE" in (jobs_row["error"] or "")
        assert "attempt_cap_exceeded" not in (jobs_row["error"] or "")
    finally:
        pool.close_all()
        conn.close()
        lease_conn.close()


async def test_nonduplicate_fk_not_duplicate_classified(tmp_path):
    """A nonduplicate IntegrityError (the round-1 critic's FK probe shape)
    keeps the existing fallback classification and is NOT diverted into
    duplicate terminal settlement."""
    assert (
        classify_ingest_error(
            sqlite3.IntegrityError("FOREIGN KEY constraint failed")
        )
        != INGEST_ERROR_DUPLICATE_FILE
    )
    db_path, conn = _seed(tmp_path, "fk.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    try:
        file_id = _insert_file(conn, file_hash="h" * 64, status="processing")
        processor = BackgroundProcessor(max_retries=3, retry_delay=0.01, pool=pool)
        task = TaskItem(
            file_path="uploads/doc.pdf",
            attempt=1,
            source="upload",
            vault_id=1,
            file_id=file_id,
            file_hash="h" * 64,
        )
        await processor._handle_failure(  # noqa: SLF001
            task, sqlite3.IntegrityError("FOREIGN KEY constraint failed")
        )
        await asyncio.sleep(0.05)
        requeued = []
        try:
            while True:
                requeued.append(processor.queue.get_nowait())
        except asyncio.QueueEmpty:
            pass
        assert requeued, (
            "a nonduplicate integrity failure keeps the generic ladder (a "
            "retry is scheduled); it must not be duplicate-settled"
        )
        row = conn.execute(
            "SELECT error_message FROM files WHERE id = ?", (file_id,)
        ).fetchone()
        assert not (row["error_message"] or "").startswith("DUPLICATE_FILE:")
    finally:
        pool.close_all()
        conn.close()


# ---------------------------------------------------------------------------
# Finalization backstop (test 8)
# ---------------------------------------------------------------------------


async def test_finalization_race_backstop_settles_row(tmp_path, monkeypatch):
    """The round-3 critic's executed shape: a confirmed-duplicate
    IntegrityError raised AT finalization (after the processing try) must
    still leave the row OUT of 'processing', carrying DUPLICATE_FILE."""
    import sqlite3 as sq

    db_path, conn = _seed(tmp_path, "fin.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    try:
        file_path, file_hash = _real_file(tmp_path)
        subject_id = _insert_file(conn, file_hash=file_hash, status="pending")
        stub = _RecordingVectorStore()
        processor = DocumentProcessor(pool=pool, vector_store=stub)

        async def _fake_finalize(**kwargs):  # noqa: ARG001
            raise sq.IntegrityError(
                "UNIQUE constraint failed: files.file_hash, files.vault_id"
            )

        monkeypatch.setattr(processor, "_finalize_indexed_success", _fake_finalize)
        # Skip the parse pipeline: drive the guard-clean path by giving the
        # subject no owner (guard passes), and stub the parse stages the way
        # the settle path needs — simplest faithful shape: pre-set the file
        # row to processing and call through the guarded pipeline with the
        # schema-file path replaced by a minimal parser stub.
        monkeypatch.setattr(
            processor,
            "_process_schema_file",
            _fake_schema_file,
        )
        monkeypatch.setattr(
            processor, "_is_schema_file", lambda path: True
        )

        class _EmbedStub:
            async def embed_batch(self, texts, fail_fast=True, **kwargs):  # noqa: ARG002
                embeddings = [[0.1, 0.2] for _ in texts]
                if fail_fast:
                    return embeddings
                return embeddings, []

        processor.embedding_service = _EmbedStub()
        with pytest.raises(sq.IntegrityError):
            await processor.process_existing_file(
                subject_id, file_path, 1, file_hash=file_hash
            )
        row = conn.execute(
            "SELECT status, error_message, phase_message FROM files WHERE id = ?",
            (subject_id,),
        ).fetchone()
        assert row["status"] == "error", (
            "the fenced finalization backstop must settle the row out of "
            f"'processing'; got {row['status']!r}"
        )
        assert (row["error_message"] or "").startswith("DUPLICATE_FILE:")
        assert (row["phase_message"] or "").startswith("DUPLICATE_FILE:")
    finally:
        pool.close_all()
        conn.close()


async def _fake_schema_file(file_path, file_id, generation_hash, parser_fingerprint):  # noqa: ARG001
    """Minimal parsed-document stub for the backstop test (schema-file arm:
    the shortest route through the guarded pipeline)."""
    from app.services.chunking import ProcessedChunk
    from app.services.document_processor import ParsedDocument

    chunks = [
        ProcessedChunk(
            text="x",
            metadata={"section": "s", "total_chunks": 1},
            chunk_index=0,
        )
    ]
    return chunks, "x", ParsedDocument(atoms=(), parser_fingerprint=parser_fingerprint)


# ---------------------------------------------------------------------------
# Reindex job semantics (tests 3, 10, 11) — the lease settler
# ---------------------------------------------------------------------------


def _seed_reindex_job(conn, *, payload_extra=None):
    ensure_jobs_schema(conn)
    conn.execute(
        "INSERT INTO jobs (queue, payload_json, status) VALUES (?, ?, 'pending')",
        (
            REINDEX_QUEUE,
            json.dumps({"vault_id": 1, **(payload_extra or {})}),
        ),
    )
    conn.commit()


async def _drive_reindex_settle(conn, pool, lease_conn, worker_id, monkeypatch, *, result, exc=None, attempts=1, max_attempts=3):
    processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
    monkeypatch.setattr(settings, "jobs_max_attempts", max_attempts)

    async def _fake_reindex(job_id, vault_id, retry_file_ids=None):  # noqa: ARG001
        return result["status"], dict(result["result"]), exc

    monkeypatch.setattr(processor, "_reindex_embed_all", _fake_reindex)
    lease = JobLease(lease_conn, reclaim_timeout_seconds=300.0, max_attempts=3)
    row = lease.claim(REINDEX_QUEUE, worker_id)
    assert row is not None
    row = dict(row) if not isinstance(row, dict) else row
    row["attempts"] = attempts
    await processor._run_reindex_job_row(row, worker_id)  # noqa: SLF001


async def test_reindex_duplicate_only_settles_failed_no_cap_prefix(
    tmp_path, monkeypatch
):
    """Duplicate-ONLY reindex failure: the job settles 'failed' on the first
    attempt with the composed duplicate outcome and NO attempt_cap_exceeded
    prefix."""
    db_path, conn = _seed(tmp_path, "rx1.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    lease_conn = _connect(db_path)
    try:
        _seed_reindex_job(conn)
        await _drive_reindex_settle(
            conn,
            pool,
            lease_conn,
            "rx1-worker",
            monkeypatch,
            result={
                "status": "failed",
                "result": {
                    "processed": 0,
                    "failed": 1,
                    "details": ["file_id=7: DUPLICATE_FILE: duplicate"],
                    "failed_ids": [7],
                    "duplicate_file_ids": [7],
                },
            },
            exc=DuplicateFileError("dup"),
        )
        jobs_row = conn.execute(
            "SELECT status, error, result_json, payload_json FROM jobs"
            " WHERE queue = ? ORDER BY id DESC LIMIT 1",
            (REINDEX_QUEUE,),
        ).fetchone()
        assert jobs_row["status"] == "failed"
        err = jobs_row["error"] or ""
        assert "DUPLICATE_FILE" in err and "attempt_cap_exceeded" not in err
        result_json = json.loads(jobs_row["result_json"] or "{}")
        assert result_json.get("duplicate_file_ids") == [7]
        assert result_json.get("failed") == 1, (
            "single-attempt duplicate-only 'failed' stays the distinct "
            f"terminal count; result={result_json}"
        )
    finally:
        pool.close_all()
        conn.close()
        lease_conn.close()


async def test_reindex_mixed_retry_scope_excludes_duplicate_and_composes(
    tmp_path, monkeypatch
):
    """Two-attempt mixed reindex: attempt 1 (duplicate + transient) requeues
    ONLY the transient id and persists the duplicate set in the payload;
    attempt 2 (transient now succeeds) still settles 'failed' with the
    CUMULATIVE duplicate composed into result_json."""
    db_path, conn = _seed(tmp_path, "rx2.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    lease_conn = _connect(db_path)
    try:
        _seed_reindex_job(conn)
        await _drive_reindex_settle(
            conn,
            pool,
            lease_conn,
            "rx2-worker",
            monkeypatch,
            result={
                "status": "failed",
                "result": {
                    "processed": 0,
                    "failed": 2,
                    "details": ["file_id=7: DUPLICATE_FILE", "file_id=9: transient"],
                    "failed_ids": [7, 9],
                    "duplicate_file_ids": [7],
                },
            },
            exc=DuplicateFileError("dup"),
        )
        jobs_row = conn.execute(
            "SELECT status, payload_json FROM jobs WHERE queue = ?"
            " ORDER BY id DESC LIMIT 1",
            (REINDEX_QUEUE,),
        ).fetchone()
        assert jobs_row["status"] == "pending", "below-cap mixed failure requeues"
        payload = json.loads(jobs_row["payload_json"] or "{}")
        assert payload.get("retry_file_ids") == [9], (
            "the duplicate id (7) must be excluded from the retry scope; "
            f"payload={payload}"
        )
        assert payload.get("duplicate_file_ids") == [7]

        # Attempt 2: the transient id now succeeds; prior duplicates persist.
        row2 = conn.execute(
            "SELECT id FROM jobs WHERE queue = ? ORDER BY id DESC LIMIT 1",
            (REINDEX_QUEUE,),
        ).fetchone()
        job_id = int(row2["id"])
        conn.execute(
            "UPDATE jobs SET run_after = NULL WHERE id = ?", (job_id,)
        )
        conn.commit()
        conn.commit()
        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        monkeypatch.setattr(settings, "jobs_max_attempts", 3)

        async def _fake_reindex2(jid, vault_id, retry_file_ids=None):  # noqa: ARG001
            return "completed", {"processed": 1, "failed": 0}, None

        monkeypatch.setattr(processor, "_reindex_embed_all", _fake_reindex2)
        lease = JobLease(lease_conn, reclaim_timeout_seconds=300.0, max_attempts=3)
        claimed = lease.claim(REINDEX_QUEUE, "rx2-worker")
        assert claimed is not None
        claimed = dict(claimed) if not isinstance(claimed, dict) else claimed
        claimed["attempts"] = 2
        await processor._run_reindex_job_row(  # noqa: SLF001
            claimed, "rx2-worker"
        )
        final = conn.execute(
            "SELECT status, result_json FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        assert final["status"] == "failed", (
            "a job holding a permanent duplicate NEVER completes; got "
            f"{final['status']!r}"
        )
        result = json.loads(final["result_json"] or "{}")
        assert result.get("duplicate_file_ids") == [7], (
            "the terminal result composes the CUMULATIVE duplicate outcome; "
            f"result={result}"
        )
        assert result.get("failed") == 1, (
            "the completed arm's 'failed' count covers the DISTINCT "
            "terminal-file union (prior duplicate 7; the transient id "
            "succeeded), matching the cap and duplicate-only arms; "
            f"result={result}"
        )
    finally:
        pool.close_all()
        conn.close()
        lease_conn.close()


async def test_reindex_terminal_cap_composes_duplicates(tmp_path, monkeypatch):
    """Terminal-cap mixed variant: the transient ids exhaust the cap with
    duplicates present — the cap-path result_json composes the cumulative
    duplicates (not current-attempt-only)."""
    db_path, conn = _seed(tmp_path, "rx3.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    lease_conn = _connect(db_path)
    try:
        _seed_reindex_job(
            conn, payload_extra={"duplicate_file_ids": [7]}
        )
        await _drive_reindex_settle(
            conn,
            pool,
            lease_conn,
            "rx3-worker",
            monkeypatch,
            attempts=1,
            max_attempts=1,
            result={
                "status": "failed",
                "result": {
                    "processed": 0,
                    "failed": 1,
                    "details": ["file_id=9: transient"],
                    "failed_ids": [9],
                    "duplicate_file_ids": [],
                },
            },
            exc=RuntimeError("transient"),
        )
        jobs_row = conn.execute(
            "SELECT status, error, result_json FROM jobs WHERE queue = ?"
            " ORDER BY id DESC LIMIT 1",
            (REINDEX_QUEUE,),
        ).fetchone()
        assert jobs_row["status"] == "failed"
        assert "attempt_cap_exceeded" in (jobs_row["error"] or "")
        result = json.loads(jobs_row["result_json"] or "{}")
        assert result.get("duplicate_file_ids") == [7], (
            "the cap path composes the CUMULATIVE duplicates from the "
            f"payload; result={result}"
        )
        assert result.get("failed") == 2, (
            "the cap-path 'failed' count covers the DISTINCT terminal-file "
            "union (transient 9 + duplicate 7), not just the cumulative "
            f"duplicates; result={result}"
        )
    finally:
        pool.close_all()
        conn.close()
        lease_conn.close()


async def test_reindex_duplicate_only_attempt2_composes_prior_payload_duplicates(
    tmp_path, monkeypatch
):
    """Multi-attempt duplicate-only settlement: attempt 1 requeues the
    transient id with the duplicate set persisted in the payload; attempt 2
    finds that id ALSO a duplicate — the empty-retry-scope terminal
    settlement recomputes 'failed' to the DISTINCT terminal-file union
    (prior payload duplicates + this attempt's), exactly like the completed
    and cap arms (Stage B review r1)."""
    db_path, conn = _seed(tmp_path, "rx4.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    lease_conn = _connect(db_path)
    try:
        _seed_reindex_job(conn)
        # Attempt 1: duplicate 7 + transient 9 -> requeue 9, persist dup 7.
        await _drive_reindex_settle(
            conn,
            pool,
            lease_conn,
            "rx4-worker",
            monkeypatch,
            result={
                "status": "failed",
                "result": {
                    "processed": 0,
                    "failed": 2,
                    "details": ["file_id=7: DUPLICATE_FILE", "file_id=9: transient"],
                    "failed_ids": [7, 9],
                    "duplicate_file_ids": [7],
                },
            },
            exc=DuplicateFileError("dup"),
        )
        jobs_row = conn.execute(
            "SELECT id, payload_json FROM jobs WHERE queue = ?"
            " ORDER BY id DESC LIMIT 1",
            (REINDEX_QUEUE,),
        ).fetchone()
        payload = json.loads(jobs_row["payload_json"] or "{}")
        assert payload.get("retry_file_ids") == [9]
        assert payload.get("duplicate_file_ids") == [7]
        job_id = int(jobs_row["id"])

        # Attempt 2: file 9 is now ALSO a duplicate -> empty retry scope,
        # terminal duplicate-only settlement below the cap.
        conn.execute("UPDATE jobs SET run_after = NULL WHERE id = ?", (job_id,))
        conn.commit()
        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        monkeypatch.setattr(settings, "jobs_max_attempts", 3)

        async def _fake_reindex2(jid, vault_id, retry_file_ids=None):  # noqa: ARG001
            return (
                "failed",
                {
                    "processed": 0,
                    "failed": 1,
                    "details": ["file_id=9: DUPLICATE_FILE: duplicate"],
                    "failed_ids": [9],
                    "duplicate_file_ids": [9],
                },
                DuplicateFileError("dup"),
            )

        monkeypatch.setattr(processor, "_reindex_embed_all", _fake_reindex2)
        lease = JobLease(lease_conn, reclaim_timeout_seconds=300.0, max_attempts=3)
        claimed = lease.claim(REINDEX_QUEUE, "rx4-worker")
        assert claimed is not None
        claimed = dict(claimed) if not isinstance(claimed, dict) else claimed
        claimed["attempts"] = 2
        await processor._run_reindex_job_row(  # noqa: SLF001
            claimed, "rx4-worker"
        )
        final = conn.execute(
            "SELECT status, error, result_json FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        assert final["status"] == "failed"
        assert "attempt_cap_exceeded" not in (final["error"] or ""), (
            "the cap was never reached; no cap prefix on the "
            "duplicate-only settlement"
        )
        result = json.loads(final["result_json"] or "{}")
        assert result.get("duplicate_file_ids") == [7, 9], (
            f"the terminal result composes the cumulative duplicates; result={result}"
        )
        assert result.get("failed") == 2, (
            "the duplicate-only arm recomputes 'failed' to the DISTINCT "
            "terminal-file union (7 + 9), not the producer's "
            f"current-attempt-only count; result={result}"
        )
    finally:
        pool.close_all()
        conn.close()
        lease_conn.close()


# ---------------------------------------------------------------------------
# Reviewer round-1 CRITICAL: set_phase must run OUTSIDE the write session
# (pool.write_permit is the same non-reentrant Semaphore(1) under
# multi-worker ingestion — awaiting set_phase inside the session deadlocks).
# ---------------------------------------------------------------------------


async def test_finalization_backstop_no_deadlock_under_write_permit(
    tmp_path, monkeypatch
):
    """Wire the multi-worker permit (one Semaphore(1) on both the processor
    and the pool, exactly BackgroundProcessor.start's wiring) and drive the
    backstop: it must complete (no deadlock) and settle the row."""
    import asyncio as _aio

    db_path, conn = _seed(tmp_path, "fin-permit.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    try:
        file_path, file_hash = _real_file(tmp_path)
        subject_id = _insert_file(conn, file_hash=file_hash, status="pending")
        stub = _RecordingVectorStore()
        processor = DocumentProcessor(pool=pool, vector_store=stub)
        permit = _aio.Semaphore(1)
        processor._write_semaphore = permit
        pool.write_permit = permit

        async def _fake_finalize(**kwargs):  # noqa: ARG001
            raise sqlite3.IntegrityError(
                "UNIQUE constraint failed: files.file_hash, files.vault_id"
            )

        monkeypatch.setattr(processor, "_finalize_indexed_success", _fake_finalize)
        monkeypatch.setattr(processor, "_process_schema_file", _fake_schema_file)
        monkeypatch.setattr(processor, "_is_schema_file", lambda path: True)

        class _EmbedStub:
            async def embed_batch(self, texts, fail_fast=True, **kwargs):  # noqa: ARG002
                embeddings = [[0.1, 0.2] for _ in texts]
                return embeddings if fail_fast else (embeddings, [])

        processor.embedding_service = _EmbedStub()
        with pytest.raises(sqlite3.IntegrityError):
            await _aio.wait_for(
                processor.process_existing_file(
                    subject_id, file_path, 1, file_hash=file_hash
                ),
                timeout=10,
            )
        row = conn.execute(
            "SELECT status, error_message, phase_message FROM files WHERE id = ?",
            (subject_id,),
        ).fetchone()
        assert row["status"] == "error"
        assert (row["error_message"] or "").startswith("DUPLICATE_FILE:")
    finally:
        pool.close_all()
        conn.close()


# ---------------------------------------------------------------------------
# Reviewer round-1 IMPORTANT 3: a GLOBAL failure shape must not terminate a
# mixed job early nor wipe the pending retry scope.
# ---------------------------------------------------------------------------


async def test_reindex_global_failure_keeps_scope_and_requeues(
    tmp_path, monkeypatch
):
    """Attempt 2 returns ("failed", {}, exc) with prior duplicates + a
    pending retry scope: the job must REQUEUE (not terminate) and the
    payload must keep its retry_file_ids."""
    db_path, conn = _seed(tmp_path, "rx4.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    lease_conn = _connect(db_path)
    try:
        _seed_reindex_job(
            conn, payload_extra={"duplicate_file_ids": [7], "retry_file_ids": [9]}
        )
        await _drive_reindex_settle(
            conn,
            pool,
            lease_conn,
            "rx4-worker",
            monkeypatch,
            attempts=2,
            result={"status": "failed", "result": {}},
            exc=RuntimeError("global transient"),
        )
        jobs_row = conn.execute(
            "SELECT status, payload_json FROM jobs WHERE queue = ?"
            " ORDER BY id DESC LIMIT 1",
            (REINDEX_QUEUE,),
        ).fetchone()
        assert jobs_row["status"] == "pending", (
            "a global failure with a pending retry scope must requeue, not "
            f"terminate; got {jobs_row['status']!r}"
        )
        payload = json.loads(jobs_row["payload_json"] or "{}")
        assert payload.get("retry_file_ids") == [9], (
            "the prior retry scope must survive a global failure; "
            f"payload={payload}"
        )
        assert payload.get("duplicate_file_ids") == [7]
    finally:
        pool.close_all()
        conn.close()
        lease_conn.close()

# ---------------------------------------------------------------------------
# FIX-1 (review TF-001): the REAL _reindex_embed_all production wiring.
# The settler legs above monkeypatch _reindex_embed_all; this leg drives the
# REAL runner over a seeded indexed owner + same-hash target so a regression
# in the per-file classify/append (background_tasks.py ~5485-5490 — the only
# production producer of duplicate_file_ids) fails THIS test instead of
# shipping mock-fed (mutation M1: delete the classify/append -> RED here).
# ---------------------------------------------------------------------------


async def test_reindex_runner_duplicate_file_ids_end_to_end(tmp_path, monkeypatch):
    """The real _reindex_embed_all classifies a same-hash target into
    result["duplicate_file_ids"] and the per-file details carry the
    DUPLICATE_FILE stable code."""
    db_path, conn = _seed(tmp_path, "e2e.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    try:
        file_path, file_hash = _real_file(tmp_path)
        owner_id = _insert_file(
            conn, file_hash=file_hash, status="indexed", path=file_path
        )
        target_id = _insert_file(
            conn,
            file_hash=file_hash,
            status="error",
            path=file_path,
        )
        assert owner_id != target_id
        bp = BackgroundProcessor(pool=pool, retry_delay=0.05)
        stub = _RecordingVectorStore()
        bp.processor.vector_store = stub

        class _EmbedStub:
            async def embed_batch(self, texts, fail_fast=True, **kwargs):  # noqa: ARG002
                embeddings = [[0.1, 0.2] for _ in texts]
                return embeddings if fail_fast else (embeddings, [])

        bp.processor.embedding_service = _EmbedStub()
        monkeypatch.setattr(bp.processor, "_is_schema_file", lambda p: True)

        async def _fake_schema_file(fpath, fid, generation_hash, parser_fp):  # noqa: ARG001
            from app.services.chunking import ProcessedChunk
            from app.services.document_processor import ParsedDocument

            chunks = [
                ProcessedChunk(
                    text="duplicate e2e body",
                    metadata={"section": "s", "total_chunks": 1},
                    chunk_index=0,
                )
            ]
            return chunks, "duplicate e2e body", ParsedDocument(
                atoms=(), parser_fingerprint=parser_fp
            )

        monkeypatch.setattr(bp.processor, "_process_schema_file", _fake_schema_file)

        status, result, error = await bp._reindex_embed_all(1, vault_id=1)  # noqa: SLF001

        assert status == "failed"
        assert result["duplicate_file_ids"] == [target_id], (
            "the REAL reindex runner must classify the same-hash target into "
            f"duplicate_file_ids; got {result} (error={error!r})"
        )
        assert target_id in result["failed_ids"]
        assert owner_id not in result.get("duplicate_file_ids", []), (
            "the indexed owner must not be reported as a duplicate (own-row "
            "exclusion at runner level)"
        )
        assert any(
            "DUPLICATE_FILE" in entry for entry in result.get("details", [])
        ), f"per-file details must carry the DUPLICATE_FILE stable code; got {result.get('details')}"
        # Exactly ONE generation written (the owner's): the duplicate target
        # must stop at the pre-vector guard with zero vector writes.
        assert stub.calls.count("add_chunks") == 1, (
            f"exactly one vector generation (the owner's) may be written; "
            f"got {stub.calls}"
        )
    finally:
        pool.close_all()
        conn.close()
