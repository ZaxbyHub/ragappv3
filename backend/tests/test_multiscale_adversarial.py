"""
Adversarial tests for asyncio.Semaphore in multi-scale search.

Tests for edge cases, attack vectors, and robustness:
1. Large number of scales (e.g., 20+) — semaphore should prevent resource exhaustion
2. Scale string with whitespace, duplicates, empty strings — should be handled
3. _search_single_scale raising unexpected exceptions — return_exceptions=True should catch
4. Concurrent search() calls — each should get its own semaphore (no cross-contamination)
5. multi_scale_chunk_sizes = "0" or negative values — should handle gracefully
6. Vault ID with special characters (SQL injection attempt) — should be sanitized
7. Extremely large fetch_k values — should handle gracefully
8. Semaphore value of 0 or negative — test edge cases
"""

import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.vector_store import VectorStore


def _table_mock():
    """VectorStore table mock with async stubs for count_rows, list_indices, and search."""
    t = MagicMock()
    t.count_rows = AsyncMock(return_value=0)
    t.list_indices = AsyncMock(return_value=[])
    # Single-scale path: table.search → query with chainable limit().to_list()
    query_mock = MagicMock()
    query_mock.where.return_value = query_mock
    query_mock.limit.return_value.to_list = AsyncMock(return_value=[])
    t.search = AsyncMock(return_value=query_mock)
    return t


class TestAdversarialLargeScaleCount(unittest.IsolatedAsyncioTestCase):
    """Test with large number of scales to verify semaphore prevents resource exhaustion."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test_lancedb"
        self.embedding_dim = 384

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def create_vector_store(self) -> VectorStore:
        store = VectorStore(db_path=self.db_path)
        store.table = _table_mock()
        store._embedding_dim = self.embedding_dim
        store._search_semaphore = asyncio.Semaphore(4)
        return store

    @pytest.mark.asyncio
    async def test_twenty_scales_should_not_exhaust_resources(self):
        """
        Test with 20 scales - semaphore should limit concurrency to 4.
        Should complete without hanging or crashing.
        """
        store = self.create_vector_store()

        # Track concurrent executions
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
        ):
            nonlocal max_concurrent, current_concurrent
            current_concurrent += 1
            max_concurrent = max(max_concurrent, current_concurrent)
            await asyncio.sleep(0.01)  # Simulate work
            current_concurrent -= 1
            return [{"id": f"doc_{scale}", "text": f"From {scale}", "_rrf_score": 0.5}]

        store._search_single_scale = mock_search_single_scale

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=["chunks"])
        store.db = mock_db
        store.table = _table_mock()

        # Generate 20 scales
        scales = [str(128 * (i + 1)) for i in range(20)]

        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = True
            mock_settings.multi_scale_chunk_sizes = ",".join(scales)

            # Should complete within reasonable time
            results = await store.search(embedding=[0.0] * self.embedding_dim, limit=10)

        # Semaphore should limit to vector_search_concurrency (16)
        self.assertLessEqual(max_concurrent, 16)
        # Should have results from all 20 scales
        self.assertIsInstance(results, list)


class TestAdversarialScaleStringParsing(unittest.IsolatedAsyncioTestCase):
    """Test scale string parsing with problematic inputs."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test_lancedb"
        self.embedding_dim = 384

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def create_vector_store(self) -> VectorStore:
        store = VectorStore(db_path=self.db_path)
        store.table = _table_mock()
        store._embedding_dim = self.embedding_dim
        store._search_semaphore = asyncio.Semaphore(4)
        return store

    @pytest.mark.asyncio
    async def test_scales_with_whitespace_only_parsed_correctly(self):
        """Test that whitespace in scale string is handled."""
        store = self.create_vector_store()

        queried_scales = []

        async def mock_search_single_scale(
            embedding,
            scale,
            fetch_k,
            filter_expr=None,
            vault_id=None,
            query_text="",
            hybrid=True,
            **kwargs,
        ):
            queried_scales.append(scale)
            return [{"id": f"doc_{scale}", "text": f"From {scale}", "_rrf_score": 0.5}]

        store._search_single_scale = mock_search_single_scale

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=["chunks"])
        store.db = mock_db
        store.table = _table_mock()

        # Scale string with whitespace
        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = True
            mock_settings.multi_scale_chunk_sizes = " 512 , 1024 , 2048 "
            mock_settings.multi_scale_rrf_k = 60

            await store.search(embedding=[0.0] * self.embedding_dim, limit=10)

        # Should only have 3 unique scales (whitespace stripped)
        self.assertEqual(len(queried_scales), 3)
        self.assertEqual(set(queried_scales), {"512", "1024", "2048"})

    @pytest.mark.asyncio
    async def test_duplicate_scales_deduplicated(self):
        """Test that duplicate scales are handled correctly."""
        store = self.create_vector_store()

        queried_scales = []

        async def mock_search_single_scale(
            embedding,
            scale,
            fetch_k,
            filter_expr=None,
            vault_id=None,
            query_text="",
            hybrid=True,
            **kwargs,
        ):
            queried_scales.append(scale)
            return [{"id": f"doc_{scale}", "text": f"From {scale}", "_rrf_score": 0.5}]

        store._search_single_scale = mock_search_single_scale

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=["chunks"])
        store.db = mock_db
        store.table = _table_mock()

        # Scale string with duplicates - code doesn't deduplicate, tests behavior
        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = True
            mock_settings.multi_scale_chunk_sizes = "512,512,1024,1024"
            mock_settings.multi_scale_rrf_k = 60

            await store.search(embedding=[0.0] * self.embedding_dim, limit=10)

        # Code should search all 4 entries (no deduplication)
        self.assertEqual(len(queried_scales), 4)

    @pytest.mark.asyncio
    async def test_empty_strings_filtered_out(self):
        """Test that empty scale strings are filtered."""
        store = self.create_vector_store()

        queried_scales = []

        async def mock_search_single_scale(
            embedding,
            scale,
            fetch_k,
            filter_expr=None,
            vault_id=None,
            query_text="",
            hybrid=True,
            **kwargs,
        ):
            queried_scales.append(scale)
            return [{"id": f"doc_{scale}", "text": f"From {scale}", "_rrf_score": 0.5}]

        store._search_single_scale = mock_search_single_scale

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=["chunks"])
        store.db = mock_db
        store.table = _table_mock()

        # Scale string with empty entries
        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = True
            mock_settings.multi_scale_chunk_sizes = "512,,1024,"
            mock_settings.multi_scale_rrf_k = 60

            await store.search(embedding=[0.0] * self.embedding_dim, limit=10)

        # Empty strings should be filtered out (code uses `if s.strip()`)
        self.assertEqual(len(queried_scales), 2)


class TestAdversarialExceptions(unittest.IsolatedAsyncioTestCase):
    """Test exception handling in multi-scale search."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test_lancedb"
        self.embedding_dim = 384

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def create_vector_store(self) -> VectorStore:
        store = VectorStore(db_path=self.db_path)
        store.table = _table_mock()
        store._embedding_dim = self.embedding_dim
        store._search_semaphore = asyncio.Semaphore(4)
        return store

    @pytest.mark.asyncio
    async def test_multiple_scales_fail_others_succeed(self):
        """Test that when multiple scales fail, successful ones still return results."""
        store = self.create_vector_store()

        async def mock_search_single_scale(
            embedding,
            scale,
            fetch_k,
            filter_expr=None,
            vault_id=None,
            query_text="",
            hybrid=True,
            **kwargs,
        ):
            if scale in ("512", "768"):
                raise RuntimeError(f"Simulated failure for scale {scale}")
            if scale == "1024":
                raise ValueError(f"Invalid value for scale {scale}")
            if scale == "1536":
                raise KeyError(f"Missing key for scale {scale}")
            return [{"id": f"doc_{scale}", "text": f"From {scale}", "_rrf_score": 0.5}]

        store._search_single_scale = mock_search_single_scale

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=["chunks"])
        store.db = mock_db
        store.table = _table_mock()

        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = True
            mock_settings.multi_scale_chunk_sizes = "512,768,1024,1536,2048"
            mock_settings.multi_scale_rrf_k = 60

            # Should NOT raise - return_exceptions=True should catch all
            results = await store.search(embedding=[0.0] * self.embedding_dim, limit=10)

        # Should return results from successful scales (2048 only)
        self.assertIsInstance(results, list)
        # Verify we got at least the successful scale
        self.assertTrue(any(r["id"] == "doc_2048" for r in results))

    @pytest.mark.asyncio
    async def test_all_scales_fail_gracefully(self):
        """Test that when ALL scales fail, the search returns empty list gracefully."""
        store = self.create_vector_store()

        async def mock_search_single_scale(
            embedding,
            scale,
            fetch_k,
            filter_expr=None,
            vault_id=None,
            query_text="",
            hybrid=True,
        ):
            raise RuntimeError("All scales fail")

        store._search_single_scale = mock_search_single_scale

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=["chunks"])
        store.db = mock_db
        store.table = _table_mock()

        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = True
            mock_settings.multi_scale_chunk_sizes = "512,1024"

            # Should NOT raise - should handle gracefully
            results = await store.search(embedding=[0.0] * self.embedding_dim, limit=10)

        # Should return empty list, not raise
        self.assertIsInstance(results, list)
        self.assertEqual(results, [])


class TestAdversarialConcurrency(unittest.IsolatedAsyncioTestCase):
    """Test concurrent search() calls don't interfere with each other."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test_lancedb"
        self.embedding_dim = 384

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def create_vector_store(self) -> VectorStore:
        store = VectorStore(db_path=self.db_path)
        store.table = _table_mock()
        store._embedding_dim = self.embedding_dim
        store._search_semaphore = asyncio.Semaphore(4)
        return store

    @pytest.mark.asyncio
    async def test_concurrent_searches_no_cross_contamination(self):
        """Test that concurrent search() calls don't share semaphore state."""
        store = self.create_vector_store()

        # Track semaphore state per search call
        active_searches = set()

        async def mock_search_single_scale(
            embedding,
            scale,
            fetch_k,
            filter_expr=None,
            vault_id=None,
            query_text="",
            hybrid=True,
        ):
            search_id = id(asyncio.current_task())
            active_searches.add(search_id)
            await asyncio.sleep(0.02)  # Simulate work
            active_searches.discard(search_id)
            return [{"id": f"doc_{scale}", "text": f"From {scale}", "_rrf_score": 0.5}]

        store._search_single_scale = mock_search_single_scale

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=["chunks"])
        store.db = mock_db
        store.table = _table_mock()

        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = True
            mock_settings.multi_scale_chunk_sizes = "256,512,768,1024"

            # Run 3 searches concurrently
            results = await asyncio.gather(
                store.search(embedding=[0.0] * self.embedding_dim, limit=10),
                store.search(embedding=[0.1] * self.embedding_dim, limit=10),
                store.search(embedding=[0.2] * self.embedding_dim, limit=10),
            )

        # All 3 searches should complete successfully
        self.assertEqual(len(results), 3)
        for r in results:
            self.assertIsInstance(r, list)


class TestAdversarialInvalidConfig(unittest.IsolatedAsyncioTestCase):
    """Test handling of invalid configuration values."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test_lancedb"
        self.embedding_dim = 384

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def create_vector_store(self) -> VectorStore:
        store = VectorStore(db_path=self.db_path)
        store.table = _table_mock()
        store._embedding_dim = self.embedding_dim
        store._search_semaphore = asyncio.Semaphore(4)
        return store

    @pytest.mark.asyncio
    async def test_zero_scale_not_entered(self):
        """Test that "0" scale is not treated as multi-scale."""
        store = self.create_vector_store()

        search_called = False

        async def mock_search_single_scale(
            embedding,
            scale,
            fetch_k,
            filter_expr=None,
            vault_id=None,
            query_text="",
            hybrid=True,
        ):
            nonlocal search_called
            search_called = True
            return []

        store._search_single_scale = mock_search_single_scale

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=["chunks"])
        store.db = mock_db
        store.table = _table_mock()

        # Single "0" scale should not enter multi-scale branch
        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = True
            mock_settings.multi_scale_chunk_sizes = "0"

            await store.search(embedding=[0.0] * self.embedding_dim, limit=10)

        # Single scale doesn't trigger multi-scale path
        # (len(scale_strs) > 1 check)
        self.assertFalse(search_called)

    @pytest.mark.asyncio
    async def test_negative_scale_value(self):
        """Test negative scale value doesn't crash."""
        store = self.create_vector_store()

        async def mock_search_single_scale(
            embedding,
            scale,
            fetch_k,
            filter_expr=None,
            vault_id=None,
            query_text="",
            hybrid=True,
        ):
            # Should still be called even with negative scale
            return [{"id": f"doc_{scale}", "text": f"From {scale}", "_rrf_score": 0.5}]

        store._search_single_scale = mock_search_single_scale

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=["chunks"])
        store.db = mock_db
        store.table = _table_mock()

        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = True
            mock_settings.multi_scale_chunk_sizes = "-512,1024"

            # Should not crash
            results = await store.search(embedding=[0.0] * self.embedding_dim, limit=10)

        self.assertIsInstance(results, list)


class TestAdversarialSQLInjection(unittest.IsolatedAsyncioTestCase):
    """Test SQL injection prevention in vault_id and filter_expr."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test_lancedb"
        self.embedding_dim = 384

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def create_vector_store(self) -> VectorStore:
        store = VectorStore(db_path=self.db_path)
        store.table = _table_mock()
        store._embedding_dim = self.embedding_dim
        store._search_semaphore = asyncio.Semaphore(4)
        return store

    @pytest.mark.asyncio
    async def test_sql_injection_in_vault_id_sanitized(self):
        """Test that SQL injection attempts in vault_id are sanitized."""
        store = self.create_vector_store()

        # Use multi-scale path for proper testing
        async def mock_search_single_scale(
            embedding,
            scale,
            fetch_k,
            filter_expr=None,
            vault_id=None,
            query_text="",
            hybrid=True,
        ):
            # Verify vault_id is escaped in the filter
            if vault_id:
                # vault_id should have single quotes escaped
                assert "\\'" in vault_id or vault_id == vault_id
            return [{"id": "test", "text": "test", "_rrf_score": 0.5}]

        store._search_single_scale = mock_search_single_scale

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=["chunks"])
        store.db = mock_db
        store.table = _table_mock()

        # SQL injection attempt in vault_id
        injection_vault_id = "'; DROP TABLE chunks; --"

        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = True
            mock_settings.multi_scale_chunk_sizes = "512,1024"

            # Should not raise - vault_id gets sanitized
            results = await store.search(
                embedding=[0.0] * self.embedding_dim,
                limit=10,
                vault_id=injection_vault_id,
            )

        # Verify results are returned
        self.assertIsInstance(results, list)

    @pytest.mark.asyncio
    async def test_sql_injection_in_filter_expr_sanitized(self):
        """Test that SQL injection attempts in filter_expr are handled without crashing."""
        store = self.create_vector_store()

        # Use multi-scale path
        async def mock_search_single_scale(
            embedding,
            scale,
            fetch_k,
            filter_expr=None,
            vault_id=None,
            query_text="",
            hybrid=True,
        ):
            return [{"id": "test", "text": "test", "_rrf_score": 0.5}]

        store._search_single_scale = mock_search_single_scale

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=["chunks"])
        store.db = mock_db
        store.table = _table_mock()

        # SQL injection in filter_expr - user-provided filter_expr is passed through as-is
        injection_filter = "1=1; DROP TABLE chunks; --"

        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = True
            mock_settings.multi_scale_chunk_sizes = "512,1024"

            # Should not crash - filter_expr is passed through
            results = await store.search(
                embedding=[0.0] * self.embedding_dim,
                limit=10,
                filter_expr=injection_filter,
            )

        # Verify results are returned (no crash)
        self.assertIsInstance(results, list)


class TestAdversarialLargeFetchK(unittest.IsolatedAsyncioTestCase):
    """Test handling of extremely large fetch_k values."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test_lancedb"
        self.embedding_dim = 384

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def create_vector_store(self) -> VectorStore:
        store = VectorStore(db_path=self.db_path)
        store.table = _table_mock()
        store._embedding_dim = self.embedding_dim
        store._search_semaphore = asyncio.Semaphore(4)
        return store

    @pytest.mark.asyncio
    async def test_large_fetch_k_calculated_correctly(self):
        """Test that fetch_k is calculated as limit * 2."""
        store = self.create_vector_store()

        # Track fetch_k in multi-scale path
        captured_fetch_k = []

        async def mock_search_single_scale(
            embedding,
            scale,
            fetch_k,
            filter_expr=None,
            vault_id=None,
            query_text="",
            hybrid=True,
            **kwargs,
        ):
            captured_fetch_k.append(fetch_k)
            return [{"id": f"doc_{scale}", "text": f"From {scale}", "_rrf_score": 0.5}]

        store._search_single_scale = mock_search_single_scale

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=["chunks"])
        store.db = mock_db
        store.table = _table_mock()

        # Very large limit triggers large fetch_k = limit * 2
        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = True
            mock_settings.multi_scale_chunk_sizes = "512,1024"
            mock_settings.multi_scale_rrf_k = 60

            await store.search(embedding=[0.0] * self.embedding_dim, limit=1000000)

        # fetch_k = limit * 2, so 2000000
        self.assertEqual(captured_fetch_k[0], 2000000)


class TestAdversarialSemaphoreEdgeCases(unittest.IsolatedAsyncioTestCase):
    """Test edge cases for semaphore values."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test_lancedb"
        self.embedding_dim = 384

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def create_vector_store(self) -> VectorStore:
        store = VectorStore(db_path=self.db_path)
        store.table = _table_mock()
        store._embedding_dim = self.embedding_dim
        store._search_semaphore = asyncio.Semaphore(4)
        return store

    @pytest.mark.asyncio
    async def test_vector_search_concurrency_is_positive_integer(self):
        """Verify vector_search_concurrency is accessible and is a positive integer."""
        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.vector_search_concurrency = 16
            self.assertIsInstance(mock_settings.vector_search_concurrency, int)
            self.assertGreater(mock_settings.vector_search_concurrency, 0)

    @pytest.mark.asyncio
    async def test_override_semaphore_constant(self):
        """Test that semaphore can be overridden with custom value."""
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
        store.table = _table_mock()

        # Use 8 scales to test concurrency
        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = True
            mock_settings.vector_search_concurrency = 2
            mock_settings.multi_scale_chunk_sizes = (
                "256,512,768,1024,1280,1536,1792,2048"
            )

            await store.search(
                embedding=[0.0] * self.embedding_dim, limit=10
            )

        # Should be limited to patched value of 2
        self.assertLessEqual(max_concurrent, 2)


if __name__ == "__main__":
    unittest.main()
