"""Retrieval-quality metric primitives (FR-001).

Provides the core metric functions used by the live-eval adapter and the
offline eval harness. This module is intentionally dependency-free (no
LanceDB, no LLM, no embeddings) so it is safe to import from production
service code.

Moved here from tests/eval/eval_harness.py to break the production→tests
import coupling that would cause ImportError in stripped prod images.
"""

from __future__ import annotations

import math
from typing import Sequence


def recall_at_k(retrieved: Sequence[str], expected: Sequence[str], k: int) -> float:
    """Fraction of expected items that appear in the top-k retrieved list."""
    if not expected:
        return 0.0
    top = set(retrieved[:k])
    return sum(1 for e in expected if e in top) / float(len(expected))


def mean_reciprocal_rank(
    retrieved: Sequence[str], expected: Sequence[str]
) -> float:
    """Reciprocal rank of the first expected item; 0.0 if none retrieved."""
    if not expected:
        return 0.0
    expected_set = set(expected)
    for i, item in enumerate(retrieved):
        if item in expected_set:
            return 1.0 / (i + 1)
    return 0.0


def ndcg_at_k(
    retrieved: Sequence[str], expected: Sequence[str], k: int
) -> float:
    """Binary-relevance nDCG at k. Returns 0.0 when ``expected`` is empty."""
    if not expected:
        return 0.0
    expected_set = set(expected)
    dcg = 0.0
    for i, item in enumerate(retrieved[:k]):
        rel = 1.0 if item in expected_set else 0.0
        dcg += rel / math.log2(i + 2)
    # Ideal DCG: every expected hits the top.
    ideal = sum(
        1.0 / math.log2(i + 2) for i in range(min(len(expected), k))
    )
    if ideal == 0.0:
        return 0.0
    return dcg / ideal


def citation_validity(cited: Sequence[str], available: Sequence[str]) -> float:
    """Fraction of cited labels that reference an available source/memory.

    Returns 1.0 when no citations were emitted (vacuously valid).
    """
    if not cited:
        return 1.0
    available_set = set(available)
    valid = sum(1 for c in cited if c in available_set)
    return valid / float(len(cited))


def fact_coverage(answer: str, expected_facts: Sequence[str]) -> float:
    """Fraction of expected fact substrings that appear (case-insensitive) in the answer."""
    if not expected_facts:
        return 0.0
    a = answer.lower()
    hit = sum(1 for f in expected_facts if f.lower() in a)
    return hit / float(len(expected_facts))


__all__ = [
    "citation_validity",
    "fact_coverage",
    "mean_reciprocal_rank",
    "ndcg_at_k",
    "recall_at_k",
]
