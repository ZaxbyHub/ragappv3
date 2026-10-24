"""Supporting (non-frozen) tests for the #1010/#1011 incident fixes.

Mirrors the frozen harnesses (tests/test_f3_stalled_reindex_lease.py,
tests/test_vector_lock_contention_retry.py): real schema via
``run_migrations`` on a temp DB, ``SQLiteConnectionPool``, the real
``_reindex_lease_worker_loop`` / ``_janitor_sweep_once`` /
``_process_ingest_job_row`` entry points, and doubles only at the documented
seams (``processor.process_existing_file`` for the reindex loop, the
VectorStore ``_add_chunks_unlocked`` data-plane seam with the REAL lock
discipline, the embedding service). Crashes are simulated by backdating
watermarks/heartbeats — never by killing processes.

Coverage (support surface, one level below the frozen checks):
* watchdog-first end-to-end stall release + same-worker re-claim (#1010);
* janitor-first backstop reclaim with no error-column rewrite (#1010);
* cap-aware stall settle (janitor sweep AND worker settle compositions,
  both stamping completed_at) (#1010);
* healthy reader: a fresh watermark reads stalled=False on BOTH reindex
  job readers (#1010);
* mutation-aware cancellation restore via the REAL process_existing_file
  (undisturbed -> prior status; disturbed+reindex -> 'error' with the
  REINDEX_INTERRUPTED code) and the exact $.retry_file_ids scope for a
  3-file run cancelled on file 2 (#1010);
* real-timing budget: jobs_stall_timeout_seconds=1.0 releases the row in
  ~1-2.5s wall (#1010);
* immediate file-state after one write-lock contention attempt (never
  'error', no error_message overwrite, VECTOR_STORE_LOCK_TIMEOUT
  classification incl. cause-wrapped contention) (#1011);
* legacy _handle_failure never-terminal for contention incl. the
  backlog-full pending/queued handoff (#1011).
"""

import asyncio
import inspect
import json
import sqlite3
import sys
import types
from types import SimpleNamespace

from app.config import settings
from app.models.database import SQLiteConnectionPool, run_migrations
from app.services.background_tasks import BackgroundProcessor, TaskItem
from app.services.job_lease import JobLease, ensure_jobs_schema
from app.services.vector_store import WriteLockTimeoutError

REINDEX_QUEUE = "reindex"
INGESTION_QUEUE = "ingestion"
ADMIN = {"id": 7, "username": "support-admin", "role": "superadmin"}

# The frozen sibling-suite stub (test_vector_lock_contention_retry.py idiom):
# unstructured is absent in the offline test env; DocumentParser's lazy import
# resolves with a zero-element partition.
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


def _seed(tmp_path, name):
    db_path = str(tmp_path / name)
    run_migrations(db_path)
    return db_path, _connect(db_path)


def _insert_file(conn, *, status, vault_id=1, name, phase="queued"):
    cur = conn.execute(
        "INSERT INTO files (vault_id, file_path, file_name, file_size, "
        "status, phase, source) VALUES (?, ?, ?, 1, ?, ?, 'upload')",
        (vault_id, f"uploads/{name}", name, status, phase),
    )
    conn.commit()
    return int(cur.lastrowid)


def _insert_job(
    conn,
    payload,
    *,
    status,
    worker_id=None,
    attempts=0,
    heartbeat_age_seconds=None,
    progress_age_seconds=None,
):
    ensure_jobs_schema(conn)
    cur = conn.execute(
        "INSERT INTO jobs (queue, payload_json, status, worker_id, attempts) "
        "VALUES (?, ?, ?, ?, ?)",
        (REINDEX_QUEUE, json.dumps(payload), status, worker_id, attempts),
    )
    job_id = int(cur.lastrowid)
    if heartbeat_age_seconds is not None:
        conn.execute(
            "UPDATE jobs SET heartbeat_at = datetime('now', ?) WHERE id = ?",
            (f"-{heartbeat_age_seconds} seconds", job_id),
        )
    if progress_age_seconds is not None:
        conn.execute(
            "UPDATE jobs SET progress_at = "
            "strftime('%Y-%m-%d %H:%M:%f', 'now', ?) WHERE id = ?",
            (f"-{progress_age_seconds} seconds", job_id),
        )
    conn.commit()
    return job_id


async def _drive_reindex_row(conn, deadline_s, *, want_error=None):
    """Poll the reindex row until its persisted ``error`` matches.

    Watchdog-FIRST observation: no janitor sweep is driven here — the worker
    loop's own watchdog must cancel the wedged run and the worker itself must
    settle (the below-cap settle requeues with ``error`` set; the loop
    re-claims almost immediately and a claim does NOT clear the error column,
    so matching on the persisted error text is race-free).

    Returns the first matching row (None at the deadline).
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    while loop.time() - started < deadline_s:
        await asyncio.sleep(0.05)
        row = conn.execute(
            "SELECT status, attempts, error, payload_json, completed_at, "
            "lease_generation FROM jobs WHERE queue = ? ORDER BY id DESC "
            "LIMIT 1",
            (REINDEX_QUEUE,),
        ).fetchone()
        if row is None:
            continue
        if want_error is None or want_error in (row["error"] or ""):
            return row
    return None


# ---------------------------------------------------------------------------
# #1010 — watchdog-first end-to-end release
# ---------------------------------------------------------------------------


async def test_watchdog_first_stall_release_and_same_worker_reclaim(
    tmp_path, monkeypatch
):
    """A live-but-stalled worker: watchdog cancels, below-cap settle requeues
    with the stable stall reason, and the SAME worker loop re-claims."""
    db_path, conn = _seed(tmp_path, "sup-a.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    worker = None
    try:
        file_id = _insert_file(conn, status="indexed", name="sup-a-file.txt")
        _insert_job(conn, {"vault_id": None}, status="pending")

        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        await processor._run_jobs_migration()  # noqa: SLF001

        async def stalled_double(existing_id, file_path, vault_id, **kwargs):  # noqa: ARG001
            await asyncio.Event().wait()

        monkeypatch.setattr(
            processor.processor, "process_existing_file", stalled_double
        )
        monkeypatch.setattr(settings, "jobs_heartbeat_interval_seconds", 0.2)
        monkeypatch.setattr(settings, "jobs_stall_timeout_seconds", 1.5)

        worker = asyncio.create_task(processor._reindex_lease_worker_loop())  # noqa: SLF001

        row = await _drive_reindex_row(
            conn, 15.0, want_error="lease_stall_reclaimed"
        )
        assert row is not None, (
            "the below-cap stall settle must persist the stable "
            "lease_stall_reclaimed reason; row never settled with it"
        )
        assert row["error"].startswith("lease_stall_reclaimed"), (
            f"jobs.error must carry the stall reason, got {row['error']!r}"
        )
        assert row["completed_at"] is None, (
            "the below-cap settle is a requeue, not terminal — completed_at "
            "must stay unstamped"
        )
        # The settled retry scope is the interrupted file only (single-file
        # run cancelled inside its one file).
        payload = json.loads(row["payload_json"])
        assert payload.get("retry_file_ids") == [file_id], (
            f"$.retry_file_ids must be the interrupted file, got {payload!r}"
        )
        # Same-worker re-claim proof: the wedged first attempt can never
        # re-claim itself, so attempts > 1 means this loop re-claimed the
        # released row (the settle->re-claim transition is fast; poll for it).
        loop = asyncio.get_running_loop()
        reclaim_deadline = loop.time() + 10.0
        attempts_now = int(row["attempts"])
        while attempts_now < 2 and loop.time() < reclaim_deadline:
            await asyncio.sleep(0.05)
            fresh = conn.execute(
                "SELECT attempts FROM jobs WHERE queue = ? "
                "ORDER BY id DESC LIMIT 1",
                (REINDEX_QUEUE,),
            ).fetchone()
            attempts_now = int(fresh["attempts"]) if fresh else attempts_now
        assert attempts_now >= 2, (
            "the worker loop must re-claim the stall-released row without a "
            f"restart; attempts={attempts_now}"
        )
        # The interrupted file sits in a selectable state (never stranded in
        # 'processing'; the hanging double never mutated it, and the REAL
        # process_existing_file restore is pinned separately below).
        frow = conn.execute(
            "SELECT status FROM files WHERE id = ?", (file_id,)
        ).fetchone()
        assert frow["status"] in ("indexed", "partial", "error", "pending"), (
            "the interrupted file must be left selectable for the narrowed "
            f"retry, got status={frow['status']!r}"
        )
    finally:
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        pool.close_all()


async def test_real_timing_budget_releases_running_row(tmp_path, monkeypatch):
    """jobs_stall_timeout_seconds=1.0 must release the row in ~1-2.5s wall
    (the deadline-driven watchdog at production-scale seconds, not the
    accelerated sub-second sweeps)."""
    db_path, conn = _seed(tmp_path, "sup-f.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    worker = None
    try:
        _insert_file(conn, status="indexed", name="sup-f-file.txt")
        _insert_job(conn, {"vault_id": None}, status="pending")

        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        await processor._run_jobs_migration()  # noqa: SLF001

        async def stalled_double(existing_id, file_path, vault_id, **kwargs):  # noqa: ARG001
            await asyncio.Event().wait()

        monkeypatch.setattr(
            processor.processor, "process_existing_file", stalled_double
        )
        monkeypatch.setattr(settings, "jobs_heartbeat_interval_seconds", 0.2)
        monkeypatch.setattr(settings, "jobs_stall_timeout_seconds", 1.0)

        worker = asyncio.create_task(processor._reindex_lease_worker_loop())  # noqa: SLF001
        loop = asyncio.get_running_loop()
        started = loop.time()
        released = False
        while loop.time() - started < 6.0:
            await asyncio.sleep(0.05)
            row = conn.execute(
                "SELECT status, attempts FROM jobs "
                "WHERE queue = ? ORDER BY id DESC LIMIT 1",
                (REINDEX_QUEUE,),
            ).fetchone()
            if row is not None and (
                row["status"] != "running" or int(row["attempts"]) >= 2
            ):
                released = True
                break
        elapsed = loop.time() - started
        assert released, (
            "a never-progressing run under a 1.0s stall budget must leave "
            "'running' (settle or re-claim) well inside the wall window"
        )
        assert elapsed <= 2.5, (
            f"the watchdog released the row only after {elapsed:.2f}s with a "
            "1.0s budget — the release must be deadline-driven, not "
            "reclaim-timeout-driven"
        )
    finally:
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        pool.close_all()


async def test_three_file_run_cancelled_on_file_two_scopes_retry_ids(
    tmp_path, monkeypatch
):
    """A 3-file run cancelled on file 2 records $.retry_file_ids exactly
    [file2, file3] — the interrupted file plus the remaining ones."""
    db_path, conn = _seed(tmp_path, "sup-e1.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    worker = None
    try:
        ids = [
            _insert_file(conn, status="indexed", name=f"sup-e1-{i}.txt")
            for i in range(3)
        ]
        _insert_job(conn, {"vault_id": None}, status="pending")

        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        await processor._run_jobs_migration()  # noqa: SLF001

        async def first_then_stall(existing_id, file_path, vault_id, **kwargs):  # noqa: ARG001
            if int(existing_id) == ids[0]:
                await asyncio.sleep(0.05)
                return None
            await asyncio.Event().wait()

        monkeypatch.setattr(
            processor.processor, "process_existing_file", first_then_stall
        )
        monkeypatch.setattr(settings, "jobs_heartbeat_interval_seconds", 0.2)
        # Budget wider than the fast first file (watermark refreshes after
        # it) but far below the second file's hang.
        monkeypatch.setattr(settings, "jobs_stall_timeout_seconds", 1.2)

        worker = asyncio.create_task(processor._reindex_lease_worker_loop())  # noqa: SLF001

        row = await _drive_reindex_row(
            conn, 15.0, want_error="lease_stall_reclaimed"
        )
        assert row is not None, "harness: the stalled run must settle"
        payload = json.loads(row["payload_json"])
        assert payload.get("retry_file_ids") == [ids[1], ids[2]], (
            "a run cancelled inside file 2 of 3 must retry exactly the "
            f"interrupted + remaining files [file2, file3], got "
            f"{payload.get('retry_file_ids')!r}"
        )
    finally:
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        pool.close_all()


# ---------------------------------------------------------------------------
# #1010 — janitor-first backstop + cap-aware settle + healthy reader
# ---------------------------------------------------------------------------


async def test_janitor_first_stall_reclaim_no_error_rewrite(tmp_path, monkeypatch):
    """A running row with a stale watermark and a FRESH heartbeat (no live
    watchdog can win) is reclaimed to pending by one janitor sweep, with NO
    error-column rewrite."""
    db_path, conn = _seed(tmp_path, "sup-b.db")
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        job_id = _insert_job(
            conn,
            {"vault_id": None},
            status="running",
            worker_id="sup-b-worker",
            attempts=1,
            heartbeat_age_seconds=2,  # FRESH: the age arm cannot fire
            progress_age_seconds=30,  # stale far beyond the 5s budget
        )
        assert conn.execute(
            "SELECT error FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()["error"] is None

        monkeypatch.setattr(settings, "jobs_stall_timeout_seconds", 5.0)
        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        await processor._run_jobs_migration()  # noqa: SLF001
        await processor._janitor_sweep_once()  # noqa: SLF001

        row = conn.execute(
            "SELECT status, worker_id, error, lease_generation FROM jobs "
            "WHERE id = ?",
            (job_id,),
        ).fetchone()
        assert row["status"] == "pending", (
            "the janitor's stall backstop must reclaim a live-heartbeat "
            f"no-progress row to pending, got {row['status']!r}"
        )
        assert row["worker_id"] is None, "reclaim must clear the worker id"
        assert row["error"] is None, (
            "a below-cap stall reclaim is a retry, not a failure — the error "
            f"column must not be rewritten, got {row['error']!r}"
        )
        assert int(row["lease_generation"]) == 1, "reclaim must bump the fence"
    finally:
        pool.close_all()


async def test_cap_aware_stall_settle_is_terminal_with_completed_at(
    tmp_path, monkeypatch
):
    """At/over the attempts cap, a stale-watermark running row settles
    terminally: the janitor sweep fails it with the attempt_cap_exceeded
    composition, and the worker's own cap settle composes the stall reason —
    both stamping completed_at."""
    db_path, conn = _seed(tmp_path, "sup-c.db")
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        cap = int(settings.jobs_max_attempts)
        assert cap >= 1
        janitor_job = _insert_job(
            conn,
            {"vault_id": None},
            status="running",
            worker_id="sup-c-janitor-worker",
            attempts=cap,
            heartbeat_age_seconds=2,  # fresh: only the stall arm matches
            progress_age_seconds=30,
        )
        settle_job = _insert_job(
            conn,
            {"vault_id": None},
            status="running",
            worker_id="sup-c-settle-worker",
            attempts=cap,
        )

        monkeypatch.setattr(settings, "jobs_stall_timeout_seconds", 5.0)
        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        await processor._run_jobs_migration()  # noqa: SLF001

        await processor._janitor_sweep_once()  # noqa: SLF001 — ONE sweep
        row = conn.execute(
            "SELECT status, error, completed_at FROM jobs WHERE id = ?",
            (janitor_job,),
        ).fetchone()
        assert row["status"] == "failed", (
            "a stale-watermark row at the attempts cap must settle "
            f"terminally, got {row['status']!r}"
        )
        assert "attempt_cap_exceeded" in (row["error"] or ""), (
            "the cap settle must carry the attempt_cap_exceeded composition, "
            f"got {row['error']!r}"
        )
        assert row["completed_at"] is not None, (
            "the terminal cap settle must stamp completed_at"
        )

        # The worker's own cap-aware settle (the arm the watchdog drives):
        # the composition wraps the stable stall reason.
        await processor._settle_stalled_reindex(  # noqa: SLF001
            settle_job, "sup-c-settle-worker"
        )
        row2 = conn.execute(
            "SELECT status, error, completed_at FROM jobs WHERE id = ?",
            (settle_job,),
        ).fetchone()
        assert row2["status"] == "failed"
        assert (row2["error"] or "").startswith(
            "attempt_cap_exceeded: lease_stall_reclaimed"
        ), (
            "the worker's cap settle must compose the cap prefix around the "
            f"stall reason, got {row2['error']!r}"
        )
        assert row2["completed_at"] is not None, (
            "lease.fail() must stamp completed_at on the cap settle"
        )
    finally:
        pool.close_all()


async def test_fresh_watermark_reads_not_stalled_on_both_readers(tmp_path):
    """A running row with a FRESH watermark reads stalled=False on BOTH
    get_reindex_job_status and list_reindex_jobs (route-function-direct, the
    f3/m04 idiom)."""
    from app.api.routes import documents

    db_path, conn = _seed(tmp_path, "sup-d.db")
    route_conn = _connect(db_path)
    try:
        job_id = _insert_job(
            conn,
            {"vault_id": None, "trigger_type": "api", "trigger_id": "7"},
            status="running",
            worker_id="sup-d-worker",
            attempts=1,
            heartbeat_age_seconds=0,  # fresh NOW
            progress_age_seconds=0,  # fresh NOW watermark
        )

        status_route = inspect.unwrap(documents.get_reindex_job_status)
        response = await status_route(
            SimpleNamespace(), job_id, route_conn, ADMIN
        )
        payload = (
            response.model_dump()
            if hasattr(response, "model_dump")
            else dict(response)
        )
        assert payload.get("status") == "running"
        assert payload.get("stalled") is False, (
            "a fresh progress watermark must read stalled=False on the "
            f"per-job reader, got {payload.get('stalled')!r}"
        )

        list_route = inspect.unwrap(documents.list_reindex_jobs)
        listed = await list_route(conn=route_conn, user=ADMIN)
        entries = (
            listed.model_dump().get("jobs")
            if hasattr(listed, "model_dump")
            else dict(listed)["jobs"]
        )
        match = [job for job in entries if job.get("id") == job_id]
        assert match, "harness: the seeded row must appear in the list"
        assert match[0].get("stalled") is False, (
            "a fresh progress watermark must read stalled=False on the list "
            f"reader, got {match[0].get('stalled')!r}"
        )
    finally:
        route_conn.close()


# ---------------------------------------------------------------------------
# #1010 — mutation-aware cancellation through the REAL process_existing_file
# ---------------------------------------------------------------------------


class _FakeEmbeddingService:
    """The C11 fake embedding seam; optionally hangs after signalling."""

    MAX_TEXT_LENGTH = 8192
    embedding_doc_prefix = ""

    def __init__(self, hang_event: "asyncio.Event | None" = None):
        self.entered = asyncio.Event()
        self._hang_event = hang_event

    async def embed_batch(self, texts, fail_fast=True, **kwargs):
        embeddings = [[0.1, 0.2, 0.3, 0.4] for _ in texts]
        if fail_fast:
            result = embeddings
        else:
            result = (embeddings, [])
        if self._hang_event is not None:
            self.entered.set()
            await self._hang_event.wait()
        return result


def _build_store(tmp_path, written, hang_event=None):
    """A real VectorStore with only the documented double seam replaced
    (the test_vector_lock_contention_retry.py shape: add_chunks keeps the
    REAL lock discipline; the data plane is the sanctioned double)."""
    from app.services.vector_store import VectorStore

    store = VectorStore(db_path=tmp_path / "sup-lance")

    async def _add_unlocked(records, *args, **kwargs):
        if hang_event is not None:
            await hang_event.wait()
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


async def test_undisturbed_cancellation_restores_prior_status(tmp_path):
    """Cancelling the REAL process_existing_file before any vector write
    (during the embed await) restores the prior 'indexed' status — the row is
    never left in 'processing'."""
    db_path, conn = _seed(tmp_path, "sup-e2.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    try:
        doc_path = tmp_path / "sup-e2-doc.sql"
        doc_path.write_text(SQL_FILE_BODY, encoding="utf-8")
        cur = conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_size, "
            "status, phase, source) VALUES (1, ?, ?, 1, 'indexed', "
            "'indexed', 'upload')",
            (str(doc_path), "sup-e2-doc.sql"),
        )
        file_id = int(cur.lastrowid)
        conn.commit()

        hang = asyncio.Event()
        embed = _FakeEmbeddingService(hang_event=hang)
        store = _build_store(tmp_path, [])
        processor = BackgroundProcessor(
            pool=pool, vector_store=store, embedding_service=embed
        )

        task = asyncio.create_task(
            processor.processor.process_existing_file(  # noqa: SLF001
                file_id, str(doc_path), 1
            )
        )
        await embed.entered.wait()  # inside the embed await: no vector write
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        row = conn.execute(
            "SELECT status FROM files WHERE id = ?", (file_id,)
        ).fetchone()
        assert row["status"] == "indexed", (
            "an undisturbed cancelled ingest must restore the prior "
            f"selectable status, got {row['status']!r}"
        )
    finally:
        pool.close_all()


async def test_disturbed_reindex_cancellation_settles_selectable_error(
    tmp_path,
):
    """Cancelling the REAL process_existing_file after the vector write began
    (inside add_chunks), with retry_context='reindex', settles the row to a
    selectable 'error' carrying the REINDEX_INTERRUPTED stable code."""
    db_path, conn = _seed(tmp_path, "sup-e3.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    try:
        doc_path = tmp_path / "sup-e3-doc.sql"
        doc_path.write_text(SQL_FILE_BODY, encoding="utf-8")
        cur = conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_size, "
            "status, phase, source) VALUES (1, ?, ?, 1, 'indexed', "
            "'indexed', 'upload')",
            (str(doc_path), "sup-e3-doc.sql"),
        )
        file_id = int(cur.lastrowid)
        conn.commit()

        hang = asyncio.Event()
        entered = asyncio.Event()
        embed = _FakeEmbeddingService()
        store = _build_store(tmp_path, [], hang_event=hang)
        real_unlocked = store._add_chunks_unlocked  # noqa: SLF001

        async def _hang_unlocked(records, *args, **kwargs):
            entered.set()
            return await real_unlocked(records, *args, **kwargs)

        store._add_chunks_unlocked = _hang_unlocked  # noqa: SLF001
        processor = BackgroundProcessor(
            pool=pool, vector_store=store, embedding_service=embed
        )

        task = asyncio.create_task(
            processor.processor.process_existing_file(  # noqa: SLF001
                file_id, str(doc_path), 1, retry_context="reindex"
            )
        )
        await entered.wait()  # inside add_chunks: the generation is disturbed
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        row = conn.execute(
            "SELECT status, error_message FROM files WHERE id = ?", (file_id,)
        ).fetchone()
        assert row["status"] == "error", (
            "a disturbed reindex cancellation must settle to the selectable "
            f"'error' state, got {row['status']!r}"
        )
        assert (row["error_message"] or "").startswith("REINDEX_INTERRUPTED"), (
            "the settled error_message must carry the stable "
            f"REINDEX_INTERRUPTED code, got {row['error_message']!r}"
        )
    finally:
        pool.close_all()


# ---------------------------------------------------------------------------
# #1011 — contention: immediate file-state + classification + legacy ladder
# ---------------------------------------------------------------------------


async def test_one_contention_attempt_leaves_row_retryable(tmp_path, monkeypatch):
    """One write-lock-contention attempt through the REAL durable ingest
    path: the files row is never 'error' (no error_message overwrite, no
    'error' phase), jobs.error classifies VECTOR_STORE_LOCK_TIMEOUT, and the
    row completes once the lock frees (the retryable state is real)."""
    from app.services.document_processor import (
        INGEST_ERROR_VECTOR_STORE_LOCK_TIMEOUT,
    )

    assert INGEST_ERROR_VECTOR_STORE_LOCK_TIMEOUT == "VECTOR_STORE_LOCK_TIMEOUT"

    db_path, conn = _seed(tmp_path, "sup-g.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    lease_conn = _connect(db_path)
    try:
        doc_path = tmp_path / "sup-g-doc.sql"
        doc_path.write_text(SQL_FILE_BODY, encoding="utf-8")
        cur = conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_size, "
            "status, phase, source) VALUES (1, ?, ?, 1, 'pending', "
            "'queued', 'upload')",
            (str(doc_path), "sup-g-doc.sql"),
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
        processor = BackgroundProcessor(
            pool=pool,
            retry_delay=0.05,
            vector_store=store,
            embedding_service=_FakeEmbeddingService(),
        )
        worker_id = "sup-g-ingest-worker"
        lease = JobLease(lease_conn, reclaim_timeout_seconds=300.0, max_attempts=3)

        monkeypatch.setattr(settings, "jobs_max_attempts", 3)
        monkeypatch.setattr(settings, "write_lock_timeout_seconds", 0.3)

        # The "index build" holds the store write lock across the attempt.
        await store._write_lock.acquire()  # noqa: SLF001
        try:
            row = lease.claim(INGESTION_QUEUE, worker_id)
            assert row is not None, "harness: the first claim must succeed"
            await processor._process_ingest_job_row(row, worker_id)  # noqa: SLF001
        finally:
            store._write_lock.release()  # noqa: SLF001

        frow = conn.execute(
            "SELECT status, phase, error_message FROM files WHERE id = ?",
            (file_id,),
        ).fetchone()
        assert frow["status"] != "error", (
            "a contention attempt must never terminally flip the files row "
            f"to error, got status={frow['status']!r}"
        )
        assert frow["error_message"] is None, (
            "a contention attempt must not overwrite error_message, got "
            f"{frow['error_message']!r}"
        )
        assert frow["phase"] != "error", (
            f"a contention attempt must not write phase='error', got {frow['phase']!r}"
        )
        jrow = conn.execute(
            "SELECT id, status, error FROM jobs WHERE queue = ? "
            "ORDER BY id DESC LIMIT 1",
            (INGESTION_QUEUE,),
        ).fetchone()
        assert jrow["status"] == "pending", (
            "the contention attempt must requeue (retryable), got "
            f"{jrow['status']!r}"
        )
        assert (jrow["error"] or "").startswith("VECTOR_STORE_LOCK_TIMEOUT"), (
            "jobs.error must carry the exact contention classification "
            f"string, got {jrow['error']!r}"
        )

        # The retryable state is real: once the lock frees, the next attempt
        # completes and indexes the file.
        conn.execute(
            "UPDATE jobs SET run_after = datetime('now', '-1 seconds') "
            "WHERE id = ? AND status = 'pending'",
            (jrow["id"],),
        )
        conn.commit()
        row2 = lease.claim(INGESTION_QUEUE, worker_id)
        assert row2 is not None, "harness: the requeued row must re-claim"
        await processor._process_ingest_job_row(row2, worker_id)  # noqa: SLF001
        final = conn.execute(
            "SELECT status FROM files WHERE id = ?", (file_id,)
        ).fetchone()
        jfinal = conn.execute(
            "SELECT status FROM jobs WHERE id = ?", (jrow["id"],)
        ).fetchone()
        assert jfinal["status"] == "completed" and final["status"] == "indexed", (
            "after the lock frees the retry must complete and index the "
            f"file; job={jfinal['status']!r} file={final['status']!r}"
        )
    finally:
        lease_conn.close()
        pool.close_all()


def test_contention_classification_strings():
    """Unit pins on the classifier: a real VectorStoreError classifies
    VECTOR_STORE_FAILED; a WriteLockTimeoutError — top-level or wrapped as a
    __cause__ — classifies VECTOR_STORE_LOCK_TIMEOUT."""
    from app.services.document_processor import classify_ingest_error
    from app.services.vector_store import VectorStoreError

    assert (
        classify_ingest_error(VectorStoreError("boom"))
        == "VECTOR_STORE_FAILED"
    ), "a genuine store rejection keeps the generic store-failure code"
    direct = WriteLockTimeoutError("write lock acquisition timed out")
    assert classify_ingest_error(direct) == "VECTOR_STORE_LOCK_TIMEOUT"
    # DocumentParseError-style wrap: the lock timeout rides the cause chain.
    wrapped = RuntimeError("failed to add chunks")
    wrapped.__cause__ = direct
    assert classify_ingest_error(wrapped) == "VECTOR_STORE_LOCK_TIMEOUT", (
        "a contention cause wrapped by another exception must still "
        "classify as contention (bounded cause-walk)"
    )


async def test_legacy_handle_failure_contention_never_terminal_backlog_full(
    tmp_path, monkeypatch
):
    """_handle_failure with task.attempt >= max_retries AND contention: no
    permanent failure, and with the retry backlog full the files row is
    persisted pending/queued (the recovery sweep's predicate) with NO
    in-memory reservation."""
    db_path, conn = _seed(tmp_path, "sup-h.db")
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        doc_path = tmp_path / "sup-h-doc.sql"
        doc_path.write_text(SQL_FILE_BODY, encoding="utf-8")
        cur = conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_size, "
            "status, phase, source) VALUES (1, ?, ?, 1, 'processing', "
            "'parsing', 'upload')",
            (str(doc_path), "sup-h-doc.sql"),
        )
        file_id = int(cur.lastrowid)
        conn.commit()

        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        assert processor.max_retries >= 1
        task = TaskItem(
            file_path=str(doc_path),
            vault_id=1,
            attempt=processor.max_retries + 2,  # WELL past the cap
            source="upload",
            file_id=file_id,
        )
        error = WriteLockTimeoutError("write lock acquisition timed out")

        scheduled: list[dict] = []
        monkeypatch.setattr(
            processor,
            "_schedule_retry",
            lambda *, queue, item, delay: scheduled.append(
                {"queue": queue, "delay": delay}
            )
            or False,  # backlog full: every scheduling attempt is refused
        )
        permanently_failed: list[tuple] = []
        monkeypatch.setattr(
            processor,
            "_mark_task_permanently_failed",
            lambda *args, **kwargs: permanently_failed.append(args),
        )

        await processor._handle_failure(task, error)  # noqa: SLF001

        assert not permanently_failed, (
            "contention is never terminal on the legacy transport even at "
            "attempt >= max_retries; _mark_task_permanently_failed must not "
            "run"
        )
        assert scheduled, "harness: the retry scheduler must have been tried"
        lock_window = float(settings.write_lock_timeout_seconds)
        delay = scheduled[0]["delay"]
        assert max(delay, lock_window) == delay >= lock_window, (
            "the contention backoff must be floored at the lock window "
            f"(delay={delay}, window={lock_window})"
        )
        assert delay <= max(60.0, lock_window), (
            f"the contention backoff must stay capped (delay={delay})"
        )
        row = conn.execute(
            "SELECT status, phase, error_message FROM files WHERE id = ?",
            (file_id,),
        ).fetchone()
        assert row["status"] == "pending" and row["phase"] == "queued", (
            "the backlog-full contention branch must persist the recovery "
            f"sweep's eligibility predicate, got {row['status']!r}/"
            f"{row['phase']!r}"
        )
        assert row["error_message"] is None, (
            f"no error_message overwrite expected, got {row['error_message']!r}"
        )
        async with processor._active_file_ids_lock:  # noqa: SLF001
            assert (
                file_id not in processor._active_file_ids  # noqa: SLF001
                and file_id not in processor._queued_file_ids  # noqa: SLF001
                and file_id not in processor._recovery_file_ids  # noqa: SLF001
            ), (
                "the backlog-full branch must create NO in-memory queued "
                "reservation (no ticket, no consumer)"
            )
    finally:
        pool.close_all()
