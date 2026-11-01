"""Issue #715 acceptance check — audit finding T1-32 (maintenance gate).

``test_eval_retrieval_respects_maintenance``

Contract under test: ``retrieve_eval_results`` (the retrieval-eval /
benchmark surface) must respect ``maintenance_mode`` exactly like the chat
``query()`` path does — ZERO vector-store searches while the RAG index is
under maintenance. A benchmark run during maintenance otherwise records
searches against a half-rebuilt index, producing bogus recall numbers.

Root cause verified at HEAD (665bbd09): ``_retrieve_eval_outcome`` calls
``_execute_retrieval`` directly (rag_engine.py ~L4305) with no maintenance
check. The gates only exist on the query()/orchestration paths
(rag_engine.py ~L1673 / L1803 / L2698 / L2794), so the eval path performs
exactly one ``store.search`` call today.

This check asserts the DESIRED post-fix behavior and is RED at HEAD with::

    AssertionError: assert 1 == 0

Harness reuse: the ``CountingVectorStore`` (records every search call) and
the fake services are imported from ``tests/test_issue494_rag_engine.py``;
the engine construction mirrors that module's ``_make_engine`` and the
``retrieve_eval_results`` driving style of ``tests/test_rag_engine.py``.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Reuse the counting store + fake services from the issue-494 harness.
from test_issue494_rag_engine import (  # noqa: E402
    CountingLLMClient,
    CountingVectorStore,
    FakeEmbeddingService,
    FakeMemoryStore,
)

from app.config import settings  # noqa: E402
from app.services.rag_engine import RAGEngine  # noqa: E402


@pytest.fixture(autouse=True)
def _patch_ssrf():
    """Prevent SSRF guard from blocking service construction in tests
    (same fixture as tests/test_rag_engine.py)."""
    with patch("app.services.embeddings.assert_url_safe"), patch(
        "app.services.llm_client.assert_url_safe"
    ):
        yield


class EvalCountingVectorStore(CountingVectorStore):
    """CountingVectorStore + the parent-window seam the eval path touches.

    ``retrieve_eval_results`` runs the real DocumentRetrievalService, whose
    parent-window expansion calls ``get_chunks_by_uid``; no adjacent chunks
    in the fake keeps the run focused on the search-call count.
    """

    async def get_chunks_by_uid(self, chunk_uids):
        return []


async def test_eval_retrieval_respects_maintenance():
    """maintenance_mode=True must gate retrieve_eval_results to zero
    vector-store searches (parity with the query() maintenance gate).
    T1-32 maintenance-gate acceptance check (issue #715)."""
    store = EvalCountingVectorStore()
    llm = CountingLLMClient()

    with patch("app.services.embeddings.assert_url_safe"), patch(
        "app.services.llm_client.assert_url_safe"
    ):
        engine = RAGEngine(
            embedding_service=FakeEmbeddingService(),
            vector_store=store,
            memory_store=FakeMemoryStore(),
            llm_client=llm,
        )
    engine.maintenance_mode = True
    # Keep this test off any developer sqlite DB — the visibility-filter
    # lookup is irrelevant to the maintenance gate under test.
    engine._get_indexed_file_ids = MagicMock(return_value=None)

    with patch.object(settings, "query_transformation_enabled", False):
        await engine.retrieve_eval_results("test query", vault_id=1)

    assert len(store.search_calls) == 0, (
        "retrieve_eval_results must perform ZERO vector-store searches while "
        "maintenance_mode is set — the eval path currently bypasses the "
        f"maintenance gate (search_calls={store.search_calls!r})"
    )
