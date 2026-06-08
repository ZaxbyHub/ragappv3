"""Unit tests for exact-match promotion in RAG engine."""

import os
import sys
import unittest
from typing import Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Stub missing optional dependencies
try:
    import lancedb
except ImportError:
    import types
    sys.modules['lancedb'] = types.ModuleType('lancedb')

try:
    import pyarrow
except ImportError:
    import types
    sys.modules['pyarrow'] = types.ModuleType('pyarrow')

try:
    from unstructured.partition.auto import partition
except ImportError:
    import types
    _unstructured = types.ModuleType('unstructured')
    _unstructured.partition = types.ModuleType('unstructured.partition')
    _unstructured.partition.auto = types.ModuleType('unstructured.partition.auto')
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    _unstructured.chunking = types.ModuleType('unstructured.chunking')
    _unstructured.chunking.title = types.ModuleType('unstructured.chunking.title')
    _unstructured.chunking.title.chunk_by_title = lambda *args, **kwargs: []
    _unstructured.documents = types.ModuleType('unstructured.documents')
    _unstructured.documents.elements = types.ModuleType('unstructured.documents.elements')
    _unstructured.documents.elements.Element = type('Element', (), {})
    sys.modules['unstructured'] = _unstructured
    sys.modules['unstructured.partition'] = _unstructured.partition
    sys.modules['unstructured.partition.auto'] = _unstructured.partition.auto
    sys.modules['unstructured.chunking'] = _unstructured.chunking
    sys.modules['unstructured.chunking.title'] = _unstructured.chunking.title
    sys.modules['unstructured.documents'] = _unstructured.documents
    sys.modules['unstructured.documents.elements'] = _unstructured.documents.elements

from app.services.llm_client import LLMClient
from app.services.memory_store import MemoryStore
from app.services.rag_engine import RAGEngine
from app.services.vector_store import VectorStore


class FakeEmbeddingService:
    """Fake embedding service that returns a fixed embedding."""

    def __init__(self, embedding: List[float] = None):
        self.embedding = embedding or [0.1] * 768

    async def embed_single(self, text: str) -> List[float]:
        return self.embedding

    async def embed_passage(self, text: str) -> List[float]:
        return self.embedding


def make_chunk(chunk_id: str, text: str = "Sample chunk text") -> Dict:
    """Helper to create a fake chunk result."""
    return {
        "id": chunk_id,
        "text": text,
        "file_id": f"file_{chunk_id}",
        "metadata": {"source_file": f"doc_{chunk_id}.md"},
        "_distance": 0.5,
    }


class TestExactMatchPromotion(unittest.IsolatedAsyncioTestCase):
    """Tests for exact-match promotion logic in _execute_retrieval."""

    def setUp(self):
        self._ssrf_embed = patch("app.services.embeddings.assert_url_safe")
        self._ssrf_llm = patch("app.services.llm_client.assert_url_safe")
        self._ssrf_embed.start()
        self._ssrf_llm.start()

    def tearDown(self):
        self._ssrf_embed.stop()
        self._ssrf_llm.stop()

    def _make_engine(self, vector_store: VectorStore) -> RAGEngine:
        """Create a RAGEngine with faked dependencies."""
        engine = RAGEngine()
        engine.embedding_service = FakeEmbeddingService()
        engine.vector_store = vector_store
        engine.memory_store = MagicMock(spec=MemoryStore)
        engine.memory_store.detect_memory_intent.return_value = None
        engine.memory_store.search_memories.return_value = []
        engine.llm_client = MagicMock(spec=LLMClient)
        engine.reranking_enabled = False
        engine.hybrid_search_enabled = False
        engine.retrieval_top_k = 10
        engine.initial_retrieval_top_k = 20
        return engine

    async def _call_execute_retrieval(
        self,
        engine: RAGEngine,
        query_embeddings: List[Tuple[str, List[float]]],
        user_input: str = "test query",
        vault_id: Optional[int] = None,
    ) -> Tuple:
        """Helper to call _execute_retrieval and return the full tuple."""
        return await engine._execute_retrieval(
            query_embeddings=query_embeddings,
            user_input=user_input,
            vault_id=vault_id,
        )

    @pytest.mark.asyncio
    async def test_promotion_when_top1_not_in_top5(self):
        """Original query's top-1 chunk is NOT in top-5 after fusion → promoted to position 4."""
        # Original query returns chunk "A" as top-1
        original_results = [
            make_chunk("A"),  # Top-1 from original
            make_chunk("B"),
            make_chunk("C"),
        ]
        # Variant query returns different chunks
        variant_results = [
            make_chunk("D"),
            make_chunk("E"),
            make_chunk("F"),
            make_chunk("G"),
            make_chunk("H"),
            make_chunk("I"),
        ]

        vector_store = MagicMock(spec=VectorStore)
        vector_store.search = AsyncMock(
            side_effect=[original_results, variant_results]
        )
        vector_store.get_fts_exceptions.return_value = 0

        engine = self._make_engine(vector_store)

        # Mock rrf_fuse to return fused list where A is NOT in top-5
        # After fusion: A is at position 6, top-5 are D,E,F,G,H
        with patch("app.services.rag_engine.rrf_fuse") as mock_fuse:
            mock_fuse.return_value = [
                make_chunk("D"),
                make_chunk("E"),
                make_chunk("F"),
                make_chunk("G"),
                make_chunk("H"),
                make_chunk("A"),  # A is at index 5 (position 6), NOT in top-5
                make_chunk("B"),
                make_chunk("C"),
            ]
            query_embeddings = [("original", [0.1] * 768), ("step_back", [0.2] * 768)]
            result = await self._call_execute_retrieval(engine, query_embeddings)

            vector_results, relevance_hint, eval_result, rerank_success, score_type, hybrid_status, fts_exceptions, rerank_status, variants_dropped, exact_match_promoted, token_pack_stats = result

            # Assert promotion happened
            assert exact_match_promoted is True, "exact_match_promoted should be True"
            # A should now be at position 4 (index 4, rank 5)
            assert vector_results[4]["id"] == "A", f"Expected A at position 4, got {vector_results[4]['id']}"
            # Top-5 should now be D, E, F, G, A
            top5_ids = [r["id"] for r in vector_results[:5]]
            assert "A" in top5_ids, f"A should be in top-5: {top5_ids}"

    @pytest.mark.asyncio
    async def test_no_promotion_when_top1_in_top5(self):
        """Original query's top-1 IS already in top-5 after fusion → no promotion."""
        original_results = [
            make_chunk("A"),  # Top-1 from original
            make_chunk("B"),
        ]
        variant_results = [
            make_chunk("C"),
            make_chunk("D"),
            make_chunk("E"),
        ]

        vector_store = MagicMock(spec=VectorStore)
        vector_store.search = AsyncMock(
            side_effect=[original_results, variant_results]
        )
        vector_store.get_fts_exceptions.return_value = 0

        engine = self._make_engine(vector_store)

        # Mock rrf_fuse to return fused list where A IS in top-5 already
        with patch("app.services.rag_engine.rrf_fuse") as mock_fuse:
            mock_fuse.return_value = [
                make_chunk("C"),
                make_chunk("A"),  # A is already in top-5 at position 2
                make_chunk("D"),
                make_chunk("E"),
                make_chunk("B"),
            ]
            query_embeddings = [("original", [0.1] * 768), ("step_back", [0.2] * 768)]
            result = await self._call_execute_retrieval(engine, query_embeddings)

            vector_results, _, _, _, _, _, _, _, _, exact_match_promoted, _ = result

            # Assert NO promotion happened
            assert exact_match_promoted is False, "exact_match_promoted should be False"

    @pytest.mark.asyncio
    async def test_no_promotion_when_feature_disabled(self):
        """When settings.exact_match_promote = False → no promotion."""
        original_results = [
            make_chunk("A"),
            make_chunk("B"),
        ]
        variant_results = [
            make_chunk("C"),
            make_chunk("D"),
            make_chunk("E"),
            make_chunk("F"),
            make_chunk("G"),
        ]

        vector_store = MagicMock(spec=VectorStore)
        vector_store.search = AsyncMock(
            side_effect=[original_results, variant_results]
        )
        vector_store.get_fts_exceptions.return_value = 0

        engine = self._make_engine(vector_store)

        # A is NOT in top-5, but feature is disabled
        with patch("app.services.rag_engine.rrf_fuse") as mock_fuse, \
             patch("app.services.rag_engine.settings") as mock_settings:
            mock_fuse.return_value = [
                make_chunk("C"),
                make_chunk("D"),
                make_chunk("E"),
                make_chunk("F"),
                make_chunk("G"),
                make_chunk("A"),  # A at position 6, not in top-5
            ]
            # Configure settings
            mock_settings.exact_match_promote = False
            mock_settings.rrf_legacy_mode = False
            mock_settings.multi_query_rrf_k = 60
            mock_settings.retrieval_recency_weight = 0.0

            query_embeddings = [("original", [0.1] * 768), ("step_back", [0.2] * 768)]
            result = await self._call_execute_retrieval(engine, query_embeddings)

            _, _, _, _, _, _, _, _, _, exact_match_promoted, _ = result

            assert exact_match_promoted is False, "exact_match_promoted should be False when feature disabled"

    @pytest.mark.asyncio
    async def test_no_promotion_in_legacy_mode(self):
        """When settings.rrf_legacy_mode = True → no promotion."""
        original_results = [
            make_chunk("A"),
            make_chunk("B"),
        ]
        variant_results = [
            make_chunk("C"),
            make_chunk("D"),
            make_chunk("E"),
            make_chunk("F"),
            make_chunk("G"),
        ]

        vector_store = MagicMock(spec=VectorStore)
        vector_store.search = AsyncMock(
            side_effect=[original_results, variant_results]
        )
        vector_store.get_fts_exceptions.return_value = 0

        engine = self._make_engine(vector_store)

        with patch("app.services.rag_engine.rrf_fuse") as mock_fuse, \
             patch("app.services.rag_engine.settings") as mock_settings:
            mock_fuse.return_value = [
                make_chunk("C"),
                make_chunk("D"),
                make_chunk("E"),
                make_chunk("F"),
                make_chunk("G"),
                make_chunk("A"),  # A at position 6
            ]
            mock_settings.exact_match_promote = True
            mock_settings.rrf_legacy_mode = True  # Legacy mode enabled
            mock_settings.multi_query_rrf_k = 60
            mock_settings.retrieval_recency_weight = 0.0

            query_embeddings = [("original", [0.1] * 768), ("step_back", [0.2] * 768)]
            result = await self._call_execute_retrieval(engine, query_embeddings)

            _, _, _, _, _, _, _, _, _, exact_match_promoted, _ = result

            assert exact_match_promoted is False, "exact_match_promoted should be False in legacy mode"

    @pytest.mark.asyncio
    async def test_no_promotion_single_variant(self):
        """Only one query variant (original only) → fusion skipped, no promotion."""
        original_results = [
            make_chunk("A"),
            make_chunk("B"),
            make_chunk("C"),
            make_chunk("D"),
            make_chunk("E"),
        ]

        vector_store = MagicMock(spec=VectorStore)
        vector_store.search = AsyncMock(return_value=original_results)
        vector_store.get_fts_exceptions.return_value = 0

        engine = self._make_engine(vector_store)

        # Only one variant - no fusion happens
        query_embeddings = [("original", [0.1] * 768)]
        result = await self._call_execute_retrieval(engine, query_embeddings)

        _, _, _, _, _, _, _, _, _, exact_match_promoted, _ = result

        assert exact_match_promoted is False, "exact_match_promoted should be False with single variant"

    @pytest.mark.asyncio
    async def test_no_promotion_fewer_than_5_results(self):
        """Fused results have fewer than 5 items → promotion skipped."""
        original_results = [
            make_chunk("A"),
            make_chunk("B"),
        ]
        variant_results = [
            make_chunk("C"),
        ]

        vector_store = MagicMock(spec=VectorStore)
        vector_store.search = AsyncMock(
            side_effect=[original_results, variant_results]
        )
        vector_store.get_fts_exceptions.return_value = 0

        engine = self._make_engine(vector_store)

        # Fused results only have 3 items (< 5)
        with patch("app.services.rag_engine.rrf_fuse") as mock_fuse:
            mock_fuse.return_value = [
                make_chunk("C"),
                make_chunk("A"),
                make_chunk("B"),
            ]
            query_embeddings = [("original", [0.1] * 768), ("step_back", [0.2] * 768)]
            result = await self._call_execute_retrieval(engine, query_embeddings)

            _, _, _, _, _, _, _, _, _, exact_match_promoted, _ = result

            assert exact_match_promoted is False, "exact_match_promoted should be False when < 5 results"

    @pytest.mark.asyncio
    async def test_retrieval_debug_includes_promoted_flag_enabled(self):
        """_build_done_message includes exact_match_promoted=True when feature enabled."""
        with patch("app.services.rag_engine.settings") as mock_settings:
            mock_settings.exact_match_promote = True
            mock_settings.vector_metric = "cosine"
            mock_settings.max_distance_threshold = 1.0
            mock_settings.retrieval_top_k = 10

            engine = RAGEngine()
            done_msg = engine._build_done_message(
                relevant_chunks=[],
                memories=[],
                score_type="distance",
                hybrid_status="disabled",
                fts_exceptions=0,
                rerank_status="disabled",
                variants_dropped=[],
                exact_match_promoted=True,
            )

            assert "retrieval_debug" in done_msg
            assert done_msg["retrieval_debug"]["exact_match_promoted"] is True

    @pytest.mark.asyncio
    async def test_retrieval_debug_includes_promoted_flag_disabled(self):
        """_build_done_message includes exact_match_promoted=None when feature disabled."""
        with patch("app.services.rag_engine.settings") as mock_settings:
            mock_settings.exact_match_promote = False
            mock_settings.vector_metric = "cosine"
            mock_settings.max_distance_threshold = 1.0
            mock_settings.retrieval_top_k = 10

            engine = RAGEngine()
            done_msg = engine._build_done_message(
                relevant_chunks=[],
                memories=[],
                score_type="distance",
                hybrid_status="disabled",
                fts_exceptions=0,
                rerank_status="disabled",
                variants_dropped=[],
                exact_match_promoted=True,  # Passed True but should be masked to None
            )

            assert "retrieval_debug" in done_msg
            assert done_msg["retrieval_debug"]["exact_match_promoted"] is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
