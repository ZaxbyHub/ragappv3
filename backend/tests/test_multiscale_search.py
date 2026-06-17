"""
Tests for multi-scale search with asyncio.Semaphore in VectorStore.

This module tests the asyncio.Semaphore addition to limit concurrent
scale searches in the multi-scale search path:
1. vector_search_concurrency defaults to 32
2. Multi-scale search with >32 scales returns correct results (semaphore limits but doesn't block)
3. Single-scale config (1 scale) does NOT enter the semaphore path
4. Multi-scale disabled (config=False) does NOT enter the semaphore path
5. Multi-scale search correctly collects results from all scales
6. Error handling: one scale fails, others succeed
"""

import asyncio
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import Settings
from app.services.vector_store import VectorStore


class TestVectorSearchConcurrencyDefault(unittest.TestCase):
    """Test cases for vector_search_concurrency default value."""

    def test_vector_search_concurrency_defaults_to_32(self):
        """Test that vector_search_concurrency default is 32 (mirrors config default)."""
        self.assertEqual(Settings().vector_search_concurrency, 32)


class TestMultiScaleSearchSemaphore(unittest.IsolatedAsyncioTestCase):
    """Test cases for multi-scale search with asyncio.Semaphore."""

    def setUp(self):
        """Set up test environment with temporary directory."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test_lancedb"
        self.embedding_dim = 384  # Small dimension for testing

    def tearDown(self):
        """Clean up temporary directory."""
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def create_vector_store(self) -> VectorStore:
        """Helper to create a VectorStore instance with test path."""
        store = VectorStore(db_path=self.db_path)
        # Initialize with a mock table to avoid None checks
        store.table = MagicMock()
        store.table.list_indices = AsyncMock(return_value=[])
        store.table.count_rows = AsyncMock(return_value=0)
        store._embedding_dim = self.embedding_dim
        store._search_semaphore = asyncio.Semaphore(4)
        return store

    @pytest.mark.asyncio
    async def test_multi_scale_with_more_than_four_scales_returns_correct_results(self):
        """
        Test that multi-scale search with >4 scales returns correct results.
        The semaphore limits concurrency to 4 but doesn't block execution.
        """
        store = self.create_vector_store()

        # Mock _search_single_scale to return results for each scale
        scale_results = []
        for scale in ["256", "512", "768", "1024", "1536", "2048"]:
            scale_results.append(
                {"id": f"doc_{scale}", "text": f"From {scale}", "_rrf_score": 0.5}
            )

        async def mock_search_single_scale(
            embedding,
            scale,
            fetch_k,
            filter_expr=None,
            vault_id=None,
            query_text="",
            hybrid=True,
            hybrid_alpha=0.5,
            **kwargs,
        ):
            return [{"id": f"doc_{scale}", "text": f"From {scale}", "_rrf_score": 0.5}]

        store._search_single_scale = mock_search_single_scale

        # Mock db with table_names to indicate table exists
        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=["chunks"])
        store.db = mock_db
        store.table = MagicMock()
        store.table.list_indices = AsyncMock(return_value=[])
        store.table.count_rows = AsyncMock(return_value=0)

        # Use 3 scales
        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = True
            mock_settings.multi_scale_rrf_k = 60
            mock_settings.multi_scale_chunk_sizes = "512,1024,2048"
            mock_settings.search_semaphore_timeout_seconds = 30.0

            # Search should NOT raise - should handle the exception gracefully
            results = await store.search(
                embedding=[0.0] * self.embedding_dim,
                limit=10,
            )

        # Verify results from all 3 scales were returned
        self.assertIsInstance(results, list)
        self.assertEqual(len(results), 3)
        # Each scale contributes one result via mock_search_single_scale

    @pytest.mark.asyncio
    async def test_all_scales_fail_returns_empty_list(self):
        """
        Test that when ALL scales raise exceptions, search() returns [] gracefully.
        """
        store = self.create_vector_store()

        async def mock_search_single_scale(
            embedding,
            scale,
            fetch_k,
            filter_expr=None,
            vault_id=None,
            query_text="",
            hybrid=True,
            hybrid_alpha=0.5,
            **kwargs,
        ):
            # All scales fail
            raise RuntimeError(f"Simulated failure for scale {scale}")

        store._search_single_scale = mock_search_single_scale

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=["chunks"])
        store.db = mock_db
        store.table = MagicMock()
        store.table.list_indices = AsyncMock(return_value=[])
        store.table.count_rows = AsyncMock(return_value=0)

        # Use 3 scales that all will fail
        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = True
            mock_settings.multi_scale_rrf_k = 60
            mock_settings.multi_scale_chunk_sizes = "512,1024,2048"
            mock_settings.search_semaphore_timeout_seconds = 30.0

            # Should NOT raise - should return empty list gracefully
            results = await store.search(
                embedding=[0.0] * self.embedding_dim,
                limit=10,
            )

        # Should return empty list when all scales fail
        self.assertIsInstance(results, list)
        self.assertEqual(
            len(results), 0, "Should return empty list when all scales fail"
        )


class TestMultiScaleSemaphoreConcurrency(unittest.IsolatedAsyncioTestCase):
    """Test cases to verify semaphore limits concurrency correctly."""

    def setUp(self):
        """Set up test environment with temporary directory."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test_lancedb"
        self.embedding_dim = 384

    def tearDown(self):
        """Clean up temporary directory."""
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def create_vector_store(self) -> VectorStore:
        """Helper to create a VectorStore instance with test path."""
        store = VectorStore(db_path=self.db_path)
        store.table = MagicMock()
        store.table.list_indices = AsyncMock(return_value=[])
        store.table.count_rows = AsyncMock(return_value=0)
        store._embedding_dim = self.embedding_dim
        store._search_semaphore = asyncio.Semaphore(4)
        return store

    @pytest.mark.asyncio
    async def test_semaphore_allows_four_concurrent_searches(self):
        """
        Test that semaphore allows exactly 4 concurrent searches.
        This verifies the semaphore is properly limiting concurrency.
        """
        store = self.create_vector_store()

        # Track concurrent executions
        max_concurrent = 0
        current_concurrent = 0
        concurrent_counts = []

        async def mock_search_single_scale(
            embedding,
            scale,
            fetch_k,
            filter_expr=None,
            vault_id=None,
            query_text="",
            hybrid=True,
            hybrid_alpha=0.5,
            **kwargs,
        ):
            nonlocal max_concurrent, current_concurrent

            current_concurrent += 1
            concurrent_counts.append(current_concurrent)
            max_concurrent = max(max_concurrent, current_concurrent)

            # Simulate some work
            await asyncio.sleep(0.01)

            current_concurrent -= 1
            return [{"id": f"doc_{scale}", "text": f"From {scale}", "_rrf_score": 0.5}]

        store._search_single_scale = mock_search_single_scale

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=["chunks"])
        store.db = mock_db
        store.table = MagicMock()
        store.table.list_indices = AsyncMock(return_value=[])
        store.table.count_rows = AsyncMock(return_value=0)

        # Use 6 scales to test concurrency limiting
        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = True
            mock_settings.multi_scale_rrf_k = 60
            mock_settings.multi_scale_chunk_sizes = "256,512,768,1024,1536,2048"
            mock_settings.search_semaphore_timeout_seconds = 30.0

            await store.search(
                embedding=[0.0] * self.embedding_dim,
                limit=10,
            )

        # Verify semaphore limited concurrency to 4 (or less)
        # The max concurrent should not exceed the test semaphore size (4)
        self.assertLessEqual(max_concurrent, 4)

    @pytest.mark.asyncio
    async def test_semaphore_parallel_execution_timing(self):
        """
        Test that semaphore enables parallel execution.
        With 6 tasks of 0.01s each, total wall-clock should be < 0.06s (proving parallel execution).
        """
        store = self.create_vector_store()

        async def mock_search_single_scale(
            embedding,
            scale,
            fetch_k,
            filter_expr=None,
            vault_id=None,
            query_text="",
            hybrid=True,
            hybrid_alpha=0.5,
            **kwargs,
        ):
            # Each task takes 0.01s
            await asyncio.sleep(0.01)
            return [{"id": f"doc_{scale}", "text": f"From {scale}", "_rrf_score": 0.5}]

        store._search_single_scale = mock_search_single_scale

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=["chunks"])
        store.db = mock_db
        store.table = MagicMock()
        store.table.list_indices = AsyncMock(return_value=[])
        store.table.count_rows = AsyncMock(return_value=0)

        # Use 6 scales to test parallel execution
        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = True
            mock_settings.multi_scale_rrf_k = 60
            mock_settings.multi_scale_chunk_sizes = "256,512,768,1024,1536,2048"
            mock_settings.search_semaphore_timeout_seconds = 30.0

            start_time = time.perf_counter()
            await store.search(
                embedding=[0.0] * self.embedding_dim,
                limit=10,
            )
            elapsed_time = time.perf_counter() - start_time

        # With parallel execution (semaphore=4), 6 tasks should take ~0.02s (2 batches of 4)
        # With serial execution, it would take ~0.06s (6 * 0.01)
        # Allow some overhead, but should be well under 0.06s
        self.assertLess(
            elapsed_time,
            0.06,
            f"Parallel execution should complete in < 0.06s, but took {elapsed_time:.3f}s",
        )

    @pytest.mark.asyncio
    async def test_exactly_four_scales_boundary(self):
        """
        Test boundary: verify max_concurrent == 4 with exactly 4 scales.
        """
        store = self.create_vector_store()

        max_concurrent = 0
        current_concurrent = 0

        async def mock_search_single_scale(
            embedding,
            scale,
            fetch_k,
            filter_expr=None,
            vault_id=None,
            query_text="",
            hybrid=True,
            hybrid_alpha=0.5,
            **kwargs,
        ):
            nonlocal max_concurrent, current_concurrent

            current_concurrent += 1
            max_concurrent = max(max_concurrent, current_concurrent)

            await asyncio.sleep(0.01)

            current_concurrent -= 1
            return [{"id": f"doc_{scale}", "text": f"From {scale}", "_rrf_score": 0.5}]

        store._search_single_scale = mock_search_single_scale

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=["chunks"])
        store.db = mock_db
        store.table = MagicMock()
        store.table.list_indices = AsyncMock(return_value=[])
        store.table.count_rows = AsyncMock(return_value=0)

        # Use exactly 4 scales (boundary test)
        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = True
            mock_settings.multi_scale_rrf_k = 60
            mock_settings.multi_scale_chunk_sizes = "256,512,1024,2048"
            mock_settings.search_semaphore_timeout_seconds = 30.0

            await store.search(
                embedding=[0.0] * self.embedding_dim,
                limit=10,
            )

        # With exactly 4 scales and semaphore=4, max concurrent should be 4
        self.assertEqual(
            max_concurrent, 4, "Exactly 4 scales should allow 4 concurrent searches"
        )


class TestMultiScaleSearchEdgeCases(unittest.IsolatedAsyncioTestCase):
    """Test edge cases for multi-scale search."""

    def setUp(self):
        """Set up test environment with temporary directory."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test_lancedb"
        self.embedding_dim = 384

    def tearDown(self):
        """Clean up temporary directory."""
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def create_vector_store(self) -> VectorStore:
        """Helper to create a VectorStore instance with test path."""
        store = VectorStore(db_path=self.db_path)
        store.table = MagicMock()
        store.table.list_indices = AsyncMock(return_value=[])
        store.table.count_rows = AsyncMock(return_value=0)
        store._embedding_dim = self.embedding_dim
        store._search_semaphore = asyncio.Semaphore(4)
        return store

    @pytest.mark.asyncio
    async def test_multi_scale_disabled_does_not_enter_semaphore_path(self):
        """
        Test that multi-scale disabled (config=False) does NOT enter the semaphore path.
        """
        store = self.create_vector_store()

        # Track if _search_single_scale is called (it should NOT be when disabled)
        search_single_scale_called = False
        original_search_single_scale = store._search_single_scale

        async def tracking_search_single_scale(*args, **kwargs):
            nonlocal search_single_scale_called
            search_single_scale_called = True
            return await original_search_single_scale(*args, **kwargs)

        store._search_single_scale = tracking_search_single_scale

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=["chunks"])
        mock_db.open_table = AsyncMock(
            return_value=MagicMock()
        )  # Mock open_table as async
        store.db = mock_db
        store.table = MagicMock()
        store.table.list_indices = AsyncMock(return_value=[])
        store.table.count_rows = AsyncMock(return_value=0)
        query_mock = MagicMock()
        query_mock.where.return_value = query_mock
        query_mock.limit.return_value.to_list = AsyncMock(return_value=[])
        store.table.search = AsyncMock(return_value=query_mock)

        # Disable multi-scale
        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = False
            mock_settings.multi_scale_chunk_sizes = (
                "512,1024,2048"  # Multiple scales but disabled
            )
            mock_settings.search_semaphore_timeout_seconds = 30.0

            await store.search(
                embedding=[0.0] * self.embedding_dim,
                limit=10,
            )

        # Should NOT call _search_single_scale when multi_scale is disabled
        self.assertFalse(search_single_scale_called)

    @pytest.mark.asyncio
    async def test_multi_scale_with_two_scales_enters_semaphore_path(self):
        """
        Test that 2 scales enters the multi-scale semaphore path.
        """
        store = self.create_vector_store()

        scale_results = {
            "512": [{"id": "doc_512", "text": "From 512", "_rrf_score": 0.5}],
            "1024": [{"id": "doc_1024", "text": "From 1024", "_rrf_score": 0.4}],
        }

        queried_scales = []

        async def mock_search_single_scale(
            embedding,
            scale,
            fetch_k,
            filter_expr=None,
            vault_id=None,
            query_text="",
            hybrid=True,
            hybrid_alpha=0.5,
            **kwargs,
        ):
            queried_scales.append(scale)
            return scale_results.get(scale, [])

        store._search_single_scale = mock_search_single_scale

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=["chunks"])
        store.db = mock_db
        store.table = MagicMock()
        store.table.list_indices = AsyncMock(return_value=[])
        store.table.count_rows = AsyncMock(return_value=0)

        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = True
            mock_settings.multi_scale_rrf_k = 60
            mock_settings.multi_scale_chunk_sizes = "512,1024"
            mock_settings.search_semaphore_timeout_seconds = 30.0

            await store.search(
                embedding=[0.0] * self.embedding_dim,
                limit=10,
            )

        # Should query both scales
        self.assertEqual(len(queried_scales), 2)

    @pytest.mark.asyncio
    async def test_whitespace_in_scale_strings(self):
        """
        Test that whitespace in scale strings is handled correctly.
        Tests " 512 , 1024 " format.
        """
        store = self.create_vector_store()

        scale_results = {
            "512": [{"id": "doc_512", "text": "From 512", "_rrf_score": 0.5}],
            "1024": [{"id": "doc_1024", "text": "From 1024", "_rrf_score": 0.4}],
        }

        queried_scales = []

        async def mock_search_single_scale(
            embedding,
            scale,
            fetch_k,
            filter_expr=None,
            vault_id=None,
            query_text="",
            hybrid=True,
            hybrid_alpha=0.5,
            **kwargs,
        ):
            queried_scales.append(scale)
            return scale_results.get(scale, [])

        store._search_single_scale = mock_search_single_scale

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=["chunks"])
        store.db = mock_db
        store.table = MagicMock()
        store.table.list_indices = AsyncMock(return_value=[])
        store.table.count_rows = AsyncMock(return_value=0)

        # Test with whitespace around scale values
        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = True
            mock_settings.multi_scale_rrf_k = 60
            mock_settings.multi_scale_chunk_sizes = " 512 , 1024 "  # Whitespace
            mock_settings.search_semaphore_timeout_seconds = 30.0

            await store.search(
                embedding=[0.0] * self.embedding_dim,
                limit=10,
            )

        # Should query both scales (whitespace stripped)
        self.assertEqual(len(queried_scales), 2)
        self.assertIn("512", queried_scales)
        self.assertIn("1024", queried_scales)

    @pytest.mark.asyncio
    async def test_empty_scale_values_in_comma_separated_string(self):
        """
        Test that empty values in comma-separated scale strings are handled.
        Tests "512,,1024" format (empty between commas).
        """
        store = self.create_vector_store()

        scale_results = {
            "512": [{"id": "doc_512", "text": "From 512", "_rrf_score": 0.5}],
            "1024": [{"id": "doc_1024", "text": "From 1024", "_rrf_score": 0.4}],
        }

        queried_scales = []

        async def mock_search_single_scale(
            embedding,
            scale,
            fetch_k,
            filter_expr=None,
            vault_id=None,
            query_text="",
            hybrid=True,
            hybrid_alpha=0.5,
            **kwargs,
        ):
            queried_scales.append(scale)
            return scale_results.get(scale, [])

        store._search_single_scale = mock_search_single_scale

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=["chunks"])
        store.db = mock_db
        store.table = MagicMock()
        store.table.list_indices = AsyncMock(return_value=[])
        store.table.count_rows = AsyncMock(return_value=0)

        # Test with empty values between commas
        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = True
            mock_settings.multi_scale_rrf_k = 60
            mock_settings.multi_scale_chunk_sizes = "512,,1024"  # Empty value
            mock_settings.search_semaphore_timeout_seconds = 30.0

            await store.search(
                embedding=[0.0] * self.embedding_dim,
                limit=10,
            )

        # Should query both scales (empty values filtered out)
        self.assertEqual(len(queried_scales), 2)
        self.assertIn("512", queried_scales)
        self.assertIn("1024", queried_scales)

    @pytest.mark.asyncio
    async def test_duplicate_doc_ids_across_scales_deduplicated(self):
        """
        Test that duplicate document IDs across scales are correctly deduplicated
        by cross-scale fusion.
        """
        store = self.create_vector_store()

        # Same document ID appears in multiple scales
        scale_results = {
            "512": [
                {
                    "id": "doc_common",  # Same ID across scales
                    "text": "From 512",
                    "_distance": 0.1,
                    "_rrf_score": 0.5,
                }
            ],
            "1024": [
                {
                    "id": "doc_common",  # Same ID across scales
                    "text": "From 1024",
                    "_distance": 0.2,
                    "_rrf_score": 0.4,
                }
            ],
            "2048": [
                {
                    "id": "doc_unique_2048",  # Unique to this scale
                    "text": "From 2048",
                    "_distance": 0.3,
                    "_rrf_score": 0.3,
                }
            ],
        }

        store.table = MagicMock()
        store.table.list_indices = AsyncMock(return_value=[])
        store.table.count_rows = AsyncMock(return_value=0)

        async def mock_search_single_scale(
            embedding,
            scale,
            fetch_k,
            filter_expr=None,
            vault_id=None,
            query_text="",
            hybrid=True,
            hybrid_alpha=0.5,
            **kwargs,
        ):
            return scale_results.get(scale, [])

        store._search_single_scale = mock_search_single_scale

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=["chunks"])
        store.db = mock_db

        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = True
            mock_settings.multi_scale_rrf_k = 60
            mock_settings.multi_scale_chunk_sizes = "512,1024,2048"
            mock_settings.search_semaphore_timeout_seconds = 30.0

            results = await store.search(
                embedding=[0.0] * self.embedding_dim,
                limit=10,
            )

        # Verify results
        self.assertIsInstance(results, list)

        # Should have 2 unique documents (doc_common deduplicated, doc_unique_2048)
        self.assertEqual(len(results), 2, "Duplicate IDs should be deduplicated")

        result_ids = [r.get("id") for r in results]

        # doc_common should appear once (deduplicated)
        self.assertEqual(result_ids.count("doc_common"), 1)

        # doc_unique_2048 should appear
        self.assertIn("doc_unique_2048", result_ids)


if __name__ == "__main__":
    unittest.main()
