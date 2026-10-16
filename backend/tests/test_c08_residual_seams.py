"""Issue #713 residual seams: additive (non-frozen) coverage beyond the
frozen c08 checks, mandated by the plan-critic Round 1.

Covers the four behaviors the frozen checks cannot see:
(a) an EMBEDDING-admission rejection at the eval-path embed must map to
    ``status="unavailable"`` (and ``query_retrieve_only`` → ``[]``), never
    raise (the AC12 contract under the NEW admission wrap);
(b) an EMBEDDING-admission rejection at the decomposition sub-query gather
    must degrade to single-query retrieval inside ``query()`` (the existing
    ``except Exception`` fallback) and still answer with the rewrite;
(c) the orchestration all-failed fallback embed (``_fallback_embedding``)
    must run inside the EMBEDDING admission;
(d) the multi-sub fusion evaluator and the context distiller must receive
    the follow-up rewrite, not the raw follow-up (issue prose names both
    seams; T1-32-K-10).

RED at the base commit (465f76d2), GREEN after the fix.
"""

from contextlib import asynccontextmanager
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from app.config import settings
from app.services.admission import AdmissionClass, AdmissionRejected
from app.services.rag_engine import EmbeddingError, RAGEngine

REWRITE = "What are the security features of the authentication system?"

CHAT_HISTORY = [
    {"role": "user", "content": "How does the authentication system work?"},
    {
        "role": "assistant",
        "content": "The system uses JWT tokens with RS256 signing...",
    },
]


@pytest.fixture(autouse=True)
def _patch_ssrf():
    """Prevent SSRF guard from blocking service construction in tests."""
    with patch("app.services.embeddings.assert_url_safe"), patch(
        "app.services.llm_client.assert_url_safe"
    ):
        yield


def _no_indexed_file_ids(vault_id=None):
    return None


class _FakeEmbeddingService:
    def __init__(self, embedding=None, fail_texts=frozenset()):
        self._embedding = embedding or [0.1, 0.2]
        self._fail_texts = fail_texts
        self.embedded: List[str] = []

    async def embed_single(self, text: str) -> List[float]:
        if text in self._fail_texts:
            raise EmbeddingError(f"planned failure for: {text[:20]}")
        self.embedded.append(text)
        return self._embedding

    async def embed_passage(self, text: str) -> List[float]:
        return await self.embed_single(text)


class _FakeLLMClient:
    def __init__(self, response: str):
        self._response = response

    async def chat_completion(self, messages, **kwargs) -> str:
        return self._response

    async def chat_completion_stream(self, messages, **kwargs):
        yield self._response


class _NoopMemoryStore:
    def detect_memory_intent(self, text):
        return None

    def search_memories(self, query, limit=5, vault_id=None, include_global=False):
        return []


class _SpyQueryTextStore:
    """Vector-store double capturing the BM25/FTS ``query_text``."""

    def __init__(self, results):
        self._results = results
        self.captured_query_texts: List[str] = []

    async def search(
        self,
        embedding,
        limit: int = 10,
        filter_expr=None,
        vault_id=None,
        query_text=None,
        hybrid=False,
        **kwargs,
    ):
        self.captured_query_texts.append(query_text)
        return self._results[:limit]

    async def get_chunks_by_uid(self, chunk_uids):
        return []

    def get_fts_exceptions(self) -> int:
        return 0


class _TwoSubQueryPlanner:
    async def plan(self, query: str) -> List[str]:
        return ["sub query one", "sub query two"]


class _RejectEmbeddingController:
    """Rejects every EMBEDDING admit."""

    @asynccontextmanager
    async def admit(self, admission_class, **kwargs):
        if admission_class is AdmissionClass.EMBEDDING:
            raise AdmissionRejected("queue_full")
        yield


class _RejectSecondEmbeddingController:
    """Admits the FIRST EMBEDDING admit (query()'s variant gather) and
    rejects every later one — isolating the decomposition gather's rejection
    degradation from the variant-gather rejection path."""

    def __init__(self):
        self._embedding_admits = 0

    @asynccontextmanager
    async def admit(self, admission_class, **kwargs):
        if admission_class is AdmissionClass.EMBEDDING:
            self._embedding_admits += 1
            if self._embedding_admits > 1:
                raise AdmissionRejected("queue_full")
        yield


class _CountingController:
    def __init__(self):
        self.counts: Dict[Any, int] = {}

    @asynccontextmanager
    async def admit(self, admission_class, **kwargs):
        self.counts[admission_class] = self.counts.get(admission_class, 0) + 1
        yield


def _chunk() -> Dict[str, Any]:
    return {"text": "chunk", "file_id": "f1", "metadata": {}, "_distance": 0.1}


def _make_engine(store, embedder=None, llm_response="answer") -> RAGEngine:
    engine = RAGEngine()
    engine.embedding_service = embedder or _FakeEmbeddingService()
    engine.vector_store = store
    engine.memory_store = _NoopMemoryStore()
    engine.llm_client = _FakeLLMClient(llm_response)
    engine.reranking_enabled = False
    engine._get_indexed_file_ids = _no_indexed_file_ids
    return engine


async def test_eval_embed_admission_rejection_maps_to_unavailable():
    """(a) T1-32-S2-02: an EMBEDDING admission rejection at the eval-path
    embed must surface as ``status="unavailable"`` with empty ids (the AC12
    contract under the new admission wrap), never raise."""
    engine = _make_engine(_SpyQueryTextStore([_chunk()]))
    with (
        patch(
            "app.services.rag_engine.get_admission_controller",
            return_value=_RejectEmbeddingController(),
        ),
        patch.object(settings, "query_transformation_enabled", False),
    ):
        outcome = await engine.retrieve_eval_results("test query", vault_id=1)
        legacy = await engine.query_retrieve_only("test query", vault_id=1)

    assert outcome.status == "unavailable", outcome.status
    assert outcome.retrieved_ids == []
    assert outcome.artifact_ids == []
    assert legacy == []


async def test_decomposition_admission_rejection_degrades_to_single_query():
    """(b) T1-32-S2-02: when the decomposition sub-query gather is rejected
    after query()'s variant gather was admitted, query() must degrade to
    single-query retrieval (its existing orchestration fallback) and still
    answer — searching with the rewrite, not the sub-queries."""
    store = _SpyQueryTextStore([_chunk()])
    engine = _make_engine(store, llm_response=REWRITE)
    engine._get_query_planner = lambda client: _TwoSubQueryPlanner()
    with (
        patch(
            "app.services.rag_engine.get_admission_controller",
            return_value=_RejectSecondEmbeddingController(),
        ),
        patch.object(settings, "query_transformation_enabled", False),
        patch.object(settings, "memory_retrieval_enabled", False),
    ):
        results = [
            msg
            async for msg in engine.query(
                "tell me more", CHAT_HISTORY, stream=False
            )
        ]

    content = [r for r in results if r.get("type") == "content"]
    assert content, f"degraded turn must still answer, got {[r.get('type') for r in results]}"
    assert store.captured_query_texts, "no retrieval ran"
    assert store.captured_query_texts[0] == REWRITE, (
        "degraded single-query route must search with the rewrite, got "
        f"{store.captured_query_texts!r}"
    )


async def test_fallback_embedding_is_admitted():
    """(c) T1-32-S2-02: when every sub-query embed fails and the orchestration
    falls back to embedding the original query, that fallback embed must run
    inside the EMBEDDING admission (gather admit + fallback admit ≥ 2)."""
    controller = _CountingController()
    embedder = _FakeEmbeddingService(fail_texts={"sub query one", "sub query two"})
    engine = _make_engine(_SpyQueryTextStore([_chunk()]), embedder=embedder)
    with patch(
        "app.services.rag_engine.get_admission_controller",
        return_value=controller,
    ):
        await engine._orchestrate_sub_query_retrieval(
            plan=["sub query one", "sub query two"],
            vault_id=1,
            user_input="original question",
            effective_alpha=0.6,
            variants_dropped=[],
            effective_initial_top_k=10,
            effective_reranker_top_n=5,
            active_client=None,
            mode=None,
        )

    assert "original question" in embedder.embedded, (
        "fixture sanity: the fallback embed must have run"
    )
    assert controller.counts.get(AdmissionClass.EMBEDDING, 0) >= 2, (
        "sub-query gather admit + fallback embed admit expected, saw "
        f"{controller.counts!r}"
    )


class _RecorderEvaluator:
    def __init__(self, llm_client):
        pass

    async def evaluate(self, query, chunks):
        self.last_query = getattr(self, "last_query", None)
        _evaluated.append(query)
        return "CONFIDENT"


class _RecorderDistiller:
    def __init__(self, embedding_service, synthesis_client):
        pass

    async def distill(self, query, sources, eval_result):
        _distilled.append(query)

        class _Result:
            pass

        result = _Result()
        result.sources = sources
        result.sentence_provenance = []
        return result


_evaluated: List[str] = []
_distilled: List[str] = []


async def test_fusion_evaluator_and_distiller_receive_rewrite():
    """(d) T1-32-K-10: the multi-sub fusion evaluator and the context
    distiller must receive the follow-up rewrite, not the raw follow-up."""
    store = _SpyQueryTextStore([_chunk()])
    engine = _make_engine(store, llm_response=REWRITE)
    engine._get_query_planner = lambda client: _TwoSubQueryPlanner()
    _evaluated.clear()
    _distilled.clear()
    with (
        patch.object(settings, "query_transformation_enabled", False),
        patch.object(settings, "retrieval_evaluation_enabled", True),
        patch.object(settings, "context_distillation_enabled", True),
        patch.object(settings, "memory_retrieval_enabled", False),
        patch("app.services.rag_engine.RetrievalEvaluator", _RecorderEvaluator),
        patch("app.services.rag_engine.ContextDistiller", _RecorderDistiller),
    ):
        _ = [
            msg
            async for msg in engine.query(
                "tell me more", CHAT_HISTORY, stream=False
            )
        ]

    assert _evaluated and _evaluated[0] == REWRITE, (
        f"fusion evaluator must receive the rewrite, got {_evaluated!r}"
    )
    assert _distilled and _distilled[0] == REWRITE, (
        f"distiller must receive the rewrite, got {_distilled!r}"
    )
