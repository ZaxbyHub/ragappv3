"""Issue #1011 acceptance checks — vector write-lock contention (frozen).

An ingest that collides with a long-held vector-store write lock (an index
build holds it) currently: (a) raises a plain VectorStoreError from the
real ``_acquire_write_lock`` site, classified as the string
"VECTOR_STORE_FAILED", and (b) burns the durable retry ladder's three
quick attempts against the still-held lock, then terminally fails the job
and flips the files row to ``status='error'``.

Checks (arm's-length, frozen):
* C11 (DISCRIMINATING): the durable ingest-job path (lease transport,
  ``_process_ingest_job_row``) driven over a REAL VectorStore whose only
  double is the documented ``_add_chunks_unlocked`` seam — while a
  background "index build" holds the store write lock for a bounded window
  — must complete (or stay retryable): the files row never flips to
  ``error`` and the job never settles ``attempt_cap_exceeded``. At base
  the three quick attempts burn out and the row errors, so the assertion
  fails for exactly that reason.
* C12 (DISCRIMINATING): the exception raised by the REAL
  ``_acquire_write_lock`` site must classify to something OTHER than the
  literal "VECTOR_STORE_FAILED". At base classify_ingest_error maps every
  VectorStoreError to exactly that string, so the assert fails.
"""

import asyncio
import json
import sqlite3
import sys
import types

from app.config import settings
from app.models.database import SQLiteConnectionPool, run_migrations
from app.services.document_processor import classify_ingest_error
from app.services.vector_store import VectorStore, VectorStoreError

INGESTION_QUEUE = "ingestion"

# Unstructured is absent in the offline test env; the sibling-suite stub
# (test_document_processor.py idiom) lets DocumentParser's lazy import
# resolve with a zero-element partition.
try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    _unstructured = types.ModuleType("unstructured")
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType("unstructured.partition")
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType("unstructured.partition.auto")
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    sys.modules["unstructured"] = _unstructured
    sys.modules["unstructured.partition"] = _unstructured.partition
    sys.modules["unstructured.partition.auto"] = _unstructured.partition.auto

SQL_FILE_BODY = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE
);
"""


def _connect(db_path):
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


class _FakeEmbeddingService:
    MAX_TEXT_LENGTH = 8192
    embedding_doc_prefix = ""

    async def embed_batch(self, texts, fail_fast=True):
        embeddings = [[0.1, 0.2, 0.3, 0.4] for _ in texts]
        if fail_fast:
            return embeddings
        # fail_fast=False contract: (embeddings, failed_batch_indices)
        return embeddings, []


def _build_store(tmp_path, written):
    """A real VectorStore with only the documented double seam replaced.

    ``add_chunks`` keeps the REAL lock discipline (``_acquire_write_lock``
    with the settings timeout and its raise site); the data-plane body is
    the ``_add_chunks_unlocked`` double the production docstring sanctions
    (vector_store.py: "test doubles commonly replace ``_add_chunks_unlocked``").
    """
    store = VectorStore(db_path=tmp_path / "vc-lance")

    async def _add_unlocked(records, *args, **kwargs):
        written.extend(records)
        return {}

    async def _count_by_file(file_id, target=None):  # noqa: ARG001
        return len(written)

    async def _get_live_dim():
        return None

    async def _init_table(embedding_dim, *args, **kwargs):  # noqa: ARG001
        return None

    async def _delete_old_generation(file_id, new_hash_short, *a, **k):  # noqa: ARG001
        return 0

    async def _delete_by_file(file_id, *args, **kwargs):  # noqa: ARG001
        return 0

    store._add_chunks_unlocked = _add_unlocked
    store.count_by_file = _count_by_file
    store.get_live_embedding_dim = _get_live_dim
    store.init_table = _init_table
    store.delete_old_generation_by_file = _delete_old_generation
    store.delete_by_file = _delete_by_file
    return store


async def test_ingest_survives_index_build_holding_write_lock(
    tmp_path, monkeypatch
):
    """C11: an ingest colliding with a held write lock must survive."""
    from app.services.background_tasks import BackgroundProcessor
    from app.services.job_lease import JobLease, ensure_jobs_schema

    db_path = str(tmp_path / "vc.db")
    run_migrations(db_path)
    conn = _connect(db_path)

    doc_path = tmp_path / "vc-doc.sql"
    doc_path.write_text(SQL_FILE_BODY, encoding="utf-8")
    cur = conn.execute(
        "INSERT INTO files (vault_id, file_path, file_name, file_size, "
        "status, phase, source) VALUES (1, ?, ?, 1, 'pending', 'queued', "
        "'upload')",
        (str(doc_path), "vc-doc.sql"),
    )
    file_id = int(cur.lastrowid)
    ensure_jobs_schema(conn)
    conn.execute(
        "INSERT INTO jobs (queue, payload_json, status) VALUES (?, ?, "
        "'pending')",
        (
            INGESTION_QUEUE,
            json.dumps(
                {"file_id": file_id, "vault_id": 1, "file_path": str(doc_path)}
            ),
        ),
    )
    conn.commit()

    written: list = []
    store = _build_store(tmp_path, written)
    pool = SQLiteConnectionPool(db_path, max_size=4)
    processor = BackgroundProcessor(
        pool=pool,
        retry_delay=0.05,
        vector_store=store,
        embedding_service=_FakeEmbeddingService(),
    )
    worker_id = "vc-ingest-worker"
    lease_conn = _connect(db_path)
    lease = JobLease(
        lease_conn,
        reclaim_timeout_seconds=300.0,
        max_attempts=3,
    )
    try:
        # The durable ladder: 3 attempts, quick backoff (0.05s base), and a
        # shrunk write-lock timeout so each attempt fails fast while the
        # lock is held.
        monkeypatch.setattr(settings, "jobs_max_attempts", 3)
        monkeypatch.setattr(processor, "retry_delay", 0.05)
        monkeypatch.setattr(settings, "write_lock_timeout_seconds", 0.3)

        # The "index build": holds the store write lock for a bounded
        # window, then releases it.
        hold_window_s = 8.0

        async def index_build_holds_write_lock():
            async with store._write_lock:  # noqa: SLF001 — the contention precondition
                await asyncio.sleep(hold_window_s)

        hold_task = asyncio.create_task(index_build_holds_write_lock())
        await asyncio.sleep(0)  # let the builder take the lock first

        loop = asyncio.get_running_loop()
        deadline = loop.time() + 30.0
        observed_file_statuses = []
        observed_job_states = []
        job_id = None
        while loop.time() < deadline:
            row = lease.claim(INGESTION_QUEUE, worker_id)
            if row is None:
                state = conn.execute(
                    "SELECT id, status, error FROM jobs "
                    "WHERE queue = ? ORDER BY id DESC LIMIT 1",
                    (INGESTION_QUEUE,),
                ).fetchone()
                if state is not None:
                    job_id = int(state["id"])
                    observed_job_states.append(
                        (state["status"], state["error"])
                    )
                frow = conn.execute(
                    "SELECT status FROM files WHERE id = ?", (file_id,)
                ).fetchone()
                if frow is not None:
                    observed_file_statuses.append(frow["status"])
                terminal = {"completed", "failed", "cancelled"}
                if state is not None and state["status"] in terminal:
                    break
                await asyncio.sleep(0.1)
                continue
            job_id = int(row["id"])
            await processor._process_ingest_job_row(row, worker_id)  # noqa: SLF001
            # Fast-forward any requeue delay so the next attempt is
            # claimable immediately (the b13 retry idiom).
            conn.execute(
                "UPDATE jobs SET run_after = datetime('now', '-1 seconds') "
                "WHERE id = ? AND status = 'pending'",
                (job_id,),
            )
            conn.commit()

        assert observed_job_states, "harness: the ingest job row must exist"
        final_status, final_error = observed_job_states[-1]

        assert "error" not in observed_file_statuses, (
            "an ingest colliding with an index build's write lock must "
            "never terminally flip the files row to status='error'; "
            f"observed file statuses={observed_file_statuses!r}"
        )
        capped = any(
            status == "failed"
            or (error or "").startswith("attempt_cap_exceeded")
            for status, error in observed_job_states
        )
        assert not capped, (
            "lock contention is a rebuild-aware retryable condition, not a "
            "terminal attempt-cap failure; observed job states="
            f"{observed_job_states!r}"
        )
        assert final_status in ("completed", "pending", "running"), (
            "the ingest must eventually complete (or stay retryable) once "
            f"the lock is released; final job state={final_status!r} "
            f"error={final_error!r}"
        )
    finally:
        if not hold_task.done():
            hold_task.cancel()
            await asyncio.gather(hold_task, return_exceptions=True)
        lease_conn.close()
        pool.close_all()


async def test_write_lock_timeout_not_classified_vector_store_failed(monkeypatch):
    """C12: the real lock-timeout raise must not classify as the generic
    VECTOR_STORE_FAILED store failure."""
    # Minimal REAL instance: _acquire_write_lock touches only _write_lock
    # and settings.write_lock_timeout_seconds.
    store = object.__new__(VectorStore)
    store._write_lock = asyncio.Lock()  # noqa: SLF001 — the attribute the raise site uses
    monkeypatch.setattr(settings, "write_lock_timeout_seconds", 0.1)

    # The lock is held by someone else (an index build).
    await store._write_lock.acquire()  # noqa: SLF001
    raised = None
    try:
        async with store._acquire_write_lock():  # noqa: SLF001 — the REAL raise site
            pass
    except VectorStoreError as exc:
        raised = exc
    finally:
        store._write_lock.release()  # noqa: SLF001

    assert raised is not None, (
        "harness: the real _acquire_write_lock must raise on timeout"
    )
    assert "timed out" in str(raised).lower(), (
        f"harness: expected the lock-timeout raise shape, got {raised!r}"
    )
    code = classify_ingest_error(raised)
    # String literal on purpose: no import of a not-yet-existing constant.
    assert code != "VECTOR_STORE_FAILED", (
        "a write-lock acquisition timeout must classify to a typed, "
        "distinguishable (rebuild-aware retryable) code — not the generic "
        f"VECTOR_STORE_FAILED store failure; classify_ingest_error returned "
        f"{code!r} for {raised!r}"
    )
