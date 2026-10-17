"""Issue #715 acceptance check — audit finding T1-32 (decomposition timeout).

``test_sub_query_timeout_propagates``

Contract under test: a ``SearchSemaphoreTimeoutError`` raised by ONE
sub-query's vector search inside the multi-sub-query fan-out must PROPAGATE
out of ``_orchestrate_sub_query_retrieval`` (search capacity is a
server-busy signal for the whole request → HTTP 503 upstream), instead of
being recorded as a merely "failed sub-query" while the surviving sub-query's
results are fused into a seemingly healthy answer.

Root cause verified at HEAD (665bbd09): ``_orchestrate_sub_query_retrieval``
gathers the per-sub-query ``_execute_retrieval`` tasks with
``return_exceptions=True`` (rag_engine.py ~L2769-2786) and records ANY
``BaseException`` result as a failed sub-query; the survivor is fused
(~L2792+). ``_execute_retrieval`` itself deliberately RE-RAISES the timeout
from its internal gather (rag_engine.py ~L3069-3081) — the orchestration
layer swallowing it is exactly the defect (the single-query ``query()`` path
treats the same exception as a 503-grade signal).

This check asserts the DESIRED post-fix behavior and is RED at HEAD with::

    Failed: DID NOT RAISE

Harness reuse: mirrors the direct ``_orchestrate_sub_query_retrieval``
driving style of ``tests/test_c08_embedding_admission.py`` (issue #713) and
the fake-services pattern of ``tests/test_rag_engine.py``. The fake store
keys the timeout off the per-sub-query EMBEDDING (the search call receives
the whole user_input as query_text for every sub-query, so the embedding is
the distinguishing argument).
"""

import os
import sys
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Reuse the fakes from the existing RAG-engine unit tests.
from test_rag_engine import (  # noqa: E402
    FakeLLMClient,
    FakeMemoryStore,
)

from app.services.rag_engine import RAGEngine  # noqa: E402
from app.services.vector_store import (  # noqa: E402
    SearchSemaphoreTimeoutError,
)

# Marker embeddings: the fake embedder encodes which sub-query is which, so
# the fake store can fail exactly one arm of the fan-out.
_EMB_TIMEOUT_ARM = [1.0, 0.0]
_EMB_HEALTHY_ARM = [0.0, 1.0]


class _SubQueryMarkerEmbeddingService:
    """Embeds each sub-query text to a marker vector identifying the arm."""

    async def embed_single(self, text: str) -> List[float]:
        if "timeout arm" in text:
            return list(_EMB_TIMEOUT_ARM)
        return list(_EMB_HEALTHY_ARM)

    async def embed_passage(self, text: str) -> List[float]:
        return await self.embed_single(text)


class _TimeoutOnMarkerVectorStore:
    """Vector-store double: raises the capacity timeout for the marker arm.

    The search call sees the whole ``user_input`` as ``query_text`` for every
    sub-query, so the EMBEDDING (unique per sub-query via the marker embedder)
    is the reliable discriminator.
    """

    def __init__(self):
        self.search_calls: List[Dict[str, Any]] = []

    async def search(
        self,
        embedding: List[float],
        limit: int = 10,
        filter_expr=None,
        vault_id=None,
        query_text=None,
        hybrid=False,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        self.search_calls.append(
            {"embedding": list(embedding), "query_text": query_text}
        )
        if embedding[:1] == _EMB_TIMEOUT_ARM[:1]:
            raise SearchSemaphoreTimeoutError(
                "Search semaphore acquisition timed out after 30.0s "
                "(simulated for the timeout sub-query arm)"
            )
        return [
            {
                "id": "healthy_chunk",
                "text": "healthy arm result",
                "file_id": "f1",
                "_distance": 0.1,
                "metadata": {},
            }
        ]

    def get_fts_exceptions(self) -> int:
        return 0


@pytest.fixture(autouse=True)
def _patch_ssrf():
    """Prevent SSRF guard from blocking service construction in tests
    (same fixture as tests/test_rag_engine.py)."""
    with patch("app.services.embeddings.assert_url_safe"), patch(
        "app.services.llm_client.assert_url_safe"
    ):
        yield


async def test_sub_query_timeout_propagates():
    """A SearchSemaphoreTimeoutError on ONE sub-query's search must propagate
    out of _orchestrate_sub_query_retrieval instead of being swallowed into
    the failed-sub-queries list while the survivor is fused. T1-32
    decomposition-timeout acceptance check (issue #715)."""
    store = _TimeoutOnMarkerVectorStore()
    engine = RAGEngine(
        embedding_service=_SubQueryMarkerEmbeddingService(),
        vector_store=store,
        memory_store=FakeMemoryStore(),
        llm_client=FakeLLMClient(response="answer"),
    )
    engine.reranking_enabled = False  # isolate the search-phase contract

    # DESIRED: the capacity timeout on sub-query A is a server-busy signal
    # for the WHOLE decomposition — it must escape the orchestration gather,
    # exactly as it escapes _execute_retrieval on the single-query path.
    with pytest.raises(SearchSemaphoreTimeoutError):
        await engine._orchestrate_sub_query_retrieval(
            plan=[
                "timeout arm sub-query about deployments",
                "healthy arm sub-query about monitoring",
            ],
            vault_id=1,
            user_input="how is the system operated in production?",
            effective_alpha=0.6,
            variants_dropped=[],
            effective_initial_top_k=10,
            effective_reranker_top_n=5,
            active_client=None,
            mode=None,
        )
