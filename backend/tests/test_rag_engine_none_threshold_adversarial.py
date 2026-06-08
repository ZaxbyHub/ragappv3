"""
Focused adversarial test for None threshold logging fix in rag_engine.

This test specifically targets the bug where logging with None thresholds
caused TypeError: must be real number, not NoneType
"""

import logging
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, 'backend')

from app.services.rag_engine import RAGEngine


class TestNoneThresholdLoggingFix:
    """Test the specific fix for None threshold logging."""

    @pytest.fixture
    def engine(self):
        """Create a RAGEngine instance with mocked dependencies."""
        with patch('app.services.rag_engine.EmbeddingService'), \
             patch('app.services.rag_engine.VectorStore'), \
             patch('app.services.rag_engine.MemoryStore'), \
             patch('app.services.rag_engine.LLMClient'):
            engine = RAGEngine()
            engine.retrieval_top_k = 5
            engine.retrieval_window = 0
            return engine

    async def test_none_max_distance_none_relevance_threshold_no_filtering(self, engine, caplog):
        """
        ATTACK: Both thresholds are None - no filtering should occur.

        When both thresholds are None, no filtering happens (all results pass),
        so fallback logging is never triggered. This is expected behavior.
        The fix prevents TypeError IF fallback were ever triggered with None.
        """
        engine.max_distance_threshold = None
        engine.relevance_threshold = None

        # Results - with no threshold, none should be filtered
        results = [
            {"_distance": 0.5, "text": "doc1", "file_id": "f1"},
            {"_distance": 0.6, "text": "doc2", "file_id": "f2"},
        ]

        with caplog.at_level(logging.WARNING):
            # This should NOT raise TypeError
            result = await engine._filter_relevant(results)

        # With no threshold, all results should pass through (no filtering)
        assert len(result) == 2

        # No fallback warning should be logged since nothing was filtered
        warning_messages = [r.message for r in caplog.records if r.levelname == 'WARNING']
        fallback_warnings = [m for m in warning_messages if 'Threshold filtering removed all' in str(m)]
        assert len(fallback_warnings) == 0, "No fallback expected when no threshold is set"

    async def test_none_max_distance_with_relevance_threshold_logging(self, engine, caplog):
        """
        ATTACK: max_distance_threshold is None but relevance_threshold has value.

        Scores 0.1 and 0.2 are both below relevance_threshold=0.3, so both are
        filtered out. The old fallback-to-all behavior was removed; the current
        implementation returns [] and sets no_match=True.
        """
        engine.max_distance_threshold = None
        engine.relevance_threshold = 0.3

        results = [
            {"score": 0.1, "text": "doc1", "file_id": "f1"},  # Below threshold
            {"score": 0.2, "text": "doc2", "file_id": "f2"},  # Below threshold
        ]

        with caplog.at_level(logging.WARNING):
            result = await engine._filter_relevant(results)

        # All results are below threshold; no fallback: returns empty list
        assert len(result) == 0
        assert engine.document_retrieval.no_match is True

        # No fallback warning should be logged
        warning_messages = [r.message for r in caplog.records if r.levelname == 'WARNING']
        fallback_warnings = [m for m in warning_messages if 'Threshold filtering removed all' in str(m)]
        assert len(fallback_warnings) == 0

    async def test_valid_max_distance_threshold_logging(self, engine, caplog):
        """
        CONTROL: Valid max_distance_threshold=0.75 filters distances 0.8 and 0.9.

        Both distances exceed the threshold; the old fallback-to-all behavior was
        removed. Current implementation returns [] and sets no_match=True.
        """
        engine.max_distance_threshold = 0.75
        engine.relevance_threshold = 0.5

        results = [
            {"_distance": 0.8, "text": "doc1", "file_id": "f1"},  # Above threshold
            {"_distance": 0.9, "text": "doc2", "file_id": "f2"},  # Above threshold
        ]

        with caplog.at_level(logging.WARNING):
            result = await engine._filter_relevant(results)

        # All results exceed threshold; no fallback: returns empty list
        assert len(result) == 0
        assert engine.document_retrieval.no_match is True

        # No fallback warning should be logged
        warning_messages = [r.message for r in caplog.records if r.levelname == 'WARNING']
        fallback_warnings = [m for m in warning_messages if 'Threshold filtering removed all' in str(m)]
        assert len(fallback_warnings) == 0

    async def test_zero_threshold_logging(self, engine, caplog):
        """
        ATTACK: Zero threshold filters distance=0.1 (exceeds 0.0).

        The old fallback-to-all behavior was removed; current implementation
        returns [] and sets no_match=True. No TypeError from %.3f on 0.0.
        """
        engine.max_distance_threshold = 0.0

        results = [
            {"_distance": 0.1, "text": "doc1", "file_id": "f1"},
        ]

        with caplog.at_level(logging.WARNING):
            result = await engine._filter_relevant(results)

        # Distance 0.1 > threshold 0.0 → filtered; no fallback
        assert len(result) == 0
        assert engine.document_retrieval.no_match is True

    async def test_negative_threshold_logging(self, engine, caplog):
        """
        ATTACK: Negative threshold (-0.5) filters distance=0.1 (0.1 > -0.5).

        The old fallback-to-all behavior was removed; current implementation
        returns [] and sets no_match=True. No TypeError from %.3f on -0.5.
        """
        engine.max_distance_threshold = -0.5

        results = [
            {"_distance": 0.1, "text": "doc1", "file_id": "f1"},
        ]

        with caplog.at_level(logging.WARNING):
            result = await engine._filter_relevant(results)

        # Distance 0.1 > threshold -0.5 → filtered; no fallback
        assert len(result) == 0
        assert engine.document_retrieval.no_match is True


class TestNoneThresholdTypeErrorPrevention:
    """Ensure TypeError is never raised for None thresholds."""

    @pytest.fixture
    def engine(self):
        """Create a RAGEngine instance with mocked dependencies."""
        with patch('app.services.rag_engine.EmbeddingService'), \
             patch('app.services.rag_engine.VectorStore'), \
             patch('app.services.rag_engine.MemoryStore'), \
             patch('app.services.rag_engine.LLMClient'):
            engine = RAGEngine()
            engine.retrieval_top_k = 5
            engine.retrieval_window = 0
            return engine

    async def test_no_typeerror_when_both_thresholds_none(self, engine):
        """
        CRITICAL ATTACK: Ensure no TypeError when both thresholds are None.

        This directly tests the bug that was fixed.
        """
        engine.max_distance_threshold = None
        engine.relevance_threshold = None

        results = [
            {"_distance": 0.5, "text": "doc1", "file_id": "f1"},
        ]

        # This must NOT raise TypeError
        try:
            await engine._filter_relevant(results)
        except TypeError as e:
            if "must be real number, not NoneType" in str(e):
                pytest.fail(f"BUG: TypeError raised when both thresholds are None: {e}")
            raise

    async def test_no_typeerror_with_legacy_score_mode(self, engine):
        """
        ATTACK: Legacy score mode with None max_distance_threshold.

        Uses 'score' key instead of '_distance' to trigger legacy mode.
        """
        engine.max_distance_threshold = None
        engine.relevance_threshold = 0.9

        results = [
            {"score": 0.5, "text": "doc1", "file_id": "f1"},
        ]

        # This must NOT raise TypeError
        try:
            await engine._filter_relevant(results)
        except TypeError as e:
            if "must be real number, not NoneType" in str(e):
                pytest.fail(f"BUG: TypeError raised in legacy mode: {e}")
            raise


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
