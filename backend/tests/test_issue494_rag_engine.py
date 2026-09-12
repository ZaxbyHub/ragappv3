"""Issue #494 acceptance checks — AC7/OPS-005, AC8/OPS-006, AC9/LLM-002.

All three DISCRIMINATING, root causes verified at base a543361:

* AC7: ``RAGEngine.query`` runs the multi-sub-query orchestration
  (~line 1384: ``if raw_rag_needed and len(plan) > 1:``) BEFORE — and
  therefore around — the ONLY maintenance gate (~line 1509, on the
  single-query ``elif not _skip_standard_retrieval`` branch). With
  ``maintenance_mode=True`` and a 2-sub-query plan, retrieval still runs.
  Contract: a multi-query plan under maintenance must perform ZERO
  document-retrieval calls and produce the same maintenance
  fallback reason/indication as the single-query path. RED at base.

* AC8: ``_execute_retrieval`` (~lines 2554-2570) gathers vector searches with
  ``return_exceptions=True`` and converts an ORIGINAL-query
  ``SearchSemaphoreTimeoutError`` into ``RAGEngineError`` (line 2561), which
  ``query()`` then swallows into the generic fallback branch — generation
  proceeds with an LLM call instead of propagating the 503 signal.
  Contract: a ``SearchSemaphoreTimeoutError`` raised by the original-query
  vector search must propagate out of ``query()`` as
  ``SearchSemaphoreTimeoutError`` (the API maps it to HTTP 503) and NO LLM
  generation call may happen. RED at base (becomes RAGEngineError /
  generation proceeds).

* AC9: ``_fallback_clients`` (~lines 3057-3070) dedups by ``base_url`` only,
  so an instant client on the same URL with a DIFFERENT model is dropped and
  never tried as a fallback. Contract: primary(U, m1) + instant(U, m2) must
  BOTH be in the fallback list; PRESERVING sub-assert: an exact duplicate
  (same url AND model) is still deduped to one. RED at base.

Fully offline: fake embedding/memory services, counting vector stores, a
counting LLM client, and a stubbed query planner (mirrors the harness in
``test_multi_sub_query_aggregate_status.py``).
"""

import asyncio
import os
import sys
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

# Add parent directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub optional dependencies same as other test files
try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow  # noqa: F401
except ImportError:
    import types

    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    import types

    _unstructured = types.ModuleType("unstructured")
    _unstructured.partition = types.ModuleType("unstructured.partition")
    _unstructured.partition.auto = types.ModuleType("unstructured.partition.auto")
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    sys.modules["unstructured"] = _unstructured
    sys.modules["unstructured.partition"] = _unstructured.partition
    sys.modules["unstructured.partition.auto"] = _unstructured.partition.auto

from app.services.llm_client import LLMClient
from app.services.rag_engine import RAGEngine
from app.services.vector_store import SearchSemaphoreTimeoutError

# ------------------------------------------------------------------
# Fake services
# ------------------------------------------------------------------


class FakeEmbeddingService:
    """Deterministic embeddings based on a call counter."""

    def __init__(self):
        self._counter = 0

    async def embed_single(self, text: str) -> List[float]:
        self._counter += 1
        return [0.1 + self._counter * 0.01, 0.2, 0.3, 0.4]

    async def embed_passage(self, text: str) -> List[float]:
        return await self.embed_single(text)


class CountingVectorStore:
    """Vector store whose search() records every call and returns hits."""

    def __init__(self):
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
        self.search_calls.append(
            {"embedding": embedding, "query_text": query_text}
        )
        return [
            {
                "id": "doc_chunk_1",
                "text": "Document chunk content",
                "file_id": "doc_a",
                "_distance": 0.12,
                "metadata": {},
            }
        ][:limit]

    def get_fts_exceptions(self) -> int:
        return 0

    def reset(self):
        self.search_calls = []


class SemaphoreTimeoutVectorStore(CountingVectorStore):
    """Every search raises SearchSemaphoreTimeoutError (original variant)."""

    async def search(self, *args, **kwargs) -> List[Dict[str, Any]]:
        self.search_calls.append({"raised": SearchSemaphoreTimeoutError})
        raise SearchSemaphoreTimeoutError(
            "Semaphore timeout after 30.0s waiting for vector store slot"
        )


class FakeMemoryStore:
    def detect_memory_intent(self, query: str) -> Optional[str]:
        return None

    async def search_memories(
        self, query: str, top_k: int, vault_id=None, include_global: bool = False
    ) -> List:
        return []


class CountingLLMClient:
    """LLM client that counts every generation call."""

    def __init__(self, answer: str = "LLM fallback answer."):
        self.calls = 0
        self._answer = answer

    async def chat_completion(self, messages, **kwargs) -> str:
        self.calls += 1
        return self._answer


class StubPlanner:
    """Deterministic query-planner stub returning a preset plan."""

    def __init__(self, plan: List[str]):
        self._plan = list(plan)

    async def plan(self, query: str) -> List[str]:
        return list(self._plan)


# ------------------------------------------------------------------
# Shared harness
# ------------------------------------------------------------------


def _make_engine(
    embedding_service: Any,
    vector_store: Any,
    llm_client: Any,
    memory_store: Any,
    instant_client: Any = None,
) -> RAGEngine:
    with patch("app.services.embeddings.assert_url_safe"), patch(
        "app.services.llm_client.assert_url_safe"
    ):
        engine = RAGEngine(
            embedding_service=embedding_service,
            vector_store=vector_store,
            memory_store=memory_store,
            llm_client=llm_client,
            instant_client=instant_client,
        )
    engine.retrieval_top_k = 10
    engine.vector_store = vector_store
    engine._get_indexed_file_ids = MagicMock(return_value=None)
    return engine


async def _drive_query(engine: RAGEngine, **query_kwargs):
    """Consume engine.query(); return (chunks, exception_or_None)."""
    chunks: List[Dict[str, Any]] = []
    error: Optional[BaseException] = None
    try:
        async for chunk in engine.query(**query_kwargs):
            chunks.append(chunk)
            if len(chunks) > 60:  # safety valve
                break
    except BaseException as exc:  # noqa: BLE001 — the propagated type IS the check
        error = exc
    return chunks, error


def _fallback_chunks(chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [c for c in chunks if c.get("type") == "fallback"]


@pytest.fixture
def patched_settings_fixture():
    """Patch settings for isolated testing (mirrors existing harness)."""
    mock_settings = MagicMock()
    mock_settings.agentic_rag_enabled = False
    mock_settings.query_transformation_enabled = False
    mock_settings.memory_retrieval_enabled = False
    mock_settings.context_distillation_enabled = False
    mock_settings.parent_retrieval_enabled = False
    mock_settings.retrieval_evaluation_enabled = False
    mock_settings.context_max_tokens = 0
    mock_settings.rag_relevance_threshold = None
    mock_settings.max_distance_threshold = None
    mock_settings.rrf_legacy_mode = True
    mock_settings.multi_query_rrf_k = 60
    mock_settings.rrf_weight_original = 1.0
    mock_settings.rrf_weight_stepback = 1.0
    mock_settings.rrf_weight_hyde = 1.0
    mock_settings.retrieval_recency_weight = 0.0
    mock_settings.exact_match_promote = False
    mock_settings.hybrid_search_enabled = False
    mock_settings.reranking_enabled = False
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
# AC7 / OPS-005
# ------------------------------------------------------------------


class TestAC7MaintenanceBlocksMultiQuery:
    """maintenance_mode=True must gate the multi-sub-query branch too."""

    async def test_maintenance_blocks_multi_query_retrieval(
        self, patched_settings_fixture
    ):
        vector_store = CountingVectorStore()
        llm = CountingLLMClient()
        engine = _make_engine(
            FakeEmbeddingService(), vector_store, llm, FakeMemoryStore()
        )
        engine.maintenance_mode = True

        # --- Baseline: single-query plan under maintenance (PRESERVING) ---
        engine._query_planners[id(llm)] = StubPlanner([])
        single_chunks, single_error = await _drive_query(
            engine,
            user_input="single query under maintenance",
            chat_history=[],
            stream=False,
        )
        single_fallbacks = _fallback_chunks(single_chunks)
        assert single_error is None, f"single-query run must complete: {single_error}"
        assert len(single_fallbacks) == 1, (
            f"single-query maintenance run must yield exactly one fallback "
            f"chunk, got {single_fallbacks}"
        )
        assert (
            single_fallbacks[0]["reason"] == "RAG index is under maintenance"
        ), f"unexpected maintenance fallback reason: {single_fallbacks[0]}"
        assert vector_store.search_calls == [], (
            "single-query maintenance run must not retrieve"
        )
        print(
            "PRESERVING GREEN: single-query maintenance path yields fallback "
            "chunk with zero retrieval"
        )

        # --- DISCRIMINATING: 2-sub-query plan under maintenance ---
        vector_store.reset()
        engine._query_planners[id(llm)] = StubPlanner(
            ["sub query alpha", "sub query beta"]
        )
        multi_chunks, multi_error = await _drive_query(
            engine,
            user_input="multi query under maintenance",
            chat_history=[],
            stream=False,
        )
        print("AC7 CHECK: FAIL", flush=True)
        assert vector_store.search_calls == [], (
            f"maintenance_mode=True must block document retrieval for "
            f"multi-sub-query plans too; {len(vector_store.search_calls)} "
            f"vector search call(s) were made"
        )
        multi_fallbacks = _fallback_chunks(multi_chunks)
        assert len(multi_fallbacks) == 1, (
            f"multi-query maintenance run must yield the same fallback "
            f"indication as the single-query path, got {multi_fallbacks}"
        )
        assert (
            multi_fallbacks[0]["reason"] == single_fallbacks[0]["reason"]
        ), (
            f"multi-query maintenance fallback reason "
            f"{multi_fallbacks[0]['reason']!r} != single-query "
            f"{single_fallbacks[0]['reason']!r}"
        )
        assert multi_error is None, (
            f"multi-query maintenance run must complete cleanly: {multi_error}"
        )


# ------------------------------------------------------------------
# AC8 / OPS-006
# ------------------------------------------------------------------


class TestAC8SemaphoreTimeoutPropagates:
    """Original-query SearchSemaphoreTimeoutError must reach the API caller."""

    async def test_semaphore_timeout_propagates_and_no_generation(
        self, patched_settings_fixture
    ):
        vector_store = SemaphoreTimeoutVectorStore()
        llm = CountingLLMClient()
        engine = _make_engine(
            FakeEmbeddingService(), vector_store, llm, FakeMemoryStore()
        )
        engine._query_planners[id(llm)] = StubPlanner([])

        chunks, error = await _drive_query(
            engine,
            user_input="query that hits the semaphore timeout",
            chat_history=[],
            stream=False,
        )

        # DISCRIMINATING: at base _execute_retrieval wraps the semaphore
        # timeout into RAGEngineError, query() swallows it into the generic
        # fallback, and generation proceeds -> both asserts RED.
        print("AC8 CHECK: FAIL", flush=True)
        assert isinstance(
            error, SearchSemaphoreTimeoutError
        ), (
            f"SearchSemaphoreTimeoutError from the original-query vector "
            f"search must propagate to the caller (API maps it to 503); got "
            f"{type(error).__name__ if error else 'no exception'}"
        )
        assert llm.calls == 0, (
            f"a search-semaphore-timeout query must never reach LLM "
            f"generation; {llm.calls} chat_completion call(s) were made"
        )


# ------------------------------------------------------------------
# AC9 / LLM-002
# ------------------------------------------------------------------


class TestAC9FallbackClientDedup:
    """Fallback dedup must key on (base_url, model), not base_url alone."""

    async def test_same_url_different_model_both_kept(self, patched_settings_fixture):
        url = "http://llm-test-host:11434"

        with patch("app.services.llm_client.settings", MagicMock()), patch(
            "app.services.llm_client.assert_url_safe"
        ):
            primary = LLMClient(base_url=url, model="thinking-model")
            instant = LLMClient(base_url=url, model="instant-model")
            duplicate = LLMClient(base_url=url, model="thinking-model")

        # PRESERVING: an exact duplicate (same url AND model) is still deduped.
        dup_engine = _make_engine(
            FakeEmbeddingService(),
            CountingVectorStore(),
            primary,
            FakeMemoryStore(),
            instant_client=duplicate,
        )
        dup_clients = dup_engine._fallback_clients(primary)
        assert len(dup_clients) == 1, (
            f"exact duplicate (same url+model) must still be deduped, got "
            f"{len(dup_clients)} entries"
        )
        print("PRESERVING GREEN: exact url+model duplicate still deduped to one")

        # DISCRIMINATING: same URL, different model must BOTH be fallbacks.
        engine = _make_engine(
            FakeEmbeddingService(),
            CountingVectorStore(),
            primary,
            FakeMemoryStore(),
            instant_client=instant,
        )
        print("AC9 CHECK: FAIL", flush=True)
        clients = engine._fallback_clients(primary)
        assert len(clients) >= 2, (
            f"fallback list must keep the instant client when it shares the "
            f"base_url but runs a different model; got "
            f"{[getattr(c, 'model', None) for c in clients]}"
        )
        assert any(
            getattr(c, "model", None) == "instant-model" for c in clients
        ), f"instant model missing from fallback list: {[getattr(c, 'model', None) for c in clients]}"
