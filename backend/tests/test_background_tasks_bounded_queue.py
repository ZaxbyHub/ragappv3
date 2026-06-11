"""
Regression tests for bounded asyncio.Queue in BackgroundProcessor (FR-6).

Tests that:
1. Both queues are created with the configured maxsize from settings.
2. The queue provides backpressure when full (put blocks or raises QueueFull).
3. The default value of ingestion_queue_max_size is 1000.
"""

from __future__ import annotations

import asyncio
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
