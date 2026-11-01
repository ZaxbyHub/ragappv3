"""Issue-tracer Phase 2.5 acceptance spec (B13): lease/reindex durability.

Eleven frozen acceptance checks over the DB-claimed lease surfaces
(``app/services/job_lease.py`` + the BackgroundProcessor lease paths in
``app/services/background_tasks.py``). Each test pins one documented
defect at master and is written arm's-length from any fix: they assert
end-state behavior through the same entry points production uses.

Fixture strategy mirrors the sibling lease suites:
- ``tests/test_559_s3_c5_reindex_lease.py`` — real schema via
  ``run_migrations`` on a temp DB + ``SQLiteConnectionPool`` + a
  ``BackgroundProcessor`` whose claim/janitor/migration entry points are
  invoked directly;
- ``tests/test_559_lease_integration.py`` — the ``_seed`` idiom (seeded
  DB, processor with ``_ingest_lease_enabled = True`` via a real pool);
- ``tests/test_559_s234_c9_rollback_surface.py`` — the
  ``_setting_overridden`` idiom for flipping env-only lease switches;
- ``tests/issue556_checks/test_startup_recovery_sweeps.py`` — the
  route-function-direct idiom via ``inspect.unwrap(documents.retry_...``).

Crashes are simulated by abandoning worker state and backdating
``heartbeat_at`` — never by killing processes or sleeping past a timeout.

Expectation: every check is RED at master for its documented reason and
turns GREEN under the B13 fix, without modification.
"""

import asyncio
import inspect
import json
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

from app.config import settings
from app.models.database import SQLiteConnectionPool, run_migrations
from app.services.background_tasks import BackgroundProcessor, TaskItem
from app.services.job_lease import JobLease, ensure_jobs_schema

INGESTION_QUEUE = "ingestion"
REINDEX_QUEUE = "reindex"
# Far beyond settings.jobs_lease_reclaim_timeout_seconds (default 300s):
# a backdated heartbeat this old is unambiguously expired.
STALE_SECONDS = 3600


@contextmanager
def _setting_overridden(name, value):
    """Toggle a settings field (the test_559_s234_c9 idiom).

    pydantic models reject setattr of unknown fields; a field that does
    not exist yields without touching anything so the surrounding check
    fails for its own behavioral reason instead of a fixture error.
    """
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
    # check_same_thread=False: processor helpers execute on the connection
    # via asyncio.to_thread, like the production pool connections they use.
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _seed(tmp_path, name="b13.db"):
    """The test_559_lease_integration ``_seed`` idiom: migrated temp DB."""
    db_path = str(tmp_path / name)
    run_migrations(db_path)
    return db_path, _connect(db_path)


def _insert_file(conn, *, status, vault_id=1, phase="queued", name="b13-file.txt"):
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
    """Seed one ``jobs`` row; a heartbeat age backdates it (crash idiom)."""
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


async def test_reindex_worker_survives_settle_exception(tmp_path, monkeypatch):
    """AC1: a transient settle error must not kill the reindex lease worker.

    ``_reindex_lease_worker_loop`` wraps ``_run_reindex_job_row`` in
    try/finally with no except: the first ``sqlite3.OperationalError``
    escaping the settle block (e.g. a locked database) ends the loop
    permanently, stranding every later pending reindex job.
    """
    db_path, conn = _seed(tmp_path)
    pool = SQLiteConnectionPool(db_path, max_size=4)
    try:
        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        for i in range(2):
            _insert_job(
                conn, REINDEX_QUEUE, {"vault_id": None, "seq": i}, status="pending"
            )
        # The boot migration opens the claim barrier the worker waits on.
        await processor._run_jobs_migration()  # noqa: SLF001

        embed_calls: list[int] = []

        async def fake_embed(job_id, vault_id):  # noqa: ARG001
            embed_calls.append(int(job_id))
            return ("completed", {}, None)

        monkeypatch.setattr(processor, "_reindex_embed_all", fake_embed)

        original_complete = JobLease.complete
        complete_calls = {"n": 0}

        def flaky_complete(self, job_id, worker_id, result=None):  # noqa: ARG001
            complete_calls["n"] += 1
            if complete_calls["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return original_complete(self, job_id, worker_id, result)

        monkeypatch.setattr(JobLease, "complete", flaky_complete)

        task = asyncio.create_task(
            processor._reindex_lease_worker_loop()  # noqa: SLF001
        )
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 5.0
            while loop.time() < deadline and len(embed_calls) < 2:
                await asyncio.sleep(0.05)
            calls = len(embed_calls)
            assert calls == 2, (
                "the worker loop must survive a transient settle exception "
                "and claim the second pending reindex job"
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    finally:
        pool.close_all()


async def test_capped_reindex_error_carries_redacted_per_file_detail(
    tmp_path, monkeypatch
):
    """AC2: a capped reindex failure must persist a redacted per-file detail.

    The per-file failure path returns ``("failed", result, None)`` so the
    cap message becomes ``attempt_cap_exceeded: None`` — neither useful
    for operators nor carrying the (redacted) per-file reason.
    """
    db_path, conn = _seed(tmp_path)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        file_id = _insert_file(conn, status="indexed", name="b13-doc.pdf")
        cap = int(settings.jobs_max_attempts)
        job_id = _insert_job(
            conn, REINDEX_QUEUE, {"vault_id": 1}, status="pending"
        )
        # Pre-seed attempts so the claim lands exactly at the cap.
        conn.execute(
            "UPDATE jobs SET attempts = ? WHERE id = ?", (cap - 1, job_id)
        )
        conn.commit()

        async def exploding(existing_id, file_path, vault_id, **kwargs):  # noqa: ARG001
            raise RuntimeError("boom /srv/b13-private/doc.pdf")

        monkeypatch.setattr(
            processor.processor, "process_existing_file", exploding
        )

        lease = JobLease(
            conn,
            reclaim_timeout_seconds=settings.jobs_lease_reclaim_timeout_seconds,
            max_attempts=cap,
        )
        row = lease.claim(REINDEX_QUEUE, "b13-reindex-worker")
        assert row is not None, "the seeded reindex job must be claimable"
        assert int(row["attempts"]) == cap

        await processor._run_reindex_job_row(  # noqa: SLF001
            row, "b13-reindex-worker"
        )

        error = conn.execute(
            "SELECT error FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()["error"]
        assert error is not None
        tail = error.split("attempt_cap_exceeded: ", 1)[-1]
        ok = tail not in ("None", "") and "/srv/b13-private" not in error
        assert int(ok) == 1, (
            "the terminal cap error must carry the (redacted) per-file "
            f"failure detail, not a bare None: {error!r}"
        )
    finally:
        pool.close_all()


async def test_reindex_retry_does_not_reembed_healthy_files(tmp_path, monkeypatch):
    """AC3: a failed reindex retry must not re-embed already-healthy files.

    The reindex scope is every ``status IN ('indexed','error')`` file and
    any single file failure requeues the WHOLE job, so each retry
    re-embeds every healthy document again.
    """
    db_path, conn = _seed(tmp_path)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        healthy_a = _insert_file(conn, status="indexed", name="b13-good-a.txt")
        healthy_b = _insert_file(conn, status="indexed", name="b13-good-b.txt")
        bad = _insert_file(conn, status="indexed", name="b13-bad.txt")
        job_id = _insert_job(
            conn, REINDEX_QUEUE, {"vault_id": 1}, status="pending"
        )

        calls: list[int] = []

        async def counting(existing_id, file_path, vault_id, **kwargs):  # noqa: ARG001
            calls.append(int(existing_id))
            if int(existing_id) == bad:
                raise RuntimeError("b13 permanently bad file")
            return None

        monkeypatch.setattr(processor.processor, "process_existing_file", counting)

        lease = JobLease(
            conn,
            reclaim_timeout_seconds=settings.jobs_lease_reclaim_timeout_seconds,
            max_attempts=int(settings.jobs_max_attempts),
        )

        row = lease.claim(REINDEX_QUEUE, "b13-retry-worker")
        assert row is not None
        await processor._run_reindex_job_row(  # noqa: SLF001
            row, "b13-retry-worker"
        )
        requeued = conn.execute(
            "SELECT status FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()["status"]
        assert requeued == "pending", "a below-cap failure must requeue for retry"

        # Let the backoff elapse immediately, then run the retry attempt.
        conn.execute(
            "UPDATE jobs SET run_after = datetime('now', '-1 seconds') "
            "WHERE id = ?",
            (job_id,),
        )
        conn.commit()
        row = lease.claim(REINDEX_QUEUE, "b13-retry-worker")
        assert row is not None, "the requeued job must be claimable again"
        await processor._run_reindex_job_row(  # noqa: SLF001
            row, "b13-retry-worker"
        )

        healthy_calls = sum(
            1 for call in calls if call in (healthy_a, healthy_b)
        )
        assert healthy_calls == 2, (
            "the retry attempt must not re-embed the files that already "
            f"re-embedded successfully on attempt 1: calls={calls!r}"
        )
    finally:
        pool.close_all()


async def test_boot_janitor_settlement_matches_docstring(tmp_path):
    """AC4: the janitor docstring's boot promise must hold in behavior.

    ``_ingest_janitor_loop`` documents that "settlement starts
    immediately" at boot, but ``reclaim_expired`` only settles heartbeats
    older than the full reclaim timeout — a boot-fresh orphaned lease
    (dead process, 1s-old heartbeat) leaves its files row stuck in
    ``processing`` for the whole timeout window.
    """
    db_path, conn = _seed(tmp_path)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        file_id = _insert_file(conn, status="processing", phase=None)
        _insert_job(
            conn,
            INGESTION_QUEUE,
            {"file_id": file_id, "vault_id": 1, "file_path": "uploads/b13-file.txt"},
            status="running",
            worker_id="b13-other-process-worker",
            attempts=1,
            heartbeat_age_seconds=1,  # FRESH: within the reclaim timeout
        )

        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        await processor._janitor_sweep_once()  # noqa: SLF001

        promise = (
            "settlement starts immediately"
            in inspect.getdoc(BackgroundProcessor._ingest_janitor_loop)
        )
        files_status = conn.execute(
            "SELECT status FROM files WHERE id = ?", (file_id,)
        ).fetchone()["status"]
        settled = files_status == "pending"
        broken = promise and not settled
        assert int(broken) == 0, (
            "the janitor docstring promises immediate boot settlement, but "
            f"the orphaned-at-boot file stayed {files_status!r}"
        )
    finally:
        pool.close_all()


async def test_queue_size_reports_durable_lease_backlog(tmp_path):
    """AC5: ``queue_size`` must count the durable lease backlog.

    In lease mode ``enqueue`` returns before any in-memory put, so
    ``self.queue.qsize()`` is always 0 — the operator-visible backlog
    metric is dead the moment the lease path is enabled.
    """
    db_path, conn = _seed(tmp_path)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        assert processor._ingest_lease_enabled, (  # noqa: SLF001
            "harness: the pooled processor must come up in lease mode"
        )
        for i in range(5):
            enqueued = await processor.enqueue(
                str(tmp_path / f"b13-{i}.txt"), 1, file_id=100 + i
            )
            assert enqueued is True
        assert processor.queue_size == 5, (
            "queue_size must report the durable pending ingestion backlog"
        )
    finally:
        pool.close_all()


async def test_cap_resync_ignores_historical_capped_jobs(tmp_path):
    """AC6: the cap resync must not act on historical capped jobs.

    The cap-resync SELECT matches every historical ``failed`` job with
    ``error='lease_attempt_cap_exceeded'`` — not just the ones the
    current sweep settled — so an unrelated pending file with an OLD
    capped job gets flipped to ``error`` on every sweep.
    """
    db_path, conn = _seed(tmp_path)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        file_f = _insert_file(conn, status="pending", name="b13-f.txt")
        file_g = _insert_file(conn, status="indexed", name="b13-g.txt")
        # F owns a HISTORICAL capped job plus fresh pending work.
        _insert_job(
            conn,
            INGESTION_QUEUE,
            {"file_id": file_f},
            status="failed",
            error="lease_attempt_cap_exceeded",
        )
        _insert_job(conn, INGESTION_QUEUE, {"file_id": file_f}, status="pending")
        # G's expired lease makes the sweep settle > 0 rows, so the resync
        # pass actually runs.
        _insert_job(
            conn,
            INGESTION_QUEUE,
            {"file_id": file_g},
            status="running",
            worker_id="b13-ghost-worker",
            attempts=1,
            heartbeat_age_seconds=STALE_SECONDS,
        )

        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        await processor._janitor_sweep_ingestion()  # noqa: SLF001

        status = conn.execute(
            "SELECT status FROM files WHERE id = ?", (file_f,)
        ).fetchone()["status"]
        assert status == "pending", (
            "a pending file whose capped job is HISTORY (not settled by "
            "this sweep) must not be flipped to error"
        )
    finally:
        pool.close_all()


async def test_boot_sync_does_not_bypass_cap_after_crash_window(tmp_path):
    """AC7: the boot sync must not mint a fresh zero-attempt job past the cap.

    The janitor's reclaim and the files resync run on separate
    connections. A crash in that window leaves the capped job terminally
    ``failed`` while the files row is still ``processing``; the boot
    sync's anti-join only excludes pending/running jobs, so it creates a
    brand-new zero-attempt job and the cap is silently bypassed.
    """
    db_path, conn = _seed(tmp_path)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        cap = int(settings.jobs_max_attempts)
        file_id = _insert_file(conn, status="processing", phase=None)
        _insert_job(
            conn,
            INGESTION_QUEUE,
            {"file_id": file_id, "vault_id": 1, "file_path": "uploads/b13-file.txt"},
            status="running",
            worker_id="b13-crashed-worker",
            attempts=cap,
            heartbeat_age_seconds=STALE_SECONDS,
        )

        # Commit 1 only — the "crash" skips the janitor's files resync.
        settled = JobLease(
            conn,
            reclaim_timeout_seconds=settings.jobs_lease_reclaim_timeout_seconds,
            max_attempts=cap,
        ).reclaim_expired(queue=INGESTION_QUEUE)
        assert settled == 1, "harness: the stale at-cap lease must settle"

        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        processor._sync_missing_ingest_job_rows()  # noqa: SLF001

        pending_jobs = int(
            conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE queue = ? AND status = "
                "'pending' AND CAST(json_extract(payload_json, '$.file_id') "
                "AS INTEGER) = ?",
                (INGESTION_QUEUE, file_id),
            ).fetchone()[0]
        )
        assert pending_jobs == 0, (
            "a file whose capped job just settled terminally must not get "
            "a fresh zero-attempt job from the boot sync"
        )
    finally:
        pool.close_all()


async def test_reverse_sync_preserves_running_reindex_row(tmp_path):
    """AC8: a lease-to-legacy rollback must preserve RUNNING reindex work.

    ``_reverse_sync_disabled_queues`` sources only ``status='pending'``
    jobs rows, so a rollback boot drops still-``running`` reindex work on
    the floor — it is copied back to neither legacy table nor claimable
    queue.
    """
    db_path, conn = _seed(tmp_path)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        _insert_job(
            conn, REINDEX_QUEUE, {"vault_id": 1}, status="running"
        )

        with _setting_overridden("reindex_job_lease_enabled", False):
            processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
            processor._reverse_sync_disabled_queues()  # noqa: SLF001

        pending_legacy = int(
            conn.execute(
                "SELECT COUNT(*) FROM document_reindex_jobs WHERE status = "
                "'pending'"
            ).fetchone()[0]
        )
        assert pending_legacy == 1, (
            "a rollback boot must copy a running reindex jobs row back to "
            "the legacy table (as claimable pending), not strand it"
        )
    finally:
        pool.close_all()


def test_release_refunds_claimed_attempt(tmp_path):
    """AC9: a graceful ``release`` must refund the claimed attempt.

    ``claim`` increments ``attempts`` but ``release`` does not refund it,
    so every graceful shutdown (release sweep, drained worker handback)
    burns one attempt of the durable retry budget for free.
    """
    conn = _connect(str(tmp_path / "b13-lease.db"))
    try:
        ensure_jobs_schema(conn)
        lease = JobLease(conn)
        job_id = lease.enqueue(INGESTION_QUEUE, {})
        row = lease.claim(INGESTION_QUEUE, "b13-release-worker")
        assert row is not None, "harness: the enqueued job must be claimable"
        assert lease.attempts_of(job_id) == 1, (
            "harness: claim must record exactly one attempt"
        )

        released = lease.release(job_id, "b13-release-worker")
        assert released, "harness: the held lease must be releasable"

        assert lease.attempts_of(job_id) == 0, (
            "release must refund the attempt the claim burned — an "
            "unworked handback must not consume the retry budget"
        )
    finally:
        conn.close()


async def test_stop_with_parked_retry_leaves_file_recoverable(tmp_path):
    """AC10: stop() must leave a parked-retry file recoverable.

    The retry branch of ``_handle_failure`` writes no files.status, and
    ``stop()`` discards ``_retry_backlog`` with a log line only — the
    files row stays ``processing`` forever (no worker, no ticket).
    """
    db_path, conn = _seed(tmp_path)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        file_id = _insert_file(conn, status="processing", phase=None)

        with (
            _setting_overridden("ingestion_job_lease_enabled", False),
            _setting_overridden("reindex_job_lease_enabled", False),
            _setting_overridden("wiki_kms_job_lease_enabled", False),
        ):
            processor = BackgroundProcessor(pool=pool, retry_delay=60.0)
            # stop() is a no-op unless the processor reports running; the
            # long retry_delay parks the ticket well past stop(timeout=1).
            processor._running = True  # noqa: SLF001
            task = TaskItem(
                file_path="uploads/b13-file.txt",
                vault_id=1,
                attempt=1,
                file_id=file_id,
            )
            await processor._handle_failure(  # noqa: SLF001
                task, RuntimeError("b13 transient failure")
            )
            assert processor._retry_backlog.qsize() == 1, (  # noqa: SLF001
                "harness: the retry branch must have parked one ticket"
            )
            await processor.stop(timeout=1.0)

        status = conn.execute(
            "SELECT status FROM files WHERE id = ?", (file_id,)
        ).fetchone()["status"]
        assert status == "pending", (
            "discarding a parked retry at shutdown must leave the file "
            "recoverable (pending), not stuck in processing"
        )
    finally:
        pool.close_all()


async def test_admin_retry_twice_in_lease_mode_reports_in_progress(
    tmp_path, monkeypatch
):
    """AC11: a double admin retry in lease mode must report the duplicate.

    The route maps ``enqueue() -> True`` to "scheduled"; lease-mode
    enqueue ALWAYS returns True (even when a non-terminal job already
    exists), so a double retry silently inserts a second jobs row instead
    of reporting ``already_in_progress``.
    """
    from app.api.routes import documents

    db_path, conn = _seed(tmp_path)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    route_conn = _connect(db_path)
    try:
        file_id = _insert_file(conn, status="error", name="b13-retry.txt")

        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        assert processor._ingest_lease_enabled, (  # noqa: SLF001
            "harness: the pooled processor must come up in lease mode"
        )
        processor._running = True  # noqa: SLF001 — is_running without workers

        route = inspect.unwrap(documents.retry_document)
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(secret_manager=None))
        )
        monkeypatch.setattr(
            documents, "_record_document_action", lambda *_a, **_k: None
        )

        def call():
            return route(
                file_id,
                request,
                route_conn,
                {},  # user (admin role resolved by the DI layer, bypassed)
                "csrf",
                None,  # secret_manager (audit stubbed)
                processor,
                {"id": 7},  # current_user
            )

        first = await call()
        second = await call()

        assert first["status"] == "scheduled", (
            "harness: the first retry must genuinely schedule"
        )
        assert second["status"] == "already_in_progress", (
            "the second retry for the same file must report "
            f"already_in_progress, got {second['status']!r}"
        )
    finally:
        route_conn.close()
        pool.close_all()

