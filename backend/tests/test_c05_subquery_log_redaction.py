"""Acceptance checks for issue #710 (C05): sub-query text in RAG engine logs.

The logging contract says user-derived text (including QueryPlanner
sub-queries) must never appear in default (INFO+) logs. These checks freeze
the two rag_engine leak sites root-caused for issue #710:

- AC4: ``_orchestrate_sub_query_retrieval`` must not log the text of a
  sub-query whose embedding failed (both WARNING sites interpolate
  ``sq[:60]``).
- AC5: ``query()`` must not log the failed-sub-query texts in its
  degraded-fusion INFO summary (``[sq[:40] for sq in sub_queries_failed]``).

RED at base, GREEN after the fix.

Fake services and the settings fixture mirror
``tests/test_multi_sub_query_aggregate_status.py``.
"""

import os
import sys
import unittest
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

from app.services.query_transformer import QueryPlanner
from app.services.rag_engine import RAGEngine

# assertLogs is a TestCase method; these checks are module-level pytest
# functions, so share one bare TestCase instance purely as the
# context-manager provider.
_ASSERT_LOGS = unittest.TestCase()

# ------------------------------------------------------------------
# Fake services (mirrored from test_multi_sub_query_aggregate_status.py)
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


class MarkerFailingEmbeddingService(FakeEmbeddingService):
    """Raises for any text carrying the C05SUBQ marker; embeds the rest.

    The exception text deliberately carries no marker so the only way the
    marker can reach a log line is via the sub-query text itself.
    """

    async def embed_single(self, text: str) -> List[float]:
        if "C05SUBQ" in text:
            raise RuntimeError("embedding backend unavailable")
        return await super().embed_single(text)

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

    async def search_memories(self, query: str, top_k: int, vault_id=None, include_global: bool = False) -> List:
        return []


class FakeLLMClient:
    """Returns a canned JSON list for every chat_completion call."""

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
# AC4: sub-query embedding failure must not log the sub-query text
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_subquery_embedding_logs_no_subquery_text(patched_settings_fixture):
    """AC4: failed sub-query embedding WARNINGs carry no sub-query text.

    The marker sub-query fails to embed (the plain one succeeds, so the
    degraded-fusion path with a populated failed list runs). RED at base
    (both WARNING sites interpolate sq[:60]), GREEN after the fix.
    """
    plan = ["plain question", "C05SUBQ facet"]

    fake_emb = MarkerFailingEmbeddingService()
    fake_vs = FakeVectorStore()
    fake_mem = FakeMemoryStore()
    fake_llm = FakeLLMClient(plan)

    engine = _make_engine(fake_emb, fake_vs, fake_llm, fake_mem)
    fake_vs.reset()

    with _ASSERT_LOGS.assertLogs("app.services.rag_engine", level="INFO") as cm:
        await engine._orchestrate_sub_query_retrieval(
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

    count = sum(1 for rec in cm.records if "C05SUBQ" in rec.getMessage())
    assert count == 0, (
        f"{count} rag_engine log record(s) carry failed sub-query text: "
        f"{cm.output}"
    )


# ------------------------------------------------------------------
# AC5: degraded-fusion INFO summary must not log the sub-query texts
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_degraded_fusion_summary_logs_no_subquery_text(patched_settings_fixture):
    """AC5: query() degraded-fusion INFO summary carries no sub-query text.

    Query planning is driven through the public query() method: the planner
    is patched to return a two-item plan (so the orchestration branch runs)
    and _orchestrate_sub_query_retrieval is patched to return the 8-tuple
    with a nonempty failed list and nonempty fused results, reaching the
    ``if sub_queries_failed:`` INFO summary. RED at base (the summary
    interpolates [sq[:40] for sq in sub_queries_failed]), GREEN after the
    fix.
    """
    query_text = "test question"
    plan = [query_text, "C05FUSED facet"]

    fake_emb = FakeEmbeddingService()
    fake_vs = FakeVectorStore()
    fake_mem = FakeMemoryStore()
    # The LLM fake only serves generation here (planning is patched below),
    # so keep its response marker-free.
    fake_llm = FakeLLMClient(["storage overview"])

    engine = _make_engine(fake_emb, fake_vs, fake_llm, fake_mem)
    fake_vs.reset()

    fused_result = {
        "id": "c05_fused_doc_a",
        "text": "Document A content for the fused set",
        "file_id": "doc_a",
        "_distance": 0.10,
        "metadata": {},
    }
    orchestration_return = (
        [fused_result],  # fused_vector_results (nonempty -> fusion branch)
        True,  # fusion_applied
        ["C05FUSED facet"],  # failed_sub_queries
        "distance",  # score_type
        None,  # relevance_hint
        None,  # agg_rerank_success
        "disabled",  # agg_hybrid_status
        "disabled",  # agg_rerank_status
    )

    with patch.object(
        QueryPlanner, "plan", new=AsyncMock(return_value=plan)
    ), patch.object(
        engine,
        "_orchestrate_sub_query_retrieval",
        new=AsyncMock(return_value=orchestration_return),
    ):
        with _ASSERT_LOGS.assertLogs("app.services.rag_engine", level="INFO") as cm:
            async for _event in engine.query(
                user_input=query_text,
                chat_history=[],
                vault_id=1,
            ):
                pass

    count = sum(1 for rec in cm.records if "C05FUSED" in rec.getMessage())
    assert count == 0, (
        f"{count} rag_engine log record(s) carry failed sub-query text: "
        f"{cm.output}"
    )
