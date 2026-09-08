"""RAG-DEEP-01 (issue #511 B2 PR2): lazy variant embeddings in RAGEngine.query().

Regression tests for the plan-first lazy-embedding contract:

1. Decomposition route (``len(plan) > 1``): only the original retrieval
   query is embedded — the step_back/hyde variant texts are never embedded
   (the multi-sub-query orchestration embeds each sub-query itself and
   never consumes the variant embeddings), and a sub-query whose text
   equals the original retrieval query REUSES the original embedding so
   each distinct string is embedded exactly once.
2. Standard route (single-item plan): all transformed variants are
   embedded and searched exactly as before the fix (preserving half).
3. The original-query embedding failure guard still fires with the same
   user-visible EMBEDDING_ERROR behavior, now placed after the
   wiki/raw-RAG decision so it only fires when raw RAG will actually run.
4. FULL-ENH-01 (finish_reason surfacing): the active client's
   ``last_metrics["finish_reason"]`` is logged at INFO (no content) and
   recorded on the RAG trace's additive ``finish_reason`` field after
   generation, on both the stream and non-stream paths.

Fixture patterns follow backend/tests/test_query_orchestration.py (engine
construction under assert_url_safe patches, per-instance engine setting
overrides, ``_get_indexed_file_ids`` stub, fake transformer/planner
injected through the engine's per-client caches) and the fake providers of
backend/tests/test_rag_engine.py.
"""

import contextlib
import logging
import os
import sys
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

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
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType("unstructured.partition")
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType("unstructured.partition.auto")
    _unstructured.partition.auto.__path__ = []
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    sys.modules["unstructured"] = _unstructured
    sys.modules["unstructured.partition"] = _unstructured.partition
    sys.modules["unstructured.partition.auto"] = _unstructured.partition.auto

from app.services.rag_engine import RAGEngine, RAGEngineError


# ------------------------------------------------------------------
# Fake providers (production query() drives all of them)
# ------------------------------------------------------------------
class RecordingEmbeddingService:
    """Fake embedding provider recording every call's method + text.

    Hands out a distinct, invertible vector per distinct text so the fake
    vector store can identify which query text each search used.
    """

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str]] = []
        self._text_to_vec: Dict[str, List[float]] = {}
        self.vec_to_text: Dict[Tuple[float, ...], str] = {}
        self._seq = 0

    def _vec_for(self, text: str) -> List[float]:
        if text not in self._text_to_vec:
            self._seq += 1
            vec = [float(self._seq), 0.25, 0.5]
            self._text_to_vec[text] = vec
            self.vec_to_text[tuple(vec)] = text
        return list(self._text_to_vec[text])

    async def embed_single(self, text: str) -> List[float]:
        self.calls.append(("embed_single", text))
        return self._vec_for(text)

    async def embed_passage(self, text: str) -> List[float]:
        self.calls.append(("embed_passage", text))
        return self._vec_for(text)

    def embedded_texts(self) -> List[str]:
        return [text for _method, text in self.calls]

    def count_for(self, text: str) -> int:
        return sum(1 for _m, t in self.calls if t == text)


class FakeVectorStore:
    """Fake vector store returning one chunk per searched query text."""

    def __init__(self, emb: RecordingEmbeddingService) -> None:
        self.emb = emb
        self.searched_texts: List[Optional[str]] = []

    async def search(
        self,
        embedding: List[float],
        limit: int = 10,
        vault_id=None,
        query_text=None,
        hybrid: bool = False,
        hybrid_alpha: float = 0.5,
        filter_expr=None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        mapped = self.emb.vec_to_text.get(tuple(embedding))
        self.searched_texts.append(mapped)
        return [
            {
                "id": f"chunk_for_{abs(hash(mapped or 'unknown')) % 10000}",
                "text": f"content retrieved for query {mapped or 'unknown'}",
                "file_id": "file_doc_1",
                "_distance": 0.10,
                "metadata": {},
            }
        ][:limit]

    def get_fts_exceptions(self) -> int:
        return 0


class FakeMemoryStore:
    def detect_memory_intent(self, text: str) -> Optional[str]:
        return None

    def search_memories(self, query, limit=5, vault_id=None, include_global=False):
        return []


class FakeLLMClient:
    """Fake LLM (mode client + answer generator) with inspectable metrics."""

    base_url = "http://fake-llm.test"

    def __init__(self, response: str = "test answer without citations.") -> None:
        self._response = response
        self.last_metrics: Dict[str, Any] = {}

    async def chat_completion(self, messages, **kwargs) -> str:
        return self._response

    async def chat_completion_stream(self, messages, **kwargs):
        yield self._response


class FakeTransformer:
    """Fake QueryTransformer returning fixed (variant_type, text) tuples."""

    def __init__(self, variants: List[Tuple[str, str]]) -> None:
        self._variants = variants

    async def transform(self, query: str) -> List[Tuple[str, str]]:
        return list(self._variants)


class FakePlanner:
    """Fake QueryPlanner returning a fixed sub-query plan."""

    def __init__(self, plan: List[str]) -> None:
        self._plan = plan

    async def plan(self, query: str) -> List[str]:
        return list(self._plan)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _patch_ssrf():
    """Prevent SSRF guard from blocking service construction in tests."""
    with patch("app.services.embeddings.assert_url_safe"), \
         patch("app.services.llm_client.assert_url_safe"):
        yield


@contextlib.contextmanager
def _settings(**overrides):
    """Patch live settings for the duration of one query() drive."""
    from app.config import settings

    values = {
        "redis_url": "",
        "query_transformation_enabled": True,
        "memory_retrieval_enabled": False,
        "context_distillation_enabled": False,
        "context_max_tokens": 0,
        "parent_retrieval_enabled": False,
        "retrieval_evaluation_enabled": False,
        "agentic_rag_enabled": False,
        "kms_enabled": False,
        "maintenance_mode": False,
        "instant_skip_followup_rewrite": False,
        "instant_skip_query_transformation": False,
        "retrieval_recency_weight": 0.0,
    }
    values.update(overrides)
    with contextlib.ExitStack() as stack:
        for key, value in values.items():
            stack.enter_context(patch.object(settings, key, value))
        yield settings


def _make_engine(
    emb: RecordingEmbeddingService,
    vs: FakeVectorStore,
    llm: FakeLLMClient,
    plan: List[str],
    variants: List[Tuple[str, str]],
) -> RAGEngine:
    """Build a RAGEngine wired with fakes (fixture per test_query_orchestration)."""
    engine = RAGEngine(
        embedding_service=emb,
        vector_store=vs,
        memory_store=FakeMemoryStore(),
        llm_client=llm,
    )
    # Per-instance overrides shadow live settings reads (engine property
    # contract, same as test_query_orchestration.py does).
    engine.retrieval_top_k = 10
    engine.initial_retrieval_top_k = 10
    engine.reranker_top_n = 10
    engine.hybrid_search_enabled = False
    engine.reranking_enabled = False
    engine.max_distance_threshold = None
    engine.relevance_threshold = None
    engine.retrieval_window = 0
    engine._get_indexed_file_ids = lambda vault_id=None: None

    async def _no_supersession(sources):
        return None

    engine._check_supersession = _no_supersession
    # Inject fake transformer/planner through the engine's own per-client
    # caches so production query() fetches them via its getters unchanged.
    engine._query_transformers[id(llm)] = FakeTransformer(variants)
    engine._query_planners[id(llm)] = FakePlanner(plan)
    return engine


async def _drive(engine: RAGEngine, user_input: str, stream: bool = False) -> List[Dict[str, Any]]:
    msgs: List[Dict[str, Any]] = []
    async for m in engine.query(user_input, [], stream=stream):
        msgs.append(m)
    return msgs


# ------------------------------------------------------------------
# RAG-DEEP-01: decomposition route skips variant embeddings
# ------------------------------------------------------------------
async def test_decomposition_route_embeds_only_original_and_reuses_for_equal_subquery():
    """plan=[Q, S2]: step_back/hyde never embedded; Q embedded exactly once
    (sub-query equal to the original reuses the original embedding); S2
    embedded once by the orchestration."""
    Q = "compare the pricing model and the security model of the platform"
    SB = "broader conceptual question about product evaluation aspects"
    HY = "hypothetical document passage discussing pricing and security models"
    S2 = "sub-query about the security model facet specifically"

    emb = RecordingEmbeddingService()
    vs = FakeVectorStore(emb)
    llm = FakeLLMClient()
    engine = _make_engine(
        emb, vs, llm,
        plan=[Q, S2],
        variants=[("original", Q), ("step_back", SB), ("hyde", HY)],
    )

    with _settings():
        msgs = await _drive(engine, Q)

    done = next((m for m in msgs if m.get("type") == "done"), None)
    assert done is not None, f"query() never produced a done message: {msgs}"
    assert done.get("fusion_used") is True, (
        "fixture integrity: the decomposition route must run "
        f"(done.fusion_used={done.get('fusion_used')!r})"
    )

    texts = emb.embedded_texts()
    assert SB not in texts, (
        f"step_back variant text must not be embedded on the decomposition "
        f"route (embedded: {texts})"
    )
    assert HY not in texts, (
        f"hyde variant text must not be embedded on the decomposition route "
        f"(embedded: {texts})"
    )
    assert emb.count_for(Q) == 1, (
        "original query text must be embedded exactly once on the "
        "decomposition route (sub-query equal to the original must REUSE "
        f"the original embedding); embedded {emb.count_for(Q)}x: {texts}"
    )
    assert S2 in texts, (
        f"sub-query S2 must be embedded by the orchestration; embedded: {texts}"
    )


# ------------------------------------------------------------------
# RAG-DEEP-01 (preserving): standard route still embeds all variants
# ------------------------------------------------------------------
async def test_standard_route_embeds_all_variants_and_searches_each():
    """plan=[Q] (no decomposition): every variant is embedded as today and
    each variant embedding drives its own vector search."""
    Q = "describe the pricing model of the platform"
    SB = "broader question about how the product is priced"
    HY = "hypothetical passage explaining the pricing model"

    emb = RecordingEmbeddingService()
    vs = FakeVectorStore(emb)
    llm = FakeLLMClient()
    engine = _make_engine(
        emb, vs, llm,
        plan=[Q],
        variants=[("original", Q), ("step_back", SB), ("hyde", HY)],
    )

    with _settings():
        msgs = await _drive(engine, Q)

    done = next((m for m in msgs if m.get("type") == "done"), None)
    assert done is not None, f"query() never produced a done message: {msgs}"

    texts = emb.embedded_texts()
    for label, text in (("original", Q), ("step_back", SB), ("hyde", HY)):
        assert emb.count_for(text) == 1, (
            f"standard route must embed variant {label} exactly once; "
            f"embedded: {texts}"
        )
    searched = [t for t in vs.searched_texts if t is not None]
    assert len(searched) >= 3, (
        "standard route must search with every variant embedding; "
        f"searched_texts={vs.searched_texts}"
    )


# ------------------------------------------------------------------
# RAG-DEEP-01: error guard fires only when raw RAG will run
# ------------------------------------------------------------------
class _FailingEmbeddingService:
    """Fails every embed call (original included)."""

    def __init__(self) -> None:
        self.calls: List[str] = []

    async def embed_single(self, text: str) -> List[float]:
        from app.services.embeddings import EmbeddingError

        self.calls.append(text)
        raise EmbeddingError("Original query embed failed")

    async def embed_passage(self, text: str) -> List[float]:
        from app.services.embeddings import EmbeddingError

        self.calls.append(text)
        raise EmbeddingError("Original query embed failed")


async def test_original_embedding_failure_yields_embedding_error_on_decomposition_route():
    """On the decomposition route the original-only embed failure must still
    surface EMBEDDING_ERROR (stream) with no searches performed."""
    from app.services.embeddings import EmbeddingError  # noqa: F401 — contract ref

    Q = "compare the pricing model and the security model of the platform"
    S2 = "sub-query about the security model facet specifically"

    emb = _FailingEmbeddingService()
    vs = FakeVectorStore(RecordingEmbeddingService())  # never searched
    llm = FakeLLMClient()
    engine = _make_engine(
        emb, vs, llm,
        plan=[Q, S2],
        variants=[("original", Q), ("step_back", "broader"), ("hyde", "passage")],
    )

    with _settings():
        msgs = await _drive(engine, Q, stream=True)

    error_chunks = [m for m in msgs if m.get("type") == "error"]
    assert len(error_chunks) == 1, f"expected exactly one error chunk, got: {msgs}"
    assert error_chunks[0]["code"] == "EMBEDDING_ERROR"
    assert "Original query embedding failed" in error_chunks[0]["message"]
    assert vs.searched_texts == [], (
        "no vector search may run when the original embedding failed before "
        f"retrieval; searched_texts={vs.searched_texts}"
    )


async def test_original_embedding_failure_raises_non_stream():
    """Non-stream decomposition route: original embed failure raises
    RAGEngineError (user-visible behavior identical to the standard path)."""
    Q = "compare the pricing model and the security model of the platform"
    S2 = "sub-query about the security model facet specifically"

    emb = _FailingEmbeddingService()
    vs = FakeVectorStore(RecordingEmbeddingService())
    llm = FakeLLMClient()
    engine = _make_engine(
        emb, vs, llm,
        plan=[Q, S2],
        variants=[("original", Q)],
    )

    with _settings():
        with pytest.raises(RAGEngineError, match="Original query embedding failed"):
            await _drive(engine, Q, stream=False)


# ------------------------------------------------------------------
# FULL-ENH-01: finish_reason surfacing onto log + trace
# ------------------------------------------------------------------
async def test_finish_reason_surfaced_on_trace_non_stream(caplog):
    """Non-stream: last_metrics['finish_reason']='length' is logged at INFO
    (no content) and recorded on the trace embedded in the done message."""
    from app.services.rag_trace import RAGTrace  # noqa: F401 — trace contract

    Q = "describe the pricing model of the platform"

    emb = RecordingEmbeddingService()
    vs = FakeVectorStore(emb)
    llm = FakeLLMClient(response="partial answer cut at the token limit")
    llm.last_metrics = {"finish_reason": "length"}
    engine = _make_engine(emb, vs, llm, plan=[Q], variants=[("original", Q)])

    with _settings(rag_trace_in_response=True):
        with caplog.at_level(logging.INFO, logger="app.services.rag_engine"):
            msgs = await _drive(engine, Q, stream=False)

    done = next((m for m in msgs if m.get("type") == "done"), None)
    assert done is not None, f"query() never produced a done message: {msgs}"
    trace = done.get("trace") or {}
    assert trace.get("finish_reason") == "length", (
        f"trace.finish_reason must surface the client's finish_reason; trace={trace}"
    )
    assert any(
        "finish_reason=length" in rec.getMessage() for rec in caplog.records
    ), (
        "finish_reason must be logged at INFO without content; records="
        f"{[r.getMessage() for r in caplog.records]}"
    )


async def test_finish_reason_surfaced_on_trace_stream(caplog):
    """Stream path: finish_reason from the streamed client's last_metrics
    reaches the trace too (same read point covers both paths)."""
    Q = "describe the pricing model of the platform"

    emb = RecordingEmbeddingService()
    vs = FakeVectorStore(emb)
    llm = FakeLLMClient(response="streamed partial answer cut at the limit")
    llm.last_metrics = {"finish_reason": "length"}
    engine = _make_engine(emb, vs, llm, plan=[Q], variants=[("original", Q)])

    with _settings(rag_trace_in_response=True):
        with caplog.at_level(logging.INFO, logger="app.services.rag_engine"):
            msgs = await _drive(engine, Q, stream=True)

    done = next((m for m in msgs if m.get("type") == "done"), None)
    assert done is not None, f"query() never produced a done message: {msgs}"
    trace = done.get("trace") or {}
    assert trace.get("finish_reason") == "length"


async def test_finish_reason_absent_leaves_trace_default():
    """Without a finish_reason in client metrics the trace field stays at its
    additive default (None) and nothing is logged."""
    Q = "describe the pricing model of the platform"

    emb = RecordingEmbeddingService()
    vs = FakeVectorStore(emb)
    llm = FakeLLMClient(response="complete answer")
    engine = _make_engine(emb, vs, llm, plan=[Q], variants=[("original", Q)])

    with _settings(rag_trace_in_response=True):
        msgs = await _drive(engine, Q)

    done = next((m for m in msgs if m.get("type") == "done"), None)
    assert done is not None
    trace = done.get("trace") or {}
    assert trace.get("finish_reason") is None
