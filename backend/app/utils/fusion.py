"""Fusion utilities for combining search results."""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def rrf_fuse(
    result_lists: List[List[Dict[str, Any]]],
    k: int = 60,
    limit: Optional[int] = None,
    recency_scores: Optional[Dict[str, float]] = None,
    recency_weight: float = 0.1,
    weights: Optional[List[float]] = None,
) -> List[Dict[str, Any]]:
    """
    Reciprocal Rank Fusion (RRF) for combining multiple result lists.

    Args:
        result_lists: List of result lists, each from a different query/scale/source
        k: RRF constant (default 60)
        limit: Maximum results to return (None = return all)
        recency_scores: Optional dict mapping record id → normalized recency score (0.0-1.0,
            where 1.0 = most recent). When provided, recency is blended into the final score.
            Records missing from recency_scores receive a neutral score of 0.5.
        recency_weight: Weight for recency blending (0.0 = disabled, default 0.1).
            Final score = rrf_score * (1 - recency_weight) + recency_score * recency_weight.
        weights: Optional list of weights, one per result list. If provided, each result list
            is weighted by the corresponding value. If None, all lists have equal weight (1.0).
            Length should match len(result_lists).

    Returns:
        Deduplicated, scored results sorted by final score descending.
        Each result has '_rrf_score' field added.
    """
    rrf_scores: Dict[str, float] = {}
    id_to_record: Dict[str, Dict[str, Any]] = {}

    for i, results in enumerate(result_lists):
        if weights is not None and i >= len(weights):
            raise ValueError(
                f"weights list has {len(weights)} items but {len(result_lists)} result lists were provided"
            )
        weight = weights[i] if weights else 1.0
        for rank, record in enumerate(results):
            uid = record.get("id", f"list_{i}_rank_{rank}")
            # RRF formula: weight * 1 / (k + rank + 1)
            score = weight * 1.0 / (k + rank + 1)
            rrf_scores[uid] = rrf_scores.get(uid, 0.0) + score
            if uid not in id_to_record:
                id_to_record[uid] = record

    # Apply recency blending when provided.
    #
    # Raw RRF scores are tiny (a top hit with k=60 scores ~1/61 ≈ 0.016) while
    # recency scores span the full [0, 1] range. Blending them directly lets the
    # recency term — even at a small weight — dominate the relevance signal it is
    # only meant to break ties on. Normalize each RRF score against the strongest
    # one in this fusion so both terms live in [0, 1] before weighting; recency
    # then contributes exactly ``recency_weight`` of the final score.
    if recency_scores and recency_weight > 0.0:
        max_rrf = max(rrf_scores.values()) if rrf_scores else 0.0
        blended: Dict[str, float] = {}
        for uid, rrf_score in rrf_scores.items():
            norm_rrf = (rrf_score / max_rrf) if max_rrf > 0.0 else 0.0
            rec_score = recency_scores.get(uid, 0.5)  # neutral for missing
            blended[uid] = (
                norm_rrf * (1.0 - recency_weight) + rec_score * recency_weight
            )
        sorted_uids = sorted(blended.keys(), key=lambda u: blended[u], reverse=True)
        final_scores = blended
    else:
        sorted_uids = sorted(
            rrf_scores.keys(), key=lambda u: rrf_scores[u], reverse=True
        )
        final_scores = rrf_scores

    # Build result list
    fused = []
    for uid in sorted_uids[:limit] if limit else sorted_uids:
        record = dict(id_to_record[uid])
        record["_rrf_score"] = final_scores[uid]
        fused.append(record)

    if logger.isEnabledFor(logging.DEBUG):
        list_sizes = [len(r) for r in result_lists]
        logger.debug(
            "RRF fused %d lists (sizes=%s) → %d results (k=%d, recency_weight=%.2f, weights=%s)",
            len(result_lists), list_sizes, len(fused), k, recency_weight,
            weights or "uniform",
        )

    return fused
