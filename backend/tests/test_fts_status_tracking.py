"""
Tests for FTS status tracking in VectorStore.

Verifies:
- FTS status tracked per arm as 'ok' | 'empty' | 'failed'
- Per-request fts_exceptions counter incremented on exception
- Results returned with _fts_status metadata
- get_fts_exceptions() returns and resets the counter
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.vector_store import VectorStore


def _make_fts_mock_builder(results, to_list_side_effect=None):
    """
    Create a mock search builder chain for FTS (query_type='fts').
    Simulates: table.search(text, query_type='fts').where(filter).limit(n).to_list()
    """
    mock_builder = MagicMock()
    mock_builder.where.return_value = mock_builder
    mock_builder.limit.return_value = mock_builder
    if to_list_side_effect is not None:
        mock_builder.to_list = to_list_side_effect
    else:
        mock_builder.to_list = AsyncMock(return_value=results)
    return mock_builder


def _make_dense_mock_builder(results):
    """Create a mock search builder chain for dense vector search."""
    mock_builder = MagicMock()
    mock_builder.where.return_value = mock_builder
    mock_builder.limit.return_value = mock_builder
    mock_builder.to_list = AsyncMock(return_value=results)
    return mock_builder


class TestFTSStatusTrackingSearch(unittest.IsolatedAsyncioTestCase):
    """Test FTS status tracking in the top-level search() method (single-scale path)."""

    def setUp(self):
        """Set up test fixtures."""
        self.store = VectorStore.__new__(VectorStore)
        # Mock db so search() doesn't try to call connect()
        self.store.db = MagicMock()
        self.store.db.table_names = AsyncMock(return_value=["chunks"])
        self.store.table = MagicMock()
        # _maybe_create_vector_index() calls list_indices() and count_rows()
        self.store.table.list_indices = AsyncMock(return_value=[])
        self.store.table.count_rows = AsyncMock(return_value=0)
        # search() calls table.schema() when _embedding_dim is set
        self.store.table.schema = AsyncMock(return_value=MagicMock())
        self.store._embedding_dim = 384
        self.store._fts_exceptions = 0

        # Default dense results (always return something so fusion has data)
        self.dense_results = [
            {"id": f"doc_{i}", "text": f"doc text {i}", "_distance": 0.1 * i}
            for i in range(3)
        ]

    # ── test_fts_status_ok ─────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_fts_status_ok(self):
        """FTS returns results → _fts_status = 'ok' on results."""
        fts_results = [
            {"id": "fts_doc_1", "text": "fts result 1"},
            {"id": "fts_doc_2", "text": "fts result 2"},
        ]

        dense_builder = _make_dense_mock_builder(self.dense_results)
        fts_builder = _make_fts_mock_builder(fts_results)

        async def search_side_effect(query, query_type=None, **kwargs):
            if query_type == "vector":
                return dense_builder
            elif query_type == "fts":
                return fts_builder
            return MagicMock()

        self.store.table.search = AsyncMock(side_effect=search_side_effect)

        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = False

            results = await self.store.search(
                embedding=[0.1] * 384,
                limit=5,
                query_text="test query",
                hybrid=True,
            )

        # Every result should carry _fts_status = 'ok'
        self.assertGreater(len(results), 0)
        for r in results:
            self.assertEqual(r.get("_fts_status"), "ok")

    # ── test_fts_status_empty ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_fts_status_empty(self):
        """FTS returns empty list → _fts_status = 'empty' on results."""
        fts_results = []

        dense_builder = _make_dense_mock_builder(self.dense_results)
        fts_builder = _make_fts_mock_builder(fts_results)

        async def search_side_effect(query, query_type=None, **kwargs):
            if query_type == "vector":
                return dense_builder
            elif query_type == "fts":
                return fts_builder
            return MagicMock()

        self.store.table.search = AsyncMock(side_effect=search_side_effect)

        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = False

            results = await self.store.search(
                embedding=[0.1] * 384,
                limit=5,
                query_text="test query",
                hybrid=True,
            )

        # Dense results still present (fallback), but all carry _fts_status = 'empty'
        self.assertGreater(len(results), 0)
        for r in results:
            self.assertEqual(r.get("_fts_status"), "empty")

    # ── test_fts_status_failed ─────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_fts_status_failed(self):
        """FTS raises exception → _fts_status = 'failed' AND _fts_exceptions incremented."""
        dense_builder = _make_dense_mock_builder(self.dense_results)
        fts_builder = MagicMock()
        fts_builder.where.return_value = fts_builder
        fts_builder.limit.return_value = fts_builder
        fts_builder.to_list = AsyncMock(side_effect=RuntimeError("FTS index corrupted"))

        async def search_side_effect(query, query_type=None, **kwargs):
            if query_type == "vector":
                return dense_builder
            elif query_type == "fts":
                return fts_builder
            return MagicMock()

        self.store.table.search = AsyncMock(side_effect=search_side_effect)

        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = False

            results = await self.store.search(
                embedding=[0.1] * 384,
                limit=5,
                query_text="test query",
                hybrid=True,
            )

        # Results should still be returned (dense fallback)
        self.assertGreater(len(results), 0)
        for r in results:
            self.assertEqual(r.get("_fts_status"), "failed")

        # _fts_exceptions counter must be incremented
        self.assertEqual(self.store._fts_exceptions, 1)

    # ── test_get_fts_exceptions_resets ────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_get_fts_exceptions_resets(self):
        """get_fts_exceptions() returns current value and resets to 0."""
        # Pre-load exceptions via direct mutation
        self.store._fts_exceptions = 5

        first = self.store.get_fts_exceptions()
        self.assertEqual(first, 5)

        # Counter should now be reset
        second = self.store.get_fts_exceptions()
        self.assertEqual(second, 0)

    # ── test_fts_exceptions_not_incremented_on_empty ───────────────────────────

    @pytest.mark.asyncio
    async def test_fts_exceptions_not_incremented_on_empty(self):
        """FTS returns empty list (not exception) → _fts_exceptions NOT incremented."""
        fts_results = []

        dense_builder = _make_dense_mock_builder(self.dense_results)
        fts_builder = _make_fts_mock_builder(fts_results)

        async def search_side_effect(query, query_type=None, **kwargs):
            if query_type == "vector":
                return dense_builder
            elif query_type == "fts":
                return fts_builder
            return MagicMock()

        self.store.table.search = AsyncMock(side_effect=search_side_effect)

        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = False

            await self.store.search(
                embedding=[0.1] * 384,
                limit=5,
                query_text="test query",
                hybrid=True,
            )

        # Empty results are NOT exceptions — counter must stay at 0
        self.assertEqual(self.store._fts_exceptions, 0)


class TestFTSStatusTrackingSingleScale(unittest.IsolatedAsyncioTestCase):
    """Test FTS status tracking in _search_single_scale() (multi-scale path)."""

    def setUp(self):
        """Set up test fixtures."""
        self.store = VectorStore.__new__(VectorStore)
        self.store.db = None
        self.store.table = MagicMock()
        self.store._embedding_dim = 384
        self.store._fts_exceptions = 0

        self.dense_results = [
            {"id": f"doc_{i}", "text": f"doc text {i}", "_distance": 0.1 * i}
            for i in range(3)
        ]

    # ── test_fts_status_ok (single-scale) ─────────────────────────────────────

    @pytest.mark.asyncio
    async def test_fts_status_ok_single_scale(self):
        """FTS returns results → _fts_status = 'ok' on results from _search_single_scale."""
        fts_results = [{"id": "fts_1", "text": "fts result"}]

        dense_builder = _make_dense_mock_builder(self.dense_results)
        fts_builder = _make_fts_mock_builder(fts_results)

        async def search_side_effect(query, query_type=None, **kwargs):
            if query_type == "vector":
                return dense_builder
            elif query_type == "fts":
                return fts_builder
            return MagicMock()

        self.store.table.search = AsyncMock(side_effect=search_side_effect)

        with patch("app.services.vector_store.settings"):
            results = await self.store._search_single_scale(
                embedding=[0.1] * 384,
                scale="default",
                fetch_k=10,
                query_text="test query",
                hybrid=True,
            )

        self.assertGreater(len(results), 0)
        for r in results:
            self.assertEqual(r.get("_fts_status"), "ok")

    # ── test_fts_status_empty (single-scale) ───────────────────────────────────

    @pytest.mark.asyncio
    async def test_fts_status_empty_single_scale(self):
        """FTS returns empty list → _fts_status = 'empty' from _search_single_scale."""
        dense_builder = _make_dense_mock_builder(self.dense_results)
        fts_builder = _make_fts_mock_builder([])

        async def search_side_effect(query, query_type=None, **kwargs):
            if query_type == "vector":
                return dense_builder
            elif query_type == "fts":
                return fts_builder
            return MagicMock()

        self.store.table.search = AsyncMock(side_effect=search_side_effect)

        with patch("app.services.vector_store.settings"):
            results = await self.store._search_single_scale(
                embedding=[0.1] * 384,
                scale="default",
                fetch_k=10,
                query_text="test query",
                hybrid=True,
            )

        self.assertGreater(len(results), 0)
        for r in results:
            self.assertEqual(r.get("_fts_status"), "empty")

    # ── test_fts_status_failed (single-scale) ──────────────────────────────────

    @pytest.mark.asyncio
    async def test_fts_status_failed_single_scale(self):
        """FTS raises exception → _fts_status = 'failed' AND _fts_exceptions incremented."""
        dense_builder = _make_dense_mock_builder(self.dense_results)
        fts_builder = MagicMock()
        fts_builder.where.return_value = fts_builder
        fts_builder.limit.return_value = fts_builder
        fts_builder.to_list = AsyncMock(
            side_effect=RuntimeError("FTS index missing")
        )

        async def search_side_effect(query, query_type=None, **kwargs):
            if query_type == "vector":
                return dense_builder
            elif query_type == "fts":
                return fts_builder
            return MagicMock()

        self.store.table.search = AsyncMock(side_effect=search_side_effect)

        with patch("app.services.vector_store.settings"):
            results = await self.store._search_single_scale(
                embedding=[0.1] * 384,
                scale="default",
                fetch_k=10,
                query_text="test query",
                hybrid=True,
            )

        # Dense results still returned (graceful fallback)
        self.assertGreater(len(results), 0)
        for r in results:
            self.assertEqual(r.get("_fts_status"), "failed")

        self.assertEqual(self.store._fts_exceptions, 1)

    # ── test_fts_exceptions_not_incremented_on_empty (single-scale) ────────────

    @pytest.mark.asyncio
    async def test_dense_failure_degrades_to_fts_single_scale(self):
        """If the dense (vector) arm raises, the concurrent dense+FTS gather
        degrades to FTS-only results instead of aborting the whole hybrid
        search (gather uses return_exceptions=True)."""
        fts_results = [{"id": "fts_1", "text": "fts result"}]

        # Dense builder whose .to_list() raises (transient LanceDB failure).
        dense_builder = MagicMock()
        dense_builder.where.return_value = dense_builder
        dense_builder.limit.return_value = dense_builder
        dense_builder.to_list = AsyncMock(side_effect=RuntimeError("dense boom"))

        fts_builder = _make_fts_mock_builder(fts_results)

        async def search_side_effect(query, query_type=None, **kwargs):
            if query_type == "vector":
                return dense_builder
            elif query_type == "fts":
                return fts_builder
            return MagicMock()

        self.store.table.search = AsyncMock(side_effect=search_side_effect)

        with patch("app.services.vector_store.settings"):
            results = await self.store._search_single_scale(
                embedding=[0.1] * 384,
                scale="default",
                fetch_k=10,
                query_text="test query",
                hybrid=True,
            )

        # FTS results survived the dense failure rather than the search crashing.
        self.assertGreater(len(results), 0)
        self.assertIn("fts_1", [r.get("id") for r in results])

    @pytest.mark.asyncio
    async def test_fts_exceptions_not_incremented_on_empty_single_scale(self):
        """FTS empty (not exception) → _fts_exceptions stays 0 in _search_single_scale."""
        dense_builder = _make_dense_mock_builder(self.dense_results)
        fts_builder = _make_fts_mock_builder([])

        async def search_side_effect(query, query_type=None, **kwargs):
            if query_type == "vector":
                return dense_builder
            elif query_type == "fts":
                return fts_builder
            return MagicMock()

        self.store.table.search = AsyncMock(side_effect=search_side_effect)

        with patch("app.services.vector_store.settings"):
            await self.store._search_single_scale(
                embedding=[0.1] * 384,
                scale="default",
                fetch_k=10,
                query_text="test query",
                hybrid=True,
            )

        self.assertEqual(self.store._fts_exceptions, 0)


if __name__ == "__main__":
    unittest.main()
