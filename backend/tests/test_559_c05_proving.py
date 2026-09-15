"""Issue #559 C05 proving test (AC5 + AC4 full form).

C05 (frontier audit 2026-09-11; conversion owed by #556 but never shipped on
master): ingestion settlement used to depend on boot-recovery ordering —
a stranded backlog had to be re-enqueued by the startup sweep, and a crash
mid-job left a 'running' row invisible until the next restart.

Under the DB-claimed lease this test proves, against the REAL
BackgroundProcessor and a real pooled SQLite database:

1. ``start()`` returns promptly with a stranded backlog outstanding (the
   in-flight migration is detached; workers poll-claim — no boot-recovery
   routine participates in settlement).
2. Every stranded row reaches TERMINAL status through the lease machinery
   alone: migration -> claim -> failing work -> bounded retries ->
   attempts-cap terminal fail (files rows marked error).
3. Restart independence: a second processor lifecycle against the same
   database creates no duplicate rows and resurrects nothing.

The ingestion jobs use missing file paths so the "work" deterministically
fails and exercises the full retry/attempt-cap path without any mocking of
the processor.
"""

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from app.config import settings
from app.models.database import SQLiteConnectionPool, run_migrations
from app.services.background_tasks import BackgroundProcessor

N_FILES = 25
SETTLE_TIMEOUT_SECONDS = 90


def _seed(tmp_path: Path) -> tuple[str, sqlite3.Connection]:
    db_path = tmp_path / "c05.db"
    run_migrations(str(db_path))
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    for i in range(N_FILES):
        conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_size, "
            "status, phase, source) VALUES (1, ?, ?, 1, 'pending', 'queued', "
            "'upload')",
            (str(tmp_path / f"missing-{i}.pdf"), f"missing-{i}.pdf"),
        )
    conn.commit()
    return str(db_path), conn


def _job_status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM jobs WHERE queue = 'ingestion' "
        "GROUP BY status"
    ).fetchall()
    return {row["status"]: row["n"] for row in rows}


async def _wait_until(predicate, timeout_seconds: float) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.25)
    return predicate()


@pytest.mark.asyncio
async def test_c05_stranded_backlog_settles_without_boot_recovery(tmp_path):
    db_path, conn = _seed(tmp_path)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        # The C05 anti-truncation property: start() must return while the
        # stranded backlog is still outstanding (detached migration).
        await asyncio.wait_for(processor.start(), timeout=10)

        settled = await _wait_until(
            lambda: _job_status_counts(conn).get("failed", 0) == N_FILES,
            SETTLE_TIMEOUT_SECONDS,
        )
        assert settled, (
            f"ingestion jobs did not all reach terminal status: "
            f"{_job_status_counts(conn)!r}"
        )

        # Exactly one jobs row per stranded file, all terminally failed at
        # the attempt cap.
        counts = _job_status_counts(conn)
        assert counts == {"failed": N_FILES}
        total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        assert total == N_FILES, "migration created duplicate job rows"
        max_attempts = conn.execute(
            "SELECT MAX(attempts) FROM jobs"
        ).fetchone()[0]
        assert max_attempts == settings.jobs_max_attempts

        # The files rows followed their jobs to a terminal error state.
        file_counts = {
            row["status"]: row["n"]
            for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM files GROUP BY status"
            ).fetchall()
        }
        assert file_counts == {"error": N_FILES}

        # Graceful stop: the durable queue is already settled, so shutdown
        # must not hang on the quiescence wait.
        await asyncio.wait_for(processor.stop(), timeout=30)
    finally:
        pool.close_all()


@pytest.mark.asyncio
async def test_c05_restart_creates_no_duplicate_rows_and_resurrects_nothing(tmp_path):
    db_path, conn = _seed(tmp_path)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        first = BackgroundProcessor(pool=pool, retry_delay=0.05)
        await asyncio.wait_for(first.start(), timeout=10)
        settled = await _wait_until(
            lambda: _job_status_counts(conn).get("failed", 0) == N_FILES,
            SETTLE_TIMEOUT_SECONDS,
        )
        assert settled
        await asyncio.wait_for(first.stop(), timeout=30)

        # "Restart": a second lifecycle over the same database. The migration
        # is idempotent — terminal files rows are not re-enqueued, terminal
        # jobs rows are not resurrected, and no new rows appear.
        second = BackgroundProcessor(pool=pool, retry_delay=0.05)
        await asyncio.wait_for(second.start(), timeout=10)
        await asyncio.sleep(2.0)  # give migration + janitor + workers a window
        counts = _job_status_counts(conn)
        assert counts == {"failed": N_FILES}, (
            f"restart changed settlement: {counts!r}"
        )
        total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        assert total == N_FILES, "restart duplicated migration rows"
        await asyncio.wait_for(second.stop(), timeout=30)
    finally:
        pool.close_all()


def _plant_crash_orphan(conn: sqlite3.Connection, tmp_path: Path, attempts: int):
    """Insert the crash-orphan signature ONLY the janitor can settle: a
    files row stuck 'processing' plus its 'running' jobs row owned by a
    worker that died (heartbeat far past the reclaim timeout)."""
    cur = conn.execute(
        "INSERT INTO files (vault_id, file_path, file_name, file_size, "
        "status, phase, source) VALUES (1, ?, 'orphan.pdf', 1, "
        "'processing', 'parsing', 'upload')",
        (str(tmp_path / "orphan.pdf"),),
    )
    file_id = int(cur.lastrowid)
    payload = json.dumps(
        {
            "file_path": str(tmp_path / "orphan.pdf"),
            "vault_id": 1,
            "source": "upload",
            "file_id": file_id,
            "file_hash": None,
        }
    )
    cur = conn.execute(
        "INSERT INTO jobs (queue, payload_json, status, worker_id, "
        "lease_generation, attempts, heartbeat_at, started_at) "
        "VALUES ('ingestion', ?, 'running', 'crashed-worker', 1, ?, "
        "datetime('now', '-600 seconds'), datetime('now', '-600 seconds'))",
        (payload, attempts),
    )
    conn.commit()
    return file_id, int(cur.lastrowid)


@pytest.mark.asyncio
async def test_c05_janitor_reclaims_crash_orphan_and_worker_drives_to_terminal(tmp_path):
    """Crash-orphan below the attempt cap: ONLY the janitor can touch the
    stale 'running' row (workers claim pending rows only; the reclaim
    timeout is 300s vs this test's 90s window). The janitor requeues it, a
    worker re-claims and the missing-file work walks the bounded retries to
    the attempts cap. With the janitor disabled this test FAILS — the row
    stays 'running' forever."""
    db_path, conn = _seed(tmp_path)
    del conn  # this scenario starts from a clean jobs table
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        file_id, job_id = _plant_crash_orphan(
            sqlite3.connect(db_path), tmp_path, attempts=1
        )
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        await asyncio.wait_for(processor.start(), timeout=10)

        settled = await _wait_until(
            lambda: (
                conn.execute(
                    "SELECT status, attempts, lease_generation FROM jobs "
                    "WHERE id = ?",
                    (job_id,),
                ).fetchone()["status"]
                == "failed"
            ),
            SETTLE_TIMEOUT_SECONDS,
        )
        assert settled, "crash-orphan lease was never settled"
        row = conn.execute(
            "SELECT status, attempts, lease_generation, worker_id FROM jobs "
            "WHERE id = ?",
            (job_id,),
        ).fetchone()
        # The janitor reclaimed (generation 1 -> 2), a worker re-claimed
        # (2 -> 3) and walked the retries to the cap.
        assert row["attempts"] == settings.jobs_max_attempts
        assert row["lease_generation"] >= 3, (
            "janitor reclaim + worker claim must both have acted"
        )
        files_status = conn.execute(
            "SELECT status FROM files WHERE id = ?", (file_id,)
        ).fetchone()["status"]
        assert files_status == "error"
        await asyncio.wait_for(processor.stop(), timeout=30)
    finally:
        pool.close_all()


@pytest.mark.asyncio
async def test_c05_janitor_caps_orphan_lease_at_attempt_limit_without_claim(tmp_path):
    """Crash-orphan already AT the attempt cap: the janitor must settle it
    terminally ('failed', lease_attempt_cap_exceeded) and fail the files row
    WITHOUT any worker ever claiming it (attempts stay at the cap). With the
    janitor disabled this test FAILS — the row stays 'running' forever."""
    db_path, conn = _seed(tmp_path)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        file_id, job_id = _plant_crash_orphan(
            sqlite3.connect(db_path), tmp_path, attempts=settings.jobs_max_attempts
        )
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        await asyncio.wait_for(processor.start(), timeout=10)

        settled = await _wait_until(
            lambda: (
                conn.execute(
                    "SELECT status, error FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()["status"]
                == "failed"
            ),
            SETTLE_TIMEOUT_SECONDS,
        )
        assert settled, "capped crash-orphan lease was never settled"
        row = conn.execute(
            "SELECT status, attempts, lease_generation, worker_id, error "
            "FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        assert row["attempts"] == settings.jobs_max_attempts, (
            "no worker may claim a cap-settled row"
        )
        assert row["worker_id"] is None
        assert row["lease_generation"] >= 2, "janitor reclaim must bump generation"
        assert row["error"] == "lease_attempt_cap_exceeded"
        # The janitor settles the job and resyncs the files row in TWO
        # commits; wait out the second instead of racing it (PR-review
        # diagnosed flake: CI read an intermediate files state between the
        # two commits).
        files_ok = await _wait_until(
            lambda: (
                conn.execute(
                    "SELECT status FROM files WHERE id = ?", (file_id,)
                ).fetchone()["status"]
                == "error"
            ),
            30,
        )
        assert files_ok, "janitor cap-resync never reached the files row"
        await asyncio.wait_for(processor.stop(), timeout=30)
    finally:
        pool.close_all()


@pytest.mark.asyncio
async def test_c05_janitor_leaves_live_lease_alone(tmp_path):
    """The janitor pass must NEVER reclaim a live (fresh-heartbeat) running
    lease — the lease-mode analogue of the legacy rescan's live-lease guard
    (issue #513 AC24). A fresh-heartbeat orphan and a stale crash-orphan are
    planted side by side; only the stale one may settle."""
    db_path, conn = _seed(tmp_path)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        _, stale_job_id = _plant_crash_orphan(
            sqlite3.connect(db_path), tmp_path, attempts=settings.jobs_max_attempts
        )
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO jobs (queue, payload_json, status, worker_id, "
            "lease_generation, attempts, heartbeat_at, started_at) VALUES "
            "('ingestion', '{}', 'running', 'live-worker', 7, 1, "
            "datetime('now'), datetime('now'))"
        )
        conn.commit()
        live_job_id = int(
            conn.execute(
                "SELECT id FROM jobs WHERE worker_id = 'live-worker'"
            ).fetchone()[0]
        )

        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        await asyncio.wait_for(processor.start(), timeout=10)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + SETTLE_TIMEOUT_SECONDS
        while loop.time() < deadline:
            if (
                conn.execute(
                    "SELECT status FROM jobs WHERE id = ?", (stale_job_id,)
                ).fetchone()["status"]
                == "failed"
            ):
                break
            await asyncio.sleep(0.25)

        stale = conn.execute(
            "SELECT status FROM jobs WHERE id = ?", (stale_job_id,)
        ).fetchone()["status"]
        live = conn.execute(
            "SELECT status, worker_id, lease_generation, attempts FROM jobs "
            "WHERE id = ?",
            (live_job_id,),
        ).fetchone()
        assert stale == "failed", "stale crash-orphan must be settled"
        assert live["status"] == "running", (
            "the janitor stole a live (fresh-heartbeat) lease"
        )
        assert live["worker_id"] == "live-worker"
        assert live["lease_generation"] == 7
        assert live["attempts"] == 1
        await asyncio.wait_for(processor.stop(timeout=10), timeout=20)
    finally:
        pool.close_all()
