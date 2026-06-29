"""Live-pipeline evaluation adapter (FR-001).

Provides a bridge between the offline eval harness (MRR/nDCG/recall) and the
live RAG retrieval pipeline, enabling benchmark-driven retrieval quality
measurement without requiring pre-supplied curated JSONL contexts.

Architecture
------------
``score_run(benchmark, retrieved_per_query)`` is the core pure function:
  - Takes a benchmark (list of {query, relevant_ids}) and pre-computed
    retrieved-rankings per query.
  - Delegates all metric math to ``eval_metrics`` (MRR/nDCG/recall).
  - Is pure w.r.t. metric computation — no RAG-stack dependencies.
  - Metadata envelope (release_id/timestamp/uuid) is injected when provided;
    unit tests should pass a fixed ``release_id`` to keep results deterministic.

``run_live(benchmark, rag_engine)`` orchestrates the live retrieval loop:
  - For each query, calls ``RAGEngine.query_retrieve_only()`` to obtain the
    ordered list of retrieved file_ids.
  - Collects results and calls ``score_run`` for metric computation.
  - Persists the run record (timestamp, release_id, per-query metrics,
    aggregate metrics) as JSONL to ``data/eval-runs/``.

Progress-Event Contract (shared with task 4.5 SSE stage events)
--------------------------------------------------------------
Pipeline stages: Searching | Reading | Drafting

``ProgressEvent`` shape::
{
    "stage": str,          # "Searching" | "Reading" | "Drafting"
    "query_id": str,       # Benchmark item id
    "timestamp": str,      # ISO 8601 UTC
}

Shared pipeline-stage vocabulary. The eval adapter emits Searching + Reading
only; Drafting is emitted by the chat answer-generation path (task 4.5).
Kept here as the canonical contract so task 4.5 can import without creating
a cross-domain dependency on the eval adapter.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Sequence

from app.config import settings

# Metric primitives — imported at top level so the pure eval functions are
# available before any service class is instantiated.
from app.services.eval_metrics import mean_reciprocal_rank as _mrr
from app.services.eval_metrics import ndcg_at_k as _ndcg
from app.services.eval_metrics import recall_at_k as _recall

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Progress-event vocabulary (shared with task 4.5)
# ---------------------------------------------------------------------------

STAGE_SEARCHING = "Searching"
STAGE_READING = "Reading"
STAGE_DRAFTING = "Drafting"

#: Canonical pipeline stages used by both the eval adapter and task 4.5 SSE.
#: The eval adapter emits only Searching and Reading (eval skips LLM drafting).
#: Drafting is emitted by the chat answer-generation path (task 4.5).
ALL_STAGES = (STAGE_SEARCHING, STAGE_READING, STAGE_DRAFTING)


@dataclass(frozen=True)
class ProgressEvent:
    """Immutable progress event emitted during live eval runs.

    Attributes:
        stage: Pipeline stage (Searching | Reading | Drafting).
        query_id: Benchmark item id this event pertains to.
        timestamp: ISO 8601 UTC timestamp when the event was emitted.
    """

    stage: str
    query_id: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, str]:
        return {"stage": self.stage, "query_id": self.query_id, "timestamp": self.timestamp}


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class BenchmarkItem:
    """A single benchmark query with ground-truth relevant document identifiers.

    Attributes:
        id: Stable identifier for this benchmark item.
        query: Natural-language query string.
        relevant_ids: Set of ground-truth file_ids (or doc identifiers) relevant
            to the query. Used for MRR/nDCG/recall computation.
    """

    id: str
    query: str
    relevant_ids: Sequence[str] = field(default_factory=list)


@dataclass
class RetrievedRanking:
    """The ordered list of file_ids retrieved for a query.

    Attributes:
        query_id: Benchmark item id this ranking pertains to.
        retrieved_ids: Ordered list of file_ids (rank 1 = most relevant).
    """

    query_id: str
    retrieved_ids: List[str] = field(default_factory=list)


@dataclass
class QueryMetrics:
    """Per-query metric breakdown."""

    query_id: str
    recall_at_k: Optional[float] = None
    mrr: Optional[float] = None
    ndcg_at_k: Optional[float] = None


@dataclass
class RunResult:
    """Result of a complete eval run.

    Attributes:
        run_id: Unique identifier for this run.
        timestamp: ISO 8601 UTC when the run started.
        release_id: Git short hash of the running commit, or a fallback string.
        top_k: The k used for recall@n and nDCG@n.
        query_metrics: Per-query metric records.
        mrr_mean: Mean Reciprocal Rank across all queries.
        ndcg_mean: Mean nDCG@k across all queries.
        recall_mean: Mean recall@k across all queries.
        progress_events: Optional list of progress events emitted during the run.
    """

    run_id: str
    timestamp: str
    release_id: str
    top_k: int
    query_metrics: List[QueryMetrics] = field(default_factory=list)
    mrr_mean: Optional[float] = None
    ndcg_mean: Optional[float] = None
    recall_mean: Optional[float] = None
    progress_events: List[ProgressEvent] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "timestamp": self.timestamp,
            "release_id": self.release_id,
            "top_k": self.top_k,
            "query_metrics": [qm.__dict__ for qm in self.query_metrics],
            "mrr_mean": self.mrr_mean,
            "ndcg_mean": self.ndcg_mean,
            "recall_mean": self.recall_mean,
            "progress_events": [e.to_dict() for e in self.progress_events],
        }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

#: Sub-directory under data_dir where run records are appended.
_EVAL_RUNS_SUBDIR = "eval-runs"


def _get_runs_dir() -> Path:
    """Return the eval-runs directory, creating it if necessary."""
    runs_dir = settings.data_dir / _EVAL_RUNS_SUBDIR
    runs_dir.mkdir(parents=True, exist_ok=True)
    return runs_dir


def _get_release_id() -> str:
    """Return the git short commit hash, falling back to env-var or 'unknown'."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=settings.data_dir.parent,  # repo root
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception as exc:
        logger.warning("Failed to get git commit hash: %s", exc)
    # Env-var fallback for containerized deployments
    for env_key in ("APP_RELEASE_ID", "APP_VERSION"):
        val = os.environ.get(env_key)
        if val:
            return val
    # Safe fallback — never crashes the run
    return getattr(settings, "app_version", "unknown") or "unknown"


# ---------------------------------------------------------------------------
# LiveEvalAdapter
# ---------------------------------------------------------------------------


class RAGEngineRetrieveOnly(Protocol):
    """Protocol describing the retrieval-only query interface required by run_live.

    callers can pass a real RAGEngine instance (which implements the method)
    or a mock in tests.
    """

    async def query_retrieve_only(
        self, query: str, vault_id: Optional[int] = None, top_k: Optional[int] = None
    ) -> List[str]:
        """Return ordered list of retrieved file_ids for a query.

        Args:
            query: Natural-language query.
            vault_id: Optional vault scope.
            top_k: Maximum number of results to return (defaults to engine setting).

        Returns:
            Ordered list of file_ids (rank 1 = most relevant).
        """
        ...


class LiveEvalAdapter:
    """Adapter that runs retrieval-quality benchmarks against the live RAG pipeline.

    This service is the bridge between the offline eval harness and the live
    retrieval pipeline. It supports two modes:

    1. ``score_run`` — Pure evaluation with injected rankings. Fully testable
       without any live services (embeddings, vector store, LLM).

    2. ``run_live`` — Orchestrates live retrieval via ``RAGEngine.query_retrieve_only``
       then delegates to ``score_run`` for metric computation and persistence.

    Usage::

        adapter = LiveEvalAdapter(top_k=5)

        # Mode 1: unit-testable with injected rankings
        result = adapter.score_run(benchmark, retrieved_rankings)

        # Mode 2: live retrieval + persistence
        result = await adapter.run_live(benchmark, rag_engine)

    Attributes:
        top_k: Default k for recall@k and nDCG@k. Can be overridden per-call.
    """

    def __init__(self, top_k: int = 5) -> None:
        self.top_k = top_k
        self._persist_lock = asyncio.Lock()  # serialize JSONL writes

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def score_run(
        self,
        benchmark: Sequence[BenchmarkItem],
        retrieved_per_query: Sequence[RetrievedRanking],
        progress_events: Optional[List[ProgressEvent]] = None,
        top_k_override: Optional[int] = None,
        release_id: Optional[str] = None,
    ) -> RunResult:
        """Compute retrieval metrics for a benchmark given injected retrieved rankings.

        Pure w.r.t. metric computation (no RAG-stack deps). Metadata envelope
        (release_id/timestamp/uuid) is injected when provided; unit tests should
        pass a fixed ``release_id`` to keep results fully deterministic.

        Args:
            benchmark: Sequence of ``BenchmarkItem`` with ground-truth relevant_ids.
            retrieved_per_query: Per-query retrieved-id rankings in the same order
                as ``benchmark`` (or at least covering all benchmark ids).
            progress_events: Optional progress events to carry through to the result.
            top_k_override: If set, use this k for nDCG@/recall@ computation and
                record it as the result's top_k instead of self.top_k.
            release_id: Optional git commit hash or build identifier. When None,
                falls back to ``_get_release_id()`` (subprocess call).

        Returns:
            ``RunResult`` with per-query metrics and aggregate means.
        """
        effective_top_k = top_k_override if top_k_override is not None else self.top_k

        retrieved_map: Dict[str, RetrievedRanking] = {
            r.query_id: r for r in retrieved_per_query
        }

        query_metrics: List[QueryMetrics] = []
        mrr_vals: List[float] = []
        ndcg_vals: List[float] = []
        recall_vals: List[float] = []

        for item in benchmark:
            ranking = retrieved_map.get(item.id)
            retrieved_ids = list(ranking.retrieved_ids) if ranking else []

            # MRR
            mrr = _mrr(retrieved_ids, list(item.relevant_ids))
            # nDCG — computed at effective_top_k
            ndcg = _ndcg(retrieved_ids, list(item.relevant_ids), effective_top_k)
            # Recall — computed at effective_top_k
            recall = _recall(retrieved_ids, list(item.relevant_ids), effective_top_k)

            # Only record a metric when we have ground-truth to compare against;
            # a 0.0 MRR means "found nothing relevant" which is a valid score,
            # but no expected ids means the metric is undefined (None).
            has_expected = bool(item.relevant_ids)
            query_metrics.append(
                QueryMetrics(
                    query_id=item.id,
                    mrr=mrr if has_expected else None,
                    ndcg_at_k=ndcg if has_expected else None,
                    recall_at_k=recall if has_expected else None,
                )
            )

            if has_expected:
                mrr_vals.append(mrr)
                ndcg_vals.append(ndcg)
                recall_vals.append(recall)

        # Aggregate means
        mrr_mean = sum(mrr_vals) / len(mrr_vals) if mrr_vals else None
        ndcg_mean = sum(ndcg_vals) / len(ndcg_vals) if ndcg_vals else None
        recall_mean = sum(recall_vals) / len(recall_vals) if recall_vals else None

        timestamp = datetime.now(timezone.utc).isoformat()
        run_id = str(uuid.uuid4())[:8]

        return RunResult(
            run_id=run_id,
            timestamp=timestamp,
            release_id=release_id if release_id is not None else _get_release_id(),
            top_k=effective_top_k,
            query_metrics=query_metrics,
            mrr_mean=mrr_mean,
            ndcg_mean=ndcg_mean,
            recall_mean=recall_mean,
            progress_events=progress_events or [],
        )

    async def run_live(
        self,
        benchmark: Sequence[BenchmarkItem],
        rag_engine: RAGEngineRetrieveOnly,
        vault_id: Optional[int] = None,
        top_k: Optional[int] = None,
        progress_events: Optional[List[ProgressEvent]] = None,
    ) -> RunResult:
        """Run the live retrieval pipeline for a benchmark and compute metrics.

        For each query in ``benchmark``, calls ``rag_engine.query_retrieve_only``
        to obtain the ordered retrieved file_ids, then delegates to ``score_run``
        for metric computation and persistence.

        Args:
            benchmark: Sequence of ``BenchmarkItem`` with ground-truth relevant_ids.
            rag_engine: ``RAGEngine`` (or test mock) providing ``query_retrieve_only``.
            vault_id: Optional vault scope for all queries.
            top_k: Override the default top_k for this run.
            progress_events: Optional progress events to carry through to the result.

        Returns:
            ``RunResult`` with per-query metrics and aggregate means.
        """
        effective_top_k = top_k if top_k is not None else self.top_k
        all_events = list(progress_events or [])

        retrieved_per_query: List[RetrievedRanking] = []

        for item in benchmark:
            # Emit Searching event
            all_events.append(ProgressEvent(stage=STAGE_SEARCHING, query_id=item.id))

            retrieved_ids = await rag_engine.query_retrieve_only(
                query=item.query,
                vault_id=vault_id,
                top_k=effective_top_k,
            )

            all_events.append(ProgressEvent(stage=STAGE_READING, query_id=item.id))

            retrieved_per_query.append(
                RetrievedRanking(query_id=item.id, retrieved_ids=retrieved_ids)
            )

        # Compute release_id off the event loop (subprocess/git call).
        release_id = await asyncio.to_thread(_get_release_id)

        # Delegate to score_run for metric computation (injecting progress events).
        # score_run uses effective_top_k for both the nDCG/recall cutoff and the
        # recorded result.top_k, so no post-mutation is needed.
        result = self.score_run(
            benchmark=benchmark,
            retrieved_per_query=retrieved_per_query,
            progress_events=all_events,
            top_k_override=effective_top_k,
            release_id=release_id,
        )

        # Persist asynchronously
        await self._persist_run(result)

        return result

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def _persist_run(self, result: RunResult) -> None:
        """Append a run record as JSONL to the eval-runs directory.

        Thread/async-safe via a lock to prevent interleaved writes.
        """
        async with self._persist_lock:
            try:
                runs_dir = _get_runs_dir()
                run_file = runs_dir / "runs.jsonl"
                record = result.to_dict()
                line = json.dumps(record, sort_keys=True)
                with run_file.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
                logger.info(
                    "Eval run %s persisted to %s (MRR=%.3f, nDCG=%.3f, recall=%.3f)",
                    result.run_id,
                    run_file,
                    result.mrr_mean or 0.0,
                    result.ndcg_mean or 0.0,
                    result.recall_mean or 0.0,
                )
            except Exception as exc:
                logger.error("Failed to persist eval run %s: %s", result.run_id, exc)


__all__ = [
    "ALL_STAGES",
    "BenchmarkItem",
    "LiveEvalAdapter",
    "ProgressEvent",
    "QueryMetrics",
    "RetrievedRanking",
    "RunResult",
    "STAGE_DRAFTING",
    "STAGE_READING",
    "STAGE_SEARCHING",
]
