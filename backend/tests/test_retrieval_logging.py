"""Unit tests for retrieval logging in RAGEngine."""

import os
import sys
import unittest
from typing import Dict, List, cast
from unittest.mock import AsyncMock, MagicMock, patch

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies before importing
import types

# Stub lancedb properly with index submodule
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


import pytest

from app.services.embeddings import EmbeddingService
from app.services.rag_engine import RAGEngine
from app.services.vector_store import VectorStore


@pytest.fixture(autouse=True)
def _patch_ssrf():
    with patch("app.services.embeddings.assert_url_safe"), \
         patch("app.services.llm_client.assert_url_safe"):
        yield


class FakeEmbeddingService:
    """Fake embedding service for testing."""

    def __init__(self, embedding: List[float]):
        self.embedding = embedding

    async def embed_single(self, text: str) -> List[float]:
        return self.embedding


class RetrievalLoggingTests(unittest.IsolatedAsyncioTestCase):
    """Tests for retrieval logging in _execute_retrieval method."""

    def _create_vector_results(self, count: int) -> List[Dict]:
        """Create fake vector search results."""
        return [
            {
                "text": f"chunk {i}",
                "file_id": f"file{i}",
                "metadata": {"source_file": f"doc{i}.md"},
                "_distance": 0.1 * (i + 1),
            }
            for i in range(count)
        ]

    async def test_fusion_log_info_level(self):
        """Verify fusion log is INFO not DEBUG (check called with logger.info)."""
        # Create engine with mocked dependencies
        engine = RAGEngine()
        engine.embedding_service = cast(
            EmbeddingService, FakeEmbeddingService([0.1, 0.2])
        )

        # Create mock vector store that returns results for multiple query embeddings
        # search() is async, so use AsyncMock
        mock_vector_store = MagicMock()

        # First search returns 3 results, second search returns 2 results
        mock_vector_store.search = AsyncMock(
            side_effect=[
                self._create_vector_results(3),  # First query variant
                self._create_vector_results(2),  # Second query variant
            ]
        )
        mock_vector_store.is_connected = MagicMock(return_value=True)

        engine.vector_store = cast(VectorStore, mock_vector_store)

        # Use multiple query embeddings to trigger fusion
        query_embeddings = [
            [0.1] * 384,  # First embedding
            [0.2] * 384,  # Second embedding - triggers fusion path
        ]

        # Capture logs
        with self.assertLogs("app.services.rag_engine", level="INFO") as log:
            result, *_ = await engine._execute_retrieval(
                query_embeddings, "test query", vault_id=1
            )

        # Verify fusion log was emitted at INFO level
        fusion_log_found = False
        for log_record in log.output:
            if "Fused results from" in log_record and "query variants" in log_record:
                fusion_log_found = True
                # Verify it's at INFO level (not DEBUG)
                self.assertIn("INFO", log_record)

        self.assertTrue(fusion_log_found, "Fusion log should be emitted at INFO level")

    async def test_vector_search_log_info_level(self):
        """Verify vector search log is INFO."""
        # Create engine with mocked dependencies
        engine = RAGEngine()
        engine.embedding_service = cast(
            EmbeddingService, FakeEmbeddingService([0.1, 0.2])
        )

        # Create mock vector store - search() is async
        mock_vector_store = MagicMock()
        mock_vector_store.search = AsyncMock(
            return_value=self._create_vector_results(5)
        )
        mock_vector_store.is_connected = MagicMock(return_value=True)

        engine.vector_store = cast(VectorStore, mock_vector_store)

        # Use single query embedding (no fusion)
        query_embeddings = [[0.1] * 384]

        # Capture logs
        with self.assertLogs("app.services.rag_engine", level="INFO") as log:
            result, *_ = await engine._execute_retrieval(
                query_embeddings, "test query", vault_id=42
            )

        # Verify vector search log was emitted at INFO level
        vector_search_log_found = False
        for log_record in log.output:
            if "Vector search:" in log_record:
                vector_search_log_found = True
                # Verify it's at INFO level
                self.assertIn("INFO", log_record)
                # Verify vault_id is in the log
                self.assertIn("vault_id=42", log_record)
                # Verify results count is in the log
                self.assertIn("results=5", log_record)

        self.assertTrue(
            vector_search_log_found, "Vector search log should be emitted at INFO level"
        )

    async def test_token_packing_log_shows_counts(self):
        """When packing active, log shows before/after counts."""
        # Create engine with mocked dependencies
        engine = RAGEngine()
        engine.embedding_service = cast(
            EmbeddingService, FakeEmbeddingService([0.1, 0.2])
        )

        # Create mock vector store with results - search() is async
        mock_vector_store = MagicMock()
        mock_vector_store.search = AsyncMock(
            return_value=self._create_vector_results(10)
        )
        mock_vector_store.is_connected = MagicMock(return_value=True)

        engine.vector_store = cast(VectorStore, mock_vector_store)

        # Mock settings.context_max_tokens to enable packing
        with patch("app.services.rag_engine.settings") as mock_settings:
            mock_settings.context_max_tokens = 1000  # Enable token packing
            mock_settings.retrieval_evaluation_enabled = False
            mock_settings.reranking_enabled = False
            mock_settings.hybrid_search_enabled = False
            mock_settings.hybrid_alpha = 0.5
            mock_settings.initial_retrieval_top_k = 10
            mock_settings.reranker_top_n = 5
            mock_settings.retrieval_recency_weight = (
                0.0  # Disable recency for logging tests
            )

            # Use single query embedding
            query_embeddings = [[0.1] * 384]

            # Capture logs
            with self.assertLogs("app.services.rag_engine", level="INFO") as log:
                result, *_ = await engine._execute_retrieval(
                    query_embeddings, "test query", vault_id=1
                )

            # Verify token packing log shows before/after counts
            packing_log_found = False
            for log_record in log.output:
                if "Token packing:" in log_record and "→" in log_record:
                    packing_log_found = True
                    # Verify it shows results count transformation
                    self.assertIn("results", log_record.lower())
                    # Should show something like "10 results → X results"
                    self.assertIn("→", log_record)

            self.assertTrue(
                packing_log_found,
                "Token packing log should show before/after counts when packing is active",
            )

    async def test_token_packing_skip_log_when_disabled(self):
        """When context_max_tokens <= 0, no packing log fires (packing is silently skipped)."""
        # Create engine with mocked dependencies
        engine = RAGEngine()
        engine.embedding_service = cast(
            EmbeddingService, FakeEmbeddingService([0.1, 0.2])
        )

        # Create mock vector store with results - search() is async
        mock_vector_store = MagicMock()
        mock_vector_store.search = AsyncMock(
            return_value=self._create_vector_results(5)
        )
        mock_vector_store.is_connected = MagicMock(return_value=True)

        engine.vector_store = cast(VectorStore, mock_vector_store)

        # Mock settings.context_max_tokens to disable packing
        with patch("app.services.rag_engine.settings") as mock_settings:
            mock_settings.context_max_tokens = 0  # Disable token packing
            mock_settings.retrieval_evaluation_enabled = False
            mock_settings.reranking_enabled = False
            mock_settings.hybrid_search_enabled = False
            mock_settings.hybrid_alpha = 0.5
            mock_settings.initial_retrieval_top_k = 10
            mock_settings.reranker_top_n = 5
            mock_settings.retrieval_recency_weight = (
                0.0  # Disable recency for logging tests
            )

            # Use single query embedding
            query_embeddings = [[0.1] * 384]

            # Capture logs
            with self.assertLogs("app.services.rag_engine", level="INFO") as log:
                result, *_ = await engine._execute_retrieval(
                    query_embeddings, "test query", vault_id=1
                )

            # Verify NO token packing log was emitted when disabled
            packing_log_found = False
            for log_record in log.output:
                if "Token packing" in log_record:
                    packing_log_found = True

            self.assertFalse(
                packing_log_found,
                "Token packing log should NOT fire when context_max_tokens <= 0",
            )

    async def test_no_packing_log_when_no_results(self):
        """When vector_results empty, no packing log fires."""
        # Create engine with mocked dependencies
        engine = RAGEngine()
        engine.embedding_service = cast(
            EmbeddingService, FakeEmbeddingService([0.1, 0.2])
        )

        # Create mock vector store that returns NO results - search() is async
        mock_vector_store = MagicMock()
        mock_vector_store.search = AsyncMock(return_value=[])
        mock_vector_store.is_connected = MagicMock(return_value=True)

        engine.vector_store = cast(VectorStore, mock_vector_store)

        # Mock settings.context_max_tokens to enable packing (but no results)
        with patch("app.services.rag_engine.settings") as mock_settings:
            mock_settings.context_max_tokens = 1000  # Enable packing (but no results)
            mock_settings.retrieval_evaluation_enabled = False
            mock_settings.reranking_enabled = False
            mock_settings.hybrid_search_enabled = False
            mock_settings.hybrid_alpha = 0.5
            mock_settings.initial_retrieval_top_k = 10
            mock_settings.reranker_top_n = 5
            mock_settings.retrieval_recency_weight = (
                0.0  # Disable recency for logging tests
            )

            # Use single query embedding
            query_embeddings = [[0.1] * 384]

            # Capture logs
            with self.assertLogs("app.services.rag_engine", level="INFO") as log:
                result, *_ = await engine._execute_retrieval(
                    query_embeddings, "test query", vault_id=1
                )

            # Verify NO token packing log was emitted when there are no results
            packing_log_found = False
            for log_record in log.output:
                if "Token packing:" in log_record:
                    packing_log_found = True

            self.assertFalse(
                packing_log_found,
                "Token packing log should NOT fire when there are no results",
            )


if __name__ == "__main__":
    unittest.main()
