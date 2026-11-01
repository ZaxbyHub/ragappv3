"""Positive-path coverage for the #710 log-redaction contract.

The frozen acceptance checks in ``test_c05_log_redaction.py`` and
``test_c05_subquery_log_redaction.py`` assert marker ABSENCE
(``count == 0``). Absence alone cannot tell "the redaction ran" from "the log
call was deleted", and ``assertLogs(level="INFO")`` also captures WARNING, so
a module's own fallback record can satisfy the capture. These companion tests
pin the positive half of the same contract WITHOUT editing the frozen check
files (their blob ids are bound to the published issue-tracer anchor receipt):

- every redacted field's summary shape, including the empty/None passthrough;
- the count-only default-level records the PR preserves for operators
  (planner decomposition, sub-query embed failure, degraded fusion);
- the DEBUG fallback lines still carry the truncated text;
- the sub-query RETRIEVAL-failure site, which the frozen AC4 check cannot
  reach (its embedding fake makes every vector-store search succeed).

RED at the pre-review head (9dbd1220) for the items marked below; GREEN after
the companion fix.
"""

import logging
import os
import sys
import unittest
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.rag_trace import RAGTrace

# Module-level instance used purely as an assertLogs context provider; the
# tests below are plain functions, so they cannot use ``self.assertLogs``.
_LOG_CTX = unittest.TestCase()


# ------------------------------------------------------------------
# Trace redaction shapes
# ------------------------------------------------------------------


def test_redacted_fields_emit_summaries_not_raw_text():
    """The INFO trace record carries summaries for every user-derived field."""
    trace = RAGTrace(original_query="what is the policy for C05POS salary")
    trace.transformed_queries = ["C05POS rephrased"]
    trace.variants_dropped = ["C05POS dropped"]
    trace.wiki_query = "C05POS wiki probe"
    trace.followup_rewrite = "C05POS rewritten followup"
    trace.query_plan = ["C05POS facet"]
    trace.unverifiable_claims = ["the C05POS claim has no citation"]

    with _LOG_CTX.assertLogs("app.services.rag_trace", level="INFO") as cm:
        trace.log()
    joined = "\n".join(cm.output)

    # Positive half: the trace record itself was emitted (not a fallback).
    assert any("RAG trace:" in rec.getMessage() for rec in cm.records)
    assert "redacted" in joined
    # Every redacted field is summarized, and no raw text survived.
    assert "<redacted count=1>" in joined
    assert "C05POS" not in joined


def test_empty_and_none_redacted_fields_survive_the_projection():
    """Falsy user-text fields stay falsy instead of becoming a fake summary."""
    emitted = RAGTrace().to_log_dict()
    assert emitted["wiki_query"] == ""
    assert emitted["followup_rewrite"] is None
    assert emitted["transformed_queries"] == "<redacted count=0>"
    assert emitted["original_query"].startswith("<redacted len=0 hash=")


def test_warning_fallback_does_not_satisfy_a_positive_trace_assertion():
    """A to_log_dict() failure emits ONLY the WARNING fallback.

    This is why the frozen AC1/AC2 checks can pass vacuously, and why the
    positive assertion must look for a 'RAG trace:' record.
    """
    trace = RAGTrace(original_query="C05POS salary policy")
    with patch.object(RAGTrace, "to_dict", side_effect=RuntimeError("boom")):
        with _LOG_CTX.assertLogs("app.services.rag_trace", level="INFO") as cm:
            trace.log()
    messages = [rec.getMessage() for rec in cm.records]
    assert any("Failed to emit RAG trace" in m for m in messages)
    assert not any("RAG trace:" in m for m in messages), (
        "with to_dict() broken there is no trace record — a check that only "
        "asserts marker absence is satisfied by the fallback WARNING alone"
    )


# ------------------------------------------------------------------
# Planner: the preserved count-only INFO line and the DEBUG detail
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_planner_emits_count_only_info_and_debug_carries_text():
    """QueryPlanner.plan() emits a count-only INFO record plus DEBUG text."""
    from app.services.llm_client import LLMClient
    from app.services.query_transformer import QueryPlanner

    client = MagicMock(spec=LLMClient)
    client.chat_completion = AsyncMock(
        return_value='["C05POS storage engines", "C05POS auth model"]'
    )
    planner = QueryPlanner(client)
    query = "compare the C05POS storage engines and the auth model"

    with _LOG_CTX.assertLogs("app.services.query_transformer", level="DEBUG") as cm:
        result = await planner.plan(query)

    assert query in result  # the plan itself is unchanged
    records = [(rec.levelno, rec.getMessage()) for rec in cm.records]
    info_lines = [
        msg for level, msg in records
        if level == logging.INFO and "decomposed" in msg
    ]
    debug_lines = [
        msg for level, msg in records if level == logging.DEBUG and "decomposed" in msg
    ]

    # Positive half: the operator-facing count-only record still fires.
    assert info_lines, (
        "count-only INFO decomposition record missing; got "
        f"{[msg for _, msg in records][:8]}"
    )
    assert info_lines[0].endswith("into 3 sub-queries")
    assert query not in info_lines[0]
    # The DEBUG fallback still carries the text for troubleshooting.
    assert debug_lines and query[:60] in debug_lines[0]


# ------------------------------------------------------------------
# Sub-query failure sites: positive records + the untested retrieval branch
# ------------------------------------------------------------------

try:
    import lancedb  # noqa: F401
except ImportError:  # pragma: no cover - CI installs lancedb for real
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")


class _EmbeddingOk:
    async def embed_single(self, text: str) -> List[float]:
        return [0.1, 0.2, 0.3, 0.4]

    async def embed_passage(self, text: str) -> List[float]:
        return await self.embed_single(text)


class _VectorStoreStub:
    """Minimal vector store for the retrieval-failure path."""

    def __init__(self) -> None:
        self.search_calls = 0

    async def search(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        self.search_calls += 1
        return [
            {"id": "d1", "text": "chunk", "file_id": "f1", "_distance": 0.2, "metadata": {}}
        ]

    def get_fts_status(self) -> str:
        return "disabled"

    def get_fts_exceptions(self) -> int:
        return 0


class _MemoryStore:
    def detect_memory_intent(self, query: str) -> Optional[str]:
        return None

    async def search_memories(
        self, query: str, top_k: int, vault_id=None, include_global: bool = False
    ) -> List:
        return []


class _LLM:
    async def chat_completion(self, messages, max_tokens=None, temperature=None):
        return "[]"


def _mock_settings() -> MagicMock:
    settings = MagicMock()
    for attr, val in {
        "agentic_rag_enabled": False,
        "query_transformation_enabled": False,
        "memory_retrieval_enabled": False,
        "context_distillation_enabled": False,
        "parent_retrieval_enabled": False,
        "retrieval_evaluation_enabled": False,
        "context_max_tokens": 0,
        "rag_relevance_threshold": None,
        "max_distance_threshold": None,
        "rrf_legacy_mode": True,
        "multi_query_rrf_k": 60,
        "rrf_weight_original": 1.0,
        "rrf_weight_stepback": 1.0,
        "rrf_weight_hyde": 1.0,
        "retrieval_recency_weight": 0.0,
        "exact_match_promote": False,
        "hybrid_search_enabled": False,
        "reranking_enabled": False,
        "parent_retrieval_max_sources": 10,
        "distillation_similarity_threshold": 0.0,
        "chunk_size_chars": 512,
        "chunk_overlap_chars": 128,
        "retrieval_window": 0,
        "instant_skip_followup_rewrite": False,
        "instant_skip_query_transformation": False,
        "instant_skip_retrieval_evaluation": False,
        "wiki_retrieval_enabled": False,
        "kms_enabled": False,
        "maintenance_mode": False,
        "rag_trace_in_response": False,
    }.items():
        setattr(settings, attr, val)
    return settings


def _make_engine(vector_store: Any) -> Any:
    from app.services.rag_engine import RAGEngine

    with patch("app.services.embeddings.assert_url_safe"), patch(
        "app.services.llm_client.assert_url_safe"
    ):
        engine = RAGEngine(
            embedding_service=_EmbeddingOk(),
            vector_store=vector_store,
            memory_store=_MemoryStore(),
            llm_client=_LLM(),
        )
    engine.retrieval_top_k = 10
    engine._get_indexed_file_ids = MagicMock(return_value=None)
    return engine


@pytest.mark.asyncio
async def test_retrieval_failure_warning_is_count_only_and_debug_carries_text():
    """The sub-query retrieval-failure site emits no sub-query text at WARNING.

    Covers the fourth rewritten rag_engine site, which no frozen check reaches:
    the frozen AC4 check's vector store always succeeds, so its retrieval
    branch never runs. Here ``_execute_retrieval`` itself raises, which is what
    the orchestrator's ``gather(return_exceptions=True)`` branch handles.

    Pinned: the operator-facing WARNING still fires and carries no sub-query
    text; the DEBUG line still carries the truncated text.
    """
    vector_store = _VectorStoreStub()
    with patch("app.services.rag_engine.settings", _mock_settings()):
        engine = _make_engine(vector_store)
        with patch.object(
            engine, "_execute_retrieval", side_effect=RuntimeError("probe retrieval failure")
        ):
            with _LOG_CTX.assertLogs("app.services.rag_engine", level="DEBUG") as cm:
                with pytest.raises(Exception):
                    await engine._orchestrate_sub_query_retrieval(
                        plan=["plain question", "C05POSFACET facet"],
                        vault_id=1,
                        user_input="test question",
                        effective_alpha=0.6,
                        variants_dropped=[],
                        effective_initial_top_k=10,
                        effective_reranker_top_n=10,
                        active_client=None,
                        mode=None,
                    )

    records = [(rec.levelno, rec.getMessage()) for rec in cm.records]
    retrieval_warnings = [
        msg for level, msg in records
        if level >= logging.WARNING and "sub-query retrieval failed" in msg
    ]
    assert retrieval_warnings, (
        "the count-only retrieval-failure WARNING is missing — the diagnostic "
        f"operators rely on was deleted. records: {[m for _, m in records][:8]}"
    )
    assert "C05POSFACET" not in retrieval_warnings[0]
    debug_text = [msg for level, msg in records if level == logging.DEBUG]
    assert any("C05POSFACET" in msg for msg in debug_text), (
        "the DEBUG detail line no longer carries the failed sub-query text"
    )


@pytest.mark.asyncio
async def test_embed_failure_warning_is_count_only():
    """The sub-query embedding-failure sites emit no sub-query text at WARNING."""
    class _EmbeddingFailsMarker:
        async def embed_single(self, text: str) -> List[float]:
            if "C05POSEMB" in text:
                raise RuntimeError("embedding backend unavailable")
            return [0.1, 0.2, 0.3, 0.4]

        async def embed_passage(self, text: str) -> List[float]:
            return await self.embed_single(text)

    class _VectorStoreOk:
        async def search(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
            return [{"id": "d1", "text": "chunk", "file_id": "f1", "_distance": 0.2, "metadata": {}}]

    from app.services.rag_engine import RAGEngine

    with patch("app.services.embeddings.assert_url_safe"), patch(
        "app.services.llm_client.assert_url_safe"
    ):
        engine = RAGEngine(
            embedding_service=_EmbeddingFailsMarker(),
            vector_store=_VectorStoreOk(),
            memory_store=_MemoryStore(),
            llm_client=_LLM(),
        )
    engine.retrieval_top_k = 10
    engine._get_indexed_file_ids = MagicMock(return_value=None)

    with patch("app.services.rag_engine.settings", _mock_settings()):
        with _LOG_CTX.assertLogs("app.services.rag_engine", level="DEBUG") as cm:
            await engine._orchestrate_sub_query_retrieval(
                plan=["plain question", "C05POSEMB facet"],
                vault_id=1,
                user_input="test question",
                effective_alpha=0.6,
                variants_dropped=[],
                effective_initial_top_k=10,
                effective_reranker_top_n=10,
                active_client=None,
                mode=None,
            )

    warnings = [rec.getMessage() for rec in cm.records if rec.levelno >= logging.WARNING]
    assert warnings, "the embedding-failure WARNING disappeared"
    assert not any("C05POSEMB" in m for m in warnings)
