"""Acceptance checks for C13 eval heuristic numeric bounds.

RED at base (master), GREEN after the fix:

- AC3 (TestSimilarityClamp): the raw cosine computed by
  ``_calculate_answer_similarity`` for identical embeddings exceeds 1.0 by
  float rounding (``1.0000000000000002`` for the vector ``[0.1, 0.7]``:
  dot = 0.49999999999999994 while sqrt(sum_of_squares)**2 =
  0.4999999999999999). The route then feeds that value into
  ``HeuristicMetrics(answer_similarity=...)`` whose ``Field`` constrains it
  to ``le=1.0``, so the whole ``/eval/heuristic`` request fails validation
  (500) instead of returning the clamped metric.
- AC8 (TestTopKCeiling): ``LiveEvalRequest.top_k`` has ``ge=1`` but no
  upper bound, so a client can push ``top_k=1_000_000`` through to the
  retrieval pipeline.

These tests intentionally FAIL on the current tree. They must pass once the
similarity is clamped to the valid ``[-1.0, 1.0]`` range and ``top_k`` gains
a ceiling. No production code is modified here.
"""

import unittest

import pytest
from pydantic import ValidationError

from app.api.routes.eval import (
    HeuristicMetrics,
    LiveEvalRequest,
    _calculate_answer_similarity,
)


class _FakeEmbeddingService:
    """Minimal async embedding-service double.

    ``_calculate_answer_similarity`` only awaits
    ``embedding_service.embed_single(text)`` and consumes the returned
    sequence, so a plain async method returning a fixed vector is enough.
    """

    def __init__(self, vector):
        self._vector = list(vector)
        self.embedded_texts = []

    async def embed_single(self, text):
        self.embedded_texts.append(text)
        return list(self._vector)


class TestSimilarityClamp(unittest.IsolatedAsyncioTestCase):
    """AC3 — identical embeddings must yield a similarity within [-1.0, 1.0]."""

    async def test_identical_embeddings_similarity_clamped(self):
        """AC3 — RED at base (master), GREEN after the fix.

        WHEN ``_calculate_answer_similarity`` embeds answer and ground truth
        to the identical vector ``[0.1, 0.7]`` THE SYSTEM SHALL return a
        similarity <= 1.0 so ``HeuristicMetrics(answer_similarity=...)``
        validates (its Field constrains the value to le=1.0; all other
        HeuristicMetrics fields have defaults, so answer_similarity is the
        only value that must be supplied here).

        Base failure signature: the unclamped cosine of the identical vector
        is 1.0000000000000002, so the assert below fails with
        ``1.0000000000000002 not less than or equal to 1.0`` (and the model
        construction afterwards would raise ValidationError for the same
        reason).
        """
        service = _FakeEmbeddingService([0.1, 0.7])

        sim = await _calculate_answer_similarity(
            "machine learning processes data",
            "machine learning processes data",
            service,
        )

        # Both texts were embedded to the identical vector, so the cosine
        # below really ran on [0.1, 0.7] vs [0.1, 0.7].
        self.assertEqual(len(service.embedded_texts), 2)
        self.assertIsNotNone(sim)
        # Base failure line: 1.0000000000000002 <= 1.0 is False.
        self.assertLessEqual(sim, 1.0)
        # The clamped value must fit the response model (other fields default).
        metrics = HeuristicMetrics(answer_similarity=sim)
        self.assertIsNotNone(metrics)


class TestTopKCeiling(unittest.TestCase):
    """AC8 — LiveEvalRequest.top_k must have an upper bound, not just ge=1."""

    def test_live_eval_top_k_has_ceiling(self):
        """AC8 — RED at base (master), GREEN after the fix.

        WHEN a ``LiveEvalRequest`` is built with one otherwise-valid
        benchmark item (unique id, non-empty query, relevant doc ids per its
        model) and ``top_k=1_000_000`` THE SYSTEM SHALL reject it with a
        pydantic ``ValidationError``.

        Base failure signature: top_k only has ge=1 and no ``le`` ceiling,
        so the model constructs and the DID NOT RAISE failure below fires.
        The sanity construction with top_k=5 proves the benchmark item is
        valid on its own, so the ceiling is the only violated constraint.
        """
        payload = {
            "benchmark": [
                {"id": "q1", "query": "test query", "relevant_ids": ["doc1"]},
            ],
            "top_k": 1_000_000,
        }

        # Sanity: with a sane top_k the request validates, so any ValidationError
        # below is attributable to the ceiling alone.
        LiveEvalRequest(**{**payload, "top_k": 5})

        with pytest.raises(ValidationError):
            LiveEvalRequest(**payload)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
