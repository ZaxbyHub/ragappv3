"""Issue #258 (E2) acceptance checks — AC1 / TEST-001: retrieval top_k contract.

Phase 2.5 CHECKS ONLY (tier L). These nodes pin the two halves of the
documented retrieval contract that the Phase-4 repair will re-assert when the
misleading tests in ``test_filter_relevant_reranked.py`` are renamed/removed:

Node (a) — PRESERVING pin of the PUBLIC caller cap.
    ``DocumentRetrievalService.filter_relevant`` deliberately does NOT cap at
    ``top_k`` (it returns every surviving source; ``expand_window`` re-reads
    ``retrieval_top_k`` on its own). The public cap exists only at the engine
    caller: ``RAGEngine._retrieve_eval_outcome`` slices
    ``capped = relevant_chunks[:top_k]`` (backend/app/services/rag_engine.py,
    the ``# Apply relevance filter and top_k cap`` block). This node executes
    the REAL ``_retrieve_eval_outcome`` body with a real
    ``DocumentRetrievalService`` that returns MORE eligible chunks than
    ``top_k`` and asserts the outcome is cut to exactly ``top_k``. A mutant
    that drops the public cap (returns all 7) fails here.

Node (b) — PRESERVING pin of the NaN-exact fallback.
    When ``_rerank_score`` is NaN, the runtime band check
    (``0.0 <= float(x) <= 1.0``) rejects it and the score falls back to the
    distance — exactly. The legacy test
    (``test_score_sourcing.py::test_reranked_true_rerank_score_nan_falls_back_to_distance``)
    accepts ``math.isnan(score) or score == 0.3`` — a disjunction that also
    passes if the NaN leaks through. This node asserts ``score == 0.3``
    EXACTLY, with no isnan disjunction, killing that drift direction.

Measured class at base a543361: BOTH nodes are PRESERVING (green at base).
That is the honest classification: the production behavior is already correct;
the defect was that the OLD tests asserted the wrong contract (uncapped
returns, loose NaN disjunction). Phase 4 renames/removes those theater tests;
these pins make the new contract ungameable. Mutation probes for the cap are
Phase 4.5 territory.
"""

import math
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Per-file optional-dependency stubs (load-bearing for the CI dependency set —
# see docs/engineering/testing.md section 2; do not remove).
try:
    import lancedb  # noqa: F401
except ImportError:
    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow  # noqa: F401
except ImportError:
    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    _unstructured = types.ModuleType("unstructured")
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType("unstructured.partition")
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType("unstructured.partition.auto")
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    _unstructured.chunking = types.ModuleType("unstructured.chunking")
    _unstructured.chunking.__path__ = []
    _unstructured.chunking.title = types.ModuleType("unstructured.chunking.title")
    _unstructured.chunking.title.chunk_by_title = lambda *args, **kwargs: []
    _unstructured.documents = types.ModuleType("unstructured.documents")
    _unstructured.documents.__path__ = []
    _unstructured.documents.elements = types.ModuleType(
        "unstructured.documents.elements"
    )
    _unstructured.documents.elements.Element = type("Element", (), {})
    sys.modules["unstructured"] = _unstructured
    sys.modules["unstructured.partition"] = _unstructured.partition
    sys.modules["unstructured.partition.auto"] = _unstructured.partition.auto
    sys.modules["unstructured.chunking"] = _unstructured.chunking
    sys.modules["unstructured.chunking.title"] = _unstructured.chunking.title
    sys.modules["unstructured.documents"] = _unstructured.documents
    sys.modules["unstructured.documents.elements"] = _unstructured.documents.elements

from app.services.document_retrieval import (  # noqa: E402
    DocumentRetrievalService,
    RAGSource,
)

_TOP_K = 3
_ELIGIBLE = 7


def _record(idx: int, distance: float = 0.3) -> dict:
    """A raw search result that passes the relevance band."""
    return {
        "_distance": distance,
        "text": f"chunk {idx}",
        "file_id": f"f{idx}",
        "id": f"chunk{idx}",
        "metadata": {"source_file": "doc.txt"},
    }


def _patched_document_retrieval_settings(mock_settings) -> None:
    """Controlled settings for the real DocumentRetrievalService."""
    mock_settings.max_distance_threshold = 1.0
    mock_settings.retrieval_top_k = _ELIGIBLE
    mock_settings.retrieval_window = 0
    mock_settings.rag_relevance_threshold = 0.5
    mock_settings.per_doc_chunk_cap = _ELIGIBLE
    mock_settings.unique_docs_in_top_k = _ELIGIBLE
    mock_settings.new_dedup_policy = False


class TestPublicCallerTopKCap(unittest.IsolatedAsyncioTestCase):
    """AC1 node (a): the engine's public retrieval path caps at top_k."""

    async def test_retrieve_eval_outcome_caps_at_top_k(self) -> None:
        with patch("app.services.document_retrieval.settings") as mock_settings:
            _patched_document_retrieval_settings(mock_settings)

            retrieval = DocumentRetrievalService(
                vector_store=None,
                max_distance_threshold=1.0,
                retrieval_top_k=_ELIGIBLE,
                retrieval_window=0,
            )

            # Sanity precondition: with more eligible results than top_k the
            # REAL filter_relevant returns ALL of them (the helper itself does
            # not cap — that is the documented helper contract; the cap is the
            # engine caller's job).
            eligible = await retrieval.filter_relevant(
                [_record(i) for i in range(_ELIGIBLE)], reranked=False
            )
            self.assertEqual(len(eligible), _ELIGIBLE)

            from app.services.rag_engine import RAGEngine

            engine = RAGEngine(
                embedding_service=MagicMock(),
                vector_store=MagicMock(),
                memory_store=MagicMock(),
                llm_client=MagicMock(),
                document_retrieval_service=retrieval,
            )
            # Seams ABOVE the production cap block: everything from embedding
            # generation to raw vector results is stubbed; the filter, the
            # cap, and the outcome assembly below run for real.
            engine._get_indexed_file_ids = MagicMock(return_value=None)
            engine._build_query_embeddings = AsyncMock(return_value=([[0.1]], 0))
            raw_results = [_record(i) for i in range(_ELIGIBLE)]
            engine._execute_retrieval = AsyncMock(
                return_value=(raw_results, [], None, None, None, None, None, None, None, None, None)
            )

            # AC1 CHECK — public-caller top_k cap absent: the engine returned
            # more file ids than the requested top_k.
            print("AC1 CHECK: FAIL — public-caller top_k cap absent (engine returned more ids than top_k)")
            outcome = await engine._retrieve_eval_outcome("query", vault_id=1, top_k=_TOP_K)
            self.assertEqual(len(outcome.retrieved_ids), _TOP_K)
            # The cap keeps the BEST-ranked prefix, not an arbitrary subset.
            self.assertEqual(
                outcome.retrieved_ids, [f"f{i}" for i in range(_TOP_K)]
            )
            self.assertEqual(outcome.status, "ok")


class TestNaNExactFallback(unittest.IsolatedAsyncioTestCase):
    """AC1 node (b): NaN _rerank_score falls back to distance — exactly."""

    async def test_nan_rerank_score_returns_distance_exactly(self) -> None:
        with patch("app.services.document_retrieval.settings") as mock_settings:
            _patched_document_retrieval_settings(mock_settings)

            service = DocumentRetrievalService(
                vector_store=None,
                max_distance_threshold=1.0,
                retrieval_top_k=5,
                retrieval_window=0,
            )
            record = {
                "_distance": 0.3,
                "text": "nan score doc",
                "file_id": "f1",
                "id": "chunk1",
                "_rerank_score": float("nan"),
            }
            sources: list = await service.filter_relevant([record], reranked=True)

            self.assertEqual(len(sources), 1)
            score = sources[0].score
            # AC1 CHECK — NaN leaked into RAGSource.score (exact distance
            # fallback not applied; the old isnan-disjunction masked this).
            print("AC1 CHECK: FAIL — NaN leaked into RAGSource.score (exact distance fallback not applied)")
            self.assertFalse(math.isnan(score), f"score leaked NaN: {score!r}")
            self.assertEqual(score, 0.3)  # EXACT fallback to distance — no disjunction
            self.assertIsInstance(score, float)
            self.assertIsInstance(sources[0], RAGSource)


if __name__ == "__main__":
    unittest.main()
