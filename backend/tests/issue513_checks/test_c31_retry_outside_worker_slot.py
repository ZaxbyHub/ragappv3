"""C31 - AC31 (UPLOAD-DEEP-04 / INGEST-008): retryable work must be scheduled
OUTSIDE the worker's consume slot.

Contract under test (issue #513 AC31):

  While a task's retry is PENDING (in its backoff window), the worker must
  keep consuming OTHER queued items - delays/retry handoff must not occupy
  worker capacity. Observed with a single worker and a bounded queue
  (maxsize=2):

    * task A fails once (retry backoff 1.5s) and succeeds on its second
      attempt;
    * task B is enqueued 0.15s into A's backoff - B must COMPLETE well within
      the backoff window (bound: 0.8s), proving the worker was not blocked
      inside the failing task's retry handling;
    * A's retry must then complete after capacity frees (both tasks finish).

Pre-fix expectation: ``_handle_failure`` sleeps the backoff INLINE inside the
worker's slot and only then requeues (background_tasks.py ~1580-1592), so B
cannot be consumed until the sleep ends (>= 1.5s) -> the 0.8s bound times out
-> FAIL.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

# Repo root: this file lives at <repo>/backend/tests/issue513_checks/<name>.py
ROOT = Path(__file__).resolve().parents[3]
_TMPDIR = tempfile.mkdtemp(prefix="issue513_c31_")
sys.path.insert(0, str(ROOT / "backend"))


def _hermetic_env() -> None:
    """Set test env BEFORE any app import (mirrors backend/tests/conftest.py)."""
    os.environ["ADMIN_SECRET_TOKEN"] = "test-secret"
    os.environ["USERS_ENABLED"] = "false"
    os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-key-for-testing-only"
    os.environ["REDIS_URL"] = ""
    os.environ["DATA_DIR"] = _TMPDIR

BACKOFF_S = 1.5
B_BOUND_S = 0.8
OVERALL_S = 8.0


async def _scenario() -> str:
    import app.services.background_tasks as bt

    orig_instance = bt._processor_instance
    bt._processor_instance = None
    try:
        with patch.object(bt.settings, "ingestion_queue_max_size", 2), \
                patch.object(bt.settings, "ingestion_worker_count", 1):
            processor = bt.BackgroundProcessor(max_retries=3, retry_delay=BACKOFF_S)
        processor.queue = asyncio.Queue(maxsize=2)
        processor.shutdown_event.clear()

        a1_started = asyncio.Event()
        b_done = asyncio.Event()
        a2_done = asyncio.Event()
        attempts = {"A": 0}
        timings: dict[str, float] = {}

        async def fake_process_existing_file(file_id, file_path, vault_id):  # noqa: ANN202
            if file_id == 1:
                attempts["A"] += 1
                if attempts["A"] == 1:
                    timings["a1"] = time.monotonic()
                    a1_started.set()
                    raise bt.DocumentProcessingError("transient failure for A")
                timings["a2"] = time.monotonic()
                a2_done.set()
                return None
            if file_id == 2:
                timings["b_done"] = time.monotonic()
                b_done.set()
                return None
            raise AssertionError(f"unexpected file_id {file_id}")

        processor.processor.process_existing_file = fake_process_existing_file

        worker = asyncio.create_task(processor._worker_loop())
        try:
            await processor.queue.put(bt.TaskItem(file_path="a.txt", vault_id=1, file_id=1))
            # Wait until A's first attempt has started (and failed).
            try:
                await asyncio.wait_for(a1_started.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                return "harness invalid: task A never started its first attempt"
            await asyncio.sleep(0.15)
            t_b_put = time.monotonic()
            await processor.queue.put(bt.TaskItem(file_path="b.txt", vault_id=1, file_id=2))

            # B must complete while A's retry is still pending in backoff.
            try:
                await asyncio.wait_for(b_done.wait(), timeout=B_BOUND_S)
            except asyncio.TimeoutError:
                return (
                    "worker blocked its consume slot during the retry backoff: "
                    f"queued task B was not processed within {B_BOUND_S}s while "
                    f"task A's {BACKOFF_S}s retry backoff was pending - retryable "
                    f"work is scheduled inside the worker slot (AC31)"
                )
            if timings["b_done"] - t_b_put >= BACKOFF_S:
                return (
                    "task B only completed after the retry backoff elapsed - the "
                    "worker slot was occupied by the pending retry (AC31)"
                )

            # A's retry completes after capacity frees; both tasks finish.
            try:
                await asyncio.wait_for(a2_done.wait(), timeout=OVERALL_S)
            except asyncio.TimeoutError:
                return (
                    "the retried task A never completed after capacity freed "
                    f"(attempts={attempts['A']}) (AC31)"
                )
            if timings["b_done"] > timings.get("a2", float("inf")):
                return "task B completed after A's retry attempt began (AC31)"
            return ""
        finally:
            processor.shutdown_event.set()
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
            processor._running = False
    finally:
        bt._processor_instance = orig_instance


def main() -> int:
    _hermetic_env()
    logging.disable(logging.CRITICAL)
    reason = asyncio.run(_scenario())
    if reason:
        print(f"C31 CHECK: FAIL: {reason}")
        return 1
    print("C31 CHECK: PASS")
    return 0


def test_c31_retry_outside_worker_slot() -> None:
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
