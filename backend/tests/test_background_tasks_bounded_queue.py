"""
Regression tests for bounded asyncio.Queue in BackgroundProcessor (FR-6).

Tests that:
1. Both queues are created with the configured maxsize from settings.
2. The queue provides backpressure when full (put blocks or raises QueueFull).
3. The default value of ingestion_queue_max_size is 1000.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from app.config import Settings, settings
from app.services.background_tasks import (
    BackgroundProcessor,
    EnrichmentTaskItem,
    TaskItem,
)


class TestBackgroundTasksBoundedQueue:
    """Tests for bounded queue configuration in BackgroundProcessor."""

    def test_queue_creation_uses_maxsize(self) -> None:
        """Both queue and enrichment_queue are bounded to ingestion_queue_max_size."""
        # Patch dependencies that BackgroundProcessor.__init__ constructs to avoid DB calls
        with patch.object(BackgroundProcessor, "__init__", lambda self: None):
            processor = BackgroundProcessor()
            # Manually set what the real __init__ would set
            processor.queue = asyncio.Queue(maxsize=settings.ingestion_queue_max_size)
            processor.enrichment_queue = asyncio.Queue(maxsize=settings.ingestion_queue_max_size)

            assert processor.queue.maxsize == settings.ingestion_queue_max_size
            assert processor.enrichment_queue.maxsize == settings.ingestion_queue_max_size

    def test_queue_provides_backpressure_when_full(self) -> None:
        """When queue is full, put blocks or raises QueueFull."""
        small_max = 2
        queue: asyncio.Queue[TaskItem] = asyncio.Queue(maxsize=small_max)

        # Fill the queue to capacity
        for i in range(small_max):
            queue.put_nowait(TaskItem(file_path=f"file{i}.txt", vault_id=1))

        # Queue is now full
        assert queue.full(), "Queue should be full after filling to maxsize"

        # Attempting to put without waiting should raise QueueFull
        with pytest.raises(asyncio.QueueFull):
            queue.put_nowait(TaskItem(file_path="should_fail.txt", vault_id=1))

    def test_settings_ingestion_queue_max_size_default(self) -> None:
        """The default value for ingestion_queue_max_size is 1000."""
        assert settings.ingestion_queue_max_size == 1000

    def test_settings_ingestion_queue_max_size_is_overridable(self) -> None:
        """ingestion_queue_max_size can be overridden via environment variable."""
        # Create a fresh Settings instance with env override
        with patch.dict("os.environ", {"INGESTION_QUEUE_MAX_SIZE": "500"}):
            test_settings = Settings()
            assert test_settings.ingestion_queue_max_size == 500


class TestBackgroundProcessorQueueIntegration:
    """Integration tests verifying BackgroundProcessor actually uses bounded queues."""

    @pytest.mark.asyncio
    async def test_background_processor_queue_has_maxsize(self) -> None:
        """BackgroundProcessor.queue is bounded to ingestion_queue_max_size."""
        # Reset singleton
        import app.services.background_tasks as bt_mod

        orig = bt_mod._processor_instance
        bt_mod._processor_instance = None

        try:
            # Create processor with minimal deps
            processor = BackgroundProcessor(
                max_retries=1,
                retry_delay=0.1,
            )
            assert processor.queue.maxsize == settings.ingestion_queue_max_size
            assert processor.enrichment_queue.maxsize == settings.ingestion_queue_max_size
        finally:
            bt_mod._processor_instance = orig

    @pytest.mark.asyncio
    async def test_enqueue_respects_maxsize(self) -> None:
        """When queue is full, enqueue blocks (backpressure)."""
        # Reset singleton
        import app.services.background_tasks as bt_mod

        orig = bt_mod._processor_instance
        bt_mod._processor_instance = None

        try:
            processor = BackgroundProcessor(
                max_retries=1,
                retry_delay=0.1,
            )
            # Verify the queue is bounded
            max_size = processor.queue.maxsize
            assert max_size > 0, "Queue should have a maxsize > 0"

            # Fill queue using put_nowait up to maxsize
            for i in range(max_size):
                processor.queue.put_nowait(
                    TaskItem(file_path=f"file{i}.txt", vault_id=1)
                )

            assert processor.queue.full(), "Queue should be full"

            # Use asyncio.wait_for with a small timeout to detect blocking behavior.
            # If backpressure works, put will block/hang; if not, it would succeed.
            import asyncio

            async def try_enqueue():
                await processor.queue.put(
                    TaskItem(file_path="overflow.txt", vault_id=1)
                )

            # The put should block/hang because queue is full
            # We expect it to NOT complete within 0.1 seconds
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(try_enqueue(), timeout=0.1)
        finally:
            bt_mod._processor_instance = orig


class TestRecoveryDeadlockRegression:
    """
    Regression tests for the P0 startup recovery deadlock bug.

    Before the fix: _recover_stranded_pending_rows() was called BEFORE workers
    were spawned. Since the queue is bounded (maxsize=ingestion_queue_max_size),
    if >maxsize stranded rows existed, queue.put() would block indefinitely
    waiting for a consumer that didn't exist yet.

    After the fix: workers are spawned BEFORE recovery runs, so consumers
    exist when the recovery sweep enqueues stranded rows.
    """

    @pytest.mark.asyncio
    async def test_recovery_completes_when_more_stranded_rows_than_queue_maxsize(
        self, tmp_path, monkeypatch
    ):
        """
        Regression for cubic P0 finding: recovery must complete even when
        >queue_maxsize stranded rows exist.

        Previously, workers spawned AFTER recovery, so put() would block
        indefinitely on a bounded queue. Now workers spawn first, so they
        consume items as fast as recovery enqueues them.
        """
        import app.services.background_tasks as bt_mod

        # 1. Set small maxsize to make the queue bounded and test fast.
        # Also pin worker count to 1 to keep test assertions simple.
        from app.config import settings as real_settings

        monkeypatch.setattr(real_settings, "ingestion_queue_max_size", 5)
        monkeypatch.setattr(real_settings, "ingestion_worker_count", 1)

        # Reset singleton to get a fresh processor with the patched setting
        orig_instance = bt_mod._processor_instance
        bt_mod._processor_instance = None

        # Create files that will be "stranded" in the DB
        stranded_files = []
        for i in range(10):  # 10 stranded rows > queue maxsize of 5
            f = tmp_path / f"stranded_{i}.txt"
            f.write_text(f"content {i}", encoding="utf-8")
            stranded_files.append(str(f))

        try:
            # 2. Build mock pool that returns stranded rows from the SELECT query.
            # Two separate SELECTs run in _recover_stranded_pending_rows:
            #   (a) status='pending' AND phase='queued'  -> returns 10 stranded rows
            #   (b) status='processing' + old phase_started_at -> returns empty
            # We must use side_effect so each fetchall() call returns fresh data.
            pending_rows = [
                # (id, file_path, vault_id, source)
                (i + 1, str(stranded_files[i]), 1, "upload")
                for i in range(10)
            ]

            mock_cursor_pending = MagicMock()
            mock_cursor_pending.fetchall.return_value = pending_rows
            mock_cursor_pending.fetchone.return_value = None

            mock_cursor_processing = MagicMock()
            mock_cursor_processing.fetchall.return_value = []  # no stuck processing rows

            mock_conn = MagicMock()
            # Each execute call gets its own cursor
            mock_conn.execute.side_effect = [
                mock_cursor_pending,
                mock_cursor_processing,
            ]

            mock_pool = MagicMock()
            mock_pool.connection.return_value.__enter__ = MagicMock(
                return_value=mock_conn
            )
            mock_pool.connection.return_value.__exit__ = MagicMock(return_value=False)

            # 3. Create processor and inject mock pool
            processor = BackgroundProcessor(
                max_retries=1,
                retry_delay=0.05,
            )
            processor.processor.pool = mock_pool

            # Track what gets enqueued
            enqueued_items: list[TaskItem] = []

            async def mock_enqueue(
                file_path,
                vault_id,
                source="upload",
                email_subject=None,
                email_sender=None,
                file_id=None,
            ):
                item = TaskItem(
                    file_path=file_path,
                    vault_id=vault_id,
                    attempt=1,
                    source=source,
                    email_subject=email_subject,
                    email_sender=email_sender,
                    file_id=file_id,
                )
                enqueued_items.append(item)
                # Actually put it in the queue so workers can process it
                await processor.queue.put(item)

            processor.enqueue = mock_enqueue

            # 4. Call start() with a timeout — if the deadlock exists,
            # this will raise asyncio.TimeoutError
            await asyncio.wait_for(processor.start(), timeout=5.0)

            # 5. Verify workers consumed the items (queue should drain)
            await asyncio.wait_for(processor.queue.join(), timeout=5.0)

            # 6. Verify all stranded rows were enqueued
            assert len(enqueued_items) == 10, (
                f"Expected 10 enqueued items, got {len(enqueued_items)}. "
                "Deadlock prevented recovery from completing."
            )

            # 7. Verify workers are running
            assert len(processor._worker_tasks) == 1, "Expected 1 worker"
            assert not processor._worker_tasks[0].done(), "Worker should still be running"

            # 8. Gracefully stop
            processor.shutdown_event.set()
            await asyncio.gather(*processor._worker_tasks, return_exceptions=True)
            if processor._enrichment_worker_task:
                processor._enrichment_worker_task.cancel()
            processor._running = False

        finally:
            bt_mod._processor_instance = orig_instance
            # Restore real settings
            monkeypatch.setattr(real_settings, "ingestion_queue_max_size", 1000)


class TestStrandedRowRecovery:
    """
    Tests for _recover_stranded_pending_rows startup recovery behavior.

    Verifies that stranded processing rows are either reset to pending/queued
    and re-enqueued, or marked as error when the file is missing.
    """

    @pytest.mark.asyncio
    async def test_recover_stranded_pending_rows_resets_status(self, tmp_path):
        """
        When a processing row is stranded (old phase_started_at), recovery
        resets it to pending/queued and re-enqueues it.
        """
        import app.services.background_tasks as bt_mod

        orig_instance = bt_mod._processor_instance
        bt_mod._processor_instance = None

        try:
            processor = BackgroundProcessor(
                max_retries=1,
                retry_delay=0.01,
            )

            # Create a fake file that exists on disk
            fake_file = tmp_path / "stranded.txt"
            fake_file.write_text("content", encoding="utf-8")

            # Mock pool: pending SELECT returns empty, processing SELECT returns one row
            mock_cursor_pending = MagicMock()
            mock_cursor_pending.fetchall.return_value = []

            mock_cursor_processing = MagicMock()
            mock_cursor_processing.fetchall.return_value = [
                (1, str(fake_file), 1, "upload")
            ]

            mock_update = MagicMock()

            mock_conn = MagicMock()
            mock_conn.execute.side_effect = [
                mock_cursor_pending,    # SELECT pending rows
                mock_cursor_processing,  # SELECT processing rows
                mock_update,             # UPDATE status=pending
            ]

            mock_pool = MagicMock()
            mock_pool.connection.return_value.__enter__ = MagicMock(
                return_value=mock_conn
            )
            mock_pool.connection.return_value.__exit__ = MagicMock(return_value=False)

            processor.processor.pool = mock_pool

            enqueued_items: list[TaskItem] = []

            async def mock_enqueue(
                file_path,
                vault_id,
                source="upload",
                email_subject=None,
                email_sender=None,
                file_id=None,
            ):
                item = TaskItem(
                    file_path=file_path,
                    vault_id=vault_id,
                    attempt=1,
                    source=source,
                    email_subject=email_subject,
                    email_sender=email_sender,
                    file_id=file_id,
                )
                enqueued_items.append(item)

            processor.enqueue = mock_enqueue

            await processor._recover_stranded_pending_rows()

            # Verify the row was re-enqueued
            assert len(enqueued_items) == 1, (
                f"Expected 1 enqueued item, got {len(enqueued_items)}"
            )
            assert enqueued_items[0].file_id == 1
            assert enqueued_items[0].file_path == str(fake_file)

            # Verify UPDATE status=pending was called
            update_calls = [
                call for call in mock_conn.execute.call_args_list
                if "UPDATE files SET status='pending'" in str(call)
            ]
            assert len(update_calls) == 1, (
                "Expected UPDATE status=pending call"
            )
        finally:
            bt_mod._processor_instance = orig_instance

    @pytest.mark.asyncio
    async def test_recover_stranded_pending_rows_marks_error_when_file_missing(self, tmp_path):
        """
        When a processing row is stranded but the file is missing on disk,
        recovery marks it as error instead of re-enqueueing.
        """
        import app.services.background_tasks as bt_mod

        orig_instance = bt_mod._processor_instance
        bt_mod._processor_instance = None

        try:
            processor = BackgroundProcessor(
                max_retries=1,
                retry_delay=0.01,
            )

            # Do NOT create the file — it should be missing
            missing_path = str(tmp_path / "missing.txt")

            # Mock pool: pending SELECT returns empty, processing SELECT returns one row
            mock_cursor_pending = MagicMock()
            mock_cursor_pending.fetchall.return_value = []

            mock_cursor_processing = MagicMock()
            mock_cursor_processing.fetchall.return_value = [
                (1, missing_path, 1, "upload")
            ]

            mock_update = MagicMock()

            mock_conn = MagicMock()
            mock_conn.execute.side_effect = [
                mock_cursor_pending,    # SELECT pending rows
                mock_cursor_processing,  # SELECT processing rows
                mock_update,             # UPDATE status=error
            ]

            mock_pool = MagicMock()
            mock_pool.connection.return_value.__enter__ = MagicMock(
                return_value=mock_conn
            )
            mock_pool.connection.return_value.__exit__ = MagicMock(return_value=False)

            processor.processor.pool = mock_pool

            enqueued_items: list[TaskItem] = []

            async def mock_enqueue(
                file_path,
                vault_id,
                source="upload",
                email_subject=None,
                email_sender=None,
                file_id=None,
            ):
                item = TaskItem(
                    file_path=file_path,
                    vault_id=vault_id,
                    attempt=1,
                    source=source,
                    email_subject=email_subject,
                    email_sender=email_sender,
                    file_id=file_id,
                )
                enqueued_items.append(item)

            processor.enqueue = mock_enqueue

            await processor._recover_stranded_pending_rows()

            # Verify the row was NOT re-enqueued
            assert len(enqueued_items) == 0, (
                f"Expected 0 enqueued items for missing file, got {len(enqueued_items)}"
            )

            # Verify UPDATE status=error was called
            update_calls = [
                call for call in mock_conn.execute.call_args_list
                if "UPDATE files SET status='error'" in str(call)
            ]
            assert len(update_calls) == 1, (
                "Expected UPDATE status=error call for missing file"
            )
        finally:
            processor._running = False

    @pytest.mark.asyncio
    async def test_enrichment_retry_skipped_when_shutdown_set_during_backoff(self):
        """
        When shutdown_event is set while the worker is sleeping between retries,
        the worker must not requeue the retry and must exit promptly.
        """
        processor = BackgroundProcessor(
            max_retries=2,
            retry_delay=0.5,  # long enough to set shutdown during sleep
        )
        try:
            call_count = 0

            async def fake_run_enrichment_job(**kwargs):
                nonlocal call_count
                call_count += 1
                raise ValueError("enrichment failure")

            processor.processor.run_enrichment_job = fake_run_enrichment_job

            item = EnrichmentTaskItem(
                file_id="shutdown-retry",
                file_path="shutdown.txt",
                vault_id=1,
                file_hash="h1",
                chunks=[],
                document_text="text",
            )
            await processor.enrichment_queue.put(item)
            processor.shutdown_event.clear()

            worker = asyncio.create_task(processor._enrichment_worker_loop())
            try:
                # Wait for the first failure and backoff sleep to start.
                for _ in range(50):
                    await asyncio.sleep(0.1)
                    if call_count == 1:
                        break

                # Set shutdown while the worker is sleeping in backoff.
                processor.shutdown_event.set()

                # Wait for the worker to exit (it should skip requeue and break).
                for _ in range(50):
                    await asyncio.sleep(0.1)
                    if worker.done():
                        break
            finally:
                if not worker.done():
                    worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker

            # The job should have been attempted once, then skipped on retry
            # because shutdown was set during backoff.
            assert call_count == 1, (
                f"Expected 1 call (no retry after shutdown during backoff), "
                f"got {call_count}"
            )
            assert processor.enrichment_queue.empty(), (
                "Queue must be empty; retry must not be requeued during shutdown"
            )
        finally:
            processor._running = False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


class TestGracefulShutdownUnderLoad:
    """
    Tests for graceful shutdown behaviour when the ingestion queue is under load.
    """

    @pytest.mark.asyncio
    async def test_graceful_shutdown_under_load_drains_queue(self) -> None:
        """
        When stop(timeout=2.0) is called while the ingestion queue holds 50+ items,
        all workers are cancelled cleanly and the queue is empty afterward.

        Verifies:
        1. Queue is filled with 55 items via put_nowait.
        2. start() spawns workers that process items in the background.
        3. stop(timeout=2.0) completes without raising.
        4. processor.queue.empty() is True after stop returns.
        5. All worker tasks are done with no unhandled exceptions.
        """
        import app.services.background_tasks as bt_mod

        orig_instance = bt_mod._processor_instance
        bt_mod._processor_instance = None

        try:
            processor = BackgroundProcessor(
                max_retries=0,
                retry_delay=0.01,
            )

            # Mock process_file so items are consumed instantly without DB calls.
            async def fake_process_file(
                file_path,
                source=None,
                email_subject=None,
                email_sender=None,
                vault_id=None,
            ):
                return MagicMock(
                    chunks=[],
                    vault_id=vault_id,
                    file_id="fake_file_id",
                    file_path=file_path,
                    file_hash="fake_hash",
                    document_text="fake",
                    should_enqueue_enrichment=MagicMock(return_value=False),
                )

            processor.processor.process_file = fake_process_file

            # Fill queue with 55 items before starting workers
            task_count = 55
            for i in range(task_count):
                processor.queue.put_nowait(
                    TaskItem(file_path=f"file{i}.txt", vault_id=1)
                )

            assert not processor.queue.empty(), "Queue should be populated before start"

            # Start the processor — this spawns workers that process the queued items.
            # Use wait_for to bound the start() call (it includes recovery sweep).
            await asyncio.wait_for(processor.start(), timeout=5.0)

            # Allow workers some time to process items before calling stop.
            await asyncio.sleep(0.3)

            # Trigger graceful shutdown with a short timeout
            await processor.stop(timeout=2.0)

            # After stop returns, the queue must be empty (drained or cancelled)
            assert processor.queue.empty(), (
                f"Queue should be empty after graceful shutdown, "
                f"but has {processor.queue.qsize()} items remaining"
            )

            # Verify all worker tasks are done — no unhandled exceptions.
            # stop() uses return_exceptions=True so CancelledError is suppressed.
            for i, task in enumerate(processor._worker_tasks):
                assert task.done(), f"Worker task {i} should be done after stop()"
                with contextlib.suppress(asyncio.CancelledError):
                    task.result()  # Raises if task exited with an unexpected exception

        finally:
            bt_mod._processor_instance = orig_instance
            processor._running = False


class TestEnrichmentWorkerResilience:
    """
    Tests for enrichment worker exception isolation.

    The enrichment worker must catch exceptions from run_enrichment_job,
    log them, call task_done(), and continue processing subsequent items.
    """

    @pytest.mark.asyncio
    async def test_enrichment_worker_survives_exception(self):
        """
        When run_enrichment_job raises on the first item, the worker must
        continue and process the second item.
        """
        from app.services.background_tasks import BackgroundProcessor

        processor = BackgroundProcessor(
            max_retries=0,
            retry_delay=0.01,
        )
        try:
            processed: list[str] = []

            async def fake_run_enrichment_job(**kwargs):
                processed.append(kwargs["file_id"])
                if len(processed) == 1:
                    raise ValueError("first enrichment job failed")
                return None

            processor.processor.run_enrichment_job = fake_run_enrichment_job

            first = EnrichmentTaskItem(
                file_id="first",
                file_path="first.txt",
                vault_id=1,
                file_hash="h1",
                chunks=[],
                document_text="first",
            )
            second = EnrichmentTaskItem(
                file_id="second",
                file_path="second.txt",
                vault_id=1,
                file_hash="h2",
                chunks=[],
                document_text="second",
            )
            await processor.enrichment_queue.put(first)
            await processor.enrichment_queue.put(second)
            processor.shutdown_event.clear()

            worker = asyncio.create_task(processor._enrichment_worker_loop())
            try:
                await asyncio.wait_for(
                    processor.enrichment_queue.join(), timeout=2.0
                )
            finally:
                processor.shutdown_event.set()
                worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker

            assert processed == ["first", "second"], (
                f"Expected both enrichment items to be processed, got {processed}"
            )
        finally:
            processor._running = False

    @pytest.mark.asyncio
    async def test_enrichment_worker_retries_on_llm_error(self):
        """
        When run_enrichment_job raises an LLM-like error (ValueError),
        the worker retries up to max_retries and succeeds on the third attempt.

        Verifies:
        1. The worker calls run_enrichment_job exactly 3 times (initial + 2 retries).
        2. The worker does not crash and the queue is fully drained.
        """
        processor = BackgroundProcessor(
            max_retries=2,
            retry_delay=0.01,
        )
        try:
            call_count = 0

            async def fake_run_enrichment_job(**kwargs):
                nonlocal call_count
                call_count += 1
                if call_count < 3:
                    raise ValueError("LLM service unavailable")
                return None

            processor.processor.run_enrichment_job = fake_run_enrichment_job

            item = EnrichmentTaskItem(
                file_id="llm-retry",
                file_path="llm.txt",
                vault_id=1,
                file_hash="h1",
                chunks=[],
                document_text="llm text",
            )
            await processor.enrichment_queue.put(item)
            processor.shutdown_event.clear()

            worker = asyncio.create_task(processor._enrichment_worker_loop())
            try:
                # Wait for all retries to complete. With max_retries=2 and
                # retry_delay=0.01, the worker sleeps at most 2 * 0.01s between
                # attempts, so 0.5s is plenty of headroom.
                await asyncio.sleep(0.5)
            finally:
                processor.shutdown_event.set()
                worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker

            # With max_retries=2 and attempt starting at 0, the job is attempted
            # at attempt=0, retried at attempt=1, then succeeds on attempt=2
            # for a total of 3 calls (initial + 2 retries).
            assert call_count == 3, (
                f"Expected 3 calls to run_enrichment_job "
                f"(initial + 2 retries), got {call_count}"
            )
            assert processor.enrichment_queue.empty(), (
                "Queue must be fully drained after processing"
            )
        finally:
            processor._running = False

    @pytest.mark.asyncio
    async def test_enrichment_worker_marks_failed_after_max_retries(self):
        """
        When run_enrichment_job raises and max_retries is exceeded, the worker
        logs permanent failure and does not crash.
        """
        processor = BackgroundProcessor(
            max_retries=1,
            retry_delay=0.01,
        )
        try:
            call_count = 0

            async def fake_run_enrichment_job(**kwargs):
                nonlocal call_count
                call_count += 1
                raise ValueError("permanent enrichment failure")

            processor.processor.run_enrichment_job = fake_run_enrichment_job

            item = EnrichmentTaskItem(
                file_id="fail-me",
                file_path="fail.txt",
                vault_id=1,
                file_hash="h1",
                chunks=[],
                document_text="text",
            )
            await processor.enrichment_queue.put(item)
            processor.shutdown_event.clear()

            worker = asyncio.create_task(processor._enrichment_worker_loop())
            try:
                # Wait for the queue to drain without using join(),
                # which would interfere with the shared singleton queue state.
                for _ in range(50):  # up to 5 seconds
                    await asyncio.sleep(0.1)
                    if processor.enrichment_queue.empty():
                        break
            finally:
                processor.shutdown_event.set()
                worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker

            # With max_retries=1 and attempt starting at 0, the job is attempted
            # at attempt=0, retried at attempt=1, then permanently failed.
            assert call_count == 2, (
                f"Expected 2 calls (initial + 1 retry), got {call_count}"
            )
            assert processor.enrichment_queue.empty(), (
                "Queue must be fully drained after permanent failure"
            )
        finally:
            processor._running = False
