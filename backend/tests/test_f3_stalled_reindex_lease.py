"""Issue #1010 acceptance checks — stalled reindex lease (frozen).

A live-but-stalled reindex worker renews its heartbeat forever while making
zero progress, so the heartbeat-age reclaim arm never fires and the job row
stays ``running`` indefinitely with no machine-readable stall signal.

Fixture strategy mirrors tests/test_b13_lease_reindex_durability.py: real
schema via ``run_migrations`` on a temp DB, ``SQLiteConnectionPool``, the
real ``_reindex_lease_worker_loop`` / ``_janitor_sweep_once`` entry points,
and doubles only for ``processor.process_existing_file``. Crashes are
simulated by backdating ``heartbeat_at`` — never by killing processes.

Checks:
* C7 (DISCRIMINATING): a live worker whose per-file double NEVER completes
  (zero progress) while its heartbeat keeps renewing must have its lease
  released inside the stall budget window. At base heartbeat age is the
  only reclaim predicate, so the row stays ``running`` and lease_generation
  never bumps — the assertion fails behaviorally.
* C8 (DISCRIMINATING): the reindex job reader must expose a machine-
  readable ``stalled`` flag for a running row whose progress watermark is
  stale while its heartbeat is fresh. At base the reader projects no such
  field (None is not True).
* C9 (PRESERVING, green at base): a job that keeps completing files is
  never reclaimed — terminal ``completed`` with no requeue even with a
  stall budget tighter than the whole run.
* C10 (PRESERVING, green at base): a dead worker's expired heartbeat is
  still reclaimed by one janitor pass (the b13 crash idiom).

All async work for C7/C9 runs inside ONE event loop (one pytest-asyncio
test = one loop; abandoned worker tasks are cancelled and gathered in the
finally so no coroutine is left never-awaited).
"""

import asyncio
import inspect
import json
import sqlite3
from types import SimpleNamespace

from app.config import settings
from app.models.database import SQLiteConnectionPool, run_migrations
from app.services.background_tasks import BackgroundProcessor
from app.services.job_lease import JobLease, ensure_jobs_schema

REINDEX_QUEUE = "reindex"
STALE_SECONDS = 3600  # far beyond the default 300s reclaim timeout


def _override_setting_if_present(monkeypatch, name, value):
    """Override a settings field only when it exists (b13 ``_setting_overridden``
    idiom). The pydantic settings model REJECTS setattr of unknown fields
    (ValueError), and a DISCRIMINATING check must fail for its behavioral
    reason — so a field that does not exist at base (the stall budget knob)
    is left untouched: the stall-aware machinery cannot arm, and the
    behavioral assertion below fails instead of an attribute error.
    """
    sentinel = object()
    previous = getattr(settings, name, sentinel)
    if previous is sentinel:
        return
    monkeypatch.setattr(settings, name, value)


def _connect(db_path):
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _seed(tmp_path, name="f3.db"):
    db_path = str(tmp_path / name)
    run_migrations(db_path)
    return db_path, _connect(db_path)


def _insert_file(conn, *, status, vault_id=1, name="f3-file.txt"):
    cur = conn.execute(
        "INSERT INTO files (vault_id, file_path, file_name, file_size, "
        "status, phase, source) VALUES (?, ?, ?, 1, ?, 'queued', 'upload')",
        (vault_id, f"uploads/{name}", name, status),
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
    conn.commit()
    return job_id


async def test_live_stalled_worker_lease_is_released(tmp_path, monkeypatch):
    """C7: a stalled-but-heartbeating worker must lose its lease."""
    db_path, conn = _seed(tmp_path, name="f3-c7.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    worker = None
    try:
        _insert_file(conn, status="indexed", name="f3-stuck.txt")
        _insert_job(conn, {"vault_id": None}, status="pending")

        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        await processor._run_jobs_migration()  # noqa: SLF001 — opens the claim barrier

        async def stalled_double(existing_id, file_path, vault_id, **kwargs):  # noqa: ARG001
            # Zero progress, forever: a live worker wedged inside one file.
            await asyncio.Event().wait()

        monkeypatch.setattr(
            processor.processor, "process_existing_file", stalled_double
        )
        # Heartbeat beats every max(1.0, interval) seconds — kept fresh so
        # the base heartbeat-age arm can never fire inside the window. The
        # reclaim timeout stays at its 300s default for the same reason.
        monkeypatch.setattr(settings, "jobs_heartbeat_interval_seconds", 0.2)
        # The stall budget knob: absent at base (no stall arm exists), small
        # once the fix adds it.
        _override_setting_if_present(
            monkeypatch, "jobs_stall_timeout_seconds", 2.0
        )

        worker = asyncio.create_task(processor._reindex_lease_worker_loop())  # noqa: SLF001

        loop = asyncio.get_running_loop()
        deadline = loop.time() + 12.0
        observed_statuses = []
        max_attempts = 0
        while loop.time() < deadline:
            await asyncio.sleep(0.1)
            await processor._janitor_sweep_once()  # noqa: SLF001
            row = conn.execute(
                "SELECT status, attempts FROM jobs "
                "WHERE queue = ? ORDER BY id DESC LIMIT 1",
                (REINDEX_QUEUE,),
            ).fetchone()
            if row is not None:
                observed_statuses.append(row["status"])
                max_attempts = max(max_attempts, int(row["attempts"]))
                if row["status"] != "running" or max_attempts >= 2:
                    break

        # Durable release evidence: the wedged worker itself can never
        # re-claim (its double never returns), so attempts can only grow
        # past the single claim through a janitor stall-release + re-claim
        # cycle; a transient non-running status observation counts too.
        released = max_attempts >= 2 or any(
            s != "running" for s in observed_statuses
        )
        assert released, (
            "a live worker whose heartbeat keeps renewing while its job "
            "makes zero progress must have its lease released inside the "
            "stall-budget window (row leaves 'running', or attempts grow "
            "through reclaim/re-claim cycles); observed "
            f"statuses={observed_statuses[-10:]!r}, max_attempts={max_attempts}"
        )
    finally:
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        pool.close_all()


async def test_stalled_running_job_is_flagged_in_reader(tmp_path, monkeypatch):
    """C8: the reindex job reader must project a machine-readable stall flag."""
    from app.api.routes import documents

    db_path, conn = _seed(tmp_path, name="f3-c8.db")
    route_conn = _connect(db_path)
    try:
        job_id = _insert_job(
            conn,
            {"vault_id": None, "trigger_type": "api", "trigger_id": "7"},
            status="running",
            worker_id="f3-stalled-worker",
            attempts=1,
            heartbeat_age_seconds=2,  # FRESH heartbeat: not reclaimable by age
        )
        # The progress watermark is stale: this row started long ago and has
        # not moved since (backdate every plausible progress timestamp; the
        # seeded schema has no progress column at base).
        conn.execute(
            "UPDATE jobs SET started_at = datetime('now', ?), "
            "created_at = datetime('now', ?) WHERE id = ?",
            (f"-{STALE_SECONDS} seconds", f"-{STALE_SECONDS} seconds", job_id),
        )
        conn.commit()

        _override_setting_if_present(
            monkeypatch, "jobs_stall_timeout_seconds", 2.0
        )

        route = inspect.unwrap(documents.get_reindex_job_status)
        response = await route(
            SimpleNamespace(),
            job_id,
            route_conn,
            {"id": 7, "username": "f3-admin", "role": "superadmin"},
        )
        payload = (
            response.model_dump()
            if hasattr(response, "model_dump")
            else dict(response)
        )
        assert payload.get("status") == "running", (
            "harness: the seeded row must read back as running, got "
            f"{payload!r}"
        )
        assert payload.get("stalled") is True, (
            "the reindex job reader must expose a machine-readable 'stalled' "
            "flag that is True for a running row whose progress watermark is "
            "stale while its heartbeat stays fresh; got "
            f"stalled={payload.get('stalled')!r} in {payload!r}"
        )
    finally:
        route_conn.close()


async def test_progressing_job_never_reclaimed(tmp_path, monkeypatch):
    """C9 (PRESERVING): a progressing job is never reclaimed."""
    db_path, conn = _seed(tmp_path, name="f3-c9.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    worker = None
    try:
        for i in range(3):
            _insert_file(conn, status="indexed", name=f"f3-progress-{i}.txt")
        _insert_job(conn, {"vault_id": None}, status="pending")

        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        await processor._run_jobs_migration()  # noqa: SLF001

        completed_files = []

        async def progressing_double(existing_id, file_path, vault_id, **kwargs):  # noqa: ARG001
            # Each file completes after a short await: progress advances
            # every ~0.3s while the whole run spans ~0.9s.
            await asyncio.sleep(0.3)
            completed_files.append(int(existing_id))
            return None

        monkeypatch.setattr(
            processor.processor, "process_existing_file", progressing_double
        )
        monkeypatch.setattr(settings, "jobs_heartbeat_interval_seconds", 0.2)
        # Stall budget TIGHTER than the whole run (0.9s) but wider than one
        # per-file step (0.3s): only a progressing-unaware stall arm could
        # trip on this; at base the field is absent and nothing changes.
        _override_setting_if_present(
            monkeypatch, "jobs_stall_timeout_seconds", 0.5
        )

        worker = asyncio.create_task(processor._reindex_lease_worker_loop())  # noqa: SLF001

        loop = asyncio.get_running_loop()
        deadline = loop.time() + 20.0
        final_row = None
        while loop.time() < deadline:
            await processor._janitor_sweep_once()  # noqa: SLF001
            await asyncio.sleep(0.1)
            final_row = conn.execute(
                "SELECT status, attempts, lease_generation FROM jobs "
                "WHERE queue = ? ORDER BY id DESC LIMIT 1",
                (REINDEX_QUEUE,),
            ).fetchone()
            if final_row is not None and final_row["status"] in (
                "completed",
                "failed",
                "cancelled",
            ):
                break

        assert final_row is not None, "harness: the reindex job row must exist"
        assert final_row["status"] == "completed", (
            "a job that keeps completing files must run to terminal "
            f"'completed', got {final_row['status']!r}"
        )
        assert int(final_row["attempts"]) == 1, (
            "a progressing job must never be requeued/reclaimed (single "
            f"claim expected, attempts={final_row['attempts']})"
        )
        # Exactly one claim (claim itself bumps lease_generation to 1); a
        # janitor reclaim + re-claim cycle would bump it past 1.
        assert int(final_row["lease_generation"]) == 1, (
            "a progressing job's lease must never be reclaimed "
            f"(lease_generation={final_row['lease_generation']}, expected "
            "exactly the single claim's bump)"
        )
        assert len(completed_files) == 3, (
            "harness: every seeded file must have been processed once, got "
            f"{completed_files!r}"
        )
    finally:
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        pool.close_all()


async def test_dead_worker_reclaim_still_fires(tmp_path):
    """C10 (PRESERVING): a dead worker's expired heartbeat is reclaimed."""
    db_path, conn = _seed(tmp_path, name="f3-c10.db")
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        job_id = _insert_job(
            conn,
            {"vault_id": None},
            status="running",
            worker_id="f3-dead-worker",
            attempts=1,
            heartbeat_age_seconds=STALE_SECONDS,
        )

        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        await processor._run_jobs_migration()  # noqa: SLF001
        await processor._janitor_sweep_once()  # noqa: SLF001

        row = conn.execute(
            "SELECT status, worker_id FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        assert row is not None, "harness: the seeded job row must exist"
        assert row["status"] == "pending", (
            "a running row whose heartbeat expired well beyond the reclaim "
            f"timeout must be reclaimed to pending, got {row['status']!r}"
        )
        assert row["worker_id"] is None, (
            "reclaim must clear the dead worker's id, got "
            f"{row['worker_id']!r}"
        )
    finally:
        pool.close_all()
