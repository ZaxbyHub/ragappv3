"""Unit tests for hybrid search logging in VectorStore.

Verifies that hybrid search INFO logs fire ONLY when the respective
search arm (BM25 FTS or sparse) actually returned results.
"""

import logging
import os
import sys
import unittest
from typing import Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies before importing app modules
import types

# Stub lancedb with index submodule
_lancedb = types.ModuleType("lancedb")
_lancedb_index = types.ModuleType("lancedb.index")
_lancedb_index.IvfPq = type("IvfPq", (), {})
_lancedb_index.FTS = type("FTS", (), {})
_lancedb.index = _lancedb_index
sys.modules["lancedb"] = _lancedb
sys.modules["lancedb.index"] = _lancedb_index

# Stub unstructured
_unstructured = types.ModuleType("unstructured")
_unstructured.partition = types.ModuleType("unstructured.partition")
_unstructured.partition.auto = types.ModuleType("unstructured.partition.auto")
_unstructured.partition.auto.partition = lambda *args, **kwargs: []
_unstructured.chunking = types.ModuleType("unstructured.chunking")
_unstructured.chunking.title = types.ModuleType("unstructured.chunking.title")
_unstructured.chunking.title.chunk_by_title = lambda *args, **kwargs: []
_unstructured.documents = types.ModuleType("unstructured.documents")
_unstructured.documents.elements = types.ModuleType("unstructured.documents.elements")
_unstructured.documents.elements.Element = type("Element", (), {})
sys.modules["unstructured"] = _unstructured
sys.modules["unstructured.partition"] = _unstructured.partition
sys.modules["unstructured.partition.auto"] = _unstructured.partition.auto
sys.modules["unstructured.chunking"] = _unstructured.chunking
sys.modules["unstructured.chunking.title"] = _unstructured.chunking.title
sys.modules["unstructured.documents"] = _unstructured.documents
sys.modules["unstructured.documents.elements"] = _unstructured.documents.elements

from app.services.vector_store import VectorStore


class HybridLoggingTests(unittest.IsolatedAsyncioTestCase):
    """Tests for hybrid search logging in VectorStore.search and _search_single_scale."""

    def _dense_results(self, count: int) -> List[Dict]:
        """Create fake dense vector search results."""
        return [
            {
                "id": f"chunk_{i}",
                "text": f"chunk text {i}",
                "file_id": f"file_{i}",
                "metadata": "{}",
                "_distance": 0.1 * (i + 1),
            }
            for i in range(count)
        ]

    def _fts_results(self, count: int) -> List[Dict]:
        """Create fake FTS search results."""
        return [
            {
                "id": f"fts_chunk_{i}",
                "text": f"fts text {i}",
                "file_id": f"fts_file_{i}",
                "metadata": "{}",
                "_distance": 0.05 * (i + 1),
            }
            for i in range(count)
        ]

    def _sparse_results(self, count: int) -> List[Dict]:
        """Create fake sparse search results."""
        return [
            {
                "id": f"sparse_chunk_{i}",
                "text": f"sparse text {i}",
                "file_id": f"sparse_file_{i}",
                "metadata": "{}",
                "sparse_embedding": '{"token_a": 0.5, "token_b": 0.3}',
                "_sparse_score": 0.8 - (i * 0.1),
            }
            for i in range(count)
        ]

    def _make_vs(self) -> VectorStore:
        """Create a VectorStore with pre-set db/table mocks."""
        vs = VectorStore()
        vs.db = MagicMock()
        vs.table = MagicMock()
        vs._embedding_dim = 768
        return vs

    def _mock_search_side_effect(self, dense_count=3, fts_count=2):
        """Build a search() side_effect that returns vector + fts results."""
        async def search_side_effect(query_val, query_type=None):
            if query_type == "vector":
                m = MagicMock()
                m.where = MagicMock(return_value=m)
                m.limit = MagicMock(return_value=m)
                m.to_list = AsyncMock(return_value=self._dense_results(dense_count))
                return m
            elif query_type == "fts":
                m = MagicMock()
                m.where = MagicMock(return_value=m)
                m.limit = MagicMock(return_value=m)
                m.to_list = AsyncMock(return_value=self._fts_results(fts_count))
                return m
            return MagicMock()
        return search_side_effect

    # -------------------------------------------------------------------------
    # BM25 FTS logging (single-scale path in search())
    # -------------------------------------------------------------------------

    async def test_bm25_fts_succeeds_logs_info(self):
        """BM25 FTS returns results -> INFO log fires."""
        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = False
            mock_settings.sparse_search_max_candidates = 1000
            mock_settings.vector_search_concurrency = 4

            vs = self._make_vs()
            vs.table.search = MagicMock(side_effect=self._mock_search_side_effect())
            vs.table.list_indices = AsyncMock(return_value=[])
            vs.table.count_rows = AsyncMock(return_value=100)

            with self.assertLogs("app.services.vector_store", level="INFO") as log:
                results = await vs.search(
                    embedding=[0.1] * 768,
                    query_text="test query",
                    hybrid=True,
                    hybrid_alpha=0.5,
                )

            self.assertGreater(len(results), 0)
            bm25_log_found = any(
                "Hybrid search (BM25 FTS) succeeded" in record
                for record in log.output
            )
            self.assertTrue(
                bm25_log_found, f"Expected BM25 FTS success log. Got: {log.output}"
            )


    async def test_bm25_fts_throws_exception_no_success_log(self):
        """BM25 FTS throws exception -> NO 'succeeded' log (WARNING fires instead)."""
        async def search_side_effect(query_val, query_type=None):
            if query_type == "vector":
                m = MagicMock()
                m.where = MagicMock(return_value=m)
                m.limit = MagicMock(return_value=m)
                m.to_list = AsyncMock(return_value=self._dense_results(3))
                return m
            elif query_type == "fts":
                raise RuntimeError("FTS index corrupted")
            return MagicMock()

        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = False
            mock_settings.sparse_search_max_candidates = 1000
            mock_settings.vector_search_concurrency = 4

            vs = self._make_vs()
            vs.table.search = MagicMock(side_effect=search_side_effect)
            vs.table.list_indices = AsyncMock(return_value=[])
            vs.table.count_rows = AsyncMock(return_value=100)

            with self.assertLogs("app.services.vector_store", level="INFO") as log:
                await vs.search(
                    embedding=[0.1] * 768,
                    query_text="test query",
                    hybrid=True,
                    hybrid_alpha=0.5,
                )

            bm25_success_log_found = any(
                "Hybrid search (BM25 FTS) succeeded" in record
                for record in log.output
            )
            self.assertFalse(
                bm25_success_log_found,
                f"Did NOT expect 'succeeded' log when FTS throws. Got: {log.output}",
            )
            # A WARNING should fire about the failure
            fts_warning_found = any(
                "FTS search failed" in record for record in log.output
            )
            self.assertTrue(
                fts_warning_found,
                f"Expected FTS failure WARNING log. Got: {log.output}",
            )

    # -------------------------------------------------------------------------
    # Dense-only path logging (single-scale path in search())
    # -------------------------------------------------------------------------

    async def test_dense_only_logs_debug(self):
        """hybrid=False -> DEBUG log fires for dense-only search."""
        async def search_side_effect(query_val, query_type=None):
            if query_type == "vector":
                m = MagicMock()
                m.where = MagicMock(return_value=m)
                m.limit = MagicMock(return_value=m)
                m.to_list = AsyncMock(return_value=self._dense_results(3))
                return m
            return MagicMock()

        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = False
            mock_settings.sparse_search_max_candidates = 1000
            mock_settings.vector_search_concurrency = 4

            vs = self._make_vs()
            vs.table.search = MagicMock(side_effect=search_side_effect)
            vs.table.list_indices = AsyncMock(return_value=[])
            vs.table.count_rows = AsyncMock(return_value=100)

            with self.assertLogs("app.services.vector_store", level="DEBUG") as log:
                results = await vs.search(
                    embedding=[0.1] * 768,
                    query_text="test query",
                    hybrid=False,  # hybrid disabled
                    hybrid_alpha=0.5,
                )

            self.assertGreater(len(results), 0)
            dense_only_log_found = any(
                "Dense-only search" in record
                for record in log.output
            )
            self.assertTrue(
                dense_only_log_found,
                f"Expected dense-only DEBUG log. Got: {log.output}",
            )

    async def test_dense_only_no_query_text(self):
        """hybrid=True but query_text='' -> DEBUG dense-only log fires."""
        async def search_side_effect(query_val, query_type=None):
            if query_type == "vector":
                m = MagicMock()
                m.where = MagicMock(return_value=m)
                m.limit = MagicMock(return_value=m)
                m.to_list = AsyncMock(return_value=self._dense_results(3))
                return m
            return MagicMock()

        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = False
            mock_settings.sparse_search_max_candidates = 1000
            mock_settings.vector_search_concurrency = 4

            vs = self._make_vs()
            vs.table.search = MagicMock(side_effect=search_side_effect)
            vs.table.list_indices = AsyncMock(return_value=[])
            vs.table.count_rows = AsyncMock(return_value=100)

            with self.assertLogs("app.services.vector_store", level="DEBUG") as log:
                results = await vs.search(
                    embedding=[0.1] * 768,
                    query_text="",  # No query text
                    hybrid=True,
                    hybrid_alpha=0.5,
                )

            self.assertGreater(len(results), 0)
            dense_only_log_found = any(
                "Dense-only search" in record
                for record in log.output
            )
            self.assertTrue(
                dense_only_log_found,
                f"Expected dense-only DEBUG log when query_text is empty. Got: {log.output}",
            )

    # -------------------------------------------------------------------------
    # BM25 FTS logging in _search_single_scale (multi-scale path)
    # -------------------------------------------------------------------------

    async def test_search_single_scale_bm25_succeeds_logs_info(self):
        """_search_single_scale: BM25 FTS returns results -> INFO log fires."""
        async def search_side_effect(query_val, query_type=None):
            if query_type == "vector":
                m = MagicMock()
                m.where = MagicMock(return_value=m)
                m.limit = MagicMock(return_value=m)
                m.to_list = AsyncMock(return_value=self._dense_results(2))
                return m
            elif query_type == "fts":
                m = MagicMock()
                m.where = MagicMock(return_value=m)
                m.limit = MagicMock(return_value=m)
                m.to_list = AsyncMock(return_value=self._fts_results(3))
                return m
            return MagicMock()

        vs = self._make_vs()
        vs.table.search = MagicMock(side_effect=search_side_effect)

        with self.assertLogs("app.services.vector_store", level="INFO") as log:
            results = await vs._search_single_scale(
                embedding=[0.1] * 768,
                scale="default",
                fetch_k=10,
                query_text="test query",
                hybrid=True,
                hybrid_alpha=0.5,
            )

        self.assertGreater(len(results), 0)
        bm25_log_found = any(
            "Hybrid search (BM25 FTS) succeeded" in record
            for record in log.output
        )
        self.assertTrue(
            bm25_log_found,
            f"Expected BM25 FTS success log in _search_single_scale. Got: {log.output}",
        )




    async def test_search_single_scale_bm25_exception_no_success_log(self):
        """_search_single_scale: BM25 FTS throws -> NO 'succeeded' log."""
        async def search_side_effect(query_val, query_type=None):
            if query_type == "vector":
                m = MagicMock()
                m.where = MagicMock(return_value=m)
                m.limit = MagicMock(return_value=m)
                m.to_list = AsyncMock(return_value=self._dense_results(2))
                return m
            elif query_type == "fts":
                raise RuntimeError("FTS search error")
            return MagicMock()

        vs = self._make_vs()
        vs.table.search = MagicMock(side_effect=search_side_effect)

        with self.assertLogs("app.services.vector_store", level="INFO") as log:
            await vs._search_single_scale(
                embedding=[0.1] * 768,
                scale="default",
                fetch_k=10,
                query_text="test query",
                hybrid=True,
                hybrid_alpha=0.5,
            )

        bm25_success_log_found = any(
            "Hybrid search (BM25 FTS) succeeded" in record
            for record in log.output
        )
        self.assertFalse(
            bm25_success_log_found,
            f"Did NOT expect BM25 success log when FTS throws. Got: {log.output}",
        )
        fts_warning_found = any(
            "FTS search failed" in record for record in log.output
        )
        self.assertTrue(
            fts_warning_found,
            f"Expected FTS failure WARNING. Got: {log.output}",
        )


if __name__ == "__main__":
    unittest.main()


# -------------------------------------------------------------------------
# Standalone pytest tests using caplog fixture (pytest handles injection)
# These test the "negative" assertions where NO log should fire.
# -------------------------------------------------------------------------

def _standalone_dense_results(count: int) -> List[Dict]:
    """Standalone version of _dense_results."""
    return [
        {
            "id": f"chunk_{i}",
            "text": f"chunk text {i}",
            "file_id": f"file_{i}",
            "metadata": "{}",
            "_distance": 0.1 * (i + 1),
        }
        for i in range(count)
    ]


def _standalone_fts_results(count: int) -> List[Dict]:
    """Standalone version of _fts_results."""
    return [
        {
            "id": f"fts_chunk_{i}",
            "text": f"fts text {i}",
            "file_id": f"fts_file_{i}",
            "metadata": "{}",
            "_distance": 0.05 * (i + 1),
        }
        for i in range(count)
    ]


def _standalone_sparse_results(count: int) -> List[Dict]:
    """Standalone version of _sparse_results."""
    return [
        {
            "id": f"sparse_chunk_{i}",
            "text": f"sparse text {i}",
            "file_id": f"sparse_file_{i}",
            "metadata": "{}",
            "sparse_embedding": '{"token_a": 0.5, "token_b": 0.3}',
            "_sparse_score": 0.8 - (i * 0.1),
        }
        for i in range(count)
    ]


def _standalone_make_vs() -> VectorStore:
    """Standalone version of _make_vs."""
    vs = VectorStore()
    vs.db = MagicMock()
    vs.table = MagicMock()
    vs._embedding_dim = 768
    return vs


@pytest.mark.asyncio
async def test_bm25_fts_returns_empty_no_success_log_pytest(caplog):
    """BM25 FTS returns empty list -> NO 'succeeded' log (pytest-native)."""
    async def search_side_effect(query_val, query_type=None):
        if query_type == "vector":
            m = MagicMock()
            m.where = MagicMock(return_value=m)
            m.limit = MagicMock(return_value=m)
            m.to_list = AsyncMock(return_value=_standalone_dense_results(3))
            return m
        elif query_type == "fts":
            m = MagicMock()
            m.where = MagicMock(return_value=m)
            m.limit = MagicMock(return_value=m)
            m.to_list = AsyncMock(return_value=[])  # Empty FTS
            return m
        return MagicMock()

    with patch("app.services.vector_store.settings") as mock_settings:
        mock_settings.multi_scale_indexing_enabled = False
        mock_settings.sparse_search_max_candidates = 1000
        mock_settings.vector_search_concurrency = 4

        vs = _standalone_make_vs()
        vs.table.search = MagicMock(side_effect=search_side_effect)
        vs.table.list_indices = AsyncMock(return_value=[])
        vs.table.count_rows = AsyncMock(return_value=100)

        with caplog.at_level(logging.INFO, "app.services.vector_store"):
            await vs.search(
                embedding=[0.1] * 768,
                query_text="test query",
                hybrid=True,
                hybrid_alpha=0.5,
            )

        bm25_success_log_found = any(
            "Hybrid search (BM25 FTS) succeeded" in record
            for record in caplog.text.splitlines()
        )
        assert not bm25_success_log_found, (
            f"Did NOT expect 'succeeded' log when FTS returns empty. Got: {caplog.text}"
        )


@pytest.mark.asyncio
async def test_search_single_scale_bm25_empty_no_success_log_pytest(caplog):
    """_search_single_scale: BM25 FTS returns empty -> NO 'succeeded' log (pytest-native)."""
    async def search_side_effect(query_val, query_type=None):
        if query_type == "vector":
            m = MagicMock()
            m.where = MagicMock(return_value=m)
            m.limit = MagicMock(return_value=m)
            m.to_list = AsyncMock(return_value=_standalone_dense_results(2))
            return m
        elif query_type == "fts":
            m = MagicMock()
            m.where = MagicMock(return_value=m)
            m.limit = MagicMock(return_value=m)
            m.to_list = AsyncMock(return_value=[])  # Empty
            return m
        return MagicMock()

    vs = _standalone_make_vs()
    vs.table.search = MagicMock(side_effect=search_side_effect)

    with caplog.at_level(logging.INFO, "app.services.vector_store"):
        await vs._search_single_scale(
            embedding=[0.1] * 768,
            scale="default",
            fetch_k=10,
            query_text="test query",
            hybrid=True,
            hybrid_alpha=0.5,
        )

    bm25_success_log_found = any(
        "Hybrid search (BM25 FTS) succeeded" in record
        for record in caplog.text.splitlines()
    )
    assert not bm25_success_log_found, (
        f"Did NOT expect BM25 success log when empty. Got: {caplog.text}"
    )

