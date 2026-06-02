"""
Unit tests for the RerankingService contract changes (Task 1.1).

Tests cover:
1. rerank() returns Tuple[List[dict], bool] — (chunks, success)
2. On success: all chunks have _rerank_score in [0, 1] (sigmoid-normalized)
3. On exception/fallback: returns (chunks[:n], False) — no _rerank_score
4. Empty chunks → returns ([], True)
5. Single chunk → returns (chunks, True)
6. Sigmoid normalization properties (via _safe_sigmoid)
7. Unconditional sigmoid: all scores are normalized, no double-norm prevention
8. _rerank_via_endpoint and _rerank_local both use unconditional _safe_sigmoid
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.document_retrieval import DocumentRetrievalService, RAGSource
from app.services.reranking import RerankingService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_chunks():
    """Five simple chunk dicts with a text key."""
    return [
        {"text": "The quick brown fox", "id": "1"},
        {"text": "jumps over the lazy dog", "id": "2"},
        {"text": "Pack my box with five dozen", "id": "3"},
        {"text": "liquor jugs in a row", "id": "4"},
        {"text": "Sphinx of black quartz judge my vow", "id": "5"},
    ]


@pytest.fixture
def service_with_url():
    """Service configured to use a TEI endpoint (mocked)."""
    return RerankingService(reranker_url="http://reranker.local", reranker_model="", top_n=3)


@pytest.fixture
def service_without_url():
    """Service configured to use local CrossEncoder (mocked)."""
    return RerankingService(reranker_url="", reranker_model="cross-encoder/ms-marco", top_n=3)


# ---------------------------------------------------------------------------
# 1. Contract: rerank() returns Tuple[List[dict], bool]
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rerank_returns_tuple_of_list_and_bool(service_with_url, sample_chunks, monkeypatch):
    """rerank() must return exactly a (list, bool) tuple."""
    mock_response = MagicMock()
    mock_response.json.return_value = [
        {"index": 0, "score": 5.0},
        {"index": 1, "score": 3.0},
        {"index": 2, "score": 1.0},
    ]
    mock_response.raise_for_status = MagicMock()

    mock_post = AsyncMock(return_value=mock_response)

    class FakeClient:
        post = mock_post

    monkeypatch.setattr(service_with_url, "_http_client", FakeClient())

    result = await service_with_url.rerank("test query", sample_chunks)

    assert isinstance(result, tuple), "rerank() must return a tuple"
    assert len(result) == 2, "rerank() tuple must have exactly 2 elements"
    assert isinstance(result[0], list), "First element must be a list"
    assert isinstance(result[1], bool), "Second element must be a bool"


# ---------------------------------------------------------------------------
# 2. Success path: chunks have _rerank_score in [0, 1]
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rerank_success_chunks_have_scores_in_01(service_with_url, sample_chunks, monkeypatch):
    """On success, every returned chunk has _rerank_score in [0, 1]."""
    mock_response = MagicMock()
    mock_response.json.return_value = [
        {"index": 0, "score": 5.0},
        {"index": 1, "score": 3.0},
        {"index": 2, "score": 1.0},
    ]
    mock_response.raise_for_status = MagicMock()

    class FakeClient:
        post = AsyncMock(return_value=mock_response)

    monkeypatch.setattr(service_with_url, "_http_client", FakeClient())

    chunks, success = await service_with_url.rerank("test query", sample_chunks)

    assert success is True
    assert len(chunks) == 3
    for chunk in chunks:
        assert "_rerank_score" in chunk, "Chunk must have _rerank_score on success"
        assert isinstance(chunk["_rerank_score"], float)
        assert 0.0 <= chunk["_rerank_score"] <= 1.0, (
            f"_rerank_score must be in [0,1], got {chunk['_rerank_score']}"
        )


@pytest.mark.asyncio
async def test_rerank_local_success_chunks_have_scores_in_01(service_without_url, sample_chunks):
    """On success via local model, every returned chunk has _rerank_score in [0, 1]."""
    with patch("app.services.reranking._get_local_model") as mock_get_model:
        mock_model = MagicMock()
        # Simulate raw logit scores (not already normalized)
        mock_model.predict.return_value = [5.0, 3.0, 1.0, -1.0, -3.0]
        mock_get_model.return_value = mock_model

        chunks, success = await service_without_url.rerank("test query", sample_chunks)

        assert success is True
        assert len(chunks) == 3
        for chunk in chunks:
            assert "_rerank_score" in chunk
            assert 0.0 <= chunk["_rerank_score"] <= 1.0


# ---------------------------------------------------------------------------
# 3. Exception fallback: returns (chunks[:n], False), no _rerank_score
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rerank_exception_returns_original_chunks_false(service_with_url, sample_chunks, monkeypatch):
    """On exception, rerank() returns (original_chunks[:n], False)."""
    monkeypatch.setattr(service_with_url, "_http_client", None)

    # Patch the HTTP client init to raise
    async def boom(*args, **kwargs):
        raise RuntimeError("Connection refused")

    with patch("httpx.AsyncClient") as MockClient:
        instance = MagicMock()
        instance.post = AsyncMock(side_effect=RuntimeError("Connection refused"))
        MockClient.return_value = instance
        instance.post = AsyncMock(side_effect=RuntimeError("Connection refused"))

        chunks, success = await service_with_url.rerank("test query", sample_chunks)

    assert success is False
    assert len(chunks) == 3, "Should return at most top_n chunks"
    for chunk in chunks:
        assert "_rerank_score" not in chunk, (
            "Fallback chunks must NOT have _rerank_score"
        )


@pytest.mark.asyncio
async def test_rerank_exception_preserves_original_data(service_with_url, sample_chunks):
    """Fallback chunks must preserve original chunk data (not mutated)."""
    with patch("httpx.AsyncClient") as MockClient:
        instance = MagicMock()
        instance.post = AsyncMock(side_effect=RuntimeError("network error"))
        MockClient.return_value = instance

        chunks, success = await service_with_url.rerank("test query", sample_chunks)

    assert success is False
    assert chunks[0]["text"] == "The quick brown fox"
    assert chunks[0]["id"] == "1"


# ---------------------------------------------------------------------------
# 4. Empty chunks → returns ([], True)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rerank_empty_chunks_returns_empty_list_true(service_with_url):
    """With empty input, rerank() returns ([], True) — success for empty input."""
    chunks, success = await service_with_url.rerank("test query", [])

    assert success is True
    assert chunks == []


# ---------------------------------------------------------------------------
# 5. Single chunk → returns (chunks, True) without calling reranker
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rerank_single_chunk_bypasses_reranker(service_with_url, sample_chunks):
    """A single chunk is returned immediately without invoking the reranker."""
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value = MagicMock()

        chunks, success = await service_with_url.rerank("test query", [sample_chunks[0]])

    assert success is True
    assert len(chunks) == 1
    assert chunks[0]["text"] == "The quick brown fox"
    assert "_rerank_score" not in chunks[0], (
        "Single-chunk path must not add _rerank_score"
    )


# ---------------------------------------------------------------------------
# 6. Sigmoid normalization properties
# ---------------------------------------------------------------------------

from app.services.reranking import _safe_sigmoid


class TestSigmoidNormalization:
    """Property-based tests for _safe_sigmoid normalization invariants."""

    def test_sigmoid_logit_zero_equals_point_five(self):
        """_safe_sigmoid(0) must equal exactly 0.5."""
        assert _safe_sigmoid(0.0) == 0.5

    def test_sigmoid_large_positive_logit_tends_to_one(self):
        """Large positive logits must produce scores approaching 1.0."""
        for logit in [5.0, 10.0, 20.0]:
            score = _safe_sigmoid(logit)
            assert 0.99 < score <= 1.0, f"_safe_sigmoid({logit}) should be near 1.0, got {score}"

    def test_sigmoid_large_negative_logit_tends_to_zero(self):
        """Large negative logits must produce scores approaching 0.0."""
        for logit in [-5.0, -10.0, -20.0]:
            score = _safe_sigmoid(logit)
            assert 0.0 <= score < 0.01, f"_safe_sigmoid({logit}) should be near 0.0, got {score}"

    def test_sigmoid_midpoint_returns_half(self):
        """_safe_sigmoid is monotonic; midpoint value must be 0.5."""
        # The logit that maps to 0.5 is always 0 (by definition)
        assert _safe_sigmoid(0.0) == 0.5

    def test_sigmoid_range_is_01_for_all_reals(self):
        """_safe_sigmoid must produce values in (0, 1) for any finite real input."""
        test_logits = [-10.0, -1.0, -0.5, 0.0, 0.5, 1.0, 10.0]
        for logit in test_logits:
            score = _safe_sigmoid(logit)
            assert 0.0 <= score <= 1.0, f"_safe_sigmoid({logit}) out of range: {score}"

    def test_sigmoid_monotonic_increasing(self):
        """_safe_sigmoid is monotonically increasing."""
        prev = _safe_sigmoid(-10.0)
        for logit in [-1.0, 0.0, 1.0, 10.0]:
            curr = _safe_sigmoid(logit)
            assert curr > prev, f"_safe_sigmoid must be monotonic: _safe_sigmoid({prev})={prev} !< {curr}=_safe_sigmoid({logit})"
            prev = curr

    def test_sigmoid_overflow_positive(self):
        """_safe_sigmoid(710) must return 1.0 (overflow guard)."""
        assert _safe_sigmoid(710.0) == 1.0

    def test_sigmoid_overflow_negative(self):
        """_safe_sigmoid(-710) must return 0.0 (overflow guard)."""
        assert _safe_sigmoid(-710.0) == 0.0

    def test_sigmoid_very_large_positive(self):
        """_safe_sigmoid(800) must return 1.0 (extreme overflow guard)."""
        assert _safe_sigmoid(800.0) == 1.0

    def test_sigmoid_very_large_negative(self):
        """_safe_sigmoid(-800) must return 0.0 (extreme overflow guard)."""
        assert _safe_sigmoid(-800.0) == 0.0


@pytest.mark.asyncio
async def test_rerank_endpoint_raw_logits_produce_sigmoid_scores(service_with_url, sample_chunks, monkeypatch):
    """Endpoint scores that are raw logits must be sigmoid-normalized to [0,1]."""
    # Return raw logits (not bounded in [0,1])
    mock_response = MagicMock()
    mock_response.json.return_value = [
        {"index": 0, "score": 0.0},    # sigmoid(0) = 0.5
        {"index": 1, "score": 10.0},   # sigmoid(10) → 1.0
        {"index": 2, "score": -10.0},  # sigmoid(-10) → 0.0
    ]
    mock_response.raise_for_status = MagicMock()

    class FakeClient:
        post = AsyncMock(return_value=mock_response)

    monkeypatch.setattr(service_with_url, "_http_client", FakeClient())

    chunks, success = await service_with_url.rerank("test query", sample_chunks)

    assert success is True
    # Check specific values: sigmoid(0)=0.5, sigmoid(10)≈1, sigmoid(-10)≈0
    scores = {c["text"]: c["_rerank_score"] for c in chunks}
    assert abs(scores["The quick brown fox"] - 0.5) < 0.01
    assert abs(scores["jumps over the lazy dog"] - 1.0) < 0.01
    assert abs(scores["Pack my box with five dozen"] - 0.0) < 0.01


# ---------------------------------------------------------------------------
# Timeout configuration: TEI client honors settings.reranker_timeout_seconds
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rerank_endpoint_uses_configured_timeout(service_with_url, sample_chunks, monkeypatch):
    """The lazily-created TEI HTTP client uses settings.reranker_timeout_seconds
    rather than a hardcoded value, so operators can bound worst-case latency."""
    import app.services.reranking as reranking_module

    monkeypatch.setattr(service_with_url, "_http_client", None)
    monkeypatch.setattr(reranking_module.settings, "reranker_timeout_seconds", 7.5)

    mock_response = MagicMock()
    mock_response.json.return_value = [{"index": 0, "score": 1.0}]
    mock_response.raise_for_status = MagicMock()

    captured = {}

    def fake_client_ctor(*args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        inst = MagicMock()
        inst.post = AsyncMock(return_value=mock_response)
        return inst

    # Patch the SSRF/DNS guard so the test does not depend on resolving the
    # fake endpoint host — we only care that the lazily-created client picks up
    # the configured timeout.
    with patch("app.services.reranking.assert_url_safe", return_value=None), \
            patch("httpx.AsyncClient", side_effect=fake_client_ctor):
        await service_with_url.rerank("test query", sample_chunks)

    assert captured["timeout"] == 7.5


# ---------------------------------------------------------------------------
# 7. Unconditional sigmoid normalization tests
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 7. Unconditional sigmoid normalization tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rerank_local_raw_logits_get_normalized(service_without_url, sample_chunks):
    """
    If local CrossEncoder returns raw logits (some > 1 or < 0),
    sigmoid must be applied.
    """
    with patch("app.services.reranking._get_local_model") as mock_get_model:
        mock_model = MagicMock()
        # Raw logits (out of [0,1] range)
        mock_model.predict.return_value = [10.0, 2.0, 0.0, -2.0, -10.0]
        mock_get_model.return_value = mock_model

        chunks, success = await service_without_url.rerank("test query", sample_chunks)

        assert success is True
        score_values = sorted(c["_rerank_score"] for c in chunks)
        # sigmoid(10) ≈ 0.99995, sigmoid(2) ≈ 0.8808, sigmoid(0) = 0.5
        # top_n=3 → normalized scores [~0.5, ~0.881, ~0.99995]
        # sorted ascending: [~0.5, ~0.881, ~0.99995]
        assert 0.49 < score_values[0] < 0.51   # sigmoid(0)
        assert 0.87 < score_values[1] < 0.89  # sigmoid(2)
        assert 0.99 < score_values[2] < 1.0   # sigmoid(10)


@pytest.mark.asyncio
async def test_rerank_local_negative_logits_trigger_sigmoid(service_without_url, sample_chunks):
    """
    A single negative logit (< 0) must trigger sigmoid for ALL scores.
    The double-norm guard checks score < 0, not just score > 1.
    """
    with patch("app.services.reranking._get_local_model") as mock_get_model:
        mock_model = MagicMock()
        # top 3 scores [1.5, 0.8, 0.5] all > 1 → triggers sigmoid
        mock_model.predict.return_value = [1.5, 0.8, 0.5, 0.2, -1.0]
        mock_get_model.return_value = mock_model

        chunks, success = await service_without_url.rerank("test query", sample_chunks)

        assert success is True
        # All scores should be sigmoid-normalized
        for chunk in chunks:
            assert 0.0 <= chunk["_rerank_score"] <= 1.0
        # Verify sigmoid was applied (e.g. 1.5 → sigmoid(1.5) ≈ 0.817, not 1.5)
        score_values = sorted(c["_rerank_score"] for c in chunks)
        # top_n=3 → highest sigmoid-normalized score ≈ sigmoid(0.5) ≈ 0.622
        assert score_values[-1] < 0.85  # definitely not 1.5


# ---------------------------------------------------------------------------
# Edge cases: top_n boundary
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rerank_top_n_larger_than_chunks(service_with_url, sample_chunks, monkeypatch):
    """top_n > len(chunks) must not raise; returns all reranked chunks."""
    mock_response = MagicMock()
    mock_response.json.return_value = [
        {"index": 0, "score": 1.0},
        {"index": 1, "score": 0.8},
        {"index": 2, "score": 0.6},
    ]
    mock_response.raise_for_status = MagicMock()

    class FakeClient:
        post = AsyncMock(return_value=mock_response)

    monkeypatch.setattr(service_with_url, "_http_client", FakeClient())

    # Request more than available chunks
    chunks, success = await service_with_url.rerank("test query", sample_chunks, top_n=100)

    assert success is True
    assert len(chunks) <= len(sample_chunks)


@pytest.mark.asyncio
async def test_rerank_preserves_order_when_scores_equal(service_with_url, sample_chunks, monkeypatch):
    """When two chunks have identical scores, their relative order is determined by sort."""
    mock_response = MagicMock()
    # All scores equal — sort by original index (stable sort / insertion order)
    mock_response.json.return_value = [
        {"index": 0, "score": 1.0},
        {"index": 2, "score": 1.0},
        {"index": 1, "score": 1.0},
    ]
    mock_response.raise_for_status = MagicMock()

    class FakeClient:
        post = AsyncMock(return_value=mock_response)

    monkeypatch.setattr(service_with_url, "_http_client", FakeClient())

    chunks, success = await service_with_url.rerank("test query", sample_chunks)

    assert success is True
    # The sort is descending by score; items with same score retain their
    # relative order from the sorted list (index 0, 2, 1)
    ids = [c.get("id") for c in chunks]
    assert ids == ["1", "3", "2"]


# ---------------------------------------------------------------------------
# Circuit breaker: handled gracefully (caught and logged, not propagated)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rerank_circuit_breaker_returns_fallback(service_with_url, sample_chunks):
    """CircuitBreakerError is caught by the generic except and returns fallback."""
    from app.services.circuit_breaker import CircuitBreakerError

    with patch("httpx.AsyncClient") as MockClient:
        instance = MagicMock()
        instance.post = AsyncMock(side_effect=CircuitBreakerError("open"))
        MockClient.return_value = instance

        # CircuitBreakerError is caught internally and returns (chunks[:n], False)
        chunks, success = await service_with_url.rerank("test query", sample_chunks)

        assert success is False
        assert len(chunks) == 3


# ---------------------------------------------------------------------------
# End-to-end score contract: rerank_score → RAGSource.score → score_type
# ---------------------------------------------------------------------------

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestEndToEndScoreContract:
    """Contract tests verifying the full reranking → RAGSource.score pipeline."""

    @pytest.mark.asyncio
    async def test_reranked_chunks_flow_to_rag_source_score(self):
        """Reranked chunks: _rerank_score flows through filter_relevant to RAGSource.score."""
        # Create reranked chunks with _rerank_score in [0, 1] and _distance (stale)
        reranked_chunks = [
            {"text": "chunk a", "id": "1", "_rerank_score": 0.95, "_distance": 0.5},
            {"text": "chunk b", "id": "2", "_rerank_score": 0.87, "_distance": 0.3},
            {"text": "chunk c", "id": "3", "_rerank_score": 0.72, "_distance": 0.7},
        ]

        with patch("app.services.document_retrieval.settings") as mock:
            mock.max_distance_threshold = 1.0
            mock.retrieval_top_k = 10
            mock.retrieval_window = 0
            mock.rag_relevance_threshold = 0.5
            service = DocumentRetrievalService(
                vector_store=None,
                max_distance_threshold=1.0,
                retrieval_top_k=10,
                retrieval_window=0,
            )

        # filter_relevant with reranked=True should use _rerank_score, not _distance
        sources = await service.filter_relevant(reranked_chunks, reranked=True)

        assert len(sources) == 3
        for chunk, source in zip(reranked_chunks, sources):
            assert isinstance(source, RAGSource)
            assert source.score == chunk["_rerank_score"], (
                f"RAGSource.score ({source.score}) should equal _rerank_score "
                f"({chunk['_rerank_score']}), not _distance ({chunk['_distance']})"
            )
            # Ensure it is NOT the distance value
            assert source.score != chunk["_distance"]

    @pytest.mark.asyncio
    async def test_fallback_chunks_flow_to_rag_source_as_distance(self):
        """Non-reranked chunks: _distance flows through filter_relevant to RAGSource.score."""
        # Create non-reranked chunks with _distance but no _rerank_score
        non_reranked_chunks = [
            {"text": "chunk x", "id": "10", "_distance": 0.2},
            {"text": "chunk y", "id": "11", "_distance": 0.4},
            {"text": "chunk z", "id": "12", "_distance": 0.6},
        ]

        with patch("app.services.document_retrieval.settings") as mock:
            mock.max_distance_threshold = 1.0
            mock.retrieval_top_k = 10
            mock.retrieval_window = 0
            mock.rag_relevance_threshold = 0.5
            service = DocumentRetrievalService(
                vector_store=None,
                max_distance_threshold=1.0,
                retrieval_top_k=10,
                retrieval_window=0,
            )

        # filter_relevant with reranked=False should use _distance
        sources = await service.filter_relevant(non_reranked_chunks, reranked=False)

        assert len(sources) == 3
        for chunk, source in zip(non_reranked_chunks, sources):
            assert isinstance(source, RAGSource)
            assert source.score == chunk["_distance"], (
                f"RAGSource.score ({source.score}) should equal _distance ({chunk['_distance']})"
            )


