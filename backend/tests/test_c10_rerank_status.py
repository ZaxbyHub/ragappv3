"""Issue #715 acceptance check — audit finding T1-32 (rerank status).

``test_rerank_exception_is_not_disabled``

Contract under test: when reranking is ENABLED but the rerank call RAISES,
``_execute_retrieval``'s returned ``rerank_status`` (tuple index 7) must NOT
be ``"disabled"`` — reranking was attempted and failed, which is a distinct
degraded state from "never attempted" ("disabled"). Reporting "disabled"
hides reranker outages from the hybrid-health telemetry consumers.

Root cause verified at HEAD (665bbd09): the rerank ``except`` branch
(rag_engine.py ~L3255-3257) leaves ``rerank_success = None``, and the status
mapping (~L3296-3302) renders ``None`` as ``"disabled"``.

This check asserts the DESIRED post-fix behavior and is RED at HEAD with::

    AssertionError: assert 'disabled' != 'disabled'

Harness reuse: mirrors the ``FailingReranker`` pattern of
``tests/test_rag_engine.py::RAGEngineTests::
test_reranker_exception_falls_back_to_unreranked`` (fakes imported from that
module), but drives ``_execute_retrieval`` directly so the returned tuple's
rerank status can be inspected without running the whole chat pipeline.
"""

import os
import sys
from typing import Any, cast
from unittest.mock import patch

import pytest

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Reuse the fakes from the existing RAG-engine unit tests.
from test_rag_engine import (  # noqa: E402
    FakeEmbeddingService,
    FakeLLMClient,
    FakeMemoryStore,
    FakeVectorStore,
)

from app.services.embeddings import EmbeddingService  # noqa: E402
from app.services.llm_client import LLMClient  # noqa: E402
from app.services.memory_store import MemoryStore  # noqa: E402
from app.services.rag_engine import RAGEngine  # noqa: E402
from app.services.vector_store import VectorStore  # noqa: E402


@pytest.fixture(autouse=True)
def _patch_ssrf():
    """Prevent SSRF guard from blocking service construction in tests
    (same fixture as tests/test_rag_engine.py)."""
    with patch("app.services.embeddings.assert_url_safe"), patch(
        "app.services.llm_client.assert_url_safe"
    ):
        yield


class FailingReranker:
    """Reranker double whose rerank call always raises (same shape as
    tests/test_rag_engine.py's FailingReranker)."""

    async def rerank(self, *args, **kwargs):
        raise RuntimeError("Reranker service unavailable")


async def test_rerank_exception_is_not_disabled():
    """rerank_status must not read 'disabled' when reranking was enabled and
    attempted but the reranker raised — that is a failed attempt, not a
    disabled feature. T1-32 rerank-status acceptance check (issue #715)."""
    engine = RAGEngine()
    engine.embedding_service = cast(
        EmbeddingService, FakeEmbeddingService([0.1, 0.2])
    )
    engine.vector_store = cast(
        VectorStore,
        FakeVectorStore([
            {"text": "chunk content", "file_id": "f1", "metadata": {},
             "score": 0.9}
        ]),
    )
    engine.memory_store = cast(MemoryStore, FakeMemoryStore())
    engine.llm_client = cast(LLMClient, FakeLLMClient(response="LLM answer"))
    engine.reranking_enabled = True
    engine.reranking_service = cast(Any, FailingReranker())

    result = await engine._execute_retrieval(
        query_embeddings=[("original", [0.1, 0.2])],
        user_input="test query",
        vault_id=None,
        variants_dropped=[],
    )

    rerank_status = result[7]
    assert rerank_status != "disabled", (
        "an enabled-but-failed rerank attempt must not be reported as "
        f"'disabled' (got rerank_status={rerank_status!r}; tuple={result!r})"
    )
