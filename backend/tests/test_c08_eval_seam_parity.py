"""Issue #713 (finding T1-32-K-02): the retrieval-eval surface must keep the
same ranked results the live chat path keeps at the ``filter_relevant`` seam.

``_retrieve_eval_outcome`` discards ``_execute_retrieval``'s
``rerank_success`` and hardcodes ``filter_relevant(..., reranked=False)``,
re-applying the raw distance threshold to results the live path keeps after
a successful rerank — so eval and ``query_retrieve_only`` measure a pipeline
the live chat path does not run.

The check below asserts the DESIRED seam parity: RED at the base commit
(465f76d2), GREEN once the eval path threads the real rerank status through
``filter_relevant`` like its sibling ``_retrieve_document_sources_for_kinds``.
"""

from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from app.config import settings
from app.services.rag_engine import RAGEngine


@pytest.fixture(autouse=True)
def _patch_ssrf():
    """Prevent SSRF guard from blocking service construction in tests."""
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
    def __init__(self, response: str):
        self._response = response

    async def chat_completion(self, messages, **kwargs) -> str:
        return self._response


class _NoopMemoryStore:
    def detect_memory_intent(self, text):
        return None

    def search_memories(self, query, limit=5, vault_id=None, include_global=False):
        return []


class _PassThroughVectorStore:
    """Vector-store double returning the fixture chunks from every search."""

    def __init__(self, results: List[Dict[str, Any]]):
        self._results = results

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
        return self._results[:limit]

    async def get_chunks_by_uid(self, chunk_uids):
        # Match the real VectorStore interface — no adjacent chunks in fake.
        return []

    def get_fts_exceptions(self) -> int:
        return 0


class _StubReranker:
    """Reranker double: keeps every chunk, in order, with a rerank score and
    success=True — the live-path signal that reranking ran."""

    async def rerank(self, query=None, chunks=None, top_n=None):
        ranked = [{**chunk, "_rerank_score": 0.9} for chunk in (chunks or [])]
        return ranked, True


async def test_eval_keeps_reranked_results():
    """AC4 (issue #713, T1-32-K-02): ``retrieve_eval_results`` must return the
    same ranked file ids the live path keeps at the ``filter_relevant`` seam,
    so reranked results are not re-filtered by the raw distance threshold.
    RED at the base commit (eval drops '1' and '2' at distance 0.9), GREEN
    after the fix."""
    chunks = [
        # Only '3' is under the 0.75 default max_distance_threshold; the
        # live path keeps all three because the reranker succeeded.
        {"text": "chunk one", "file_id": "1", "metadata": {}, "_distance": 0.9},
        {"text": "chunk two", "file_id": "2", "metadata": {}, "_distance": 0.9},
        {"text": "chunk three", "file_id": "3", "metadata": {}, "_distance": 0.1},
    ]
    engine = RAGEngine(
        embedding_service=_FakeEmbeddingService([0.1, 0.2]),
        vector_store=_PassThroughVectorStore(chunks),
        memory_store=_NoopMemoryStore(),
        llm_client=_FakeLLMClient("answer"),
    )
    engine.reranking_enabled = True
    engine.reranking_service = _StubReranker()
    engine._get_indexed_file_ids = _no_indexed_file_ids

    with patch.object(settings, "query_transformation_enabled", False):
        outcome = await engine.retrieve_eval_results("test query")

    assert outcome.retrieved_ids == ["1", "2", "3"], (
        "eval must keep the reranked results the live path keeps at the "
        f"filter_relevant seam, got {outcome.retrieved_ids!r}"
    )
