"""RAG-DEEP-02 (issue #511 B2 PR2): consolidate-before-rerank with facet
preservation in the multi-sub-query orchestration.

Regression tests:

1. With ``retrieval_consolidated_rerank`` enabled (default), the fan-out
   sub-query retrievals run WITHOUT per-sub-query rerank and exactly ONE
   rerank call runs over the consolidated, identity-deduped, capped fused
   set — the duplicate shared chunk is reranked once, every facet chunk
   survives, and score_type/rerank_status derive from that single call.
2. Flag off (rollback): per-sub-query rerank behavior is preserved
   exactly (one rerank call per sub-query).
3. A failing consolidated rerank call degrades to the fused order with
   score_type="distance" / rerank_status="disabled" (same semantics as
   ``_execute_retrieval``'s rerank exception fallback).
4. Identical plan strings are deduplicated before fan-out (order
   preserved), so a repeated sub-query costs no extra embedding, search,
   or rerank.

Fixture: chunk A is returned by BOTH S1 and S2 (identical file_id + text
-> same ``source_dedup_key`` identity), and distinct chunks B (S1 only),
C (S2 only), D (S3 only). The fake reranker mirrors the production
``RerankingService.rerank`` contract and scores B > C > D > A so a single
consolidated call with top_n=3 returns exactly the facet set {B, C, D}.

Fixture patterns follow backend/tests/test_query_orchestration.py.
"""

import contextlib
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

from app.services.rag_engine import RAGEngine

# ------------------------------------------------------------------
# Chunk fixture: A is shared by S1+S2 (duplicate), B/C/D per-facet unique
# ------------------------------------------------------------------
CHUNK_A = {
    "id": "chunkA",
    "text": "shared chunk covering both facet one and facet two",
    "file_id": "fileA",
    "_distance": 0.11,
    "metadata": {},
}
CHUNK_B = {
    "id": "chunkB",
    "text": "chunk exclusively about facet one",
    "file_id": "fileB",
    "_distance": 0.12,
    "metadata": {},
}
CHUNK_C = {
    "id": "chunkC",
    "text": "chunk exclusively about facet two",
    "file_id": "fileC",
    "_distance": 0.13,
    "metadata": {},
}
CHUNK_D = {
    "id": "chunkD",
    "text": "chunk exclusively about facet three",
    "file_id": "fileD",
    "_distance": 0.14,
    "metadata": {},
}
EXPECTED_FACETS = {CHUNK_B["file_id"], CHUNK_C["file_id"], CHUNK_D["file_id"]}


# ------------------------------------------------------------------
# Fake providers
# ------------------------------------------------------------------
class RecordingEmbeddingService:
    """Distinct invertible vector per distinct text; records every call."""

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

    def count_for(self, text: str) -> int:
        return sum(1 for _m, t in self.calls if t == text)


class FacetVectorStore:
    """Returns per-sub-query chunk sets keyed by the searched query text."""

    def __init__(self, emb: RecordingEmbeddingService, per_query: Dict[str, List[Dict[str, Any]]]) -> None:
        self.emb = emb
        self.per_query = per_query
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
        chunks = self.per_query.get(mapped, [])
        return [dict(c) for c in chunks[:limit]]

    def get_fts_exceptions(self) -> int:
        return 0


class RecordingRerankingService:
    """Fake reranker mirroring the production rerank() contract.

    Scores by a fixed per-file_id priority (B > C > D > A), sorts
    descending, trims to top_n, attaches ``_rerank_score`` and returns
    ``(chunks, success=True)``. Every call records its query text, the
    identity keys of the chunk set (via the REAL ``source_dedup_key``),
    chunk count and top_n.
    """

    PRIORITY = {"fileB": 0.9, "fileC": 0.8, "fileD": 0.7, "fileA": 0.1}

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        from app.services.document_retrieval import source_dedup_key

        self._identity = lambda chunk: source_dedup_key(
            chunk.get("file_id", ""), chunk.get("text", ""), chunk.get("metadata")
        )

    async def rerank(
        self,
        query: str,
        chunks: List[Dict[str, Any]],
        top_n: Optional[int] = None,
    ) -> Tuple[List[Dict[str, Any]], bool]:
        n = top_n or len(chunks)
        self.calls.append(
            {
                "query": query,
                "identities": [self._identity(c) for c in chunks],
                "chunk_count": len(chunks),
                "top_n": n,
            }
        )
        scored = sorted(
            enumerate(chunks),
            key=lambda pair: self.PRIORITY.get(pair[1].get("file_id"), 0.0),
            reverse=True,
        )
        result = []
        for idx, _chunk in scored[:n]:
            chunk = dict(chunks[idx])
            chunk["_rerank_score"] = self.PRIORITY.get(chunk.get("file_id"), 0.0)
            result.append(chunk)
        return result, True


class FailingRerankingService:
    """Always raises — mirrors a reranker outage on the consolidated call."""

    def __init__(self) -> None:
        self.calls = 0

    async def rerank(self, query, chunks, top_n=None):
        self.calls += 1
        raise RuntimeError("Reranker service unavailable")


class FakeMemoryStore:
    def detect_memory_intent(self, text: str) -> Optional[str]:
        return None

    def search_memories(self, query, limit=5, vault_id=None, include_global=False):
        return []


class FakeLLMClient:
    base_url = "http://fake-llm.test"

    def __init__(self, response: str = "consolidated answer without citations.") -> None:
        self._response = response

    async def chat_completion(self, messages, **kwargs) -> str:
        return self._response

    async def chat_completion_stream(self, messages, **kwargs):
        yield self._response


class FakeTransformer:
    def __init__(self, variants: List[Tuple[str, str]]) -> None:
        self._variants = variants

    async def transform(self, query: str) -> List[Tuple[str, str]]:
        return list(self._variants)


class FakePlanner:
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
        "retrieval_recency_weight": 0.0,
    }
    values.update(overrides)
    with contextlib.ExitStack() as stack:
        for key, value in values.items():
            stack.enter_context(patch.object(settings, key, value))
        yield settings


def _make_engine(
    emb: RecordingEmbeddingService,
    vs: FacetVectorStore,
    llm: FakeLLMClient,
    plan: List[str],
    query: str,
    reranker: Any,
    reranker_top_n: int,
) -> RAGEngine:
    engine = RAGEngine(
        embedding_service=emb,
        vector_store=vs,
        memory_store=FakeMemoryStore(),
        llm_client=llm,
    )
    engine.retrieval_top_k = 10
    engine.initial_retrieval_top_k = 10
    engine.reranker_top_n = reranker_top_n
    engine.hybrid_search_enabled = False
    engine.reranking_enabled = True
    engine.reranking_service = reranker
    engine.max_distance_threshold = None
    engine.relevance_threshold = None
    engine.retrieval_window = 0
    engine._get_indexed_file_ids = lambda vault_id=None: None

    async def _no_supersession(sources):
        return None

    engine._check_supersession = _no_supersession
    engine._query_transformers[id(llm)] = FakeTransformer([("original", query)])
    engine._query_planners[id(llm)] = FakePlanner(plan)
    return engine


async def _drive(engine: RAGEngine, user_input: str) -> List[Dict[str, Any]]:
    msgs: List[Dict[str, Any]] = []
    async for m in engine.query(user_input, [], stream=False):
        msgs.append(m)
    return msgs


def _identity_counts(call: Dict[str, Any]) -> Dict[tuple, int]:
    counts: Dict[tuple, int] = {}
    for ident in call["identities"]:
        counts[ident] = counts.get(ident, 0) + 1
    return counts


# ------------------------------------------------------------------
# RAG-DEEP-02: exactly one consolidated rerank, facets preserved
# ------------------------------------------------------------------
async def test_single_consolidated_rerank_preserves_facets():
    """Flag on (default): exactly ONE rerank call over the identity-deduped
    fused set; duplicate A appears once in its input; facets B/C/D survive
    to the final evidence; score_type/rerank_status come from that call."""
    Q = "multi-facet question about facet one, facet two and facet three"
    S1 = "sub-query one about facet one"
    S2 = "sub-query two about facet two"
    S3 = "sub-query three about facet three"

    emb = RecordingEmbeddingService()
    vs = FacetVectorStore(emb, per_query={S1: [CHUNK_A, CHUNK_B], S2: [CHUNK_A, CHUNK_C], S3: [CHUNK_D]})
    reranker = RecordingRerankingService()
    llm = FakeLLMClient()
    engine = _make_engine(emb, vs, llm, plan=[S1, S2, S3], query=Q, reranker=reranker, reranker_top_n=3)

    with _settings():
        msgs = await _drive(engine, Q)

    done = next((m for m in msgs if m.get("type") == "done"), None)
    assert done is not None, f"query() never produced a done message: {msgs}"
    assert done.get("fusion_used") is True, (
        "fixture integrity: the multi-sub-query orchestration must run "
        f"(done.fusion_used={done.get('fusion_used')!r})"
    )

    assert len(reranker.calls) == 1, (
        "exactly ONE consolidated rerank call must run for the whole "
        f"orchestration; observed calls: {reranker.calls}"
    )
    single = reranker.calls[0]
    assert single["query"] == Q, (
        f"consolidated rerank must query the user input; got {single['query']!r}"
    )
    assert single["top_n"] == 3, f"top_n must be effective_reranker_top_n; got {single['top_n']}"

    counts = _identity_counts(single)
    a_key = ("chunk", CHUNK_A["file_id"], CHUNK_A["text"])
    assert counts.get(a_key, 0) == 1, (
        "duplicate chunk A must be identity-deduped BEFORE the consolidated "
        f"rerank (seen {counts.get(a_key, 0)}x; identities={single['identities']!r})"
    )
    for chunk in (CHUNK_B, CHUNK_C, CHUNK_D):
        key = ("chunk", chunk["file_id"], chunk["text"])
        assert counts.get(key, 0) == 1, (
            f"facet chunk {chunk['file_id']} missing from the consolidated "
            f"rerank input; identities={single['identities']!r}"
        )

    final_file_ids = {s.get("file_id") for s in done.get("sources", [])}
    assert final_file_ids == EXPECTED_FACETS, (
        f"facets lost after consolidation+rerank: {sorted(final_file_ids)} "
        f"vs expected {sorted(EXPECTED_FACETS)}"
    )
    assert done.get("score_type") == "rerank", (
        f"score_type must reflect the single successful rerank call; got "
        f"{done.get('score_type')!r}"
    )
    assert (done.get("retrieval_debug") or {}).get("rerank_status") == "ok", (
        f"rerank_status must be 'ok' for the single successful call; got "
        f"{(done.get('retrieval_debug') or {}).get('rerank_status')!r}"
    )


# ------------------------------------------------------------------
# RAG-DEEP-02 (rollback flag): per-sub-query rerank preserved
# ------------------------------------------------------------------
async def test_flag_off_keeps_per_sub_query_rerank():
    """retrieval_consolidated_rerank=False: each sub-query retrieval reranks
    its own sub-result exactly as before the fix (N calls, not 1)."""
    Q = "multi-facet question about facet one, facet two and facet three"
    S1 = "sub-query one about facet one"
    S2 = "sub-query two about facet two"
    S3 = "sub-query three about facet three"

    emb = RecordingEmbeddingService()
    vs = FacetVectorStore(emb, per_query={S1: [CHUNK_A, CHUNK_B], S2: [CHUNK_A, CHUNK_C], S3: [CHUNK_D]})
    reranker = RecordingRerankingService()
    llm = FakeLLMClient()
    engine = _make_engine(emb, vs, llm, plan=[S1, S2, S3], query=Q, reranker=reranker, reranker_top_n=3)

    with _settings(retrieval_consolidated_rerank=False):
        msgs = await _drive(engine, Q)

    done = next((m for m in msgs if m.get("type") == "done"), None)
    assert done is not None, f"query() never produced a done message: {msgs}"

    assert len(reranker.calls) == 3, (
        "flag off must keep the legacy per-sub-query rerank (3 calls for 3 "
        f"sub-queries); observed {len(reranker.calls)}: {reranker.calls}"
    )
    assert all(c["query"] == Q for c in reranker.calls)
    assert sorted(c["chunk_count"] for c in reranker.calls) == [1, 2, 2], (
        f"per-sub-query rerank inputs must be the per-facet sub-results; got "
        f"{[c['chunk_count'] for c in reranker.calls]}"
    )
    assert done.get("score_type") == "rerank"


# ------------------------------------------------------------------
# RAG-DEEP-02: consolidated rerank failure degrades honestly
# ------------------------------------------------------------------
async def test_consolidated_rerank_failure_falls_back_to_fused_order():
    """A reranker outage on the consolidated call keeps the fused order with
    score_type='distance' and rerank_status='disabled' (mirrors
    _execute_retrieval's exception semantics)."""
    Q = "multi-facet question about facet one, facet two and facet three"
    S1 = "sub-query one about facet one"
    S2 = "sub-query two about facet two"
    S3 = "sub-query three about facet three"

    emb = RecordingEmbeddingService()
    vs = FacetVectorStore(emb, per_query={S1: [CHUNK_A, CHUNK_B], S2: [CHUNK_A, CHUNK_C], S3: [CHUNK_D]})
    reranker = FailingRerankingService()
    llm = FakeLLMClient()
    engine = _make_engine(emb, vs, llm, plan=[S1, S2, S3], query=Q, reranker=reranker, reranker_top_n=3)

    with _settings():
        msgs = await _drive(engine, Q)

    done = next((m for m in msgs if m.get("type") == "done"), None)
    assert done is not None, f"query() never produced a done message: {msgs}"
    assert reranker.calls == 1, (
        f"exactly one (failed) consolidated rerank attempt expected; got {reranker.calls}"
    )
    final_file_ids = {s.get("file_id") for s in done.get("sources", [])}
    assert final_file_ids == {"fileA", "fileB", "fileC", "fileD"}, (
        f"fused evidence must survive the reranker outage; got {sorted(final_file_ids)}"
    )
    assert done.get("score_type") == "distance"
    assert (done.get("retrieval_debug") or {}).get("rerank_status") == "disabled"


# ------------------------------------------------------------------
# RAG-DEEP-02: identical plan strings deduped before fan-out
# ------------------------------------------------------------------
async def test_duplicate_plan_strings_deduped_before_fanout():
    """plan=[S1, S1, S2]: S1 is embedded and searched once (dedup before
    fan-out, order preserved) and the consolidated rerank input contains
    each chunk once."""
    Q = "multi-facet question about facet one and facet two"
    S1 = "sub-query one about facet one"
    S2 = "sub-query two about facet two"

    emb = RecordingEmbeddingService()
    vs = FacetVectorStore(emb, per_query={S1: [CHUNK_A], S2: [CHUNK_B]})
    reranker = RecordingRerankingService()
    llm = FakeLLMClient()
    engine = _make_engine(emb, vs, llm, plan=[S1, S1, S2], query=Q, reranker=reranker, reranker_top_n=5)

    with _settings():
        msgs = await _drive(engine, Q)

    done = next((m for m in msgs if m.get("type") == "done"), None)
    assert done is not None, f"query() never produced a done message: {msgs}"
    assert done.get("fusion_used") is True

    assert emb.count_for(S1) == 1, (
        f"identical plan strings must be deduped before fan-out (S1 embedded "
        f"{emb.count_for(S1)}x; calls={emb.calls})"
    )
    searched = [t for t in vs.searched_texts if t is not None]
    assert searched.count(S1) == 1, (
        f"S1 must be searched exactly once; searched_texts={vs.searched_texts}"
    )
    assert len(reranker.calls) == 1, (
        f"one consolidated rerank call expected; got {reranker.calls}"
    )
    counts = _identity_counts(reranker.calls[0])
    a_key = ("chunk", CHUNK_A["file_id"], CHUNK_A["text"])
    b_key = ("chunk", CHUNK_B["file_id"], CHUNK_B["text"])
    assert counts.get(a_key, 0) == 1 and counts.get(b_key, 0) == 1, (
        f"consolidated rerank input must contain A and B exactly once; got {counts}"
    )
