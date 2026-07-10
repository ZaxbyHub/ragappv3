"""Structured trace instrumentation for RAG queries (P3.1).

A :class:`RAGTrace` accumulates the per-query observability data that the
engine emits at each pipeline stage: query transformation, retrieval,
fusion, reranking, distillation, packing, parent-window expansion, and
generation/citation validation.

Traces are always built (cheap; just a dict). They are emitted to the
logger at INFO level for any query, and surfaced in the streaming
``done`` event's ``trace`` field when ``settings.rag_trace_in_response``
is true. The default keeps trace data out of normal user-visible
metadata — operators flip the flag on for evaluation runs.

The logged form never contains the raw user query: ``log()`` emits
``to_log_dict()`` (which omits ``original_query`` and carries only a
length/hash), while ``to_dict()`` (which keeps ``original_query``) is
reserved for the in-response trace behind ``rag_trace_in_response``.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class RAGTrace:
    """Aggregated observability snapshot for a single RAG query."""

    original_query: str = ""
    transformed_queries: List[str] = field(default_factory=list)
    variants_dropped: List[str] = field(default_factory=list)
    dense_hits_per_variant: List[int] = field(default_factory=list)
    fts_status: str = "disabled"
    fts_exceptions: int = 0
    fused_hits: int = 0
    reranked_hits: Optional[int] = None
    rerank_status: str = "disabled"
    filtered_hits: int = 0
    distance_threshold: Optional[float] = None
    distillation_before: Optional[int] = None
    distillation_after: Optional[int] = None
    parent_windows_expanded: int = 0
    token_pack_included: int = 0
    token_pack_skipped: int = 0
    token_pack_truncated: int = 0
    final_sources: List[str] = field(default_factory=list)
    final_memories: List[str] = field(default_factory=list)
    cited_sources: List[str] = field(default_factory=list)
    cited_memories: List[str] = field(default_factory=list)
    invalid_citations: List[str] = field(default_factory=list)
    # FR-004: per-citation confidence scores (label -> Jaccard overlap [0,1])
    citation_confidence: Dict[str, float] = field(default_factory=dict)
    # FR-004: answer sentences with no citation and low source overlap
    unverifiable_claims: List[str] = field(default_factory=list)
    answer_supported: Optional[bool] = None
    exact_match_promoted: bool = False
    multi_scale_used: bool = False
    # Wiki retrieval fields
    wiki_query: str = ""
    wiki_candidates_total: int = 0
    wiki_injected: int = 0
    wiki_cited: List[str] = field(default_factory=list)
    wiki_filtered: List[str] = field(default_factory=list)
    answer_source_mode: str = "documents"
    # Set when a short/referential follow-up is rewritten before retrieval.
    followup_rewrite: Optional[str] = None
    # FR-002 part 1: LLM-generated sub-query plan from QueryPlanner.
    # Populated by RAGEngine._run_query_plan() during query processing.
    # Consumed by task 3.2 (orchestration/fusion). Empty list means
    # the planner was not invoked or returned no plan.
    query_plan: List[str] = field(default_factory=list)
    # FR-002 part 2: Set True when sub-query RRF fusion was applied.
    fusion_used: bool = False
    # FR-007 part 3: A/B experiment observability.
    ab_experiment_id: Optional[int] = None
    ab_variant: Optional[str] = None  # 'control' | 'challenger' | None
    # SC-015: effective prompt version label used for this query (e.g. 'v1', 'v3.5-org-override')
    prompt_version: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "original_query": self.original_query,
            "transformed_queries": list(self.transformed_queries),
            "variants_dropped": list(self.variants_dropped),
            "dense_hits_per_variant": list(self.dense_hits_per_variant),
            "fts_status": self.fts_status,
            "fts_exceptions": self.fts_exceptions,
            "fused_hits": self.fused_hits,
            "reranked_hits": self.reranked_hits,
            "rerank_status": self.rerank_status,
            "filtered_hits": self.filtered_hits,
            "distance_threshold": self.distance_threshold,
            "distillation_before": self.distillation_before,
            "distillation_after": self.distillation_after,
            "parent_windows_expanded": self.parent_windows_expanded,
            "token_pack_included": self.token_pack_included,
            "token_pack_skipped": self.token_pack_skipped,
            "token_pack_truncated": self.token_pack_truncated,
            "final_sources": list(self.final_sources),
            "final_memories": list(self.final_memories),
            "cited_sources": list(self.cited_sources),
            "cited_memories": list(self.cited_memories),
            "invalid_citations": list(self.invalid_citations),
            "citation_confidence": dict(self.citation_confidence),
            "unverifiable_claims": list(self.unverifiable_claims),
            "answer_supported": self.answer_supported,
            "exact_match_promoted": self.exact_match_promoted,
            "multi_scale_used": self.multi_scale_used,
            "wiki_query": self.wiki_query,
            "wiki_candidates_total": self.wiki_candidates_total,
            "wiki_injected": self.wiki_injected,
            "wiki_cited": list(self.wiki_cited),
            "wiki_filtered": list(self.wiki_filtered),
            "answer_source_mode": self.answer_source_mode,
            "followup_rewrite": self.followup_rewrite,
            "query_plan": list(self.query_plan),
            "fusion_used": self.fusion_used,
            "ab_experiment_id": self.ab_experiment_id,
            "ab_variant": self.ab_variant,
            "prompt_version": self.prompt_version,
        }

    def to_log_dict(self) -> Dict[str, Any]:
        """Return a redaction-safe dict for the LOG path.

        Identical to :meth:`to_dict` except ``original_query`` (the raw
        user chat input) is replaced by a non-reversible length + short
        hash. This is the form emitted by :meth:`log`; the full
        ``original_query`` is only ever returned via :meth:`to_dict`,
        which is reserved for the in-response trace behind the
        ``rag_trace_in_response`` setting.
        """
        data = self.to_dict()
        query = self.original_query or ""
        # SHA-1 truncated to 12 hex chars — enough to correlate logs to a
        # specific query without persisting the query text itself.
        query_hash = hashlib.sha1(query.encode("utf-8")).hexdigest()[:12]
        data["original_query"] = f"<redacted len={len(query)} hash={query_hash}>"
        return data

    def log(self) -> None:
        """Emit the trace to the application logger.

        Uses INFO so that production deployments capture the structured
        signal without flipping debug logging globally. Reasoning text is
        never persisted to the trace, only counts and identifiers — and
        the raw ``original_query`` is never logged (see
        :meth:`to_log_dict`).
        """
        try:
            logger.info("RAG trace: %s", self.to_log_dict())
        except Exception:  # pragma: no cover — defensive
            logger.warning("Failed to emit RAG trace")


__all__ = ["RAGTrace"]
