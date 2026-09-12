"""Reporting contract for the offline eval harness (issue #237, AC7).

Freezes the set of mean metrics a summary must report and documents the
uncertainty method used for every mean. ``EvalRunner.summarize()`` keys are
additive on top of this contract: existing keys never disappear.

The uncertainty method for every mean is ``normal_approx_95_clamped``:
``low = max(0.0, mean - 1.96*sd/sqrt(n))`` and
``high = min(1.0, mean + 1.96*sd/sqrt(n))`` where ``sd = statistics.stdev``
for ``n >= 2`` and ``0.0`` for ``n == 1`` (a degenerate interval
``[mean, mean]``). Bounds are clamped to ``[0, 1]`` because every metric in
this contract is a rate. This is a deterministic approximation, not a
calibrated statistical guarantee; the ``method`` key states it in-band.
"""

from __future__ import annotations

from typing import Tuple

#: Every mean key ``EvalRunner.summarize()`` must report, exactly. The
#: per-case attribute is the key minus the ``_mean`` suffix, except
#: ``no_match_correct_rate`` which maps to ``no_match_correct``.
MEAN_METRIC_KEYS: Tuple[str, ...] = (
    "recall_at_k_mean",
    "mrr_mean",
    "ndcg_at_k_mean",
    "citation_validity_mean",
    "memory_recall_mean",
    "wiki_recall_mean",
    "fact_coverage_mean",
    "no_match_correct_rate",
    "artifact_recall_at_k_mean",
    "artifact_mrr_mean",
    "artifact_ndcg_at_k_mean",
    "artifact_citation_validity_mean",
    "vision_use_rate_mean",
    "vision_degradation_rate_mean",
    "modality_match_rate_mean",
)

#: Documented uncertainty method identifier recorded in every interval entry.
UNCERTAINTY_METHOD = "normal_approx_95_clamped"

#: One-line definition per mean key. Never labels a lexical heuristic as an
#: external-library (e.g. RAGAS) metric.
METRIC_DEFINITIONS: dict[str, str] = {
    "recall_at_k_mean": (
        "Mean over cases of recall@k: fraction of expected chunk ids present "
        "in the top-k retrieved chunk ids."
    ),
    "mrr_mean": (
        "Mean reciprocal rank of the first expected chunk id in the retrieved "
        "ordering; 0.0 when no expected id was retrieved."
    ),
    "ndcg_at_k_mean": (
        "Mean binary-relevance nDCG@k of retrieved chunk ids against expected "
        "chunk ids."
    ),
    "citation_validity_mean": (
        "Mean fraction of cited source labels that reference a retrieved "
        "source label; vacuously 1.0 when nothing was cited."
    ),
    "memory_recall_mean": (
        "Mean recall@k of expected memory labels against cited memory labels."
    ),
    "wiki_recall_mean": (
        "Mean recall@k of expected wiki labels against cited wiki labels."
    ),
    "fact_coverage_mean": (
        "Mean fraction of expected fact substrings appearing (case-insensitive, "
        "literal containment) in the answer."
    ),
    "no_match_correct_rate": (
        "Fraction of cases with a no-match expectation where the system "
        "correctly returned no match (or correctly did not, for "
        "match-expected cases)."
    ),
    "artifact_recall_at_k_mean": (
        "Mean recall@k of expected artifact ids against retrieved artifact "
        "ids (independent of file/chunk metrics)."
    ),
    "artifact_mrr_mean": (
        "Mean reciprocal rank of the first expected artifact id in the "
        "retrieved artifact ordering."
    ),
    "artifact_ndcg_at_k_mean": (
        "Mean binary-relevance nDCG@k of retrieved artifact ids against "
        "expected artifact ids."
    ),
    "artifact_citation_validity_mean": (
        "Mean fraction of cited artifact ids that reference a retrieved "
        "artifact id."
    ),
    "vision_use_rate_mean": (
        "Mean fraction of expected artifacts for which vision analysis was "
        "used at query time."
    ),
    "vision_degradation_rate_mean": (
        "Mean fraction of expected artifacts whose vision analysis degraded "
        "to a non-vision path."
    ),
    "modality_match_rate_mean": (
        "Mean fraction of expected modality kinds observed among the "
        "retrieved per-source modality kinds."
    ),
}

__all__ = ["MEAN_METRIC_KEYS", "METRIC_DEFINITIONS", "UNCERTAINTY_METHOD"]
