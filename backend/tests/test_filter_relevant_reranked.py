"""
Unit tests for filter_relevant reranked parameter (Task 1.3).

Tests cover:
1. filter_relevant(..., reranked=True)  → skips distance filter (records beyond
   threshold are NOT rejected)
2. filter_relevant(..., reranked=False) → applies distance filter (records beyond
   threshold ARE rejected)
3. The old heuristic any("_rerank_score" in r...) is no longer present in the
   filter_relevant body.
4. An EXPLICITLY-passed top_k is a hard cap on the returned list in both
   reranked modes (issue #258 TEST-001; omitted-top_k behavior is pinned in
   test_issue258_retrieval_contract_explicit.py).
"""

import os
import sys
from unittest.mock import patch

import pytest

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies
try:
    import lancedb
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow
except ImportError:
    import types

    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

try:
    from unstructured.partition.auto import partition
except ImportError:
    import types

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
    _unstructured.documents.elements = types.ModuleType("unstructured.documents.elements")
    _unstructured.documents.elements.Element = type("Element", (), {})
    sys.modules["unstructured"] = _unstructured
    sys.modules["unstructured.partition"] = _unstructured.partition
    sys.modules["unstructured.partition.auto"] = _unstructured.partition.auto
    sys.modules["unstructured.chunking"] = _unstructured.chunking
    sys.modules["unstructured.chunking.title"] = _unstructured.chunking.title
    sys.modules["unstructured.documents"] = _unstructured.documents
    sys.modules["unstructured.documents.elements"] = _unstructured.documents.elements

from app.services.document_retrieval import DocumentRetrievalService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_settings():
    """Stub settings so the service can be instantiated without a real config."""
    with patch("app.services.document_retrieval.settings") as mock:
        mock.max_distance_threshold = 1.0
        mock.retrieval_top_k = 5
        mock.retrieval_window = 1
        mock.rag_relevance_threshold = 0.5
        mock.new_dedup_policy = False
        yield mock


@pytest.fixture
def service(mock_settings) -> DocumentRetrievalService:
    """Service with a controlled distance threshold of 1.0."""
    return DocumentRetrievalService(
        vector_store=None,
        max_distance_threshold=1.0,
        retrieval_top_k=5,
        retrieval_window=1,
    )


@pytest.fixture
def results_beyond_threshold():
    """
    Three records where _distance > 1.0 (the mocked threshold).

    All would be rejected if the distance filter were applied.
    Each also carries _rerank_score so we can verify that key is NOT inspected.
    """
    return [
        {"_distance": 5.0, "text": "doc far away 1", "file_id": "f1", "_rerank_score": 0.3},
        {"_distance": 9.0, "text": "doc far away 2", "file_id": "f2", "_rerank_score": 0.7},
        {"_distance": 2.5, "text": "doc far away 3", "file_id": "f3", "_rerank_score": 0.1},
    ]


@pytest.fixture
def results_within_threshold():
    """
    Three records where _distance <= 1.0 (within the mocked threshold).
    All would pass the distance filter.
    """
    return [
        {"_distance": 0.1, "text": "close doc 1", "file_id": "f4"},
        {"_distance": 0.5, "text": "close doc 2", "file_id": "f5"},
        {"_distance": 0.9, "text": "close doc 3", "file_id": "f6"},
    ]


@pytest.fixture
def mixed_results():
    """
    Four records: two within threshold, two beyond.

    Useful for verifying selective filtering when reranked=False.
    """
    return [
        {"_distance": 0.1, "text": "close 1", "file_id": "m1"},
        {"_distance": 0.9, "text": "close 2", "file_id": "m2"},
        {"_distance": 2.0, "text": "far 1", "file_id": "m3"},
        {"_distance": 5.0, "text": "far 2", "file_id": "m4"},
    ]


# ---------------------------------------------------------------------------
# Tests — reranked=True skips distance filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reranked_true_skips_distance_filter(service, results_beyond_threshold):
    """
    When reranked=True, records with _distance > max_distance_threshold are NOT
    rejected. The distance threshold is bypassed entirely.
    """
    sources = await service.filter_relevant(results_beyond_threshold, reranked=True)

    # All 3 records must be returned (none rejected by distance)
    assert len(sources) == 3
    texts = {s.text for s in sources}
    assert texts == {"doc far away 1", "doc far away 2", "doc far away 3"}
    assert service.no_match is False


@pytest.mark.asyncio
async def test_reranked_true_accepts_records_without_distance_key(service):
    """
    When reranked=True, even records that have no _distance key at all are not
    rejected by the distance filter (threshold is skipped entirely).
    """
    records = [
        {"text": "no distance 1", "file_id": "n1"},
        {"text": "no distance 2", "file_id": "n2"},
    ]
    sources = await service.filter_relevant(records, reranked=True)

    assert len(sources) == 2
    texts = {s.text for s in sources}
    assert texts == {"no distance 1", "no distance 2"}


# ---------------------------------------------------------------------------
# Tests — reranked=False applies distance filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reranked_false_rejects_records_beyond_threshold(
    service, results_beyond_threshold
):
    """
    When reranked=False (default), records with _distance > max_distance_threshold
    ARE rejected by the distance filter.
    """
    sources = await service.filter_relevant(results_beyond_threshold, reranked=False)

    # All 3 records have _distance > 1.0 → all must be rejected
    assert len(sources) == 0
    assert service.no_match is True


@pytest.mark.asyncio
async def test_reranked_false_accepts_records_within_threshold(
    service, results_within_threshold
):
    """
    When reranked=False, records with _distance <= max_distance_threshold pass.
    """
    sources = await service.filter_relevant(results_within_threshold, reranked=False)

    assert len(sources) == 3
    texts = {s.text for s in sources}
    assert texts == {"close doc 1", "close doc 2", "close doc 3"}
    assert service.no_match is False


@pytest.mark.asyncio
async def test_reranked_false_selectively_filters_mixed(service, mixed_results):
    """
    When reranked=False on mixed results, only records within threshold are kept.
    """
    sources = await service.filter_relevant(mixed_results, reranked=False)

    texts = {s.text for s in sources}
    assert texts == {"close 1", "close 2"}
    # far 1 (_distance=2.0) and far 2 (_distance=5.0) must be rejected
    assert "far 1" not in texts
    assert "far 2" not in texts


@pytest.mark.asyncio
async def test_reranked_false_default_is_reranked_false(service, results_beyond_threshold):
    """
    The default value of reranked is False, so omitting it applies the distance filter.
    """
    sources = await service.filter_relevant(results_beyond_threshold)
    assert len(sources) == 0
    assert service.no_match is True


# ---------------------------------------------------------------------------
# Tests — explicit top_k is a hard cap regardless of reranked
#
# Issue #258 (TEST-001): an EXPLICITLY-passed top_k slices the returned list
# to exactly that length. (Omitting top_k returns the full within-threshold
# set — that omitted-mode contract is pinned in
# test_issue258_retrieval_contract_explicit.py.)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reranked_true_explicit_top_k_hard_caps(service, results_within_threshold):
    """Explicit top_k caps the result list even when reranked=True skips the distance filter."""
    sources = await service.filter_relevant(
        results_within_threshold, top_k=2, reranked=True
    )
    # 3 within-threshold records exist, but explicit top_k=2 is a hard cap.
    assert len(sources) == 2
    # The cap keeps the ranked prefix (input order is the relevance order).
    assert [s.text for s in sources] == ["close doc 1", "close doc 2"]


@pytest.mark.asyncio
async def test_reranked_false_explicit_top_k_hard_caps(service, results_within_threshold):
    """Explicit top_k caps the result list when the distance filter also applies."""
    sources = await service.filter_relevant(
        results_within_threshold, top_k=1, reranked=False
    )
    # All 3 records are within the distance threshold, but explicit top_k=1
    # is a hard cap applied after filtering.
    assert len(sources) == 1
    assert sources[0].text == "close doc 1"


# ---------------------------------------------------------------------------
# Tests — old _rerank_score heuristic is gone
# ---------------------------------------------------------------------------


def test_old_rerank_score_heuristic_not_in_filter_relevant_source():
    """
    Verify that the old heuristic `any("_rerank_score" in r ...)` is not present
    in the filter_relevant method body.

    This guards against regression if someone re-introduces the heuristic.
    """
    import inspect

    import app.services.document_retrieval as dr_module

    source = inspect.getsource(dr_module.DocumentRetrievalService.filter_relevant)

    # The old heuristic looked like: any("_rerank_score" in r for r in results)
    # or any("_rerank_score" in record ...).
    # Neither pattern should appear in the method source.
    assert '("_rerank_score" in ' not in source, (
        "Old _rerank_score heuristic detected in filter_relevant source. "
        "The skip_distance_filter decision must use only the explicit reranked parameter."
    )
    assert "'_rerank_score' in " not in source, (
        "Old _rerank_score heuristic detected in filter_relevant source. "
        "The skip_distance_filter decision must use only the explicit reranked parameter."
    )


def test_skip_distance_filter_uses_reranked_parameter_only():
    """
    Verify that skip_distance_filter is assigned directly from the reranked
    parameter and not derived from any record inspection.
    """
    import inspect

    import app.services.document_retrieval as dr_module

    source = inspect.getsource(dr_module.DocumentRetrievalService.filter_relevant)

    # The correct pattern: skip_distance_filter = reranked
    # The wrong pattern: skip_distance_filter = any(...) or any(...)
    lines_with_skip = [
        line.strip()
        for line in source.splitlines()
        if "skip_distance_filter" in line
        and not line.strip().startswith("#")
        and not line.strip().startswith("if")
        and not line.strip().startswith("not")
    ]
    assert len(lines_with_skip) == 1, (
        f"Expected exactly 1 skip_distance_filter assignment, got: {lines_with_skip}"
    )
    line = lines_with_skip[0]
    assert "=" in line, f"skip_distance_filter line is not an assignment: {line}"
    rhs = line.split("=", 1)[1].strip()
    assert rhs == "reranked", (
        f"skip_distance_filter must be assigned 'reranked', got: {rhs}. "
        "Do not derive the flag from record inspection."
    )
