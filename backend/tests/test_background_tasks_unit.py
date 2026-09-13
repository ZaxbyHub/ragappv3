"""Collected pytest coverage for the queue-processor worker-loop contract (issue #563 / C11).

Ported from ``backend/test_background_tasks_unit.py``, a standalone script at
the package root that pytest never collected. It exercises the asyncio
queue-processor pattern — singleton access, start/stop lifecycle, queue drain,
and graceful shutdown with pending items — against a minimal in-file processor
double, exactly as the original script did; production-side equivalents for
``BackgroundProcessor`` live in ``tests/test_singleton_processor.py``.
"""

import asyncio

import pytest


class MockTask:
    def __init__(self, file_path: str, attempt: int = 1):
        self.file_path = file_path
        self.attempt = attempt


class MockProcessor:
    def __init__(self, max_retries=3, retry_delay=1.0):
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.queue = asyncio.Queue()
        self.shutdown_event = asyncio.Event()
        self._worker_task = None
        self._running = False
        self.processed = []

    async def start(self):
        if self._running:
            return
        self._running = True
        self.shutdown_event.clear()
        self._worker_task = asyncio.create_task(self._worker_loop())

    async def stop(self, timeout=5.0):
        if not self._running:
            return
        self.shutdown_event.set()
        if self._worker_task:
            try:
                await asyncio.wait_for(self._worker_task, timeout=timeout)
            except asyncio.TimeoutError:
                self._worker_task.cancel()
                try:
                    await self._worker_task
                except asyncio.CancelledError:
                    pass
        self._running = False

    async def enqueue(self, file_path: str):
        task = MockTask(file_path=file_path, attempt=1)
        await self.queue.put(task)

    async def _worker_loop(self):
        while True:
            # Check if we should shutdown: shutdown_event is set AND queue is empty
            if self.shutdown_event.is_set() and self.queue.empty():
                break

            try:
                task = await asyncio.wait_for(self.queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue

            if task is None:
                continue

            await self._process_task_wrapper(task)

    async def _process_task_wrapper(self, task):
        try:
            await self._process_task(task)
        finally:
            self.queue.task_done()

    async def _process_task(self, task):
        # Simulate processing
        await asyncio.sleep(0.01)
        self.processed.append(task.file_path)

    @property
    def is_running(self):
        return self._running

    @property
    def queue_size(self):
        return self.queue.qsize()


# Singleton instance for the tests below
_processor_instance = None


def get_mock_processor(max_retries=3, retry_delay=1.0):
    global _processor_instance
    if _processor_instance is None:
        _processor_instance = MockProcessor(
            max_retries=max_retries, retry_delay=retry_delay
        )
    return _processor_instance


def reset_mock_processor():
    global _processor_instance
    if _processor_instance is not None and _processor_instance.is_running:
        asyncio.create_task(_processor_instance.stop())
    _processor_instance = None


@pytest.fixture()
def processor_factory():
    """Hand out fresh mock processors, containing the module global per test."""
    global _processor_instance

    def _factory():
        reset_mock_processor()
        return get_mock_processor()

    yield _factory

    # The test's own event loop is already closed by the time fixture
    # finalizers run, so a processor leaked by a FAILED test cannot be stopped:
    # MockProcessor.stop() awaits a worker task bound to that closed loop and
    # reset_mock_processor() would call asyncio.create_task without a running
    # loop (PRR-004, reproduced empirically). The leaked instance is inert once
    # its loop is closed — clear the module global unconditionally.
    _processor_instance = None


async def test_singleton_pattern(processor_factory):
    """get_mock_processor returns the same instance across calls."""
    processor1 = processor_factory()
    processor2 = get_mock_processor()

    assert processor1 is processor2, (
        "get_mock_processor should return the singleton instance"
    )


async def test_processor_lifecycle(processor_factory):
    """The processor starts and stops cleanly."""
    processor = processor_factory()

    assert not processor.is_running, "Processor should not be running initially"

    await processor.start()
    assert processor.is_running, "Processor should be running after start()"

    await processor.stop(timeout=5.0)
    assert not processor.is_running, "Processor should not be running after stop()"


async def test_processor_queues_items(processor_factory):
    """Enqueued items drain through the worker loop to the task handler."""
    processor = processor_factory()
    test_items = ["file1.txt", "file2.txt", "file3.txt"]

    await processor.start()

    for item in test_items:
        await processor.enqueue(item)

    assert processor.queue_size == len(test_items), (
        f"Queue should have {len(test_items)} items"
    )

    waited = 0.0
    while processor.queue_size > 0 and waited < 10.0:
        await asyncio.sleep(0.05)
        waited += 0.05

    assert processor.queue_size == 0, "Queue should be empty after processing"
    assert len(processor.processed) == len(test_items), (
        f"All {len(test_items)} items should be processed"
    )

    await processor.stop(timeout=5.0)


async def test_graceful_shutdown_with_pending_items(processor_factory):
    """The processor processes all queued items before shutdown completes."""
    processor = processor_factory()
    test_items = ["fileA.txt", "fileB.txt"]

    await processor.start()

    for item in test_items:
        await processor.enqueue(item)

    # Initiate shutdown immediately (without waiting for the queue to drain)
    await processor.stop(timeout=5.0)

    assert len(processor.processed) == len(test_items), (
        f"All {len(test_items)} items should be processed before shutdown"
    )
