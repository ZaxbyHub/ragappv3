"""C7 - AC7 (INGEST-008): no worker self-deadlock when a retry meets a full queue.

Contract under test (issue #513 AC7):

  A failing task whose retry re-enters the SAME bounded work queue while the
  queue is already full (a next task is queued behind it) must not deadlock the
  worker: both the retried task and the already-queued next task must COMPLETE.
  Applies to the ingestion worker and the enrichment worker (both requeue via
  ``await queue.put(...)`` from inside the sole consumer coroutine after an
  inline backoff sleep - background_tasks.py ~912-917, ~1259-1278, ~1580-1592).

  The drain wait is wrapped in ``asyncio.wait_for`` so the pre-fix permanent
  self-block is detected and reported as a FAIL verdict instead of hanging.

Pre-fix expectation: the sole worker blocks in ``queue.put`` forever (queue
full, itself the only consumer) -> neither the retry nor the queued task
completes -> timeout converted to FAIL.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

# Repo root: this file lives at <repo>/backend/tests/issue513_checks/<name>.py
ROOT = Path(__file__).resolve().parents[3]
_TMPDIR = tempfile.mkdtemp(prefix="issue513_c7_")
sys.path.insert(0, str(ROOT / "backend"))


def _hermetic_env() -> None:
    """Set test env BEFORE any app import (mirrors backend/tests/conftest.py)."""
    os.environ["ADMIN_SECRET_TOKEN"] = "test-secret"
    os.environ["USERS_ENABLED"] = "false"
    os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-key-for-testing-only"
    os.environ["REDIS_URL"] = ""
    os.environ["DATA_DIR"] = _TMPDIR

_SCENARIO_TIMEOUT_S = 5.0


async def _ingestion_scenario() -> tuple[bool, bool]:
    """Run the ingestion worker against a full maxsize=1 queue.

    Returns (retry_completed, queued_task_completed).
    """
    import app.services.background_tasks as bt

    orig_instance = bt._processor_instance
    bt._processor_instance = None
    try:
        with patch.object(bt.settings, "ingestion_queue_max_size", 1), \
                patch.object(bt.settings, "ingestion_worker_count", 1):
            processor = bt.BackgroundProcessor(max_retries=3, retry_delay=0.05)
        # The constructor already built queues from settings; force a tiny bound.
        processor.queue = asyncio.Queue(maxsize=1)
        processor.shutdown_event.clear()

        retry_done = asyncio.Event()
        queued_done = asyncio.Event()
        first_attempt_started = asyncio.Event()
        attempts: dict[str, int] = {"A": 0}

        async def fake_process_existing_file(file_id, file_path, vault_id):  # noqa: ANN202
            if file_id == 1:
                first_attempt_started.set()
                attempts["A"] += 1
                if attempts["A"] == 1:
                    raise bt.DocumentProcessingError("transient failure for A")
                retry_done.set()
                return None
            if file_id == 2:
                queued_done.set()
                return None
            raise AssertionError(f"unexpected file_id {file_id}")

        processor.processor.process_existing_file = fake_process_existing_file

        worker = asyncio.create_task(processor._worker_loop())
        try:
            await processor.queue.put(bt.TaskItem(file_path="a.txt", vault_id=1, file_id=1))
            # Let the worker pick up A before filling the queue behind it.
            for _ in range(250):
                if first_attempt_started.is_set():
                    break
                await asyncio.sleep(0.02)
            # Queue is empty again (A was consumed): fill it so it is FULL when the
            # retry tries to re-enter it.
            await processor.queue.put(bt.TaskItem(file_path="b.txt", vault_id=1, file_id=2))
            if not processor.queue.full():
                return False, False

            await asyncio.wait_for(retry_done.wait(), timeout=_SCENARIO_TIMEOUT_S)
            await asyncio.wait_for(queued_done.wait(), timeout=_SCENARIO_TIMEOUT_S)
        except asyncio.TimeoutError:
            return retry_done.is_set(), queued_done.is_set()
        finally:
            processor.shutdown_event.set()
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
            processor._running = False
        return retry_done.is_set(), queued_done.is_set()
    finally:
        bt._processor_instance = orig_instance


async def _enrichment_scenario() -> tuple[bool, bool]:
    """Run the enrichment worker against a full maxsize=1 queue.

    Returns (retry_completed, queued_task_completed).
    """
    import app.services.background_tasks as bt

    orig_instance = bt._processor_instance
    bt._processor_instance = None
    try:
        processor = bt.BackgroundProcessor(max_retries=3, retry_delay=0.05)
        processor.enrichment_queue = asyncio.Queue(maxsize=1)
        processor.shutdown_event.clear()

        retry_done = asyncio.Event()
        queued_done = asyncio.Event()
        first_started = asyncio.Event()
        calls: dict[str, int] = {"A": 0, "B": 0}

        async def fake_run_enrichment_job(**kwargs):  # noqa: ANN202
            key = str(kwargs.get("file_id"))
            if key == "A":
                first_started.set()
                calls["A"] += 1
                if calls["A"] == 1:
                    raise ValueError("transient enrichment failure for A")
                retry_done.set()
                return None
            if key == "B":
                calls["B"] += 1
                queued_done.set()
                return None
            raise AssertionError(f"unexpected file_id {key}")

        processor.processor.run_enrichment_job = fake_run_enrichment_job

        def _item(fid: str) -> bt.EnrichmentTaskItem:
            return bt.EnrichmentTaskItem(
                file_id=fid, file_path=f"{fid}.txt", vault_id=1,
                file_hash="h", chunks=[], document_text="t",
            )

        worker = asyncio.create_task(processor._enrichment_worker_loop())
        try:
            await processor.enrichment_queue.put(_item("A"))
            for _ in range(250):
                if first_started.is_set():
                    break
                await asyncio.sleep(0.02)
            await processor.enrichment_queue.put(_item("B"))
            if not processor.enrichment_queue.full():
                return False, False

            await asyncio.wait_for(retry_done.wait(), timeout=_SCENARIO_TIMEOUT_S)
            await asyncio.wait_for(queued_done.wait(), timeout=_SCENARIO_TIMEOUT_S)
        except asyncio.TimeoutError:
            return retry_done.is_set(), queued_done.is_set()
        finally:
            processor.shutdown_event.set()
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
            processor._running = False
        return retry_done.is_set(), queued_done.is_set()
    finally:
        bt._processor_instance = orig_instance


def main() -> int:
    _hermetic_env()
    logging.disable(logging.CRITICAL)

    ingestion_ok, ingestion_queued = asyncio.run(_ingestion_scenario())
    if not (ingestion_ok and ingestion_queued):
        print(
            f"C7 CHECK: FAIL: ingestion worker self-deadlocked on the full "
            f"bounded queue (retried task completed={ingestion_ok}, queued next "
            f"task completed={ingestion_queued}) - the retry re-enters the queue "
            f"it alone drains (INGEST-008)"
        )
        return 1

    enrich_ok, enrich_queued = asyncio.run(_enrichment_scenario())
    if not (enrich_ok and enrich_queued):
        print(
            f"C7 CHECK: FAIL: enrichment worker self-deadlocked on the full "
            f"bounded queue (retried job completed={enrich_ok}, queued next "
            f"job completed={enrich_queued}) - the retry re-enters the queue "
            f"it alone drains (INGEST-008)"
        )
        return 1

    print("C7 CHECK: PASS")
    return 0


def test_c7_retry_deadlock_full_queue() -> None:
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
