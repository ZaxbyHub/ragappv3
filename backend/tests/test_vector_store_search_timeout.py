"""
Tests for vector store search semaphore timeout behavior.

This module tests the bounded timeout added to search semaphore acquisition:
1. search_semaphore_timeout_seconds defaults to 30.0
2. _acquire_search_semaphore raises VectorStoreError when acquisition times out
3. _acquire_search_semaphore uses the configured timeout value
4. search() raises VectorStoreError when semaphore acquisition times out
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import Settings, settings
from app.services.vector_store import VectorStore, VectorStoreError


class TestSearchSemaphoreTimeoutDefaults(unittest.TestCase):
    """Test cases for search semaphore timeout config defaults."""

    def test_search_semaphore_timeout_seconds_default_is_30(self):
        """Test that search_semaphore_timeout_seconds defaults to 30.0."""
        settings = Settings()
        self.assertEqual(settings.search_semaphore_timeout_seconds, 30.0)

    def test_search_semaphore_timeout_seconds_validator_rejects_zero(self):
        """Test that search_semaphore_timeout_seconds validator rejects 0."""
        with self.assertRaises(ValueError):
            Settings(search_semaphore_timeout_seconds=0)

    def test_search_semaphore_timeout_seconds_validator_rejects_negative(self):
        """Test that search_semaphore_timeout_seconds validator rejects negative values."""
        with self.assertRaises(ValueError):
            Settings(search_semaphore_timeout_seconds=-1)

    def test_search_semaphore_timeout_seconds_validator_rejects_over_300(self):
        """Test that search_semaphore_timeout_seconds validator rejects values over 300."""
        with self.assertRaises(ValueError):
            Settings(search_semaphore_timeout_seconds=301)


class TestAcquireSearchSemaphoreTimeout(unittest.IsolatedAsyncioTestCase):
    """Test cases for _acquire_search_semaphore timeout behavior."""

    async def test_acquire_search_semaphore_raises_on_timeout(self):
        """
        Test that _acquire_search_semaphore raises VectorStoreError on timeout.

        When asyncio.wait_for times out, _acquire_search_semaphore should raise
        VectorStoreError with a message indicating the timeout duration.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        with patch("app.services.vector_store.asyncio.wait_for") as mock_wait_for:
            mock_wait_for.side_effect = asyncio.TimeoutError()

            with self.assertRaises(VectorStoreError) as ctx:
                async with store._acquire_search_semaphore():
                    pass

            self.assertIn("timed out", str(ctx.exception))
            self.assertIn("30.0", str(ctx.exception))

    async def test_acquire_search_semaphore_uses_correct_timeout_from_settings(self):
        """
        Test that _acquire_search_semaphore passes the correct timeout to asyncio.wait_for.

        The timeout should be settings.search_semaphore_timeout_seconds.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        captured_timeout = None

        async def mock_wait_for(coro, timeout):
            nonlocal captured_timeout
            captured_timeout = timeout
            return await coro

        with patch("app.services.vector_store.asyncio.wait_for", side_effect=mock_wait_for):
            store._search_semaphore = asyncio.Semaphore(1)
            store._search_semaphore.acquire = AsyncMock(return_value=True)
            store._search_semaphore.release = MagicMock()

            async with store._acquire_search_semaphore():
                pass

        self.assertEqual(captured_timeout, 30.0)

    async def test_acquire_search_semaphore_releases_on_timeout(self):
        """
        Test that _acquire_search_semaphore does NOT release the semaphore when
        acquisition times out (since it was never acquired).
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        with patch("app.services.vector_store.asyncio.wait_for") as mock_wait_for:
            mock_wait_for.side_effect = asyncio.TimeoutError()

            with self.assertRaises(VectorStoreError):
                async with store._acquire_search_semaphore():
                    pass

        # Semaphore should remain at full value since acquire never completed
        self.assertEqual(store._get_search_semaphore()._value, 32)


class TestSearchSemaphoreTimeoutIntegration(unittest.IsolatedAsyncioTestCase):
    """Integration tests for search timeout behavior."""

    async def test_search_raises_vector_store_error_on_semaphore_timeout(self):
        """
        Test that VectorStore.search raises VectorStoreError when the search
        semaphore acquisition times out.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))
        store.db = MagicMock()
        store.db.table_names = AsyncMock(return_value=["chunks"])
        store.table = MagicMock()
        store.table.list_indices = AsyncMock(return_value=[])
        store.table.count_rows = AsyncMock(return_value=0)

        with patch("app.services.vector_store.asyncio.wait_for") as mock_wait_for:
            mock_wait_for.side_effect = asyncio.TimeoutError()
            with patch.object(settings, "multi_scale_indexing_enabled", False):
                with self.assertRaises(VectorStoreError) as ctx:
                    await store.search(
                        embedding=[0.0] * 384,
                        limit=10,
                    )

                self.assertIn("timed out", str(ctx.exception))

    async def test_search_semaphore_timeout_uses_configured_value(self):
        """
        Test that _acquire_search_semaphore respects search_semaphore_timeout_seconds
        from settings.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        captured_timeout = None

        async def mock_wait_for(coro, timeout):
            nonlocal captured_timeout
            captured_timeout = timeout
            # Simulate immediate success by acquiring the semaphore directly
            sem = store._get_search_semaphore()
            await sem.acquire()
            return None

        with patch("app.services.vector_store.asyncio.wait_for", side_effect=mock_wait_for):
            store._search_semaphore = asyncio.Semaphore(32)
            store._search_semaphore.release = MagicMock()

            async with store._acquire_search_semaphore():
                pass

        self.assertEqual(captured_timeout, 30.0)


if __name__ == "__main__":
    unittest.main()
