"""Collected pytest coverage for the BackgroundProcessor singleton (issue #563 / C11).

Ported from ``backend/test_singleton_processor.py``, a standalone script parked
at the package root that pytest never collected (``testpaths = ["tests"]``) and
that had rotted against the current API (``enqueue`` now requires ``vault_id``)
and mutated ``os.environ`` at import time. The tests below exercise the real
singleton/lifecycle/queue contract in-process with no environment mutation; the
worker's document parsing is stubbed so the queue-drain test stays hermetic in
CI (no vector store, embeddings, or DB required).
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import app.services.background_tasks as background_tasks
from app.config import settings
from app.services.background_tasks import (
    get_background_processor,
    reset_background_processor,
)


@pytest.fixture()
def processor_factory():
    """Hand out fresh singleton processors, containing global state per test."""

    def _factory():
        reset_background_processor()
        processor = get_background_processor(
            chunk_size_chars=settings.chunk_size_chars,
            chunk_overlap_chars=settings.chunk_overlap_chars,
        )
        return processor

    yield _factory

    # Fixture finalizers run after pytest-asyncio has already closed the test's
    # event loop, so a processor leaked by a FAILED test cannot be stopped here:
    # BackgroundProcessor.stop() awaits worker tasks bound to that closed loop
    # and raises cross-loop errors (ValueError / "Event loop is closed") — the
    # exact failure this teardown was meant to prevent (PRR-004, reproduced
    # empirically). A leaked instance is inert once its loop is closed, and
    # reset_background_processor()'s create_task branch needs a running loop,
    # so clear the singleton unconditionally instead.
    background_tasks._processor_instance = None


async def test_singleton_pattern(processor_factory):
    """get_background_processor returns the same instance across calls."""
    processor1 = processor_factory()
    processor2 = get_background_processor(
        chunk_size_chars=settings.chunk_size_chars,
        chunk_overlap_chars=settings.chunk_overlap_chars,
    )

    assert processor1 is processor2, (
        "get_background_processor should return the singleton instance"
    )


async def test_processor_lifecycle(processor_factory):
    """The processor starts and stops cleanly (no pool: recovery sweeps no-op)."""
    processor = processor_factory()

    assert not processor.is_running, "Processor should not be running initially"

    await processor.start()
    assert processor.is_running, "Processor should be running after start()"

    await processor.stop(timeout=5.0)
    assert not processor.is_running, "Processor should not be running after stop()"


async def test_processor_queues_items(processor_factory, tmp_path: Path):
    """Enqueued items drain through the real worker loop to the document parser."""
    processor = processor_factory()
    test_file = tmp_path / "test_document.txt"
    test_file.write_text("This is a test document for background processing.\n" * 10)

    mock_process = AsyncMock(return_value=None)
    with patch.object(processor.processor, "process_file", mock_process):
        await processor.start()
        await processor.enqueue(str(test_file), vault_id=1, source="upload")

        waited = 0.0
        while processor.queue_size > 0 and waited < 10.0:
            await asyncio.sleep(0.05)
            waited += 0.05

        assert processor.queue_size == 0, "Queue should be empty after processing"
        assert mock_process.await_count >= 1, "Worker should have processed the item"

        await processor.stop(timeout=5.0)
        assert not processor.is_running, "Processor should be stopped after drain"
