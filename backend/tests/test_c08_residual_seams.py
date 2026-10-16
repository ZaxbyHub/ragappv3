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

import asyncio
import threading
import warnings
from contextlib import asynccontextmanager
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from app.config import settings
from app.services.admission import AdmissionClass, AdmissionRejected
from app.services.llm_client import LLMClient
from app.services.rag_engine import EmbeddingError, RAGEngine, RAGEngineError

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


async def _no_supersession(sources):
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
    # Hermeticity (OOB L-9): the supersession check reads the developer's
    # ./data/app.db via the pool; these tests pin seam wiring, not
    # supersession, so stub it instead of touching local state.
    engine._check_supersession = _no_supersession
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
    # Degradation proof (OOB L-7): the fan-out must have been skipped —
    # exactly one retrieval call (the degraded single-query route).
    assert len(store.captured_query_texts) == 1, (
        "admission-rejected decomposition must degrade to a single retrieval "
        f"call, got {len(store.captured_query_texts)}"
    )
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


def _make_recorders():
    """Per-test recorder doubles (OOB L-13 hygiene: no module-global state
    shared across tests — each call returns fresh classes bound to fresh
    lists)."""
    evaluated: List[str] = []
    distilled: List[str] = []

    class _RecorderEvaluator:
        def __init__(self, llm_client):
            pass

        async def evaluate(self, query, chunks):
            evaluated.append(query)
            return "CONFIDENT"

    class _RecorderDistiller:
        def __init__(self, embedding_service, synthesis_client):
            pass

        async def distill(self, query, sources, eval_result):
            distilled.append(query)

            class _Result:
                pass

            result = _Result()
            result.sources = sources
            result.sentence_provenance = []
            return result

    return _RecorderEvaluator, _RecorderDistiller, evaluated, distilled


async def test_fusion_evaluator_and_distiller_receive_rewrite():
    """(d) T1-32-K-10: the multi-sub fusion evaluator and the context
    distiller must receive the follow-up rewrite, not the raw follow-up."""
    store = _SpyQueryTextStore([_chunk()])
    engine = _make_engine(store, llm_response=REWRITE)
    engine._get_query_planner = lambda client: _TwoSubQueryPlanner()
    _RecorderEvaluator, _RecorderDistiller, evaluated, distilled = (
        _make_recorders()
    )
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

    assert evaluated and evaluated[0] == REWRITE, (
        f"fusion evaluator must receive the rewrite, got {evaluated!r}"
    )
    assert distilled and distilled[0] == REWRITE, (
        f"distiller must receive the rewrite, got {distilled!r}"
    )
    # Fan-out search seam (PRR-006): every per-sub-query search must carry
    # the rewrite, not just the first call.
    assert store.captured_query_texts, "no fan-out search ran"
    assert set(store.captured_query_texts) == {REWRITE}, (
        "fan-out vector_store.search calls must all use the rewrite, got "
        f"{store.captured_query_texts!r}"
    )


# ----------------------------------------------------------------------
# Feedback-round additions (PRR-003, PRR-010, PRR-011, PRR-013, PRR-015,
# OOB F-1, OOB L-5): blank-completion raise paths, admission placement
# and rejection exits, and the rerank-failure distance-filter branch.
# ----------------------------------------------------------------------


class _CompletionClient(LLMClient):
    """LLMClient whose chat_completion returns a fixed string — no IO."""

    def __init__(self, content: str, model: str):
        super().__init__(
            base_url="http://localhost:9",
            model=model,
            cb_name="c08_residual_blank",
        )
        self._content = content

    async def chat_completion(self, messages, **kwargs) -> str:
        return self._content


def _bare_engine_with_clients(primary, fallback) -> RAGEngine:
    engine = RAGEngine.__new__(RAGEngine)
    engine._instant_client_override = fallback
    engine._thinking_client_override = None
    engine.llm_client = fallback
    return engine


async def test_all_blank_chain_raises_rag_engine_error():
    """OOB F-1: when EVERY fallback client returns a blank completion, the
    non-stream path must raise RAGEngineError (never yield a silent empty
    answer — the empty-200 defect AC5 closes)."""
    primary = _CompletionClient("", model="r-blank-a")
    fallback = _CompletionClient("", model="r-blank-b")
    engine = _bare_engine_with_clients(primary, fallback)
    with pytest.raises(RAGEngineError, match="no user-visible content"):
        async for _chunk in engine._get_llm_response(
            [{"role": "user", "content": "q"}], client=primary, max_tokens=64
        ):
            pass


async def test_single_blank_client_raises_rag_engine_error():
    """OOB F-1 (single-provider variant): a one-client chain that returns
    blank is the main blank path for single-provider deployments and must
    raise instead of persisting an empty answer."""
    only = _CompletionClient("", model="r-blank-only")
    engine = _bare_engine_with_clients(only, only)
    with pytest.raises(RAGEngineError, match="no user-visible content"):
        async for _chunk in engine._get_llm_response(
            [{"role": "user", "content": "q"}], client=only, max_tokens=64
        ):
            pass


async def test_whitespace_only_completion_falls_through():
    """PRR-003: whitespace-only completions are blank (the predicate strips);
    they must fall through to the next client's visible content."""
    primary = _CompletionClient("   ", model="r-ws-a")
    fallback = _CompletionClient("real answer", model="r-ws-b")
    engine = _bare_engine_with_clients(primary, fallback)
    chunks = [
        c
        async for c in engine._get_llm_response(
            [{"role": "user", "content": "q"}], client=primary, max_tokens=64
        )
    ]
    content = [c for c in chunks if c.get("type") == "content"]
    assert content and content[0]["content"] == "real answer", chunks


class _FailingSignalReranker:
    """Reranker that runs but reports failure (rerank_success=False)."""

    async def rerank(self, query=None, chunks=None, top_n=None):
        return [dict(c, _rerank_score=0.1) for c in (chunks or [])], False


async def test_eval_distance_filter_applies_when_rerank_fails():
    """OOB L-5: the rerank_success=False branch of the eval threading must
    keep the raw distance filter — a failed rerank must not lift results
    above the threshold."""
    chunks = [
        {"text": "chunk one", "file_id": "1", "metadata": {}, "_distance": 0.9},
        {"text": "chunk two", "file_id": "2", "metadata": {}, "_distance": 0.9},
        {"text": "chunk three", "file_id": "3", "metadata": {}, "_distance": 0.1},
    ]
    engine = RAGEngine(
        embedding_service=_FakeEmbeddingService([0.1, 0.2]),
        vector_store=_SpyQueryTextStore(chunks),
        memory_store=_NoopMemoryStore(),
        llm_client=_FakeLLMClient("answer"),
    )
    engine.reranking_enabled = True
    engine.reranking_service = _FailingSignalReranker()
    engine._get_indexed_file_ids = _no_indexed_file_ids

    with patch.object(settings, "query_transformation_enabled", False):
        outcome = await engine.retrieve_eval_results("test query")

    assert outcome.retrieved_ids == ["3"], (
        "a failed rerank must keep the distance filter (only '3' is under "
        f"the threshold), got {outcome.retrieved_ids!r}"
    )


class _ActiveFlagAdmissionController:
    """Controller double whose ``active`` set contains a class only while a
    caller is inside its admit context (OOB F-2's placement pattern)."""

    def __init__(self):
        self.active: set = set()

    @asynccontextmanager
    async def admit(self, admission_class, **kwargs):
        self.active.add(admission_class)
        try:
            yield
        finally:
            self.active.discard(admission_class)


class _AdmissionAwareEmbeddingService(_FakeEmbeddingService):
    """Fake embedder that records every embed executed while the EMBEDDING
    admission was NOT held."""

    def __init__(self, controller, fail_texts=frozenset()):
        super().__init__(fail_texts=fail_texts)
        self._controller = controller
        self.embedded_outside: List[str] = []

    async def embed_single(self, text: str) -> List[float]:
        if text in self._fail_texts:
            raise EmbeddingError(f"planned failure for: {text[:20]}")
        if AdmissionClass.EMBEDDING not in self._controller.active:
            self.embedded_outside.append(text)
        self.embedded.append(text)
        return self._embedding


async def test_fallback_embed_runs_inside_admission():
    """PRR-011 / OOB F-2: the orchestration all-failed fallback embed must
    execute while the EMBEDDING admission slot is HELD — placement, not just
    an admit count."""
    controller = _ActiveFlagAdmissionController()
    embedder = _AdmissionAwareEmbeddingService(
        controller, fail_texts={"sub query one", "sub query two"}
    )
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
    assert embedder.embedded_outside == [], (
        "the fallback embed ran outside the held EMBEDDING admission: "
        f"{embedder.embedded_outside!r}"
    )


async def test_fallback_admit_rejection_propagates():
    """PRR-015: when the all-failed fallback's own EMBEDDING admit is
    rejected (the gather's admit was granted first), the rejection must
    propagate out of the orchestration (query()'s degrade handler catches
    it) — proving the fallback embed site actually acquires admission."""
    controller = _RejectSecondEmbeddingController()
    embedder = _FakeEmbeddingService(fail_texts={"sub query one", "sub query two"})
    engine = _make_engine(_SpyQueryTextStore([_chunk()]), embedder=embedder)
    with patch(
        "app.services.rag_engine.get_admission_controller",
        return_value=controller,
    ):
        with pytest.raises(AdmissionRejected):
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


async def test_query_rejection_non_stream_raises():
    """PRR-015: the NON-stream branch of the embedding-admission rejection
    must raise RAGEngineError (the streaming branch yields the error chunk;
    only that is covered by the frozen checks)."""
    engine = _make_engine(_SpyQueryTextStore([_chunk()]))
    with (
        patch(
            "app.services.rag_engine.get_admission_controller",
            return_value=_RejectEmbeddingController(),
        ),
        patch.object(settings, "memory_retrieval_enabled", False),
        patch.object(settings, "query_transformation_enabled", False),
    ):
        with pytest.raises(RAGEngineError, match="Embedding admission rejected"):
            async for _msg in engine.query("test query", [], stream=False):
                pass


async def test_retrieve_sources_admission_rejection_records_kind():
    """PRR-010: an AdmissionRejected from the admitted query embed must
    surface through retrieve_sources' per-kind failure recording — the
    'document' kind lands in failed_kinds, never an empty success."""
    engine = _make_engine(_SpyQueryTextStore([_chunk()]))
    with patch(
        "app.services.rag_engine.get_admission_controller",
        return_value=_RejectEmbeddingController(),
    ):
        result = await engine.retrieve_sources("test query", 1, limit=3)

    assert "document" in result.failed_kinds, (
        f"admission-rejected document retrieval must be recorded as a failed "
        f"kind, got failed={set(result.failed_kinds)!r} "
        f"successful={set(result.successful_kinds)!r}"
    )


class _WitnessMemoryStore:
    """Memory-store double that signals when its search is in flight, so the
    liveness witness observes the overlap task as a TASK OBJECT (rename-
    immune — no coroutine-name matching)."""

    def __init__(self):
        self.entered = threading.Event()
        self._release = threading.Event()

    def detect_memory_intent(self, text):
        return None

    def search_memories(self, query, limit=5, vault_id=None, include_global=False):
        self.entered.set()
        self._release.wait(timeout=5.0)
        return []

    def release(self):
        self._release.set()


async def test_admission_rejection_liveness_witness():
    """PRR-013 + OOB L-6: the memory overlap task must be genuinely IN FLIGHT
    when the turn is rejected (liveness precondition: the search entered the
    store), and cancellation must have been requested for it by the time the
    ADMISSION_REJECTED chunk is yielded. The task is matched as an OBJECT
    (never by coroutine name), and ``Task.cancelling()`` persists after the
    task completes, so the assert is race-free against the fix's own
    cancel-before-yield ordering."""
    store = _WitnessMemoryStore()
    engine = _make_engine(_SpyQueryTextStore([_chunk()]), llm_response="answer")
    engine.memory_store = store
    controller = _RejectEmbeddingController()
    collected: List[dict] = []
    cancellation_requested = False
    with (
        patch(
            "app.services.rag_engine.get_admission_controller",
            return_value=controller,
        ),
        patch.object(settings, "memory_retrieval_enabled", True),
        patch.object(settings, "retrieval_kms_overlap", False),
        patch.object(settings, "query_transformation_enabled", False),
        warnings.catch_warnings(record=True) as recorded,
    ):
        warnings.simplefilter("always")
        async for msg in engine.query("test query", [], stream=True):
            collected.append(msg)
            if (
                msg.get("type") == "error"
                and msg.get("code") == "ADMISSION_REJECTED"
            ):
                # Liveness precondition (the witness): the overlap search
                # genuinely entered the store before the rejection landed —
                # a worker never scheduled would otherwise pass the counter
                # check vacuously.
                assert store.entered.is_set(), (
                    "memory search never started; the cancellation check has "
                    "no in-flight task to testify about"
                )
                # Same-tick snapshot: any live task other than this one whose
                # cancellation counter is set IS the cancelled overlap task
                # (object identity, no name matching).
                cancellation_requested = any(
                    t is not asyncio.current_task() and t.cancelling() > 0
                    for t in asyncio.all_tasks()
                )
                break
        store.release()

    error_chunks = [
        m
        for m in collected
        if m.get("type") == "error" and m.get("code") == "ADMISSION_REJECTED"
    ]
    assert error_chunks, f"ADMISSION_REJECTED chunk missing: {collected!r}"
    assert cancellation_requested, (
        "no task had cancellation requested at the ADMISSION_REJECTED chunk; "
        "the overlap-task cancel (T1-32-S2-03 fix) did not run"
    )
    never_awaited = [
        w
        for w in recorded
        if issubclass(w.category, RuntimeWarning)
        and "never awaited" in str(w.message)
    ]
    assert never_awaited == [], (
        f"never-awaited coroutines leaked on the rejection path: "
        f"{[str(w.message) for w in never_awaited]}"
    )
