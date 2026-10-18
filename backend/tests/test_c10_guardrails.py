"""Issue #715 guardrails (Phase 4.2) — wire-hygiene and notice pins.

These are ADDITIVE guardrails beyond the frozen acceptance checks C01-C12:
they pin the new surfaces the fixes introduced (fallback content notice on
both chat surfaces, the private `_chunk_text` evidence channel never leaking
to any client wire chunk, and the SynthesisTool evidence budget caps).

Reuse: engine fakes from tests/test_rag_engine.py (same import style as the
frozen c10 checks); the direct-call route harness of
tests/test_non_stream_error_handling.py.
"""

import os
import re
import sys
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_rag_engine import (  # noqa: E402
    FakeEmbeddingService,
    FakeLLMClient,
    FakeMemoryStore,
    FakeVectorStore,
)

from app.api.routes.chat import non_stream_chat_response  # noqa: E402
from app.config import settings  # noqa: E402
from app.services.embeddings import EmbeddingService  # noqa: E402
from app.services.llm_client import LLMClient  # noqa: E402
from app.services.memory_store import MemoryStore  # noqa: E402
from app.services.rag_engine import RAGEngine  # noqa: E402
from app.services.vector_store import VectorStore  # noqa: E402


@pytest.fixture(autouse=True)
def _patch_ssrf():
    with patch("app.services.embeddings.assert_url_safe"), patch(
        "app.services.llm_client.assert_url_safe"
    ):
        yield


async def test_fallback_chunk_carries_content_notice():
    """T1-32-K-01 companion (#715): the engine's fallback chunk must carry a
    `content` notice so a degraded turn is visible to chunk consumers (the
    stream route re-emits fallback content — pinned by
    test_chat_stage_events.py::test_fallback_event_re_emitted_as_content)."""
    engine = RAGEngine()
    engine.embedding_service = cast(
        EmbeddingService, FakeEmbeddingService([0.1, 0.2])
    )
    engine.vector_store = cast(VectorStore, FakeVectorStore([]))
    engine.memory_store = cast(MemoryStore, FakeMemoryStore())
    engine.llm_client = cast(
        LLMClient, FakeLLMClient(response="nothing retrieved.")
    )
    engine.maintenance_mode = True
    engine._get_indexed_file_ids = lambda vault_id: None

    with patch.object(settings, "query_transformation_enabled", False):
        results = [
            msg
            async for msg in engine.query("what changed?", [], stream=False)
        ]

    fallback_chunks = [
        r for r in results if isinstance(r, dict) and r.get("type") == "fallback"
    ]
    assert fallback_chunks, (
        f"expected a fallback chunk, got: {[r.get('type') for r in results]}"
    )
    notice = fallback_chunks[0].get("content", "")
    assert notice, "fallback chunk must carry a non-empty degraded-retrieval notice"
    assert "degraded" in notice.lower()
    # PRR-002 (feedback): the notice is a FIXED generic string — raw
    # exception text (fallback_reason = str(exc) on the generic error
    # path) must never reach client-visible content.
    assert "RAG index is under maintenance" not in notice, (
        "notice must be the fixed generic text, not the interpolated reason"
    )
    assert notice.endswith("\n\n"), (
        "notice must end with a blank-line separator (feedback: it was "
        "running into the answer text)"
    )


@pytest.mark.asyncio
async def test_generic_error_path_notice_has_no_exception_text():
    """PRR-002/PRR-014 (feedback): on the generic vector-store error path
    (fallback_reason = str(exc)), the client-visible notice stays the fixed
    generic string — the raw exception text goes only to the `reason` field
    and the server-side WARNING."""
    class _FailingStore(FakeVectorStore):
        async def search(self, *args, **kwargs):
            raise RuntimeError(
                "Failed to connect to LanceDB at /srv/secret/lancedb: errno 13"
            )

    engine = RAGEngine()
    engine.embedding_service = cast(
        EmbeddingService, FakeEmbeddingService([0.1, 0.2])
    )
    engine.vector_store = cast(VectorStore, _FailingStore([]))
    engine.memory_store = cast(MemoryStore, FakeMemoryStore())
    engine.llm_client = cast(
        LLMClient, FakeLLMClient(response="ungrounded answer.")
    )
    engine.maintenance_mode = False
    engine._get_indexed_file_ids = lambda vault_id: None

    with patch.object(settings, "query_transformation_enabled", False):
        results = [
            msg async for msg in engine.query("what changed?", [], stream=False)
        ]

    fallback_chunks = [
        r for r in results if isinstance(r, dict) and r.get("type") == "fallback"
    ]
    assert fallback_chunks, "generic error path must still emit the fallback chunk"
    assert fallback_chunks[0].get("reason"), "reason field keeps the exception text"
    notice = fallback_chunks[0].get("content", "")
    assert "LanceDB" not in notice and "srv" not in notice and "errno" not in notice, (
        f"exception text leaked into client content: {notice!r}"
    )
    assert "degraded" in notice.lower()


@pytest.mark.asyncio
async def test_cited_memories_under_degradation_keep_decision_basis():
    """PRR-015 (feedback): a degraded turn whose answer CITES memories keeps
    abstention_basis 'decision' (citing evidence is a real decision); only
    the no-evidence branch maps to 'unavailable'."""
    from app.services.memory_store import MemoryRecord

    memory = FakeMemoryStore(
        memories=[
            MemoryRecord(
                id=1,
                content="the deploy key was rotated yesterday",
                category=None,
                tags=None,
                source="test",
                created_at=None,
                updated_at=None,
            )
        ]
    )
    engine = RAGEngine()
    engine.embedding_service = cast(
        EmbeddingService, FakeEmbeddingService([0.1, 0.2])
    )
    engine.vector_store = cast(VectorStore, FakeVectorStore([]))
    engine.memory_store = cast(MemoryStore, memory)
    engine.llm_client = cast(
        LLMClient,
        FakeLLMClient(response="The deploy key was rotated yesterday [M1]."),
    )
    engine.maintenance_mode = True
    engine._get_indexed_file_ids = lambda vault_id: None

    with patch.object(settings, "query_transformation_enabled", False):
        results = [
            msg
            async for msg in engine.query("what is the deploy key?", [], stream=False)
        ]

    done = [r for r in results if isinstance(r, dict) and r.get("type") == "done"]
    assert done, "expected a done payload"
    # PRR-015 feedback-round correction (reviewer probe): the assertion must
    # be UNCONDITIONAL — the earlier draft gated it on memories_used, which
    # was always empty because the fixture overrode the wrong method
    # (`search` instead of `search_memories`).
    assert done[0].get("memories_used"), (
        "the offered memory must reach memories_used (cited by the answer)"
    )
    contract = done[0].get("answer_contract") or {}
    assert contract.get("abstention_basis") == "decision", (
        "a cited answer under degradation is a real decision"
    )


@pytest.mark.asyncio
async def test_agentic_maintenance_falls_through_to_standard_fallback():
    """PRR-003 (feedback): agentic_rag_enabled + maintenance_mode falls
    through to the standard pipeline's maintenance handling — a fallback
    chunk with the notice and an 'unavailable' no-evidence basis — instead
    of a normal-looking ungrounded synthesis."""
    engine = RAGEngine()
    engine.embedding_service = cast(
        EmbeddingService, FakeEmbeddingService([0.1, 0.2])
    )
    engine.vector_store = cast(VectorStore, FakeVectorStore([]))
    engine.memory_store = cast(MemoryStore, FakeMemoryStore())
    engine.llm_client = cast(
        LLMClient, FakeLLMClient(response="nothing retrieved.")
    )
    engine.maintenance_mode = True
    engine._get_indexed_file_ids = lambda vault_id: None

    with patch.object(settings, "agentic_rag_enabled", True), patch.object(
        settings, "query_transformation_enabled", False
    ):
        results = [
            msg async for msg in engine.query("deep question?", [], stream=False)
        ]

    assert not any(
        r.get("answer_source_mode") == "agentic"
        for r in results
        if isinstance(r, dict)
    ), "agentic path must not answer under maintenance"
    fallback_chunks = [
        r for r in results if isinstance(r, dict) and r.get("type") == "fallback"
    ]
    assert fallback_chunks, "standard-pipeline fallback notice must appear"
    done = [r for r in results if isinstance(r, dict) and r.get("type") == "done"]
    contract = (done[0].get("answer_contract") or {}) if done else {}
    assert contract.get("abstention_basis") == "unavailable"


# PRR-017 (feedback, RECORD disposition): the frozen check
# test_c10_multiscale_timeout.py depends on store.table.count_rows()==0 so
# `_vector_index_needs_creation()` short-circuits before `_acquire_write_lock`
# reads the patched-settings MagicMock `write_lock_timeout_seconds` (a real
# read there raises TypeError under the patch). The dependency is inherent to
# the wholesale settings patch; the frozen file would need a CHECK_WRONG
# AMEND to document it in place, so the note lives here.


@pytest.mark.asyncio
async def test_semantic_mode_hybrid_status_is_disabled():
    """PRR-001 (feedback): retrieval_mode='semantic' with hybrid globally
    enabled reports hybrid_status 'disabled' — the effective per-query flag
    is authoritative (the former second derivation is gone)."""
    from test_chat_mode_dispatch import _make_engine_for_retrieval

    engine = _make_engine_for_retrieval()
    engine.hybrid_search_enabled = True
    engine.reranking_enabled = False
    engine.maintenance_mode = False
    with patch("app.services.rag_engine.settings") as mock:
        mock.maintenance_mode = False
        mock.hybrid_search_enabled = True
        mock.reranking_enabled = False
        mock.retrieval_evaluation_enabled = False
        mock.context_max_tokens = 0
        mock.retrieval_recency_weight = 0.0
        mock.retrieval_top_k = 10
        result = await engine._execute_retrieval(
            [("original", [0.1, 0.2, 0.3])],
            "question",
            vault_id=None,
            retrieval_mode="semantic",
        )
    assert result[5] == "disabled", (
        f"semantic mode must report hybrid_status 'disabled', got {result[5]!r}"
    )
    assert result[7] == "disabled", "rerank never attempted -> 'disabled'"


@pytest.mark.asyncio
async def test_non_stream_route_appends_fallback_notice():
    """T1-32-K-01 companion (#715): the non-streaming collector must append
    the fallback notice to the visible answer, mirroring the stream route
    (chat.py fallback branch), so both surfaces behave identically."""
    notice = (
        "Retrieval is degraded, so this answer is not grounded in "
        "retrieved documents.\n\n"
    )

    async def _fallback_then_done():
        yield {"type": "fallback", "reason": "maintenance", "content": notice}
        yield {"type": "done", "sources": [], "memories_used": []}

    engine = MagicMock()
    engine.query.return_value = _fallback_then_done()
    result = await non_stream_chat_response(
        message="hello", history=[], rag_engine=engine
    )
    assert notice.strip() in result.content, (
        "non-streaming response must include the degraded-retrieval notice"
    )
    # The separator survives the JOIN with answer text: the engine emits
    # the notice with a trailing blank line and collectors join with an
    # empty separator, so answer characters never glue onto the notice's
    # last word (feedback: "...documents.I cannot").


class _SeqLLM:
    """Fake LLM returning queued responses (planner decision JSON, then
    synthesis text) and recording every message payload it receives."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.messages_log = []

    async def chat_completion(self, messages, **kwargs):
        self.messages_log.append(messages)
        return self._responses.pop(0)


async def test_agentic_wire_chunks_never_carry_chunk_text():
    """TQ-instruction-batch-4-07 guardrail (#715): RetrievalTool attaches a
    private `_chunk_text` evidence channel for SynthesisTool; NO chunk the
    agentic path yields (done payload sources AND evidence_candidates) may
    carry it — both boundaries share the same dicts and the engine strips
    the key in place before the first yield. PRR-011 (feedback): the test
    also asserts the agentic branch was actually taken (answer_source_mode)
    and an evidence_candidates chunk was produced, so a fall-through to the
    standard pipeline cannot pass vacuously."""
    engine = RAGEngine()
    engine.embedding_service = cast(
        EmbeddingService, FakeEmbeddingService([0.1, 0.2, 0.3])
    )
    long_text = ("agentic evidence sentence. " * 40).strip()
    engine.vector_store = cast(
        VectorStore,
        FakeVectorStore(
            [
                {
                    "id": "f1_0",
                    "file_id": "f1",
                    "text": long_text,
                    "_distance": 0.1,
                    "metadata": {"raw_text": long_text, "source_file": "doc.txt"},
                }
            ]
        ),
    )
    engine.memory_store = cast(MemoryStore, FakeMemoryStore())
    seq_llm = _SeqLLM(['{"action": "synthesize"}', "synthesized answer"])
    engine.llm_client = cast(LLMClient, seq_llm)
    engine._get_indexed_file_ids = lambda vault_id: None

    with patch.object(settings, "agentic_rag_enabled", True), patch.object(
        settings, "query_transformation_enabled", False
    ):
        results = [
            msg async for msg in engine.query("deep question?", [], stream=False)
        ]

    # The retrieval really happened and the private channel was populated
    # upstream (otherwise this guardrail would be vacuous).
    decision_calls = [m for m in seq_llm.messages_log]
    assert decision_calls, "planner LLM was never called — agentic path not driven"
    done_chunks = [r for r in results if r.get("type") == "done"]
    assert done_chunks and done_chunks[0].get("sources"), (
        "agentic done payload must carry the retrieved source — "
        "guardrail needs a populated wire to be meaningful"
    )
    # PRR-011: the AGENTIC branch must have produced this done payload — a
    # fall-through to the standard pipeline would otherwise pass vacuously.
    assert done_chunks[0].get("answer_source_mode") == "agentic", (
        f"expected the agentic branch, got answer_source_mode="
        f"{done_chunks[0].get('answer_source_mode')!r}"
    )
    candidates = [
        r for r in results
        if isinstance(r, dict) and r.get("type") == "evidence_candidates"
    ]
    assert candidates, (
        "evidence_candidates chunk must be produced on the agentic path — "
        "without it the second wire boundary is never exercised"
    )
    for chunk in results:
        if chunk.get("type") == "done":
            for src in chunk.get("sources", []):
                assert "_chunk_text" not in src, (
                    f"_chunk_text leaked on done.sources: {sorted(src)}"
                )
        if chunk.get("type") == "evidence_candidates":
            for cand in chunk.get("candidates", []):
                assert "_chunk_text" not in cand, (
                    f"_chunk_text leaked on evidence_candidates: {sorted(cand)}"
                )


class _CapturingLLM:
    def __init__(self):
        self.messages_log = []

    async def chat_completion(self, messages, **kwargs):
        self.messages_log.extend(messages)
        return "answer"


@pytest.mark.asyncio
async def test_synthesis_evidence_budget_caps():
    """TQ-instruction-batch-4-07 guardrail (#715): the SynthesisTool evidence
    block shares an 8000-char global budget proportionally so EVERY source
    contributes evidence (feedback: first-come dropped later rounds), with a
    300-char per-source floor that takes priority beyond TOTAL//FLOOR
    sources — so the total is bounded by max(8000, 300 x n), never above the
    old 300-char-preview envelope. PRR-015-feedback review round 1: the
    floor-priority regime (n >= 27) is pinned explicitly."""
    from app.services.agentic_tools import (
        _SYNTHESIS_EVIDENCE_FLOOR_CHARS,
        _SYNTHESIS_EVIDENCE_MAX_CHARS,
        _SYNTHESIS_EVIDENCE_TOTAL_CHARS,
        SynthesisTool,
    )

    async def _run(n):
        sources = [
            {"id": f"s{i}", "snippet": "s" * 3000, "_chunk_text": "y" * 3000}
            for i in range(n)
        ]
        llm = _CapturingLLM()
        tool = SynthesisTool(llm_client=llm)
        result = await tool.execute(text="question?", sources=sources)
        assert result.success is True
        user_prompt = llm.messages_log[-1]["content"]
        passages = re.findall(
            r"<source_passages>(.*?)</source_passages>", user_prompt, re.DOTALL
        )
        return sources, passages

    # Budget-priority regime (n <= 26): total within the 8000-char budget.
    sources, passages = await _run(12)
    total = sum(len(p) for p in passages)
    assert len(passages) == len(sources), "no source dropped (n=12)"
    assert total <= _SYNTHESIS_EVIDENCE_TOTAL_CHARS, (
        f"global budget exceeded in budget regime: {total}"
    )
    assert max(len(p) for p in passages) <= _SYNTHESIS_EVIDENCE_MAX_CHARS

    # Floor-priority regime (n >= 27): every source still keeps the 300-char
    # preview floor; the total is bounded by 300 x n — the old preview
    # envelope — NOT by 8000 (reviewer round 1: the unqualified "<=8000"
    # claim was false here; this pin matches actual behavior).
    sources30, passages30 = await _run(30)
    total30 = sum(len(p) for p in passages30)
    assert len(passages30) == 30, "no source dropped in floor regime"
    assert max(len(p) for p in passages30) == _SYNTHESIS_EVIDENCE_FLOOR_CHARS
    assert total30 <= _SYNTHESIS_EVIDENCE_FLOOR_CHARS * 30, (
        f"floor-regime total {total30} exceeds the 300x30 preview envelope"
    )


@pytest.mark.asyncio
async def test_maintenance_gate_surfaces_unavailable_outcome():
    """PRR-013 (feedback): under maintenance, retrieve_eval_results must
    surface RetrievalOutcome(status='unavailable') — not just zero search
    calls (a wrong 'ok/empty' outcome used to keep the count-only check
    green)."""
    from test_issue494_rag_engine import (
        CountingLLMClient,
        CountingVectorStore,
        FakeEmbeddingService,
        FakeMemoryStore,
    )

    engine = RAGEngine()
    engine.embedding_service = cast(EmbeddingService, FakeEmbeddingService())
    store = CountingVectorStore()
    engine.vector_store = cast(VectorStore, store)
    engine.memory_store = cast(MemoryStore, FakeMemoryStore())
    engine.llm_client = cast(LLMClient, CountingLLMClient())
    engine.maintenance_mode = True
    engine._get_indexed_file_ids = lambda vault_id: None

    outcome = await engine.retrieve_eval_results("test query", vault_id=1)
    assert getattr(outcome, "status", None) == "unavailable", (
        f"maintenance must surface status='unavailable', got {outcome!r}"
    )
    assert len(store.search_calls) == 0


@pytest.mark.asyncio
async def test_retrieve_sources_records_document_kind_failed_under_maintenance():
    """External review tests-falsifiability-04 (#715 feedback): no
    maintenance test drove retrieve_sources — its 'document' kind must be
    recorded as failed (never a silent empty success)."""
    engine = RAGEngine()
    engine.embedding_service = cast(
        EmbeddingService, FakeEmbeddingService([0.1, 0.2])
    )
    engine.vector_store = cast(VectorStore, FakeVectorStore([]))
    engine.memory_store = cast(MemoryStore, FakeMemoryStore())
    engine.llm_client = cast(LLMClient, FakeLLMClient(response="x"))
    engine.maintenance_mode = True
    engine._get_indexed_file_ids = lambda vault_id: None

    result = await engine.retrieve_sources("question", vault_id=1, limit=5)
    assert "document" in result.failed_kinds, (
        f"'document' kind must be recorded failed under maintenance: "
        f"status={result.status!r} failed_kinds={sorted(result.failed_kinds)}"
    )


@pytest.mark.asyncio
async def test_rerank_exception_maps_to_fallback_exactly():
    """External review test-strength item (#715 feedback): the single-query
    rerank-exception path pins the EXACT value 'fallback' (a mutant mapping
    it to 'ok' survived the frozen != check)."""
    from test_rag_engine import (
        FakeEmbeddingService,
        FakeLLMClient,
        FakeMemoryStore,
        FakeVectorStore,
    )

    class FailingReranker:
        async def rerank(self, *args, **kwargs):
            raise RuntimeError("Reranker service unavailable")

    engine = RAGEngine()
    engine.embedding_service = cast(
        EmbeddingService, FakeEmbeddingService([0.1, 0.2])
    )
    engine.vector_store = cast(
        VectorStore,
        FakeVectorStore(
            [
                {
                    "id": "f1_0",
                    "file_id": "f1",
                    "text": "rerank probe text",
                    "_distance": 0.1,
                    "metadata": {},
                }
            ]
        ),
    )
    engine.memory_store = cast(MemoryStore, FakeMemoryStore())
    engine.llm_client = cast(LLMClient, FakeLLMClient(response="x"))
    engine.reranking_enabled = True
    engine.reranking_service = FailingReranker()
    engine._get_indexed_file_ids = lambda vault_id: None

    result = await engine._execute_retrieval(
        [("original", [0.1, 0.2])], "question", vault_id=None
    )
    assert result[7] == "fallback", (
        f"attempted-and-failed rerank must map to 'fallback', got {result[7]!r}"
    )
