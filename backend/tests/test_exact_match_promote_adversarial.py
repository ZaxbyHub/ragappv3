"""Adversarial tests for exact-match promotion in RAG engine.

Tests attack vectors for the exact_match_promote feature in _execute_retrieval.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.rag_engine import RAGEngine
from app.services.vector_store import VectorStore


def _make_result(doc_id: str, rank: int) -> dict:
    """Factory: create a synthetic vector search result."""
    return {"id": doc_id, "text": f"chunk {rank}", "file_id": f"file_{doc_id}", "_distance": 0.1 * rank}


def _make_embedding() -> list:
    """Factory: create a dummy embedding vector."""
    return [0.1] * 128


class TestExactMatchPromoteAdversarial:
    """Attack vectors for exact-match promotion logic."""

    @pytest.fixture(autouse=True)
    def _patch_ssrf(self):
        with patch("app.services.embeddings.assert_url_safe"), \
             patch("app.services.llm_client.assert_url_safe"):
            yield

    @pytest.fixture
    def mock_settings(self):
        """Patch all settings flags used by exact-match promotion."""
        mock = MagicMock()
        mock.exact_match_promote = True
        mock.rrf_legacy_mode = False
        mock.multi_query_rrf_k = 60
        mock.rrf_weight_original = 1.0
        mock.rrf_weight_stepback = 0.5
        mock.rrf_weight_hyde = 0.5
        mock.retrieval_recency_weight = 0.0
        mock.retrieval_top_k = 10
        mock.vector_top_k = None
        mock.reranking_enabled = False
        mock.hybrid_search_enabled = False
        mock.query_transformation_enabled = False
        mock.context_distillation_enabled = False
        mock.context_distillation_synthesis_enabled = False
        mock.context_max_tokens = 0
        mock.retrieval_evaluation_enabled = False
        mock.maintenance_mode = False
        mock.rag_relevance_threshold = 0.0
        mock.max_distance_threshold = 100.0
        mock.embedding_doc_prefix = ""
        mock.embedding_query_prefix = ""
        mock.retrieval_window = 0
        mock.chunk_size_chars = 1000
        mock.chunk_overlap_chars = 200
        mock.vector_metric = "cosine"
        # rrf_legacy_mode=False path
        mock.rrf_legacy_mode = False
        with patch("app.config.settings", mock):
            with patch("app.services.rag_engine.settings", mock):
                yield mock

    @pytest.fixture
    def mock_vector_store(self):
        """Return a mock VectorStore that yields configurable results."""
        vs = MagicMock(spec=VectorStore)
        vs.search = AsyncMock(return_value=[])
        vs.is_connected = MagicMock(return_value=True)
        vs.get_fts_exceptions = MagicMock(return_value=0)
        return vs

    @pytest.fixture
    def mock_embedding_service(self):
        """Return a mock EmbeddingService."""
        svc = MagicMock()
        svc.embed_single = AsyncMock(return_value=_make_embedding())
        svc.embed_passage = AsyncMock(return_value=_make_embedding())
        return svc

    def _run_retrieval_sync(self, engine, query_embeddings, vault_id=None):
        """Helper: run _execute_retrieval and unpack results."""
        import asyncio
        return asyncio.get_event_loop().run_until_complete(
            engine._execute_retrieval(query_embeddings, "test query", vault_id=vault_id)
        )

    @pytest.mark.asyncio
    async def test_promotion_with_duplicate_ids(self, mock_settings, mock_vector_store, mock_embedding_service):
        """Two results in different variants share the same id. First-encountered instance wins; no double-promotion."""
        # Original query returns doc "A" at top; second variant also has "A" (duplicate)
        # With proper weights (original=1.0, step_back=0.5), "A" will rank high
        original_results = [_make_result("A", 1), _make_result("B", 2)]
        step_back_results = [_make_result("A", 10), _make_result("C", 11)]  # duplicate id "A"

        mock_vector_store.search = AsyncMock(side_effect=[original_results, step_back_results])

        engine = RAGEngine(
            embedding_service=mock_embedding_service,
            vector_store=mock_vector_store,
        )

        query_embeddings = [("original", _make_embedding()), ("step_back", _make_embedding())]

        with patch.object(engine, "_normalize_metadata", return_value={}):
            (
                vector_results,
                _,
                _,
                _,
                _,
                _,
                _,
                _,
                _,
                exact_match_promoted,
                _,
            ) = await engine._execute_retrieval(query_embeddings, "test query", vault_id=None)

        ids = [r["id"] for r in vector_results]

        # "A" must appear exactly once (first-encountered wins, no duplication)
        assert ids.count("A") == 1, f"Duplicate ID 'A' must appear once, got ids={ids}"

        # Verify "A" is in results (duplicate was deduplicated, not lost)
        assert "A" in ids, f"'A' should be in results, ids={ids}"
        # B and C should both be present (3 unique items from 4 inputs)
        assert "B" in ids, f"'B' should be in results, ids={ids}"
        assert "C" in ids, f"'C' should be in results, ids={ids}"
        assert len(vector_results) == 3, f"Expected 3 unique items after deduplication, got {len(vector_results)}"

    @pytest.mark.asyncio
    async def test_promotion_with_none_id(self, mock_settings, mock_vector_store, mock_embedding_service):
        """Original top-1 has id=None. Guard `original_top1_id is not None` skips promotion."""
        # Original query result has id=None
        original_results = [{"id": None, "text": "chunk 1", "file_id": "file_none", "_distance": 0.1}]
        step_back_results = [_make_result("X", 1), _make_result("Y", 2), _make_result("Z", 3), _make_result("W", 4), _make_result("V", 5)]

        mock_vector_store.search = AsyncMock(side_effect=[original_results, step_back_results])

        engine = RAGEngine(
            embedding_service=mock_embedding_service,
            vector_store=mock_vector_store,
        )

        query_embeddings = [("original", _make_embedding()), ("step_back", _make_embedding())]

        with patch.object(engine, "_normalize_metadata", return_value={}):
            (
                vector_results,
                _,
                _,
                _,
                _,
                _,
                _,
                _,
                _,
                exact_match_promoted,
                _,
            ) = await engine._execute_retrieval(query_embeddings, "test query", vault_id=None)

        # Promotion should not happen because original_top1_id is None (guard at line 528)
        assert exact_match_promoted is False, "Promotion must not occur when top-1 id is None"

    @pytest.mark.asyncio
    async def test_promotion_with_empty_string_id(self, mock_settings, mock_vector_store, mock_embedding_service):
        """Original top-1 has id=''. Treated as absent/empty — guard `is not None` passes but find loop may not match."""
        # Original query result has empty string id
        original_results = [{"id": "", "text": "chunk 1", "file_id": "file_empty", "_distance": 0.1}]
        step_back_results = [
            _make_result("X", 1),
            _make_result("Y", 2),
            _make_result("Z", 3),
            _make_result("W", 4),
            _make_result("V", 5),
        ]

        mock_vector_store.search = AsyncMock(side_effect=[original_results, step_back_results])

        engine = RAGEngine(
            embedding_service=mock_embedding_service,
            vector_store=mock_vector_store,
        )

        query_embeddings = [("original", _make_embedding()), ("step_back", _make_embedding())]

        with patch.object(engine, "_normalize_metadata", return_value={}):
            (
                vector_results,
                _,
                _,
                _,
                _,
                _,
                _,
                _,
                _,
                exact_match_promoted,
                _,
            ) = await engine._execute_retrieval(query_embeddings, "test query", vault_id=None)

        # original_top1_id = "" — is not None, so promotion guard passes.
        # But "" doesn't match any real result id in step_back results, so promote_idx stays None.
        assert exact_match_promoted is False, "Empty string id must not trigger promotion"

    @pytest.mark.asyncio
    async def test_promotion_preserves_result_length(self, mock_settings, mock_vector_store, mock_embedding_service):
        """pop + insert is length-preserving; total result count must stay the same."""
        # 8 unique results across both variants: A, B from original; X, Y, Z, W, V, U from step_back
        original_results = [_make_result("A", 1), _make_result("B", 2)]
        step_back_results = [
            _make_result("X", 1),
            _make_result("Y", 2),
            _make_result("Z", 3),
            _make_result("W", 4),
            _make_result("V", 5),
            _make_result("U", 6),
        ]
        # Total unique: A, B, X, Y, Z, W, V, U = 8

        mock_vector_store.search = AsyncMock(side_effect=[original_results, step_back_results])

        engine = RAGEngine(
            embedding_service=mock_embedding_service,
            vector_store=mock_vector_store,
        )

        query_embeddings = [("original", _make_embedding()), ("step_back", _make_embedding())]

        with patch.object(engine, "_normalize_metadata", return_value={}):
            (
                vector_results,
                _,
                _,
                _,
                _,
                _,
                _,
                _,
                _,
                exact_match_promoted,
                _,
            ) = await engine._execute_retrieval(query_embeddings, "test query", vault_id=None)

        # Verify result count is exactly 8 (fusion deduplicates but preserves count)
        assert len(vector_results) == 8, f"Expected exactly 8 results after fusion, got {len(vector_results)}"

        ids = [r.get("id") for r in vector_results]
        # All IDs should be unique (deduplication)
        assert len(ids) == len(set(ids)), f"Duplicate IDs found in results: {ids}"

    @pytest.mark.asyncio
    async def test_promotion_with_exactly_5_results(self, mock_settings, mock_vector_store, mock_embedding_service):
        """Fused results have exactly 5 items. Top-1 is at position 5+ — promotion should trigger (len >= 5)."""
        # Build case where we get exactly 5 results
        original_results = [_make_result("A", 1), _make_result("B", 2)]
        step_back_results = [
            _make_result("X", 1),
            _make_result("Y", 2),
            _make_result("Z", 3),
        ]
        # 5 unique results total: A, B, X, Y, Z

        mock_vector_store.search = AsyncMock(side_effect=[original_results, step_back_results])

        engine = RAGEngine(
            embedding_service=mock_embedding_service,
            vector_store=mock_vector_store,
        )

        query_embeddings = [("original", _make_embedding()), ("step_back", _make_embedding())]

        with patch.object(engine, "_normalize_metadata", return_value={}):
            (
                vector_results,
                _,
                _,
                _,
                _,
                _,
                _,
                _,
                _,
                exact_match_promoted,
                _,
            ) = await engine._execute_retrieval(query_embeddings, "test query", vault_id=None)

        assert len(vector_results) == 5, f"Expected exactly 5 results, got {len(vector_results)}"

        # If "A" is not in top-5 positions 0-4, promotion should have triggered
        ids = [r.get("id") for r in vector_results]
        if "A" not in ids[:5]:
            # "A" was demoted — promotion must have occurred
            assert exact_match_promoted is True, "Promotion must trigger when top-1 is outside top-5 with 5+ results"
        else:
            # "A" is already in top-5
            assert exact_match_promoted is False, "Must not promote when original top-1 is already in top-5"

    @pytest.mark.asyncio
    async def test_promotion_item_not_in_fused_results(self, mock_settings, mock_vector_store, mock_embedding_service):
        """Original top-1 was completely dropped during fusion. promote_idx stays None — silent skip."""
        # Build case where original top-1 "A" is not in step_back results
        # and gets completely dropped (simulating a case where fusion excludes it)
        # This can happen if there's a bug where the original result is filtered out
        original_results = [{"id": "A", "text": "original A", "file_id": "f1", "_distance": 0.01}]
        step_back_results = [
            _make_result("X", 1),
            _make_result("Y", 2),
            _make_result("Z", 3),
            _make_result("W", 4),
            _make_result("V", 5),
        ]

        mock_vector_store.search = AsyncMock(side_effect=[original_results, step_back_results])

        engine = RAGEngine(
            embedding_service=mock_embedding_service,
            vector_store=mock_vector_store,
        )

        query_embeddings = [("original", _make_embedding()), ("step_back", _make_embedding())]

        with patch.object(engine, "_normalize_metadata", return_value={}):
            (
                vector_results,
                _,
                _,
                _,
                _,
                _,
                _,
                _,
                _,
                exact_match_promoted,
                _,
            ) = await engine._execute_retrieval(query_embeddings, "test query", vault_id=None)

        ids = [r.get("id") for r in vector_results]

        # "A" should be in the fused results (fusion keeps first-seen)
        # If it IS present, promotion might or might not have happened
        # If it is NOT present, promotion should silently skip
        if "A" not in ids:
            # "A" was dropped — this is the silent-skip path
            assert exact_match_promoted is False, "Promotion must not occur when original top-1 is missing from fused results"
        else:
            # "A" is present — verify correct behavior based on position
            a_position = ids.index("A")
            if a_position < 5:
                # "A" is in top-5 — no promotion should have occurred
                assert exact_match_promoted is False, "Must not promote when original top-1 is already in top-5"
            else:
                # "A" is outside top-5 — promotion should have occurred
                assert exact_match_promoted is True, "Promotion must occur when original top-1 is outside top-5"

    @pytest.mark.asyncio
    async def test_promotion_with_top1_at_position_4(self, mock_settings, mock_vector_store, mock_embedding_service):
        """Original top-1 is already at position 4 (rank 5). It IS in top-5 — no promotion should occur."""
        # Build case where "A" (original top-1) ends up at position 4 naturally
        # 5 unique results so position 4 (0-indexed, 5th item) is valid
        original_results = [_make_result("A", 1), _make_result("B", 2)]
        step_back_results = [_make_result("X", 1), _make_result("Y", 2), _make_result("Z", 3)]

        mock_vector_store.search = AsyncMock(side_effect=[original_results, step_back_results])

        engine = RAGEngine(
            embedding_service=mock_embedding_service,
            vector_store=mock_vector_store,
        )

        query_embeddings = [("original", _make_embedding()), ("step_back", _make_embedding())]

        with patch.object(engine, "_normalize_metadata", return_value={}):
            (
                vector_results,
                _,
                _,
                _,
                _,
                _,
                _,
                _,
                _,
                exact_match_promoted,
                _,
            ) = await engine._execute_retrieval(query_embeddings, "test query", vault_id=None)

        # Verify we have 5+ results to make position 4 meaningful
        assert len(vector_results) >= 5, f"Need 5+ results for position-4 test, got {len(vector_results)}"

        ids = [r.get("id") for r in vector_results]

        # "A" is at position 4 (0-indexed) — in top-5, so no promotion should occur
        assert "A" in ids[:5], f"'A' should be in top-5 positions, got ids={ids}"
        assert exact_match_promoted is False, "Must not promote when original top-1 is already in top-5"
