"""C8 - AC8 (INGEST-009): a running reindex job must not survive a restart as
'running'.

Contract under test (issue #513 AC8):

  Simulate a process stop while a reindex job is executing (a
  ``document_reindex_jobs`` row sits at status='running'), then restart the
  background processor on the same database (``BackgroundProcessor.start()``,
  which runs every startup recovery sweep). After the restart the job must be
  RESUMED (re-enqueued and driven to a terminal status) or explicitly marked
  interrupted/terminal - it must never remain an abandoned 'running' row.

Pre-fix expectation: startup recovery covers files/enrichment/atom/vector-delete/
artifact sweeps only; nothing resets or resumes ``document_reindex_jobs``
(background_tasks.py:1321-1334 writes 'running'; no recovery entry exists), so
the row stays 'running' forever -> FAIL.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

# Repo root: this file lives at <repo>/backend/tests/issue513_checks/<name>.py
ROOT = Path(__file__).resolve().parents[3]
_TMPDIR = tempfile.mkdtemp(prefix="issue513_c8_")
sys.path.insert(0, str(ROOT / "backend"))


def _hermetic_env() -> None:
    """Set test env BEFORE any app import (mirrors backend/tests/conftest.py)."""
    os.environ["ADMIN_SECRET_TOKEN"] = "test-secret"
    os.environ["USERS_ENABLED"] = "false"
    os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-key-for-testing-only"
    os.environ["REDIS_URL"] = ""
    os.environ["DATA_DIR"] = _TMPDIR

_RESTART_WINDOW_S = 8.0
_TERMINAL = {"completed", "failed", "cancelled", "interrupted", "error"}


def _job_status(db_path: str, job_id: int) -> str:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT status FROM document_reindex_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return str(row[0]) if row else "<missing>"
    finally:
        conn.close()


async def _restart_and_wait(db_path: str, job_id: int) -> tuple[str, bool]:
    """Start the processor (the 'restart') and wait for the job to recover.

    Returns (final_status_observed, resumed_or_terminal).
    """
    import app.services.background_tasks as bt
    from app.models.database import get_pool

    orig_instance = bt._processor_instance
    bt._processor_instance = None
    processor = bt.BackgroundProcessor(
        max_retries=1, retry_delay=0.01, pool=get_pool(db_path, max_size=3)
    )
    try:
        await asyncio.wait_for(processor.start(), timeout=6.0)

        deadline = time.monotonic() + _RESTART_WINDOW_S
        last = _job_status(db_path, job_id)
        while time.monotonic() < deadline:
            last = _job_status(db_path, job_id)
            if last in _TERMINAL:
                return last, True
            if last == "pending":
                # Reset without re-enqueue within the window would be another
                # form of abandonment; accept pending only once the job is
                # actually queued for resumption.
                if processor.reindex_queue.qsize() > 0:
                    return last, True
            await asyncio.sleep(0.1)
        return last, False
    finally:
        processor.shutdown_event.set()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(processor.stop(timeout=1.0), timeout=5.0)
        processor._running = False
        bt._processor_instance = orig_instance


def main() -> int:
    _hermetic_env()
    logging.disable(logging.CRITICAL)
    from app.models.database import init_db

    tmp = Path(tempfile.mkdtemp(prefix="c8_db_"))
    db_path = str(tmp / "app.db")
    init_db(db_path)

    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO vaults (name) VALUES ('v')")
    vault_id = conn.execute("SELECT id FROM vaults LIMIT 1").fetchone()[0]
    # A file row that is NOT eligible for reindex selection (status='pending'),
    # so a post-fix resumed job finds zero files and completes quickly.
    conn.execute(
        "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, status) "
        "VALUES (?, ?, ?, ?, ?, 'pending')",
        (vault_id, str(tmp / "doc.txt"), "doc.txt", "hash-c8", 8),
    )
    # The simulated mid-run crash state: a reindex job stuck at 'running'.
    conn.execute(
        "INSERT INTO document_reindex_jobs (vault_id, trigger_type, status, started_at) "
        "VALUES (?, 'manual', 'running', CURRENT_TIMESTAMP)",
        (vault_id,),
    )
    conn.commit()
    job_id = conn.execute("SELECT id FROM document_reindex_jobs LIMIT 1").fetchone()[0]
    conn.close()

    status, recovered = asyncio.run(_restart_and_wait(db_path, job_id))

    if not recovered:
        print(
            f"C8 CHECK: FAIL: after processor restart the interrupted reindex job "
            f"id={job_id} is still abandoned at status='{status}' (never resumed, "
            f"never marked interrupted) - no shutdown/startup ownership for "
            f"document_reindex_jobs (INGEST-009)"
        )
        return 1

    print("C8 CHECK: PASS")
    return 0


def test_c8_reindex_restart_recovery() -> None:
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
