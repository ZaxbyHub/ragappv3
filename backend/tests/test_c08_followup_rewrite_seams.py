"""Issue #713 (finding T1-32-K-10): the follow-up rewrite must reach every
retrieval seam, not only the dense-embedding leg.

``RAGEngine.query()`` documents that a referential follow-up ("tell me more")
is retrieved against a self-contained rewrite of the question, but only the
embedding seam consumes that rewrite: the BM25/FTS ``query_text`` of
``vector_store.search``, the ``query`` argument of
``reranking_service.rerank`` and the query argument of
``RetrievalEvaluator.evaluate`` all receive the raw follow-up because
``query()`` forwards ``user_input`` (not ``retrieval_query``) to
``_execute_retrieval``.

Each check below asserts the DESIRED wiring: RED at the base commit
(465f76d2), GREEN after the fix threads the rewrite through every seam.
"""

from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from app.config import settings
from app.services.rag_engine import RAGEngine

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
    """Prevent SSRF guard from blocking service construction in tests.

    Same convention as tests/test_rag_engine.py: the engine lazily wires real
    EmbeddingService/LLMClient instances whose base_url is SSRF-checked; the
    fakes here never touch the network.
    """
    with patch("app.services.embeddings.assert_url_safe"), patch(
        "app.services.llm_client.assert_url_safe"
    ):
        yield


def _no_indexed_file_ids(vault_id=None):
    """Visibility filter stub — a developer's backend/data/app.db must not
    filter the fixture's file ids (known poisoning trap)."""
    return None


class _FakeEmbeddingService:
    def __init__(self, embedding: List[float]):
        self._embedding = embedding

    async def embed_single(self, text: str) -> List[float]:
        return self._embedding

    async def embed_passage(self, text: str) -> List[float]:
        return self._embedding


class _FakeLLMClient:
    """chat_completion always returns ``response`` (the follow-up rewrite)."""

    def __init__(self, response: str):
        self._response = response

    async def chat_completion(self, messages, **kwargs) -> str:
        return self._response


class _NoopMemoryStore:
    def detect_memory_intent(self, text):
        return None

    def search_memories(self, query, limit=5, vault_id=None, include_global=False):
        return []


class _QueryTextSpyStore:
    """Vector-store double capturing the BM25/FTS ``query_text`` of each search."""

    def __init__(self, results: List[Dict[str, Any]]):
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
        # Match the real VectorStore interface — no adjacent chunks in fake.
        return []

    def get_fts_exceptions(self) -> int:
        return 0


class _QuerySpyReranker:
    """Reranker double capturing the rerank ``query``; passes chunks through
    with a rerank score and success=True (the live-path signal)."""

    def __init__(self):
        self.captured_queries: List[str] = []

    async def rerank(self, query=None, chunks=None, top_n=None):
        self.captured_queries.append(query)
        ranked = [{**chunk, "_rerank_score": 0.9} for chunk in (chunks or [])]
        return ranked, True


def _fixture_chunks() -> List[Dict[str, Any]]:
    # Explicit _distance under the 0.75 default threshold (distance polarity:
    # lower is better) so the record survives filter_relevant.
    return [{"text": "chunk", "file_id": "f1", "metadata": {}, "_distance": 0.1}]


def _make_engine(store, reranker=None) -> RAGEngine:
    """Engine with fake services; the LLM's canned reply IS the rewrite
    (the test_followup_rewrite_modifies_retrieval_query pattern)."""
    engine = RAGEngine()
    engine.embedding_service = _FakeEmbeddingService([0.1, 0.2])
    engine.vector_store = store
    engine.memory_store = _NoopMemoryStore()
    engine.llm_client = _FakeLLMClient(REWRITE)
    if reranker is not None:
        engine.reranking_enabled = True
        engine.reranking_service = reranker
    engine._get_indexed_file_ids = _no_indexed_file_ids
    return engine


async def test_fts_query_text_is_rewrite():
    """AC1 (issue #713, T1-32-K-10): the BM25/FTS leg of a rewritten follow-up
    must search with the rewrite. RED at the base commit (query_text receives
    the raw follow-up), GREEN after the fix."""
    store = _QueryTextSpyStore(_fixture_chunks())
    engine = _make_engine(store)
    with (
        patch.object(settings, "query_transformation_enabled", False),
        patch.object(settings, "retrieval_evaluation_enabled", False),
    ):
        _ = [msg async for msg in engine.query("tell me more", CHAT_HISTORY, stream=False)]

    assert store.captured_query_texts, "vector_store.search was never called"
    assert store.captured_query_texts[0] == REWRITE, (
        "BM25/FTS query_text must be the follow-up rewrite, got "
        f"{store.captured_query_texts!r}"
    )


async def test_rerank_query_is_rewrite():
    """AC2 (issue #713, T1-32-K-10): the cross-encoder reranker of a rewritten
    follow-up must rerank against the rewrite. RED at the base commit (rerank
    query receives the raw follow-up), GREEN after the fix."""
    reranker = _QuerySpyReranker()
    store = _QueryTextSpyStore(_fixture_chunks())
    engine = _make_engine(store, reranker=reranker)
    with (
        patch.object(settings, "query_transformation_enabled", False),
        patch.object(settings, "retrieval_evaluation_enabled", False),
    ):
        _ = [msg async for msg in engine.query("tell me more", CHAT_HISTORY, stream=False)]

    assert reranker.captured_queries, "reranking_service.rerank was never called"
    assert reranker.captured_queries[0] == REWRITE, (
        "rerank query must be the follow-up rewrite, got "
        f"{reranker.captured_queries!r}"
    )


async def test_evaluator_query_is_rewrite():
    """AC3 (issue #713, T1-32-K-10): CRAG retrieval evaluation of a rewritten
    follow-up must evaluate the rewrite. RED at the base commit (the evaluator
    receives the raw follow-up), GREEN after the fix."""
    evaluated: List[str] = []

    class _RecordingEvaluator:
        """RetrievalEvaluator double recording every evaluate() query."""

        def __init__(self, llm_client):
            pass

        async def evaluate(self, query, chunks):
            evaluated.append(query)
            return "CONFIDENT"

    store = _QueryTextSpyStore(_fixture_chunks())
    engine = _make_engine(store)
    with (
        patch.object(settings, "query_transformation_enabled", False),
        patch.object(settings, "retrieval_evaluation_enabled", True),
        patch("app.services.rag_engine.RetrievalEvaluator", _RecordingEvaluator),
    ):
        _ = [msg async for msg in engine.query("tell me more", CHAT_HISTORY, stream=False)]

    assert evaluated, "RetrievalEvaluator.evaluate was never called"
    assert evaluated[0] == REWRITE, (
        f"evaluator query must be the follow-up rewrite, got {evaluated!r}"
    )
