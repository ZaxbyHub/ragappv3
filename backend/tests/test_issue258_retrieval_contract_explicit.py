"""Issue #258 (E2) — Phase-4 additive pin: the explicit/omitted top_k contract.

Authored during Phase 4 implementation (not a pre-existing Phase-2.5 check):
this file pins the two-mode ``filter_relevant`` top_k contract introduced by
the TEST-001 repair in ``backend/app/services/document_retrieval.py``:

Mode 1 — EXPLICIT top_k is a HARD CAP.
    When the caller passes a non-None ``top_k``, the final returned list is
    sliced to at most that many sources. Previously the parameter was
    silently accepted but never applied (the legacy
    ``test_reranked_*_respects_top_k`` tests even asserted the NON-capping
    outcome under a "respects top_k" name — the theater this repair removed).

Mode 2 — OMITTED top_k (None) returns the full within-threshold set.
    No cap is applied inside the helper. This is load-bearing by design and
    deliberately NOT a defect: the agentic RetrievalTool
    (backend/app/services/agentic_tools.py) consumes the uncapped
    within-threshold set, and the token-budget-governed main query path
    (backend/app/services/rag_engine.py) packs context only after receiving
    everything above threshold; the eval path caps explicitly at its own call
    site (``_retrieve_eval_outcome`` slices ``[:top_k]``).

The production docstring documents this two-mode contract; node (iii) pins
that the documentation cannot silently drift away from the behavior.
"""

import inspect
import os
import sys
import types
import unittest
from unittest.mock import patch

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Per-file optional-dependency stubs (load-bearing for CI — see
# docs/engineering/testing.md section 2; do not remove).
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

from app.services.document_retrieval import DocumentRetrievalService  # noqa: E402

_ELIGIBLE = 5
_EXPLICIT_TOP_K = 2


def _record(idx: int) -> dict:
    """A raw search result that passes the relevance band (distance 0.3 <= 1.0)."""
    return {
        "_distance": 0.3,
        "text": f"chunk {idx}",
        "file_id": f"f{idx}",
        "id": f"chunk{idx}",
        "metadata": {"source_file": "doc.txt"},
    }


class TestExplicitTopKHardCap(unittest.IsolatedAsyncioTestCase):
    """Mode 1: an explicitly-passed top_k slices the returned list exactly."""

    async def test_explicit_top_k_caps_to_exactly_top_k(self) -> None:
        with patch("app.services.document_retrieval.settings") as mock_settings:
            mock_settings.max_distance_threshold = 1.0
            mock_settings.retrieval_top_k = _ELIGIBLE
            mock_settings.retrieval_window = 0
            mock_settings.rag_relevance_threshold = 0.5
            mock_settings.per_doc_chunk_cap = _ELIGIBLE
            mock_settings.unique_docs_in_top_k = _ELIGIBLE
            mock_settings.new_dedup_policy = False

            service = DocumentRetrievalService(
                vector_store=None,
                max_distance_threshold=1.0,
                retrieval_top_k=_ELIGIBLE,
                retrieval_window=0,
            )
            results = [_record(i) for i in range(_ELIGIBLE)]

            sources = await service.filter_relevant(
                results, top_k=_EXPLICIT_TOP_K, reranked=False
            )

            # Exactly top_k sources — the cap really applies.
            self.assertEqual(len(sources), _EXPLICIT_TOP_K)
            # The cap keeps the ranked prefix, not an arbitrary subset.
            self.assertEqual(
                [s.file_id for s in sources],
                [f"f{i}" for i in range(_EXPLICIT_TOP_K)],
            )
            # Capping is not a no_match: results existed and passed threshold.
            self.assertFalse(service.no_match)

    async def test_explicit_top_k_above_eligible_returns_all(self) -> None:
        """The cap is a ceiling, not padding: top_k=10 with 5 eligible → 5."""
        with patch("app.services.document_retrieval.settings") as mock_settings:
            mock_settings.max_distance_threshold = 1.0
            mock_settings.retrieval_top_k = _ELIGIBLE
            mock_settings.retrieval_window = 0
            mock_settings.rag_relevance_threshold = 0.5
            mock_settings.per_doc_chunk_cap = _ELIGIBLE
            mock_settings.unique_docs_in_top_k = _ELIGIBLE
            mock_settings.new_dedup_policy = False

            service = DocumentRetrievalService(
                vector_store=None,
                max_distance_threshold=1.0,
                retrieval_top_k=_ELIGIBLE,
                retrieval_window=0,
            )
            sources = await service.filter_relevant(
                [_record(i) for i in range(_ELIGIBLE)], top_k=10, reranked=False
            )
            self.assertEqual(len(sources), _ELIGIBLE)


class TestOmittedTopKReturnsFullSet(unittest.IsolatedAsyncioTestCase):
    """Mode 2: an omitted top_k (None) deliberately does NOT cap.

    This is the documented agentic/main-path contract — the helper returns
    every source that survives the relevance filter so downstream consumers
    (RetrievalTool, token-budget context packing) can apply their own limits.
    """

    async def test_omitted_top_k_returns_all_eligible(self) -> None:
        with patch("app.services.document_retrieval.settings") as mock_settings:
            mock_settings.max_distance_threshold = 1.0
            # retrieval_top_k deliberately >= eligible so an unconditional
            # settings-based cap would ALSO pass; the sibling test below
            # closes that loophole.
            mock_settings.retrieval_top_k = _ELIGIBLE
            mock_settings.retrieval_window = 0
            mock_settings.rag_relevance_threshold = 0.5
            mock_settings.per_doc_chunk_cap = _ELIGIBLE
            mock_settings.unique_docs_in_top_k = _ELIGIBLE
            mock_settings.new_dedup_policy = False

            service = DocumentRetrievalService(
                vector_store=None,
                max_distance_threshold=1.0,
                retrieval_top_k=_ELIGIBLE,
                retrieval_window=0,
            )
            sources = await service.filter_relevant(
                [_record(i) for i in range(_ELIGIBLE)], reranked=False
            )
            self.assertEqual(len(sources), _ELIGIBLE)
            self.assertEqual(
                [s.file_id for s in sources],
                [f"f{i}" for i in range(_ELIGIBLE)],
            )

    async def test_omitted_top_k_ignores_retrieval_top_k_setting(self) -> None:
        """The omitted mode must not silently become a settings-based cap.

        With retrieval_top_k=2 but top_k OMITTED, all 5 eligible results are
        returned — the omitted-mode contract is "no cap here", not "cap at
        self.retrieval_top_k". (The legacy dead code
        ``if top_k is None: top_k = self.retrieval_top_k`` never applied the
        value; this pin keeps any revival honest.)
        """
        with patch("app.services.document_retrieval.settings") as mock_settings:
            mock_settings.max_distance_threshold = 1.0
            mock_settings.retrieval_top_k = 2
            mock_settings.retrieval_window = 0
            mock_settings.rag_relevance_threshold = 0.5
            mock_settings.per_doc_chunk_cap = _ELIGIBLE
            mock_settings.unique_docs_in_top_k = _ELIGIBLE
            mock_settings.new_dedup_policy = False

            service = DocumentRetrievalService(
                vector_store=None,
                max_distance_threshold=1.0,
                retrieval_top_k=2,
                retrieval_window=0,
            )
            sources = await service.filter_relevant(
                [_record(i) for i in range(_ELIGIBLE)], reranked=False
            )
            self.assertEqual(len(sources), _ELIGIBLE)


class TestTwoModeContractDocumented(unittest.TestCase):
    """Node (iii): the production docstring documents both modes."""

    def test_filter_relevant_docstring_documents_two_mode_contract(self) -> None:
        docstring = inspect.getdoc(
            DocumentRetrievalService.filter_relevant
        )
        self.assertIsNotNone(docstring)
        lowered = docstring.lower()
        # Mode 1 wording: explicit top_k as a hard cap.
        self.assertIn("hard cap", lowered)
        self.assertIn("explicitly", lowered)
        # Mode 2 wording: omitted top_k does not cap (full within-threshold set).
        self.assertIn("omitted", lowered)
        self.assertIn("no cap", lowered)


if __name__ == "__main__":
    unittest.main()
