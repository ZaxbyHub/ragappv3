"""
Unit tests for score sourcing in filter_relevant (Task 1.1).

Tests cover _rerank_score validation when reranked=True:
1. Valid _rerank_score values (0.85, 0.0, 1.0) are used as RAGSource.score
2. Invalid _rerank_score values (None, string, out-of-range, NaN) fall back to distance
3. When reranked=False, _rerank_score is ignored and distance is used
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
        mock.per_doc_chunk_cap = 5
        mock.unique_docs_in_top_k = 5
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
def base_record():
    """Base record with distance within threshold."""
    return {"_distance": 0.3, "text": "test doc", "file_id": "f1", "id": "chunk1"}


# ---------------------------------------------------------------------------
# Tests — reranked=True with valid _rerank_score
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reranked_true_valid_rerank_score_085(service, base_record):
    """
    When reranked=True and _rerank_score is a valid float (0.85),
    the RAGSource.score should be 0.85 (not the distance).
    """
    record = {**base_record, "_rerank_score": 0.85}
    sources = await service.filter_relevant([record], reranked=True)

    assert len(sources) == 1
    assert sources[0].score == 0.85


@pytest.mark.asyncio
async def test_reranked_true_valid_rerank_score_0(service, base_record):
    """
    When reranked=True and _rerank_score is 0.0,
    the RAGSource.score should be 0.0.
    """
    record = {**base_record, "_rerank_score": 0.0}
    sources = await service.filter_relevant([record], reranked=True)

    assert len(sources) == 1
    assert sources[0].score == 0.0


@pytest.mark.asyncio
async def test_reranked_true_valid_rerank_score_1(service, base_record):
    """
    When reranked=True and _rerank_score is 1.0,
    the RAGSource.score should be 1.0.
    """
    record = {**base_record, "_rerank_score": 1.0}
    sources = await service.filter_relevant([record], reranked=True)

    assert len(sources) == 1
    assert sources[0].score == 1.0


# ---------------------------------------------------------------------------
# Tests — reranked=True with invalid _rerank_score falls back to distance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reranked_true_rerank_score_none_falls_back_to_distance(service, base_record):
    """
    When reranked=True but _rerank_score is None,
    falls back to distance (0.3).
    """
    record = {**base_record, "_rerank_score": None}
    sources = await service.filter_relevant([record], reranked=True)

    assert len(sources) == 1
    assert sources[0].score == 0.3  # falls back to distance


@pytest.mark.asyncio
async def test_reranked_true_rerank_score_string_falls_back_to_distance(service, base_record):
    """
    When reranked=True but _rerank_score is a string ("bad"),
    falls back to distance (0.3).
    """
    record = {**base_record, "_rerank_score": "bad"}
    sources = await service.filter_relevant([record], reranked=True)

    assert len(sources) == 1
    assert sources[0].score == 0.3  # falls back to distance


@pytest.mark.asyncio
async def test_reranked_true_rerank_score_out_of_range_high_falls_back_to_distance(
    service, base_record
):
    """
    When reranked=True but _rerank_score is 1.5 (> 1.0),
    falls back to distance (0.3).
    """
    record = {**base_record, "_rerank_score": 1.5}
    sources = await service.filter_relevant([record], reranked=True)

    assert len(sources) == 1
    assert sources[0].score == 0.3  # falls back to distance


@pytest.mark.asyncio
async def test_reranked_true_rerank_score_out_of_range_low_falls_back_to_distance(
    service, base_record
):
    """
    When reranked=True but _rerank_score is -0.1 (< 0.0),
    falls back to distance (0.3).
    """
    record = {**base_record, "_rerank_score": -0.1}
    sources = await service.filter_relevant([record], reranked=True)

    assert len(sources) == 1
    assert sources[0].score == 0.3  # falls back to distance


@pytest.mark.asyncio
async def test_reranked_true_rerank_score_nan_falls_back_to_distance(service, base_record):
    """
    When reranked=True but _rerank_score is float('nan'),
    falls back to distance (0.3).
    """
    record = {**base_record, "_rerank_score": float("nan")}
    sources = await service.filter_relevant([record], reranked=True)

    assert len(sources) == 1
    # EXACT fallback: the runtime band check (0.0 <= x <= 1.0) rejects NaN,
    # so the score must fall back to the distance (0.3) — never NaN itself.
    # (Legacy form asserted `math.isnan(score) or score == 0.3`, which also
    # passed when NaN leaked through — issue #258 TEST-001 / C1b.)
    assert sources[0].score == 0.3


# ---------------------------------------------------------------------------
# Tests — reranked=False ignores _rerank_score
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reranked_false_ignores_rerank_score_uses_distance(
    service, base_record
):
    """
    When reranked=False, _rerank_score is ignored even if present.
    RAGSource.score should be the distance (0.3).
    """
    record = {**base_record, "_rerank_score": 0.99}
    sources = await service.filter_relevant([record], reranked=False)

    assert len(sources) == 1
    assert sources[0].score == 0.3  # uses distance, ignores _rerank_score


@pytest.mark.asyncio
async def test_reranked_false_without_rerank_score_uses_distance(service, base_record):
    """
    When reranked=False and _rerank_score is not present,
    RAGSource.score should be the distance (existing behavior).
    """
    # base_record doesn't have _rerank_score
    sources = await service.filter_relevant([base_record], reranked=False)

    assert len(sources) == 1
    assert sources[0].score == 0.3  # uses distance


# ---------------------------------------------------------------------------
# Tests — multiple records with different _rerank_score validity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reranked_true_mixed_valid_and_invalid_scores(service):
    """
    When reranked=True with multiple records having valid and invalid _rerank_score,
    each score should be resolved independently.
    """
    records = [
        {"_distance": 0.1, "text": "doc1", "file_id": "f1", "id": "c1", "_rerank_score": 0.9},
        {"_distance": 0.2, "text": "doc2", "file_id": "f2", "id": "c2", "_rerank_score": None},
        {"_distance": 0.3, "text": "doc3", "file_id": "f3", "id": "c3", "_rerank_score": 0.5},
        {"_distance": 0.4, "text": "doc4", "file_id": "f4", "id": "c4", "_rerank_score": "invalid"},
    ]
    sources = await service.filter_relevant(records, reranked=True)

    assert len(sources) == 4
    # Find sources by text to check their scores
    scores_by_text = {s.text: s.score for s in sources}

    assert scores_by_text["doc1"] == 0.9  # valid _rerank_score
    assert scores_by_text["doc2"] == 0.2  # None → falls back to distance
    assert scores_by_text["doc3"] == 0.5  # valid _rerank_score
    assert scores_by_text["doc4"] == 0.4  # "invalid" → falls back to distance
