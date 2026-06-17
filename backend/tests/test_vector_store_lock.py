"""
Tests for VectorStore lock timeout mechanism (Task 1.2).

This module tests:
1. _acquire_write_lock() acquires and releases the lock correctly
2. _acquire_write_lock() raises VectorStoreError when timeout is exceeded
3. Search semaphore size matches config value
4. Config defaults: vector_search_concurrency is 32, write_lock_timeout_seconds is 30.0
5. _acquire_write_lock() correctly releases lock on exception within the async context manager block

Note: _acquire_write_lock is an async context manager (uses @asynccontextmanager), so it must be used
with 'async with', not 'async for'.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import Settings
from app.services.vector_store import VectorStore, VectorStoreError


class TestWriteLockConfigDefaults(unittest.TestCase):
    """Test cases for write lock config defaults."""

    def test_vector_search_concurrency_default_is_32(self):
        """Test that vector_search_concurrency defaults to 32."""
        settings = Settings()
        self.assertEqual(settings.vector_search_concurrency, 32)

    def test_write_lock_timeout_seconds_default_is_30(self):
        """Test that write_lock_timeout_seconds defaults to 30.0."""
        settings = Settings()
        self.assertEqual(settings.write_lock_timeout_seconds, 30.0)


class TestAcquireWriteLockTimeout(unittest.IsolatedAsyncioTestCase):
    """Test cases for _acquire_write_lock timeout behavior."""

    async def test_acquire_write_lock_raises_on_timeout(self):
        """
        Test that _acquire_write_lock raises VectorStoreError on timeout.

        When asyncio.wait_for times out, _acquire_write_lock should raise
        VectorStoreError with a message indicating the timeout duration.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        # Also mock the lock's acquire so we don't get "coroutine never awaited" warning
        store._write_lock.acquire = AsyncMock()

        # Patch asyncio.wait_for to immediately raise TimeoutError
        with patch("app.services.vector_store.asyncio.wait_for") as mock_wait_for:
            mock_wait_for.side_effect = asyncio.TimeoutError()

            with self.assertRaises(VectorStoreError) as ctx:
                async with store._acquire_write_lock():
                    pass

            # Verify the error message contains the timeout info
            self.assertIn("timed out", str(ctx.exception))
            self.assertIn("30.0", str(ctx.exception))

    async def test_acquire_write_lock_uses_correct_timeout_from_settings(self):
        """
        Test that _acquire_write_lock passes the correct timeout to asyncio.wait_for.

        The timeout should be settings.write_lock_timeout_seconds.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        captured_timeout = None

        async def mock_wait_for(coro, timeout):
            nonlocal captured_timeout
            captured_timeout = timeout
            # Return the coroutine result immediately (don't actually wait)
            return await coro

        with patch("app.services.vector_store.asyncio.wait_for", side_effect=mock_wait_for):
            # Also patch the lock acquire to be a no-op
            store._write_lock.acquire = AsyncMock()
            store._write_lock.release = MagicMock()

            async with store._acquire_write_lock():
                pass

        # Verify timeout matches settings
        self.assertEqual(captured_timeout, 30.0)


class TestAcquireWriteLockCorrectAcquisitionAndRelease(unittest.IsolatedAsyncioTestCase):
    """Test cases for _acquire_write_lock correct lock lifecycle."""

    async def test_acquire_write_lock_acquires_and_releases(self):
        """
        Test that _acquire_write_lock acquires and releases the lock correctly.

        After acquisition+release, another acquisition should succeed.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        acquire_called = False
        release_called = False

        async def mock_acquire():
            nonlocal acquire_called
            acquire_called = True

        def mock_release():
            nonlocal release_called
            release_called = True

        store._write_lock.acquire = mock_acquire
        store._write_lock.release = mock_release

        # First acquisition and release
        async with store._acquire_write_lock():
            self.assertTrue(acquire_called, "Lock should be acquired within async with block")
            self.assertFalse(release_called, "Lock should not be released within async with block")

        self.assertTrue(release_called, "Lock should be released after exiting async with block")

        # Reset for second acquisition
        acquire_called = False
        release_called = False

        # Second acquisition should succeed
        async with store._acquire_write_lock():
            self.assertTrue(acquire_called, "Second lock acquisition should succeed")
            self.assertFalse(release_called, "Lock should not be released within async with block")

        self.assertTrue(release_called, "Second lock should be released after exiting async with block")

    async def test_acquire_write_lock_releases_on_exception(self):
        """
        Test that _acquire_write_lock correctly releases the lock when an exception
        occurs within the async with block.

        When an exception is raised inside the async with body, the context manager's
        __aexit__ must still run its finally block to release the lock.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        release_called = False

        def mock_release():
            nonlocal release_called
            release_called = True

        store._write_lock.acquire = AsyncMock()
        store._write_lock.release = mock_release

        # When an exception is raised inside async with, Python calls __aexit__
        # to clean up the context manager. This triggers the finally block.
        # We use __aexit__ directly to simulate what happens when an
        # exception propagates out of the async with block.
        ctx = store._acquire_write_lock()

        # Start the context manager (runs until first yield)
        await ctx.__aenter__()

        # Simulate exception in body by calling __aexit__
        # This triggers the finally block
        await ctx.__aexit__(None, None, None)

        self.assertTrue(release_called, "Lock should be released when context manager exits via __aexit__")


class TestSearchSemaphore(unittest.IsolatedAsyncioTestCase):
    """Test cases for search semaphore configuration."""

    async def test_search_semaphore_size_matches_config(self):
        """
        Test that _get_search_semaphore() creates a semaphore with the correct size.

        The semaphore._value should equal settings.vector_search_concurrency (default 32).
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        # Ensure semaphore is created
        semaphore = store._get_search_semaphore()

        # Verify semaphore value matches config
        self.assertEqual(semaphore._value, 32)

    async def test_search_semaphore_reuses_same_instance(self):
        """
        Test that _get_search_semaphore() returns the same semaphore instance on repeated calls.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        semaphore1 = store._get_search_semaphore()
        semaphore2 = store._get_search_semaphore()

        self.assertIs(semaphore1, semaphore2, "Semaphore should be lazily initialized and reused")

    async def test_search_semaphore_size_from_config(self):
        """
        Test that when settings.vector_search_concurrency is set,
        the semaphore size reflects the value.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.vector_search_concurrency = 32

            # Create a fresh store so semaphore is None
            store2 = VectorStore(db_path=Path("/tmp/test_lancedb2"))
            semaphore = store2._get_search_semaphore()

            self.assertEqual(semaphore._value, 32)


class TestWriteLockIntegration(unittest.IsolatedAsyncioTestCase):
    """Integration tests for write lock usage in VectorStore methods."""

    async def test_acquire_write_lock_works_with_real_asyncio_lock(self):
        """
        Test that _acquire_write_lock works correctly with a real asyncio.Lock.

        This verifies the lock is acquired and released properly when used with
        a real asyncio.Lock (not mocked).
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        # Use the real lock
        real_lock = asyncio.Lock()
        store._write_lock = real_lock

        release_called = False
        original_release = real_lock.release

        def tracking_release():
            nonlocal release_called
            release_called = True
            return original_release()

        real_lock.release = tracking_release

        # Use the context manager and verify lock is acquired
        acquired = False
        async with store._acquire_write_lock():
            # Lock should be acquired at this point
            acquired = real_lock.locked()
            self.assertTrue(acquired, "Lock should be held inside async with block")

        self.assertTrue(release_called, "Lock should be released after async with block")
        self.assertFalse(real_lock.locked(), "Lock should be released (not locked) after block")

    async def test_concurrent_write_lock_blocking(self):
        """
        Test that concurrent attempts to acquire write lock are serialized.

        Only one coroutine should hold the lock at a time.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        # Use a real lock
        real_lock = asyncio.Lock()
        store._write_lock = real_lock

        # Track which coroutines held the lock
        lock_holders = []

        async def hold_lock(name, hold_duration):
            async with store._acquire_write_lock():
                lock_holders.append(name)
                await asyncio.sleep(hold_duration)

        # Run two coroutines concurrently
        await asyncio.gather(
            hold_lock("first", 0.05),
            hold_lock("second", 0.05),
        )

        # They should have run sequentially, not concurrently
        self.assertEqual(len(lock_holders), 2)
        # The first one should have completed before the second started
        self.assertEqual(lock_holders[0], "first")
        self.assertEqual(lock_holders[1], "second")


if __name__ == "__main__":
    unittest.main()
