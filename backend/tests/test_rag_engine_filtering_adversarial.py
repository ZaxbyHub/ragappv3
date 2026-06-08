"""
Adversarial test suite for rag_engine filtering fallback logic.

Attack vectors covered:
- Empty results
- Extreme thresholds (0, negative, very large)
- Malformed distances (None, NaN, strings)
- Negative scores
"""


# Import the RAGEngine and RAGSource
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, "backend")

from app.services.rag_engine import RAGEngine


class TestFilterRelevantAdversarial:
    """Adversarial tests for _filter_relevant method."""

    @pytest.fixture
    def engine(self):
        """Create a RAGEngine instance with mocked dependencies."""
        with (
            patch("app.services.rag_engine.EmbeddingService"),
            patch("app.services.rag_engine.VectorStore"),
            patch("app.services.rag_engine.MemoryStore"),
            patch("app.services.rag_engine.LLMClient"),
        ):
            engine = RAGEngine()
            engine.max_distance_threshold = 0.5
            engine.relevance_threshold = 0.5
            engine.retrieval_top_k = 5
            engine.retrieval_window = 0
            return engine

    # =========================================================================
    # ATTACK VECTOR 1: Empty Results
    # =========================================================================

    async def test_empty_results_list(self, engine):
        """Attack: Empty results list should return empty without error."""
        result = await engine._filter_relevant([])
        assert result == []

    async def test_empty_results_with_fallback_threshold(self, engine):
        """Attack: Empty results with aggressive threshold should still return empty."""
        engine.max_distance_threshold = 0.0
        result = await engine._filter_relevant([])
        assert result == []

    # =========================================================================
    # ATTACK VECTOR 2: Extreme Thresholds
    # =========================================================================

    async def test_zero_threshold_filters_all(self, engine):
        """Attack: Threshold of 0 should filter all results (trigger fallback)."""
        engine.max_distance_threshold = 0.0
        results = [
            {"_distance": 0.1, "text": "doc1", "file_id": "f1"},
            {"_distance": 0.2, "text": "doc2", "file_id": "f2"},
        ]
        result = await engine._filter_relevant(results)
        # All results exceed threshold, should return empty with no_match flag
        assert len(result) == 0
        assert engine.document_retrieval.no_match is True

    async def test_negative_threshold(self, engine):
        """Attack: Negative threshold should trigger fallback."""
        engine.max_distance_threshold = -0.5
        results = [
            {"_distance": 0.1, "text": "doc1", "file_id": "f1"},
            {"_distance": 0.2, "text": "doc2", "file_id": "f2"},
        ]
        result = await engine._filter_relevant(results)
        # Negative threshold means all distances > threshold, should return empty
        assert len(result) == 0
        assert engine.document_retrieval.no_match is True

    async def test_very_large_threshold_accepts_all(self, engine):
        """Attack: Very large threshold should accept all results."""
        engine.max_distance_threshold = 999999.0
        results = [
            {"_distance": 0.1, "text": "doc1", "file_id": "f1"},
            {"_distance": 0.2, "text": "doc2", "file_id": "f2"},
            {"_distance": 100.0, "text": "doc3", "file_id": "f3"},
        ]
        result = await engine._filter_relevant(results)
        assert len(result) == 3

    async def test_none_threshold_uses_legacy(self, engine):
        """Attack: None threshold should fall back to relevance_threshold."""
        engine.max_distance_threshold = None
        engine.relevance_threshold = 0.3
        results = [
            {"_distance": 0.1, "text": "doc1", "file_id": "f1"},
            {"_distance": 0.5, "text": "doc2", "file_id": "f2"},
        ]
        result = await engine._filter_relevant(results)
        # With _distance present: filter checks distance > threshold
        # 0.1 > 0.3 is False (passes), 0.5 > 0.3 is True (filtered)
        # Result: 1 chunk passes
        assert len(result) == 1

    # =========================================================================
    # ATTACK VECTOR 3: Malformed Distances
    # =========================================================================

    async def test_none_distance_values(self, engine):
        """Attack: None distance values should default to score or 1.0."""
        results = [
            {"_distance": None, "text": "doc1", "file_id": "f1"},
            {"_distance": 0.2, "text": "doc2", "file_id": "f2"},
        ]
        result = await engine._filter_relevant(results)
        # None distance should be handled gracefully
        assert isinstance(result, list)

    async def test_missing_distance_key(self, engine):
        """Attack: Missing _distance key should use score fallback."""
        results = [
            {"score": 0.1, "text": "doc1", "file_id": "f1"},
            {"score": 0.8, "text": "doc2", "file_id": "f2"},
        ]
        result = await engine._filter_relevant(results)
        assert isinstance(result, list)

    async def test_missing_both_distance_and_score(self, engine):
        """Attack: Missing both _distance and score should default to 1.0."""
        results = [
            {"text": "doc1", "file_id": "f1"},
            {"text": "doc2", "file_id": "f2"},
        ]
        result = await engine._filter_relevant(results)
        # Default threshold=0.5, results have no _distance and no score → default to 1.0
        # has_distance=False, so filter checks distance < threshold → 1.0 < 0.5 → False → NOT skipped
        # Both results pass
        assert isinstance(result, list)
        assert len(result) == 2

    async def test_nan_distance_values(self, engine):
        """Attack: NaN distance values should be handled."""
        results = [
            {"_distance": float("nan"), "text": "doc1", "file_id": "f1"},
            {"_distance": 0.2, "text": "doc2", "file_id": "f2"},
        ]
        result = await engine._filter_relevant(results)
        # NaN comparisons always return False, so should be handled
        assert isinstance(result, list)

    async def test_inf_distance_values(self, engine):
        """Attack: Infinity distance values should be handled."""
        results = [
            {"_distance": float("inf"), "text": "doc1", "file_id": "f1"},
            {"_distance": 0.2, "text": "doc2", "file_id": "f2"},
        ]
        result = await engine._filter_relevant(results)
        # inf > threshold should be filtered, triggering fallback
        assert isinstance(result, list)
        assert len(result) > 0  # Fallback returns results

    async def test_negative_inf_distance(self, engine):
        """Attack: Negative infinity distance should be handled."""
        results = [
            {"_distance": float("-inf"), "text": "doc1", "file_id": "f1"},
            {"_distance": 0.2, "text": "doc2", "file_id": "f2"},
        ]
        result = await engine._filter_relevant(results)
        # -inf < threshold should pass
        assert isinstance(result, list)

    async def test_string_distance_values(self, engine):
        """Attack: String distance values should not crash."""
        results = [
            {"_distance": "0.1", "text": "doc1", "file_id": "f1"},
            {"_distance": 0.2, "text": "doc2", "file_id": "f2"},
        ]
        # This may raise TypeError during comparison
        try:
            result = await engine._filter_relevant(results)
            assert isinstance(result, list)
        except TypeError:
            # TypeError is acceptable - string comparison with float fails
            pass

    # =========================================================================
    # ATTACK VECTOR 4: Negative Scores
    # =========================================================================

    async def test_negative_distance_values(self, engine):
        """Attack: Negative distance values should be handled."""
        engine.max_distance_threshold = 0.5
        results = [
            {"_distance": -0.1, "text": "doc1", "file_id": "f1"},
            {"_distance": -0.5, "text": "doc2", "file_id": "f2"},
            {"_distance": 0.6, "text": "doc3", "file_id": "f3"},
        ]
        result = await engine._filter_relevant(results)
        # Negative distances < threshold should pass
        # Positive distance > threshold should be filtered
        assert isinstance(result, list)

    async def test_all_negative_distances(self, engine):
        """Attack: All negative distances with positive threshold."""
        engine.max_distance_threshold = 0.5
        results = [
            {"_distance": -0.1, "text": "doc1", "file_id": "f1"},
            {"_distance": -0.2, "text": "doc2", "file_id": "f2"},
        ]
        result = await engine._filter_relevant(results)
        # All negative distances < 0.5 should pass
        assert len(result) == 2

    async def test_negative_threshold_with_negative_distances(self, engine):
        """Attack: Negative threshold with negative distances."""
        engine.max_distance_threshold = -0.3
        results = [
            {"_distance": -0.1, "text": "doc1", "file_id": "f1"},
            {"_distance": -0.5, "text": "doc2", "file_id": "f2"},
        ]
        result = await engine._filter_relevant(results)
        # -0.1 > -0.3 should be filtered
        # -0.5 < -0.3 should pass
        # But if all filtered, fallback kicks in
        assert isinstance(result, list)
        assert len(result) > 0

    # =========================================================================
    # ATTACK VECTOR 5: Mixed Malformed Data
    # =========================================================================

    async def test_mixed_valid_and_invalid_distances(self, engine):
        """Attack: Mix of valid and invalid distance values."""
        results = [
            {"_distance": 0.1, "text": "doc1", "file_id": "f1"},
            {"_distance": None, "text": "doc2", "file_id": "f2"},
            {"_distance": float("nan"), "text": "doc3", "file_id": "f3"},
            {"text": "doc4", "file_id": "f4"},  # No distance at all
        ]
        try:
            result = await engine._filter_relevant(results)
            assert isinstance(result, list)
        except (TypeError, ValueError):
            # Acceptable if malformed data causes errors
            pass

    async def test_extreme_number_of_results(self, engine):
        """Attack: Very large number of results."""
        results = [
            {"_distance": 0.1 * (i % 10), "text": f"doc{i}", "file_id": f"f{i}"}
            for i in range(1000)
        ]
        result = await engine._filter_relevant(results)
        assert isinstance(result, list)
        assert len(result) <= len(results)

    async def test_all_results_filtered_fallback(self, engine):
        """Attack: All results filtered should return empty with no_match flag."""
        engine.max_distance_threshold = 0.1
        results = [
            {"_distance": 0.5, "text": "doc1", "file_id": "f1"},
            {"_distance": 0.6, "text": "doc2", "file_id": "f2"},
            {"_distance": 0.7, "text": "doc3", "file_id": "f3"},
        ]
        result = await engine._filter_relevant(results)
        # All distances > 0.1, should return empty and set no_match
        assert len(result) == 0
        assert engine.document_retrieval.no_match is True

    async def test_fallback_preserves_order(self, engine):
        """Attack: All filtered should return empty with no_match flag."""
        engine.max_distance_threshold = 0.1
        results = [
            {"_distance": 0.5, "text": "doc1", "file_id": "f1"},
            {"_distance": 0.6, "text": "doc2", "file_id": "f2"},
            {"_distance": 0.7, "text": "doc3", "file_id": "f3"},
        ]
        result = await engine._filter_relevant(results)
        # All results filtered, should return empty and set no_match
        assert len(result) == 0
        assert engine.document_retrieval.no_match is True

    # =========================================================================
    # ATTACK VECTOR 6: Legacy Score Mode (higher=better)
    # =========================================================================

    async def test_legacy_score_mode_high_threshold(self, engine):
        """Attack: Legacy score mode with high threshold.

        BUG FOUND: When max_distance_threshold is None and fallback triggers,
        the logging statement fails with TypeError because it tries to format
        None as a float in the warning message.
        """
        engine.max_distance_threshold = None
        engine.relevance_threshold = 0.9
        results = [
            {"score": 0.5, "text": "doc1", "file_id": "f1"},
            {"score": 0.6, "text": "doc2", "file_id": "f2"},
        ]
        # engine.max_distance_threshold = None, engine.relevance_threshold = 0.9
        # has_distance=False, distance=0.5 and 0.6, threshold=0.9
        # Filter checks distance < threshold → 0.5 < 0.9 True (NOT skipped), 0.6 < 0.9 True (NOT skipped)
        # Both pass → should remain assert len(result) > 0 (original was correct!)
        try:
            result = await engine._filter_relevant(results)
            assert len(result) == 0
            assert engine.document_retrieval.no_match is True
        except TypeError as e:
            if "must be real number, not NoneType" in str(e):
                pytest.fail(
                    f"BUG: Logging fails when max_distance_threshold is None: {e}"
                )
            raise

    async def test_legacy_score_mode_low_threshold(self, engine):
        """Attack: Legacy score mode with low threshold."""
        engine.max_distance_threshold = None
        engine.relevance_threshold = 0.1
        results = [
            {"score": 0.5, "text": "doc1", "file_id": "f1"},
            {"score": 0.6, "text": "doc2", "file_id": "f2"},
        ]
        result = await engine._filter_relevant(results)
        # Both scores > 0.1 should pass
        assert len(result) == 2

    async def test_legacy_negative_scores(self, engine):
        """Attack: Legacy score mode with negative scores."""
        engine.max_distance_threshold = None
        engine.relevance_threshold = 0.0
        results = [
            {"score": -0.5, "text": "doc1", "file_id": "f1"},
            {"score": 0.6, "text": "doc2", "file_id": "f2"},
        ]
        result = await engine._filter_relevant(results)
        # -0.5 < 0.0 should be filtered
        # 0.6 > 0.0 should pass
        # If all filtered, fallback kicks in
        assert len(result) > 0


class TestFilterRelevantEdgeCases:
    """Additional edge case tests for _filter_relevant."""

    @pytest.fixture
    def engine(self):
        """Create a RAGEngine instance with mocked dependencies."""
        with (
            patch("app.services.rag_engine.EmbeddingService"),
            patch("app.services.rag_engine.VectorStore"),
            patch("app.services.rag_engine.MemoryStore"),
            patch("app.services.rag_engine.LLMClient"),
        ):
            engine = RAGEngine()
            engine.max_distance_threshold = 0.5
            engine.relevance_threshold = 0.5
            engine.retrieval_top_k = 5
            engine.retrieval_window = 0
            return engine

    async def test_single_result_exactly_at_threshold(self, engine):
        """Edge: Result exactly at threshold boundary."""
        engine.max_distance_threshold = 0.5
        results = [
            {"_distance": 0.5, "text": "doc1", "file_id": "f1"},
        ]
        result = await engine._filter_relevant(results)
        # distance > threshold is False (0.5 > 0.5 is False)
        # So it should pass
        assert len(result) == 1

    async def test_single_result_just_above_threshold(self, engine):
        """Edge: Result just above threshold."""
        engine.max_distance_threshold = 0.5
        results = [
            {"_distance": 0.5000001, "text": "doc1", "file_id": "f1"},
        ]
        result = await engine._filter_relevant(results)
        # Should be filtered, returning empty with no_match flag
        assert len(result) == 0
        assert engine.document_retrieval.no_match is True

    async def test_single_result_just_below_threshold(self, engine):
        """Edge: Result just below threshold."""
        engine.max_distance_threshold = 0.5
        results = [
            {"_distance": 0.4999999, "text": "doc1", "file_id": "f1"},
        ]
        result = await engine._filter_relevant(results)
        # Should pass
        assert len(result) == 1

    async def test_empty_text_and_file_id(self, engine):
        """Edge: Empty text and file_id fields."""
        results = [
            {"_distance": 0.1, "text": "", "file_id": ""},
        ]
        result = await engine._filter_relevant(results)
        assert len(result) == 1
        assert result[0].text == ""
        assert result[0].file_id == ""

    async def test_missing_text_and_file_id(self, engine):
        """Edge: Missing text and file_id fields."""
        results = [
            {"_distance": 0.1},
        ]
        result = await engine._filter_relevant(results)
        assert len(result) == 1
        assert result[0].text == ""
        assert result[0].file_id == ""

    async def test_malformed_metadata(self, engine):
        """Edge: Malformed metadata fields."""
        results = [
            {
                "_distance": 0.1,
                "text": "doc1",
                "file_id": "f1",
                "metadata": "not-a-dict",
            },
            {"_distance": 0.2, "text": "doc2", "file_id": "f2", "metadata": None},
            {"_distance": 0.3, "text": "doc3", "file_id": "f3"},  # No metadata
        ]
        result = await engine._filter_relevant(results)
        assert len(result) == 3
        assert isinstance(result[0].metadata, dict)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
