"""Acceptance check for issue #715 finding TQ-instruction-batch-4-07 (C8).

Desired post-fix behavior (RED on current master):

The agentic RetrievalTool returns a source whose underlying chunk text is
1,200 characters with a sentinel token at offset 800. SynthesisTool must
include that sentinel in the evidence sent to the synthesis LLM.

Today the sentinel never reaches the LLM:
DocumentRetrievalService.to_source_metadata truncates the snippet to
``raw_text[:300]`` (document_retrieval.py) and SynthesisTool renders
``str(src.get("snippet", ""))[:500]`` (agentic_tools.py), so only the
first 300 characters of a 1,200-character chunk are ever shown to the
model — content past offset 300, including offset 800, is silently
dropped even though the full text lives in the RAGSource/chunk.

Non-vacuous construction: the fake engine's ``document_retrieval`` is a
REAL DocumentRetrievalService (pattern copied from
``_RealDRSEngineFactory`` in tests/test_agentic_tools.py), so
``filter_relevant`` builds a real RAGSource from the vector-result dict
and ``to_source_metadata`` genuinely produces the 300-character snippet
preview. The snippet is never faked directly with 500+ characters.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.agentic_tools import RetrievalTool, SynthesisTool
from app.services.document_retrieval import DocumentRetrievalService
from app.services.rag_engine import RAGEngine

# Unique sentinel — appears nowhere in the production prompts or fixture text.
SENTINEL = "ZQ7X-SENTINEL-C10-EVIDENCE-9K"
SENTINEL_OFFSET = 800
CHUNK_LENGTH = 1200


def _make_1200_char_text() -> str:
    """Build chunk text of exactly 1,200 chars with SENTINEL at offset 800."""
    prefix = "a" * SENTINEL_OFFSET
    tail_len = CHUNK_LENGTH - SENTINEL_OFFSET - len(SENTINEL)
    text = prefix + SENTINEL + "b" * tail_len
    assert len(text) == CHUNK_LENGTH
    assert text.index(SENTINEL) == SENTINEL_OFFSET
    return text


def _make_engine(vector_results):
    """Mock RAGEngine whose document_retrieval is a REAL service.

    Modeled on ``_RealDRSEngineFactory.make`` in tests/test_agentic_tools.py:
    the real DocumentRetrievalService makes ``filter_relevant`` +
    ``to_source_metadata`` run the genuine pipeline (snippet =
    ``raw_text[:300]``) rather than returning a stubbed metadata dict.
    """
    mock_engine = MagicMock(spec=RAGEngine)
    mock_engine.embedding_service = MagicMock()
    mock_engine.embedding_service.embed_single = AsyncMock(return_value=[0.1] * 3)
    mock_engine._execute_retrieval = AsyncMock(
        return_value=(
            vector_results,
            None,  # relevance_hint
            "CONFIDENT",  # eval_result
            None,  # rerank_success (4th of the 11-tuple) -> reranked=False
            "distance",  # score_type
            "disabled",  # hybrid_status
            0,  # fts_exceptions
            "disabled",  # rerank_status
            [],  # variants_dropped
            False,  # exact_match_promoted
            {},  # token_pack_stats
        )
    )
    mock_engine.document_retrieval = DocumentRetrievalService(
        vector_store=None,
        max_distance_threshold=0.5,
        retrieval_top_k=10,
        retrieval_window=0,
    )
    mock_engine._sync_document_retrieval_settings = MagicMock()
    return mock_engine


class _CapturingLLM:
    """Fake LLM client (pattern copied from the SynthesisTool capturing-LLM
    tests in tests/test_agentic_tools.py) that records every message payload
    it receives."""

    def __init__(self):
        self.messages_log = []

    async def chat_completion(self, messages, **kwargs):
        self.messages_log.extend(messages)
        return "synthesized answer"


@pytest.mark.asyncio
async def test_synthesis_sees_full_chunk_text():
    """C8: evidence past the 300-char snippet preview must reach the LLM.

    Module-level test function so the frozen acceptance-check argv
    ``tests/test_c10_agentic_synthesis_evidence.py::test_synthesis_sees_full_chunk_text``
    resolves verbatim (class-qualified ids would not).
    """
    raw_text = _make_1200_char_text()
    vector_results = [
        {
            "id": "42_0",
            "file_id": "42",
            "text": raw_text,
            "_distance": 0.10,  # survives the 0.5 distance threshold
            "metadata": {
                "raw_text": raw_text,
                "source_file": "evidence.pdf",
            },
        }
    ]

    # --- RetrievalTool: real DocumentRetrievalService pipeline ---------
    engine = _make_engine(vector_results)
    retrieval_tool = RetrievalTool(retrieval_top_k=5, engine=engine)
    retrieval_result = await retrieval_tool.execute(
        query="What does the deep section say?", vault_id=1
    )

    # Fixture sanity: exactly one source came back through the real
    # pipeline. Its snippet is the genuine raw_text[:300] preview
    # produced by the real to_source_metadata — the sentinel at offset
    # 800 lies far beyond it, so the only way the sentinel can reach the
    # synthesis LLM is if SynthesisTool sends the full chunk text.
    assert retrieval_result.success is True
    assert len(retrieval_result.sources) == 1

    # --- SynthesisTool: capturing LLM ----------------------------------
    llm = _CapturingLLM()
    synthesis_tool = SynthesisTool(llm_client=llm)
    synthesis_result = await synthesis_tool.execute(
        text="What does the deep section say?",
        sources=retrieval_result.sources,
    )

    assert synthesis_result.success is True
    assert llm.messages_log, "synthesis LLM must have been called"

    captured_prompt = "\n".join(m["content"] for m in llm.messages_log)
    found = SENTINEL in captured_prompt
    # E712 suppressed: the acceptance-check red signature is
    # ``AssertionError: assert False == True``.
    assert found == True  # noqa: E712
