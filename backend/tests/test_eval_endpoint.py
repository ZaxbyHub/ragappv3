"""Tests for RAGAS evaluation endpoint in eval.py.

Tests cover:
- All 6 metric calculation functions
- Edge cases: empty inputs, single words, no ground truth
- FastAPI endpoint integration test
- Pydantic validation: min_length=1 on query and contexts
"""

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

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
    import ragas
except ImportError:
    import types

    sys.modules["ragas"] = types.ModuleType("ragas")

from fastapi.testclient import TestClient

# Import metric functions directly
from app.api.routes.eval import (
    RAGASEvaluationRequest,
    RAGASMetrics,
    _calculate_answer_relevancy,
    _calculate_answer_similarity,
    _calculate_context_precision,
    _calculate_context_recall,
    _calculate_context_relevancy,
    _calculate_faithfulness,
)


class TestCalculateFaithfulness(unittest.TestCase):
    """Tests for _calculate_faithfulness metric (n-gram overlap)."""

    def test_empty_answer_returns_zero(self):
        """Empty answer should return 0.0."""
        result = _calculate_faithfulness("", ["Some context"])
        self.assertEqual(result, 0.0)

    def test_empty_contexts_returns_zero(self):
        """Empty contexts list should return 0.0."""
        result = _calculate_faithfulness("Some answer", [])
        self.assertEqual(result, 0.0)

    def test_none_answer_returns_zero(self):
        """None answer should return 0.0."""
        result = _calculate_faithfulness(None, ["context"])
        self.assertEqual(result, 0.0)

    def test_none_contexts_returns_zero(self):
        """None contexts should return 0.0."""
        result = _calculate_faithfulness("answer", None)
        self.assertEqual(result, 0.0)

    def test_full_overlap_returns_one(self):
        """Answer sentences fully present in context should return 1.0."""
        context = ["The quick brown fox jumps over the lazy dog."]
        answer = "The quick brown fox jumps."
        result = _calculate_faithfulness(answer, context)
        self.assertEqual(result, 1.0)

    def test_partial_overlap_returns_fraction(self):
        """Partial overlap should return fraction between 0 and 1."""
        context = ["Machine learning is a subset of artificial intelligence."]
        answer = "Machine learning is important. Unrelated statement here."
        result = _calculate_faithfulness(answer, context)
        # "Machine learning is" has overlap, "Unrelated statement" doesn't
        self.assertGreater(result, 0.0)
        self.assertLess(result, 1.0)

    def test_single_word_sentence_skipped(self):
        """Very short sentences (< 5 chars) are skipped."""
        context = ["Hello world testing."]
        answer = "Hi. Hello world testing."
        result = _calculate_faithfulness(answer, context)
        # "Hi." is skipped, "Hello world testing." has overlap
        self.assertGreater(result, 0.0)

    def test_multiple_contexts_combined(self):
        """Multiple contexts should be combined for overlap check."""
        contexts = [
            "First context about machine learning.",
            "Second context about neural networks.",
        ]
        answer = "Machine learning and neural networks are related."
        result = _calculate_faithfulness(answer, contexts)
        self.assertGreater(result, 0.0)

    def test_case_insensitive_matching(self):
        """Overlap matching should be case-insensitive."""
        context = ["THE QUICK BROWN FOX"]
        answer = "the quick brown fox."
        result = _calculate_faithfulness(answer, context)
        self.assertEqual(result, 1.0)


class TestCalculateAnswerRelevancy(unittest.TestCase):
    """Tests for _calculate_answer_relevancy metric (keyword overlap)."""

    def test_empty_query_returns_zero(self):
        """Empty query should return 0.0."""
        result = _calculate_answer_relevancy("", "Some answer")
        self.assertEqual(result, 0.0)

    def test_empty_answer_returns_zero(self):
        """Empty answer should return 0.0."""
        result = _calculate_answer_relevancy("Some query", "")
        self.assertEqual(result, 0.0)

    def test_none_query_returns_zero(self):
        """None query should return 0.0."""
        result = _calculate_answer_relevancy(None, "answer")
        self.assertEqual(result, 0.0)

    def test_none_answer_returns_zero(self):
        """None answer should return 0.0."""
        result = _calculate_answer_relevancy("query", None)
        self.assertEqual(result, 0.0)

    def test_stop_words_removed(self):
        """Stop words should be removed before overlap calculation."""
        # Query and answer with only stop words should return 1.0 (no meaningful words)
        result = _calculate_answer_relevancy("the is are", "was were been")
        self.assertEqual(result, 1.0)

    def test_full_keyword_overlap_returns_one(self):
        """All query keywords in answer should return 1.0."""
        result = _calculate_answer_relevancy(
            "machine learning algorithms", "Machine learning algorithms are powerful."
        )
        self.assertEqual(result, 1.0)

    def test_partial_keyword_overlap_returns_fraction(self):
        """Partial keyword overlap should return correct fraction."""
        # "machine learning" in answer, "algorithms" not in answer
        result = _calculate_answer_relevancy(
            "machine learning algorithms", "Machine learning is popular."
        )
        # 2 out of 3 keywords found
        self.assertAlmostEqual(result, 2 / 3, places=2)

    def test_no_keyword_overlap_returns_zero(self):
        """No keyword overlap should return 0.0."""
        result = _calculate_answer_relevancy("python programming", "Java development")
        self.assertEqual(result, 0.0)

    def test_case_insensitive_matching(self):
        """Keyword matching should be case-insensitive."""
        result = _calculate_answer_relevancy(
            "MACHINE LEARNING", "machine learning algorithms"
        )
        self.assertEqual(result, 1.0)


class TestCalculateContextPrecision(unittest.TestCase):
    """Tests for _calculate_context_precision metric."""

    def test_empty_contexts_returns_zero(self):
        """Empty contexts should return 0.0."""
        result = _calculate_context_precision([], "query")
        self.assertEqual(result, 0.0)

    def test_empty_query_returns_zero(self):
        """Empty query should return 0.0."""
        result = _calculate_context_precision(["context"], "")
        self.assertEqual(result, 0.0)

    def test_none_contexts_returns_zero(self):
        """None contexts should return 0.0."""
        result = _calculate_context_precision(None, "query")
        self.assertEqual(result, 0.0)

    def test_none_query_returns_zero(self):
        """None query should return 0.0."""
        result = _calculate_context_precision(["context"], None)
        self.assertEqual(result, 0.0)

    def test_all_contexts_relevant_returns_one(self):
        """All relevant contexts should return 1.0."""
        # Query has 3 words after stop word removal: machine, learning, algorithms
        # 20% threshold = max(1, 3 * 0.2) = max(1, 0.6) = 1
        # Each context needs at least 1 matching word to be relevant
        contexts = [
            "Machine learning is great.",
            "Learning algorithms are useful.",
            "Deep learning uses algorithms.",
        ]
        result = _calculate_context_precision(contexts, "machine learning algorithms")
        # First context: "machine", "learning" -> 2 overlap >= 1 -> relevant
        # Second context: "learning", "algorithms" -> 2 overlap >= 1 -> relevant
        # Third context: "learning", "algorithms" -> 2 overlap >= 1 -> relevant
        self.assertEqual(result, 1.0)

    def test_no_relevant_contexts_returns_zero(self):
        """No relevant contexts should return 0.0."""
        contexts = ["Cooking recipes are fun.", "Gardening tips for spring."]
        result = _calculate_context_precision(contexts, "machine learning python")
        self.assertEqual(result, 0.0)

    def test_partial_relevance_returns_fraction(self):
        """Partial relevance should return correct fraction."""
        contexts = [
            "Machine learning is great.",
            "Cooking recipes for dinner.",
        ]
        result = _calculate_context_precision(contexts, "machine learning python")
        # First context is relevant, second is not
        self.assertEqual(result, 0.5)

    def test_query_with_only_stop_words_returns_one(self):
        """Query with only stop words should return 1.0 (default)."""
        result = _calculate_context_precision(["any context"], "the is are")
        self.assertEqual(result, 1.0)

    def test_twenty_percent_threshold(self):
        """Context is relevant if it shares >= 20% of query terms."""
        # Query has 5 words, context needs at least 1 word (20% threshold)
        contexts = ["python programming"]  # Has 0 of the 5 query words
        result = _calculate_context_precision(
            contexts, "machine learning algorithms data science"
        )
        self.assertEqual(result, 0.0)

        # Now add one matching word
        contexts = ["machine programming"]
        result = _calculate_context_precision(
            contexts, "machine learning algorithms data science"
        )
        # "machine" matches (1/5 = 20%), so this should be relevant
        self.assertEqual(result, 1.0)


class TestCalculateContextRecall(unittest.TestCase):
    """Tests for _calculate_context_recall metric."""

    def test_empty_contexts_returns_one(self):
        """Empty contexts with no ground truth defaults to 1.0."""
        result = _calculate_context_recall([], "ground truth")
        self.assertEqual(result, 1.0)

    def test_none_ground_truth_returns_one(self):
        """None ground truth should return 1.0 (assumed perfect)."""
        result = _calculate_context_recall(["context"], None)
        self.assertEqual(result, 1.0)

    def test_empty_ground_truth_returns_one(self):
        """Empty ground truth should return 1.0."""
        result = _calculate_context_recall(["context"], "")
        self.assertEqual(result, 1.0)

    def test_full_coverage_returns_one(self):
        """All ground truth words in context should return 1.0."""
        contexts = ["The quick brown fox jumps over the lazy dog."]
        ground_truth = "quick brown fox jumps"
        result = _calculate_context_recall(contexts, ground_truth)
        self.assertEqual(result, 1.0)

    def test_partial_coverage_returns_fraction(self):
        """Partial coverage should return correct fraction."""
        contexts = ["The quick brown fox jumps."]
        ground_truth = "quick brown dog sleeps"  # "dog" and "sleeps" not in context
        result = _calculate_context_recall(contexts, ground_truth)
        # Stop words removed: "quick", "brown", "dog", "sleeps" = 4 words
        # Found: "quick", "brown" = 2 words
        self.assertAlmostEqual(result, 0.5, places=2)

    def test_no_coverage_returns_zero(self):
        """No ground truth words in context should return 0.0."""
        contexts = ["Python programming tutorial."]
        ground_truth = "java ruby golang"
        result = _calculate_context_recall(contexts, ground_truth)
        self.assertEqual(result, 0.0)

    def test_stop_words_removed_from_ground_truth(self):
        """Stop words should be removed from ground truth."""
        contexts = ["important data here"]
        ground_truth = "the important data is here"
        result = _calculate_context_recall(contexts, ground_truth)
        # Stop words removed, remaining: "important", "data", "here"
        self.assertEqual(result, 1.0)

    def test_case_insensitive_matching(self):
        """Ground truth matching should be case-insensitive."""
        contexts = ["MACHINE LEARNING ALGORITHMS"]
        ground_truth = "machine learning"
        result = _calculate_context_recall(contexts, ground_truth)
        self.assertEqual(result, 1.0)


class TestCalculateContextRelevancy(unittest.TestCase):
    """Tests for _calculate_context_relevancy metric."""

    def test_empty_contexts_returns_zero(self):
        """Empty contexts should return 0.0."""
        result = _calculate_context_relevancy([], "query")
        self.assertEqual(result, 0.0)

    def test_empty_query_returns_zero(self):
        """Empty query should return 0.0."""
        result = _calculate_context_relevancy(["context"], "")
        self.assertEqual(result, 0.0)

    def test_none_contexts_returns_zero(self):
        """None contexts should return 0.0."""
        result = _calculate_context_relevancy(None, "query")
        self.assertEqual(result, 0.0)

    def test_none_query_returns_zero(self):
        """None query should return 0.0."""
        result = _calculate_context_relevancy(["context"], None)
        self.assertEqual(result, 0.0)

    def test_all_contexts_relevant_returns_high(self):
        """All relevant contexts should return high score."""
        contexts = [
            "Machine learning processes data.",
            "Machine learning algorithms learn.",
        ]
        result = _calculate_context_relevancy(contexts, "machine learning")
        # Each context has overlap, scaled up but capped at 1.0
        self.assertEqual(result, 1.0)

    def test_mixed_relevancy_returns_average(self):
        """Mixed relevancy should return average."""
        contexts = [
            "Machine learning is great.",
            "Cooking recipes for dinner.",
        ]
        result = _calculate_context_relevancy(contexts, "machine learning python")
        # First: 2/3 overlap = 0.67, scaled *2 = 1.33 capped to 1.0
        # Second: 0/3 overlap = 0.0
        # Average: (1.0 + 0.0) / 2 = 0.5
        self.assertEqual(result, 0.5)

    def test_query_with_only_stop_words_returns_one(self):
        """Query with only stop words should return 1.0."""
        result = _calculate_context_relevancy(["any context"], "the is are")
        self.assertEqual(result, 1.0)

    def test_relevancy_scaled_and_capped(self):
        """Relevancy is scaled by *2 but capped at 1.0."""
        # If overlap is 0.6, scaled is 1.2, capped to 1.0
        contexts = ["python machine learning"]
        result = _calculate_context_relevancy(
            contexts, "python machine learning data science"
        )
        # 3/5 overlap = 0.6, scaled *2 = 1.2, capped to 1.0
        self.assertEqual(result, 1.0)


class TestCalculateAnswerSimilarity(unittest.IsolatedAsyncioTestCase):
    """Tests for _calculate_answer_similarity async metric."""

    async def test_none_ground_truth_returns_none(self):
        """None ground truth should return None."""
        mock_service = MagicMock()
        result = await _calculate_answer_similarity("answer", None, mock_service)
        self.assertIsNone(result)

    async def test_empty_ground_truth_returns_none(self):
        """Empty ground truth should return None."""
        mock_service = MagicMock()
        result = await _calculate_answer_similarity("answer", "", mock_service)
        self.assertIsNone(result)

    async def test_empty_answer_returns_none(self):
        """Empty answer should return None."""
        mock_service = MagicMock()
        result = await _calculate_answer_similarity("", "ground truth", mock_service)
        self.assertIsNone(result)

    async def test_valid_inputs_returns_cosine_similarity(self):
        """Valid inputs should return cosine similarity."""
        mock_service = MagicMock()
        # Pre-computed embeddings: [1, 0] and [0.707, 0.707] -> cos = 0.707
        mock_service.embed_single = AsyncMock(
            side_effect=[
                [1.0, 0.0],  # answer embedding
                [0.707, 0.707],  # ground_truth embedding
            ]
        )

        result = await _calculate_answer_similarity(
            "answer", "ground truth", mock_service
        )

        # Cosine similarity: dot/(norm1*norm2) = 0.707 / (1.0 * 1.0) = 0.707
        self.assertAlmostEqual(result, 0.707, places=2)

    async def test_identical_embeddings_returns_one(self):
        """Identical embeddings should return 1.0."""
        mock_service = MagicMock()
        mock_service.embed_single = AsyncMock(return_value=[1.0, 0.0, 0.0])

        result = await _calculate_answer_similarity("answer", "answer", mock_service)

        self.assertEqual(result, 1.0)

    async def test_orthogonal_embeddings_returns_zero(self):
        """Orthogonal embeddings should return 0.0."""
        mock_service = MagicMock()
        mock_service.embed_single = AsyncMock(
            side_effect=[
                [1.0, 0.0],  # answer
                [0.0, 1.0],  # ground_truth (orthogonal)
            ]
        )

        result = await _calculate_answer_similarity(
            "answer", "ground truth", mock_service
        )

        self.assertAlmostEqual(result, 0.0, places=5)

    async def test_embedding_service_error_returns_none(self):
        """Embedding service exception should return None."""
        mock_service = MagicMock()
        mock_service.embed_single = AsyncMock(side_effect=Exception("API error"))

        result = await _calculate_answer_similarity(
            "answer", "ground truth", mock_service
        )

        self.assertIsNone(result)

    async def test_zero_norm_returns_zero(self):
        """Zero norm embedding should return 0.0."""
        mock_service = MagicMock()
        mock_service.embed_single = AsyncMock(
            side_effect=[
                [0.0, 0.0, 0.0],  # zero vector
                [1.0, 0.0, 0.0],
            ]
        )

        result = await _calculate_answer_similarity(
            "answer", "ground truth", mock_service
        )

        self.assertEqual(result, 0.0)


class TestRAGASModels(unittest.TestCase):
    """Tests for Pydantic models validation."""

    def test_request_valid_minimal(self):
        """Valid minimal request should parse correctly."""
        request = RAGASEvaluationRequest(
            query="What is ML?",
            answer="Machine learning is AI.",
            contexts=["ML is a subset of AI."],
        )
        self.assertEqual(request.query, "What is ML?")
        self.assertEqual(request.ground_truth, None)

    def test_request_valid_with_ground_truth(self):
        """Valid request with ground truth should parse correctly."""
        request = RAGASEvaluationRequest(
            query="What is ML?",
            answer="Machine learning is AI.",
            contexts=["ML is a subset of AI."],
            ground_truth="ML is artificial intelligence.",
        )
        self.assertEqual(request.ground_truth, "ML is artificial intelligence.")

    def test_request_empty_query_fails(self):
        """Empty query should fail validation (min_length=1)."""
        with self.assertRaises(Exception):  # ValidationError
            RAGASEvaluationRequest(
                query="",
                answer="Some answer",
                contexts=["context"],
            )

    def test_request_empty_contexts_fails(self):
        """Empty contexts list should fail validation (min_length=1)."""
        with self.assertRaises(Exception):  # ValidationError
            RAGASEvaluationRequest(
                query="query",
                answer="answer",
                contexts=[],
            )

    def test_metrics_defaults(self):
        """RAGASMetrics should have correct defaults."""
        metrics = RAGASMetrics()
        self.assertEqual(metrics.faithfulness, 0.0)
        self.assertEqual(metrics.answer_relevancy, 0.0)
        self.assertEqual(metrics.context_precision, 0.0)
        self.assertEqual(metrics.context_recall, 0.0)
        self.assertEqual(metrics.context_relevancy, 0.0)
        self.assertIsNone(metrics.answer_similarity)

    def test_metrics_boundary_values(self):
        """RAGASMetrics should accept boundary values 0 and 1."""
        metrics = RAGASMetrics(
            faithfulness=0.0,
            answer_relevancy=1.0,
            context_precision=0.0,
            context_recall=1.0,
            context_relevancy=0.5,
            answer_similarity=0.5,
        )
        self.assertEqual(metrics.faithfulness, 0.0)
        self.assertEqual(metrics.answer_relevancy, 1.0)


class TestRAGASEndpointIntegration(unittest.TestCase):
    """Integration tests for POST /api/eval/ragas endpoint."""

    @classmethod
    def setUpClass(cls):
        """Set up test client and mock dependencies."""
        # Import app after stubs are set up
        from app.main import app

        cls.app = app

    def setUp(self):
        """Set up test client and mock embedding service for all tests."""
        self.client = TestClient(self.app)

        # Set up mock embedding service for ALL tests (including validation tests)
        # because FastAPI resolves dependencies before Pydantic validation
        mock_service = MagicMock()
        mock_service.embed_single = AsyncMock(return_value=[0.1] * 384)
        self._mock_service = mock_service

        from app.api.deps import get_current_active_user, get_embedding_service
        from app.config import settings

        self._get_embedding_service = get_embedding_service
        self._get_current_active_user = get_current_active_user
        self.app.dependency_overrides[get_embedding_service] = lambda: mock_service
        self.app.dependency_overrides[get_current_active_user] = lambda: {
            "id": 1, "username": "admin", "role": "admin", "is_active": True,
            "must_change_password": 0,
        }
        self._eval_patcher = patch("app.config.settings.eval_enabled", True, create=True)
        self._eval_patcher.start()

    def tearDown(self):
        """Clean up dependency overrides."""
        self._eval_patcher.stop()
        self.app.dependency_overrides.pop(self._get_embedding_service, None)
        self.app.dependency_overrides.pop(self._get_current_active_user, None)

    def test_endpoint_valid_request_returns_200(self):
        """Valid request should return 200 with metrics."""
        payload = {
            "query": "What is machine learning?",
            "answer": "Machine learning is a subset of artificial intelligence.",
            "contexts": [
                "Machine learning algorithms process data.",
                "AI includes machine learning and deep learning.",
            ],
            "ground_truth": "ML is a branch of AI.",
        }

        response = self.client.post("/api/eval/ragas", json=payload)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("metrics", data)
        self.assertIn("evaluation_time_ms", data)
        self.assertIn("details", data)

        # Check metrics structure
        metrics = data["metrics"]
        self.assertIn("faithfulness", metrics)
        self.assertIn("answer_relevancy", metrics)
        self.assertIn("context_precision", metrics)
        self.assertIn("context_recall", metrics)
        self.assertIn("context_relevancy", metrics)
        self.assertIn("answer_similarity", metrics)

        # Check details
        self.assertEqual(data["details"]["query_length"], len(payload["query"]))
        self.assertEqual(data["details"]["answer_length"], len(payload["answer"]))
        self.assertEqual(data["details"]["context_count"], 2)
        self.assertTrue(data["details"]["ground_truth_provided"])

    def test_endpoint_minimal_request_returns_200(self):
        """Minimal request (no ground_truth) should return 200."""
        payload = {
            "query": "What is ML?",
            "answer": "Machine learning is AI.",
            "contexts": ["ML processes data."],
        }

        response = self.client.post("/api/eval/ragas", json=payload)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsNone(data["metrics"]["answer_similarity"])
        self.assertFalse(data["details"]["ground_truth_provided"])

    def test_endpoint_empty_query_returns_422(self):
        """Empty query should return 422 validation error."""
        payload = {
            "query": "",
            "answer": "Some answer",
            "contexts": ["context"],
        }

        response = self.client.post("/api/eval/ragas", json=payload)

        self.assertEqual(response.status_code, 422)

    def test_endpoint_empty_contexts_returns_422(self):
        """Empty contexts should return 422 validation error."""
        payload = {
            "query": "What is ML?",
            "answer": "Answer",
            "contexts": [],
        }

        response = self.client.post("/api/eval/ragas", json=payload)

        self.assertEqual(response.status_code, 422)

    def test_endpoint_missing_query_returns_422(self):
        """Missing query field should return 422."""
        payload = {
            "answer": "Answer",
            "contexts": ["context"],
        }

        response = self.client.post("/api/eval/ragas", json=payload)

        self.assertEqual(response.status_code, 422)

    def test_endpoint_missing_contexts_returns_422(self):
        """Missing contexts field should return 422."""
        payload = {
            "query": "Query",
            "answer": "Answer",
        }

        response = self.client.post("/api/eval/ragas", json=payload)

        self.assertEqual(response.status_code, 422)


class TestMetricEdgeCases(unittest.TestCase):
    """Additional edge case tests for metric functions."""

    def test_faithfulness_single_word_answer(self):
        """Single word answer should return 0.0 (no valid sentences)."""
        context = ["test word"]
        answer = "word"  # Too short for a valid sentence
        result = _calculate_faithfulness(answer, context)
        self.assertEqual(result, 0.0)

    def test_answer_relevancy_unicode_characters(self):
        """Unicode characters should be handled correctly."""
        result = _calculate_answer_relevancy(
            "machine learning", "Machine Learning 是人工智能"
        )
        self.assertEqual(result, 1.0)  # "machine", "learning" match

    def test_context_precision_whitespace_handling(self):
        """Extra whitespace should be handled correctly."""
        contexts = ["  machine   learning  algorithms  "]
        result = _calculate_context_precision(contexts, "machine learning")
        self.assertEqual(result, 1.0)

    def test_context_recall_special_characters(self):
        """Special characters in ground truth should be handled."""
        contexts = ["The price is $100 (approx. £80)."]
        ground_truth = "price $100"
        result = _calculate_context_recall(contexts, ground_truth)
        self.assertGreater(result, 0.0)

    def test_context_relevancy_numbers_in_query(self):
        """Numbers in query should be handled correctly."""
        contexts = ["Python 3.11 was released in 2022."]
        result = _calculate_context_relevancy(contexts, "Python 3.11 2022")
        self.assertEqual(result, 1.0)


if __name__ == "__main__":
    unittest.main()
