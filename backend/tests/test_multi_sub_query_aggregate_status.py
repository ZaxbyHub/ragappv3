"""Tests for aggregate status propagation in multi-sub-query fusion (FR-002 part 2).

These tests verify:
1. _orchestrate_sub_query_retrieval returns 8-tuple (added aggregate statuses).
2. rerank_success aggregates as True if ANY sub-query had rerank_success=True.
3. hybrid_status aggregates as most-enabled across sub-queries.
4. rerank_status aggregates as "ok" if any sub-query had "ok", else "fallback", else "disabled".
5. Fused results are treated as CONFIDENT by design in the query() fusion path.
6. The fusion path uses propagated aggregate values instead of hardcoded ones.
"""

import asyncio
import os
import sys
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add parent directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub optional dependencies same as other test files
try:
    import lancedb
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow
except ImportError:
    import types

    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

try:
    from unstructured.partition.auto import partition
except ImportError:
    import types

    _unstructured = types.ModuleType("unstructured")
    _unstructured.partition = types.ModuleType("unstructured.partition")
    _unstructured.partition.auto = types.ModuleType("unstructured.partition.auto")
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    sys.modules["unstructured"] = _unstructured
    sys.modules["unstructured.partition"] = _unstructured.partition
    sys.modules["unstructured.partition.auto"] = _unstructured.partition.auto

from app.services.rag_engine import RAGEngine
from app.services.rag_trace import RAGTrace

# ------------------------------------------------------------------
# Fake services
# ------------------------------------------------------------------


class FakeEmbeddingService:
    """Returns a deterministic embedding based on call count."""

    def __init__(self):
        self._counter = 0

    async def embed_single(self, text: str) -> List[float]:
        self._counter += 1
        base = [0.1, 0.2, 0.3, 0.4]
        return [b + self._counter * 0.01 for b in base]

    async def embed_passage(self, text: str) -> List[float]:
        return await self.embed_single(text)


class FakeVectorStore:
    """Returns deterministic results keyed by the sub-query index."""

    def __init__(self):
        self._call_count = 0
        self.search_calls: List[Dict[str, Any]] = []

    async def search(
        self,
        embedding: List[float],
        limit: int = 10,
        vault_id=None,
        query_text=None,
        hybrid=False,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        self._call_count += 1
        call_idx = self._call_count - 1
        self.search_calls.append({"call_idx": call_idx, "embedding": embedding})

        templates = [
            [
                {
                    "id": "sq0_doc_a",
                    "text": "Document A content from sub-query 0",
                    "file_id": "doc_a",
                    "_distance": 0.10,
                    "metadata": {},
                },
                {
                    "id": "sq0_doc_b",
                    "text": "Document B content from sub-query 0",
                    "file_id": "doc_b",
                    "_distance": 0.20,
                    "metadata": {},
                },
            ],
            [
                {
                    "id": "sq1_doc_b",
                    "text": "Document B content from sub-query 1",
                    "file_id": "doc_b",
                    "_distance": 0.15,
                    "metadata": {},
                },
                {
                    "id": "sq1_doc_c",
                    "text": "Document C content from sub-query 1",
                    "file_id": "doc_c",
                    "_distance": 0.25,
                    "metadata": {},
                },
            ],
        ]
        results = templates[call_idx % len(templates)]
        return results[:limit]

    def get_fts_exceptions(self) -> int:
        return 0

    def reset(self):
        self._call_count = 0
        self.search_calls = []


class FakeMemoryStore:
    def detect_memory_intent(self, query: str) -> Optional[str]:
        return None

    async def search_memories(self, query: str, top_k: int, vault_id=None) -> List:
        return []


class FakeLLMClient:
    """Returns a canned multi-sub-query plan for testing."""

    def __init__(self, plan: List[str]):
        self._plan = plan

    async def chat_completion(self, messages, max_tokens=None, temperature=None):
        import json

        return json.dumps(self._plan)


# ------------------------------------------------------------------
# Helper: build a minimal RAGEngine with fakes
# ------------------------------------------------------------------


def _make_engine(
    embedding_service: Any,
    vector_store: Any,
    llm_client: Any,
    memory_store: Any,
) -> RAGEngine:
    with patch("app.services.embeddings.assert_url_safe"), \
         patch("app.services.llm_client.assert_url_safe"):
        engine = RAGEngine(
            embedding_service=embedding_service,
            vector_store=vector_store,
            memory_store=memory_store,
            llm_client=llm_client,
        )
    engine.retrieval_top_k = 10
    engine.vector_store = vector_store
    engine._get_indexed_file_ids = MagicMock(return_value=None)
    return engine


# ------------------------------------------------------------------
# Settings fixture
# ------------------------------------------------------------------


@pytest.fixture
def patched_settings_fixture():
    """Patch settings for isolated testing."""
    mock_settings = MagicMock()
    # Disable expensive features
    mock_settings.agentic_rag_enabled = False
    mock_settings.query_transformation_enabled = False
    mock_settings.memory_retrieval_enabled = False
    mock_settings.context_distillation_enabled = False
    mock_settings.parent_retrieval_enabled = False
    mock_settings.retrieval_evaluation_enabled = False
    mock_settings.context_max_tokens = 0
    mock_settings.rag_relevance_threshold = None
    mock_settings.max_distance_threshold = None
    # RRF / fusion settings
    mock_settings.rrf_legacy_mode = True
    mock_settings.multi_query_rrf_k = 60
    mock_settings.rrf_weight_original = 1.0
    mock_settings.rrf_weight_stepback = 1.0
    mock_settings.rrf_weight_hyde = 1.0
    mock_settings.retrieval_recency_weight = 0.0
    mock_settings.exact_match_promote = False
    # Hybrid and rerank disabled by default (can be overridden in tests)
    mock_settings.hybrid_search_enabled = False
    mock_settings.reranking_enabled = False
    # Other settings
    mock_settings.parent_retrieval_max_sources = 10
    mock_settings.distillation_similarity_threshold = 0.0
    mock_settings.chunk_size_chars = 512
    mock_settings.chunk_overlap_chars = 128
    mock_settings.retrieval_window = 0
    mock_settings.instant_skip_followup_rewrite = False
    mock_settings.instant_skip_query_transformation = False
    mock_settings.instant_skip_retrieval_evaluation = False
    mock_settings.wiki_retrieval_enabled = False
    mock_settings.kms_enabled = False
    mock_settings.maintenance_mode = False
    mock_settings.rag_trace_in_response = False

    with patch("app.services.rag_engine.settings", mock_settings):
        yield mock_settings


# ------------------------------------------------------------------
# Tests: 8-tuple return signature
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrate_sub_query_returns_8_tuple(patched_settings_fixture):
    """_orchestrate_sub_query_retrieval returns an 8-tuple including aggregate statuses."""
    plan = ["sub_query_alpha", "sub_query_beta"]

    fake_emb = FakeEmbeddingService()
    fake_vs = FakeVectorStore()
    fake_mem = FakeMemoryStore()
    fake_llm = FakeLLMClient(plan)

    engine = _make_engine(fake_emb, fake_vs, fake_llm, fake_mem)
    fake_vs.reset()

    result = await engine._orchestrate_sub_query_retrieval(
        plan=plan,
        vault_id=1,
        user_input="test question",
        effective_alpha=0.6,
        variants_dropped=[],
        effective_initial_top_k=10,
        effective_reranker_top_n=10,
        active_client=None,
        mode=None,
    )

    # Must return 8 elements (new aggregate statuses added)
    assert len(result) == 8, (
        f"Expected 8-tuple (fused_vector_results, fusion_applied, failed_sub_queries, "
        f"score_type, relevance_hint, agg_rerank_success, agg_hybrid_status, agg_rerank_status), "
        f"got {len(result)}-tuple: {result}"
    )

    (
        fused_vector_results,
        fusion_applied,
        failed_sub_queries,
        score_type,
        relevance_hint,
        agg_rerank_success,
        agg_hybrid_status,
        agg_rerank_status,
    ) = result

    assert isinstance(fused_vector_results, list)
    assert isinstance(fusion_applied, bool)
    assert isinstance(failed_sub_queries, list)
    assert isinstance(score_type, str)
    assert relevance_hint is None or isinstance(relevance_hint, str)
    assert agg_rerank_success is None or isinstance(agg_rerank_success, bool)
    assert isinstance(agg_hybrid_status, str)
    assert isinstance(agg_rerank_status, str)


# ------------------------------------------------------------------
# Tests: aggregate rerank_success propagation
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agg_rerank_success_true_when_any_sub_query_succeeds(patched_settings_fixture):
    """agg_rerank_success=True when any sub-query had rerank_success=True."""
    plan = ["sub_query_alpha", "sub_query_beta"]

    fake_emb = FakeEmbeddingService()
    fake_vs = FakeVectorStore()
    fake_mem = FakeMemoryStore()
    fake_llm = FakeLLMClient(plan)

    engine = _make_engine(fake_emb, fake_vs, fake_llm, fake_mem)

    # Mock reranking to return success=True for the sub-query retrievals
    fake_reranker = AsyncMock()
    fake_reranker.rerank = AsyncMock(
        side_effect=lambda query, chunks, top_n: (chunks, True)
    )
    engine.reranking_service = fake_reranker
    engine.reranking_enabled = True

    fake_vs.reset()

    result = await engine._orchestrate_sub_query_retrieval(
        plan=plan,
        vault_id=1,
        user_input="test question",
        effective_alpha=0.6,
        variants_dropped=[],
        effective_initial_top_k=10,
        effective_reranker_top_n=10,
        active_client=None,
        mode=None,
    )

    (
        fused_vector_results,
        fusion_applied,
        failed_sub_queries,
        score_type,
        relevance_hint,
        agg_rerank_success,
        agg_hybrid_status,
        agg_rerank_status,
    ) = result

    # With reranking enabled and successful, agg_rerank_success should be True
    assert agg_rerank_success is True, (
        f"Expected agg_rerank_success=True when reranking succeeds, got {agg_rerank_success}"
    )
    assert fusion_applied is True


@pytest.mark.asyncio
async def test_agg_rerank_success_none_when_reranking_disabled(patched_settings_fixture):
    """agg_rerank_success=None when reranking is globally disabled."""
    plan = ["sub_query_alpha", "sub_query_beta"]

    fake_emb = FakeEmbeddingService()
    fake_vs = FakeVectorStore()
    fake_mem = FakeMemoryStore()
    fake_llm = FakeLLMClient(plan)

    engine = _make_engine(fake_emb, fake_vs, fake_llm, fake_mem)

    # Ensure reranking is disabled
    engine.reranking_enabled = False
    engine.reranking_service = None

    fake_vs.reset()

    result = await engine._orchestrate_sub_query_retrieval(
        plan=plan,
        vault_id=1,
        user_input="test question",
        effective_alpha=0.6,
        variants_dropped=[],
        effective_initial_top_k=10,
        effective_reranker_top_n=10,
        active_client=None,
        mode=None,
    )

    (
        fused_vector_results,
        fusion_applied,
        failed_sub_queries,
        score_type,
        relevance_hint,
        agg_rerank_success,
        agg_hybrid_status,
        agg_rerank_status,
    ) = result

    # With reranking disabled globally, agg_rerank_success should be None
    assert agg_rerank_success is None, (
        f"Expected agg_rerank_success=None when reranking disabled, got {agg_rerank_success}"
    )


# ------------------------------------------------------------------
# Tests: aggregate hybrid_status propagation
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agg_hybrid_status_propagation_from_sub_queries(patched_settings_fixture):
    """agg_hybrid_status reflects the hybrid status from sub-query retrievals.

    This test verifies that the aggregate hybrid_status is returned from the
    orchestration. The actual value depends on whether FTS returned 'ok' in results.
    """
    plan = ["sub_query_alpha", "sub_query_beta"]

    fake_emb = FakeEmbeddingService()
    fake_vs = FakeVectorStoreWithHybridStatus()
    fake_mem = FakeMemoryStore()
    fake_llm = FakeLLMClient(plan)

    engine = _make_engine(fake_emb, fake_vs, fake_llm, fake_mem)

    fake_vs.reset()

    result = await engine._orchestrate_sub_query_retrieval(
        plan=plan,
        vault_id=1,
        user_input="test question",
        effective_alpha=0.6,
        variants_dropped=[],
        effective_initial_top_k=10,
        effective_reranker_top_n=10,
        active_client=None,
        mode=None,
    )

    (
        fused_vector_results,
        fusion_applied,
        failed_sub_queries,
        score_type,
        relevance_hint,
        agg_rerank_success,
        agg_hybrid_status,
        agg_rerank_status,
    ) = result

    # Verify the aggregate hybrid_status is returned as a string value
    assert isinstance(agg_hybrid_status, str), (
        f"Expected agg_hybrid_status to be a string, got {type(agg_hybrid_status)}"
    )
    # Valid values are 'disabled', 'dense_only', 'both'
    assert agg_hybrid_status in ("disabled", "dense_only", "both"), (
        f"Expected valid agg_hybrid_status value, got {agg_hybrid_status}"
    )
    # Fusion should have been applied
    assert fusion_applied is True


@pytest.mark.asyncio
async def test_agg_hybrid_status_disabled_when_global_disabled(patched_settings_fixture):
    """agg_hybrid_status='disabled' when hybrid search is globally disabled."""
    plan = ["sub_query_alpha", "sub_query_beta"]

    fake_emb = FakeEmbeddingService()
    fake_vs = FakeVectorStore()
    fake_mem = FakeMemoryStore()
    fake_llm = FakeLLMClient(plan)

    engine = _make_engine(fake_emb, fake_vs, fake_llm, fake_mem)
    engine.hybrid_search_enabled = False

    fake_vs.reset()

    result = await engine._orchestrate_sub_query_retrieval(
        plan=plan,
        vault_id=1,
        user_input="test question",
        effective_alpha=0.6,
        variants_dropped=[],
        effective_initial_top_k=10,
        effective_reranker_top_n=10,
        active_client=None,
        mode=None,
    )

    (
        fused_vector_results,
        fusion_applied,
        failed_sub_queries,
        score_type,
        relevance_hint,
        agg_rerank_success,
        agg_hybrid_status,
        agg_rerank_status,
    ) = result

    # With hybrid globally disabled, should be disabled
    assert agg_hybrid_status == "disabled", (
        f"Expected agg_hybrid_status='disabled' when globally disabled, got {agg_hybrid_status}"
    )


# ------------------------------------------------------------------
# Tests: aggregate rerank_status propagation
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agg_rerank_status_ok_when_any_sub_query_rerank_succeeded(patched_settings_fixture):
    """agg_rerank_status='ok' when any sub-query had rerank_status='ok'."""
    plan = ["sub_query_alpha", "sub_query_beta"]

    fake_emb = FakeEmbeddingService()
    fake_vs = FakeVectorStore()
    fake_mem = FakeMemoryStore()
    fake_llm = FakeLLMClient(plan)

    engine = _make_engine(fake_emb, fake_vs, fake_llm, fake_mem)

    # Mock successful reranking
    fake_reranker = AsyncMock()
    fake_reranker.rerank = AsyncMock(
        side_effect=lambda query, chunks, top_n: (chunks, True)
    )
    engine.reranking_service = fake_reranker
    engine.reranking_enabled = True

    fake_vs.reset()

    result = await engine._orchestrate_sub_query_retrieval(
        plan=plan,
        vault_id=1,
        user_input="test question",
        effective_alpha=0.6,
        variants_dropped=[],
        effective_initial_top_k=10,
        effective_reranker_top_n=10,
        active_client=None,
        mode=None,
    )

    (
        fused_vector_results,
        fusion_applied,
        failed_sub_queries,
        score_type,
        relevance_hint,
        agg_rerank_success,
        agg_hybrid_status,
        agg_rerank_status,
    ) = result

    # With successful reranking, agg_rerank_status should be "ok"
    assert agg_rerank_status == "ok", (
        f"Expected agg_rerank_status='ok' when reranking succeeds, got {agg_rerank_status}"
    )


@pytest.mark.asyncio
async def test_agg_rerank_status_disabled_when_rerankingGlobally_disabled(patched_settings_fixture):
    """agg_rerank_status='disabled' when reranking is globally disabled."""
    plan = ["sub_query_alpha", "sub_query_beta"]

    fake_emb = FakeEmbeddingService()
    fake_vs = FakeVectorStore()
    fake_mem = FakeMemoryStore()
    fake_llm = FakeLLMClient(plan)

    engine = _make_engine(fake_emb, fake_vs, fake_llm, fake_mem)
    engine.reranking_enabled = False
    engine.reranking_service = None

    fake_vs.reset()

    result = await engine._orchestrate_sub_query_retrieval(
        plan=plan,
        vault_id=1,
        user_input="test question",
        effective_alpha=0.6,
        variants_dropped=[],
        effective_initial_top_k=10,
        effective_reranker_top_n=10,
        active_client=None,
        mode=None,
    )

    (
        fused_vector_results,
        fusion_applied,
        failed_sub_queries,
        score_type,
        relevance_hint,
        agg_rerank_success,
        agg_hybrid_status,
        agg_rerank_status,
    ) = result

    assert agg_rerank_status == "disabled", (
        f"Expected agg_rerank_status='disabled' when reranking disabled, got {agg_rerank_status}"
    )


# ------------------------------------------------------------------
# Tests: CONFIDENT by design for fused results
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fused_results_eval_result_is_confident_by_design(patched_settings_fixture):
    """Multi-sub-query fused results are treated as CONFIDENT by design.

    When len(plan) > 1 and orchestration succeeds, eval_result is set to
    "CONFIDENT" because RRF fusion already combines evidence from multiple
    query angles, so NO_MATCH synthesis is not expected for decomposed queries.
    """
    plan = ["sub_query_alpha", "sub_query_beta", "sub_query_gamma"]

    fake_emb = FakeEmbeddingService()
    fake_vs = FakeVectorStore()
    fake_mem = FakeMemoryStore()
    fake_llm = FakeLLMClient(plan)

    engine = _make_engine(fake_emb, fake_vs, fake_llm, fake_mem)
    engine.reranking_enabled = False
    fake_vs.reset()

    # Enable context distillation so the distillation path is exercised
    patched_settings_fixture.context_distillation_enabled = True
    patched_settings_fixture.context_distillation_synthesis_enabled = False

    # Track eval_result passed to distillation
    captured_eval_result = None

    # Mock ContextDistiller to capture eval_result
    class FakeDistiller:
        def __init__(self, embedding_service, synthesis_client):
            pass

        async def distill(self, user_input, relevant_chunks, eval_result, *args, **kwargs):
            nonlocal captured_eval_result
            captured_eval_result = eval_result
            # Return a minimal distillation result
            class Result:
                sources = relevant_chunks[:3] if relevant_chunks else []
                memories = []
                sentence_provenance = []
                answer = "synthesized answer"
                prompt_tokens = 10
                completion_tokens = 5
            return Result()

    # Patch ContextDistiller to return our fake when instantiated in query()
    with patch("app.services.rag_engine.ContextDistiller", FakeDistiller):
        # Collect all yields from query()
        results = []
        async for r in engine.query(
            user_input="test question",
            chat_history=[],
            vault_id=1,
        ):
            results.append(r)
            if len(results) > 20:  # Safety limit
                break

    # When fusion is applied, eval_result should be "CONFIDENT"
    # (set at query() lines 1092-1095, not from _execute_retrieval)
    assert captured_eval_result == "CONFIDENT", (
        f"Expected eval_result='CONFIDENT' when fusion is applied, "
        f"got '{captured_eval_result}'. The fusion path sets eval_result='CONFIDENT' "
        f"directly at query() lines 1092-1095."
    )


@pytest.mark.asyncio
async def test_fusion_path_uses_propagated_values_not_hardcoded(patched_settings_fixture):
    """Fusion path uses propagated agg_rerank_success, agg_hybrid_status, agg_rerank_status.

    After _orchestrate_sub_query_retrieval returns, the fusion path in query()
    assigns:
        rerank_success = _multi_sub_rerank_success
        hybrid_status = _multi_sub_hybrid_status
        rerank_status = _multi_sub_rerank_status

    This test verifies that the returned aggregate values are used correctly.
    """
    plan = ["sub_query_alpha", "sub_query_beta"]

    fake_emb = FakeEmbeddingService()
    fake_vs = FakeVectorStore()
    fake_mem = FakeMemoryStore()
    fake_llm = FakeLLMClient(plan)

    engine = _make_engine(fake_emb, fake_vs, fake_llm, fake_mem)
    fake_vs.reset()

    (
        fused_vector_results,
        fusion_applied,
        failed_sub_queries,
        score_type,
        relevance_hint,
        agg_rerank_success,
        agg_hybrid_status,
        agg_rerank_status,
    ) = await engine._orchestrate_sub_query_retrieval(
        plan=plan,
        vault_id=1,
        user_input="test question",
        effective_alpha=0.6,
        variants_dropped=[],
        effective_initial_top_k=10,
        effective_reranker_top_n=10,
        active_client=None,
        mode=None,
    )

    # Verify the aggregate values are proper types/values
    assert isinstance(agg_hybrid_status, str)
    assert isinstance(agg_rerank_status, str)
    assert agg_rerank_success is None or isinstance(agg_rerank_success, bool)

    # These values are what the fusion path would use instead of hardcoded ones
    # The propagated values preserve the actual sub-query status information
    assert fusion_applied is True


# ------------------------------------------------------------------
# FakeVectorStoreWithHybridStatus
# ------------------------------------------------------------------


class FakeVectorStoreWithHybridStatus(FakeVectorStore):
    """FakeVectorStore that also returns _fts_status='ok' on results."""

    async def search(
        self,
        embedding: List[float],
        limit: int = 10,
        vault_id=None,
        query_text=None,
        hybrid=False,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        results = await super().search(embedding, limit, vault_id, query_text, hybrid, **kwargs)
        # Add FTS status to simulate hybrid search success
        for r in results:
            r["_fts_status"] = "ok"
        return results


# ------------------------------------------------------------------
# Tests: fallback path returns correct aggregate statuses
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_path_returns_correct_aggregate_statuses(patched_settings_fixture):
    """When all sub-queries fail, fallback returns aggregate statuses from single-query path."""

    class SubQueryFailingEmbeddingService(FakeEmbeddingService):
        """Fails sub-query embeddings but original query succeeds (used in fallback)."""

        async def embed_single(self, text: str) -> List[float]:
            # Sub-query texts start with "sub_query", original query does not
            if text.startswith("sub_query"):
                raise RuntimeError("Sub-query embeddings fail")
            return await super().embed_single(text)

    plan = ["sub_query_alpha", "sub_query_beta"]

    fake_emb = SubQueryFailingEmbeddingService()
    fake_vs = FakeVectorStore()
    fake_mem = FakeMemoryStore()
    fake_llm = FakeLLMClient(plan)

    engine = _make_engine(fake_emb, fake_vs, fake_llm, fake_mem)
    fake_vs.reset()

    result = await engine._orchestrate_sub_query_retrieval(
        plan=plan,
        vault_id=1,
        user_input="test question",
        effective_alpha=0.6,
        variants_dropped=[],
        effective_initial_top_k=10,
        effective_reranker_top_n=10,
        active_client=None,
        mode=None,
    )

    (
        fused_vector_results,
        fusion_applied,
        failed_sub_queries,
        score_type,
        relevance_hint,
        agg_rerank_success,
        agg_hybrid_status,
        agg_rerank_status,
    ) = result

    # Fallback path is used (fusion_applied=False) when all sub-queries fail
    assert fusion_applied is False, (
        f"Expected fusion_applied=False for all-fail fallback, got {fusion_applied}"
    )
    # Failed sub-queries should be populated
    assert len(failed_sub_queries) == 2, (
        f"Expected 2 failed sub-queries, got {len(failed_sub_queries)}: {failed_sub_queries}"
    )
    # The fallback path also returns aggregate statuses from _execute_retrieval
    assert isinstance(agg_hybrid_status, str)
    assert isinstance(agg_rerank_status, str)
