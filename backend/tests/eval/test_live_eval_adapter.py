"""Tests for LiveEvalAdapter (FR-001 eval adapter).

These tests verify:
1. ``score_run`` computes MRR/nDCG/recall correctly with injected rankings
   (no live services required — fully unit-testable).
2. Progress-event contract is correctly defined.
3. Persistence writes timestamp + release_id to the runs JSONL.

Because ``score_run`` is a pure function with injected rankings, these tests
do NOT require embeddings, vector store, or LLM services.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
from unittest.mock import patch

import pytest

# Import the public API from the adapter
from app.services.eval_adapter import (
    ALL_STAGES,
    STAGE_DRAFTING,
    STAGE_READING,
    STAGE_SEARCHING,
    BenchmarkItem,
    LiveEvalAdapter,
    ProgressEvent,
    QueryMetrics,
    RetrievedRanking,
    RunResult,
    _get_release_id,
)

#: Fixed release_id injected into all score_run calls in unit tests.
_FIXED_RELEASE_ID = "test-run-abc123"

# ---------------------------------------------------------------------------
# Hand-computed test fixtures
# ---------------------------------------------------------------------------

# Benchmark: 3 queries with known ground-truth relevant_ids
_BENCHMARK = [
    BenchmarkItem(id="q1", query="What is Python?", relevant_ids=["f1", "f3"]),
    BenchmarkItem(id="q2", query="How does async work?", relevant_ids=["f2"]),
    BenchmarkItem(id="q3", query="Explain decorators", relevant_ids=["f5", "f6", "f7"]),
]

# Retrieved rankings for each query (rank order)
# q1: f2 at rank 1 (not relevant), f1 at rank 2 (relevant), f3 at rank 3 (relevant)
#     -> MRR = 1/2 = 0.5, recall@3 = 2/2 = 1.0
# q2: f2 at rank 1 (relevant)
#     -> MRR = 1/1 = 1.0, recall@3 = 1/1 = 1.0
# q3: f1 at rank 1 (not relevant), f5 at rank 2 (relevant), f6 at rank 3 (relevant), f7 not in top-3
#     -> MRR = 1/2 = 0.5, recall@3 = 2/3 ≈ 0.667
_RETRIEVED_RANKINGS = [
    RetrievedRanking(query_id="q1", retrieved_ids=["f2", "f1", "f3"]),
    RetrievedRanking(query_id="q2", retrieved_ids=["f2"]),
    RetrievedRanking(query_id="q3", retrieved_ids=["f1", "f5", "f6"]),
]

# Expected per-query metrics (hand-computed):
# q1: MRR=0.5, recall@3=1.0, nDCG@3 = DCG(f2,f1,f3)/IDCG(f1,f3) = (0 + 1/log2(3) + 1/log2(4)) / (1/log2(2) + 1/log2(3))
#     DCG = 0 + 0.631 + 0.5 = 1.131, IDCG = 1 + 0.631 = 1.631, nDCG = 0.693
# q2: MRR=1.0, recall@3=1.0, nDCG@3 = (1/log2(2))/1 = 1.0
# q3: MRR=0.5, recall@3=2/3=0.667, nDCG@3 = DCG(f1,f5,f6)/IDCG(f5,f6,f7)
#     DCG = 0 + 1/log2(3) + 1/log2(4) = 0 + 0.631 + 0.5 = 1.131
#     IDCG = 1/log2(2) + 1/log2(3) + 1/log2(4) = 1 + 0.631 + 0.5 = 2.131
#     nDCG = 1.131/2.131 = 0.531
#
# Aggregate means:
# MRR_mean = (0.5 + 1.0 + 0.5) / 3 = 2/3 ≈ 0.667
# recall_mean = (1.0 + 1.0 + 0.667) / 3 ≈ 0.889
# nDCG_mean = (0.693 + 1.0 + 0.531) / 3 ≈ 0.741


class TestScoreRunMetrics:
    """Test that score_run computes MRR, nDCG, and recall correctly."""

    def test_score_run_mrr(self):
        """MRR: first relevant item rank determines reciprocal rank."""
        adapter = LiveEvalAdapter(top_k=3)

        # Only q1: f1 at rank 2 -> MRR = 1/2 = 0.5
        single_query = [BenchmarkItem(id="q1", query="test", relevant_ids=["f1"])]
        single_ranking = [RetrievedRanking(query_id="q1", retrieved_ids=["x", "f1", "z"])]

        result = adapter.score_run(single_query, single_ranking, release_id=_FIXED_RELEASE_ID)

        assert result.mrr_mean is not None
        assert abs(result.mrr_mean - 0.5) < 1e-6

    def test_score_run_mrr_first_relevant_at_rank_1(self):
        """MRR = 1.0 when the first retrieved item is relevant."""
        adapter = LiveEvalAdapter(top_k=3)

        single_query = [BenchmarkItem(id="q1", query="test", relevant_ids=["f1"])]
        single_ranking = [RetrievedRanking(query_id="q1", retrieved_ids=["f1", "x", "y"])]

        result = adapter.score_run(single_query, single_ranking, release_id=_FIXED_RELEASE_ID)

        assert result.mrr_mean == 1.0

    def test_score_run_mrr_no_relevant_retrieved(self):
        """MRR = 0.0 when no relevant items are retrieved."""
        adapter = LiveEvalAdapter(top_k=3)

        single_query = [BenchmarkItem(id="q1", query="test", relevant_ids=["f1"])]
        single_ranking = [RetrievedRanking(query_id="q1", retrieved_ids=["x", "y", "z"])]

        result = adapter.score_run(single_query, single_ranking, release_id=_FIXED_RELEASE_ID)

        assert result.mrr_mean == 0.0

    def test_score_run_recall_at_k_all_found(self):
        """Recall = 1.0 when all relevant items are in top-k."""
        adapter = LiveEvalAdapter(top_k=5)

        single_query = [
            BenchmarkItem(id="q1", query="test", relevant_ids=["f1", "f2"])
        ]
        single_ranking = [
            RetrievedRanking(query_id="q1", retrieved_ids=["f1", "f2", "f3"])
        ]

        result = adapter.score_run(single_query, single_ranking, release_id=_FIXED_RELEASE_ID)

        assert result.recall_mean == 1.0

    def test_score_run_recall_at_k_partial(self):
        """Recall = partial when only some relevant items are in top-k."""
        adapter = LiveEvalAdapter(top_k=3)

        single_query = [
            BenchmarkItem(id="q1", query="test", relevant_ids=["f1", "f2", "f3"])
        ]
        # Only f1 and f2 in top-3, f3 is rank 4
        single_ranking = [
            RetrievedRanking(query_id="q1", retrieved_ids=["f1", "f2", "x"])
        ]

        result = adapter.score_run(single_query, single_ranking, release_id=_FIXED_RELEASE_ID)

        assert abs(result.recall_mean - 2 / 3) < 1e-6

    def test_score_run_ndcg_at_k(self):
        """nDCG@k penalizes relevant items at lower ranks."""
        adapter = LiveEvalAdapter(top_k=3)

        single_query = [BenchmarkItem(id="q1", query="test", relevant_ids=["f1"])]
        # f1 at rank 3 (worst) -> lower nDCG
        single_ranking = [
            RetrievedRanking(query_id="q1", retrieved_ids=["x", "y", "f1"])
        ]

        result = adapter.score_run(single_query, single_ranking, release_id=_FIXED_RELEASE_ID)

        # nDCG = DCG/IDCG, DCG = 1/log2(4) = 0.5, IDCG = 1/log2(2) = 1.0
        assert result.ndcg_mean is not None
        assert abs(result.ndcg_mean - 0.5) < 1e-6

    def test_score_run_ndcg_at_k_perfect(self):
        """nDCG = 1.0 when the first retrieved item is relevant."""
        adapter = LiveEvalAdapter(top_k=3)

        single_query = [BenchmarkItem(id="q1", query="test", relevant_ids=["f1"])]
        single_ranking = [
            RetrievedRanking(query_id="q1", retrieved_ids=["f1", "x", "y"])
        ]

        result = adapter.score_run(single_query, single_ranking, release_id=_FIXED_RELEASE_ID)

        assert abs(result.ndcg_mean - 1.0) < 1e-6

    def test_score_run_full_benchmark_hand_computed(self):
        """Full 3-query benchmark with hand-computed aggregate means."""
        adapter = LiveEvalAdapter(top_k=3)
        result = adapter.score_run(_BENCHMARK, _RETRIEVED_RANKINGS, release_id=_FIXED_RELEASE_ID)

        # Verify aggregate means are close to hand-computed values
        assert result.mrr_mean is not None
        assert abs(result.mrr_mean - 2 / 3) < 1e-5  # ≈ 0.667

        assert result.recall_mean is not None
        assert abs(result.recall_mean - 0.889) < 1e-3  # ≈ 0.889

        assert result.ndcg_mean is not None
        assert abs(result.ndcg_mean - 0.741) < 1e-3  # ≈ 0.741

    def test_score_run_per_query_metrics_count(self):
        """Each query gets its own QueryMetrics entry."""
        adapter = LiveEvalAdapter(top_k=3)
        result = adapter.score_run(_BENCHMARK, _RETRIEVED_RANKINGS)

        assert len(result.query_metrics) == 3
        assert {qm.query_id for qm in result.query_metrics} == {"q1", "q2", "q3"}

    def test_score_run_run_id_is_present(self):
        """Run result has a non-empty run_id."""
        adapter = LiveEvalAdapter(top_k=3)
        result = adapter.score_run(_BENCHMARK, _RETRIEVED_RANKINGS, release_id=_FIXED_RELEASE_ID)

        assert result.run_id is not None
        assert len(result.run_id) > 0

    def test_score_run_timestamp_is_iso_utc(self):
        """Timestamp is an ISO 8601 UTC string."""
        adapter = LiveEvalAdapter(top_k=3)
        result = adapter.score_run(_BENCHMARK, _RETRIEVED_RANKINGS, release_id=_FIXED_RELEASE_ID)

        # Should not raise
        dt = datetime.fromisoformat(result.timestamp.replace("Z", "+00:00"))
        assert dt.tzinfo is not None

    def test_score_run_empty_benchmark(self):
        """Empty benchmark returns a valid result with all-None aggregates."""
        adapter = LiveEvalAdapter(top_k=3)
        result = adapter.score_run([], [], release_id=_FIXED_RELEASE_ID)

        assert result.mrr_mean is None
        assert result.ndcg_mean is None
        assert result.recall_mean is None
        assert result.query_metrics == []

    def test_score_run_empty_ranking_for_query(self):
        """Empty retrieved list for a query is handled gracefully."""
        adapter = LiveEvalAdapter(top_k=3)

        single_query = [BenchmarkItem(id="q1", query="test", relevant_ids=["f1"])]
        single_ranking = [RetrievedRanking(query_id="q1", retrieved_ids=[])]

        result = adapter.score_run(single_query, single_ranking, release_id=_FIXED_RELEASE_ID)

        assert result.mrr_mean == 0.0
        assert result.recall_mean == 0.0
        assert result.ndcg_mean == 0.0

    def test_score_run_top_k_override_changes_cutoff(self):
        """top_k_override causes metrics to be computed at the override k.

        Scenario: adapter default top_k=5, but we call score_run with
        top_k_override=2. The retrieved list has relevant items at ranks 2 and 3.

        At k=2: only rank 1-2 are considered → 1 relevant found → recall=0.5
        At k=3: ranks 1-3 considered → 2 relevant found → recall=1.0

        The result.top_k must also be set to the override (2), not the default.
        """
        adapter = LiveEvalAdapter(top_k=5)

        single_query = [
            BenchmarkItem(id="q1", query="test", relevant_ids=["f1", "f2"])
        ]
        # f1 at rank 2, f2 at rank 3
        single_ranking = [
            RetrievedRanking(query_id="q1", retrieved_ids=["x", "f1", "f2", "y", "z"])
        ]

        # Override k=2: only top-2 considered, only f1 is relevant → recall=0.5
        result = adapter.score_run(single_query, single_ranking, top_k_override=2, release_id=_FIXED_RELEASE_ID)

        assert result.top_k == 2, "result.top_k must be the override value"
        assert result.recall_mean == 0.5, "recall@2 should be 0.5 (1/2 relevant in top-2)"

        # Without override (k=5): both f1 and f2 in top-5 → recall=1.0
        result_default = adapter.score_run(single_query, single_ranking, release_id=_FIXED_RELEASE_ID)
        assert result_default.top_k == 5
        assert result_default.recall_mean == 1.0, "recall@5 should be 1.0 (2/2 relevant)"

    def test_score_run_top_k_override_affects_ndcg(self):
        """top_k_override changes nDCG computation (not just recall)."""
        adapter = LiveEvalAdapter(top_k=5)

        # Single relevant item at rank 3
        single_query = [
            BenchmarkItem(id="q1", query="test", relevant_ids=["f1"])
        ]
        # f1 at rank 3
        single_ranking = [
            RetrievedRanking(query_id="q1", retrieved_ids=["x", "y", "f1"])
        ]

        # nDCG@1: f1 not in top-1 → DCG=0 → nDCG=0
        result_k1 = adapter.score_run(single_query, single_ranking, top_k_override=1, release_id=_FIXED_RELEASE_ID)
        assert result_k1.ndcg_mean == 0.0

        # nDCG@3: f1 at rank 3 → DCG = 1/log2(4) = 0.5, IDCG = 1/log2(2) = 1.0
        result_k3 = adapter.score_run(single_query, single_ranking, top_k_override=3, release_id=_FIXED_RELEASE_ID)
        assert abs(result_k3.ndcg_mean - 0.5) < 1e-6


class TestProgressEventContract:
    """Verify the progress-event vocabulary and ProgressEvent shape."""

    def test_stage_constants_present(self):
        """Searching, Reading, Drafting stages are defined."""
        assert STAGE_SEARCHING == "Searching"
        assert STAGE_READING == "Reading"
        assert STAGE_DRAFTING == "Drafting"

    def test_all_stages_tuple(self):
        """ALL_STAGES contains all three stages."""
        assert ALL_STAGES == ("Searching", "Reading", "Drafting")

    def test_progress_event_to_dict(self):
        """ProgressEvent.to_dict returns the expected shape."""
        event = ProgressEvent(stage=STAGE_SEARCHING, query_id="q1")
        d = event.to_dict()

        assert d["stage"] == "Searching"
        assert d["query_id"] == "q1"
        assert "timestamp" in d

    def test_progress_event_timestamp_auto_generated(self):
        """ProgressEvent auto-generates ISO UTC timestamp."""
        before = datetime.now(timezone.utc).isoformat()
        event = ProgressEvent(stage=STAGE_READING, query_id="q2")
        after = datetime.now(timezone.utc).isoformat()

        assert before <= event.timestamp <= after

    def test_progress_event_frozen(self):
        """ProgressEvent is frozen (immutable)."""
        event = ProgressEvent(stage=STAGE_SEARCHING, query_id="q1")
        with pytest.raises(AttributeError):
            event.stage = STAGE_READING  # type: ignore

    def test_progress_events_carried_through_score_run(self):
        """Progress events passed to score_run appear in the result."""
        adapter = LiveEvalAdapter(top_k=3)
        events = [
            ProgressEvent(stage=STAGE_SEARCHING, query_id="q1"),
            ProgressEvent(stage=STAGE_READING, query_id="q1"),
        ]
        result = adapter.score_run(_BENCHMARK, _RETRIEVED_RANKINGS, progress_events=events, release_id=_FIXED_RELEASE_ID)

        assert len(result.progress_events) == 2
        assert result.progress_events[0].stage == STAGE_SEARCHING
        assert result.progress_events[1].stage == STAGE_READING


class TestPersistence:
    """Test that persistence writes timestamp + release_id to the runs JSONL."""

    def test_persist_run_writes_jsonl(self, tmp_path: Path):
        """A run record is appended as a single JSON line to runs.jsonl."""
        import app.services.eval_adapter as adapter_module

        # Patch data_dir to tmp_path
        with patch.object(adapter_module.settings, "data_dir", tmp_path):
            adapter = LiveEvalAdapter(top_k=3)
            result = adapter.score_run(_BENCHMARK, _RETRIEVED_RANKINGS, release_id=_FIXED_RELEASE_ID)

            # Use asyncio.run() which is Python 3.7+ compatible
            import asyncio
            asyncio.run(adapter._persist_run(result))

            # Verify file was created
            runs_file = tmp_path / "eval-runs" / "runs.jsonl"
            assert runs_file.exists()

            # Verify line is valid JSON
            lines = runs_file.read_text(encoding="utf-8").strip().split("\n")
            assert len(lines) == 1

            record = json.loads(lines[0])

            # Verify required fields
            assert record["run_id"] == result.run_id
            assert record["timestamp"] == result.timestamp
            assert "release_id" in record
            assert record["release_id"] not in ("", "None")

            # Verify metrics are present
            assert "mrr_mean" in record
            assert "ndcg_mean" in record
            assert "recall_mean" in record

    def test_persist_run_multiple_runs_append(self, tmp_path: Path):
        """Multiple runs are appended as separate JSON lines."""
        import app.services.eval_adapter as adapter_module

        with patch.object(adapter_module.settings, "data_dir", tmp_path):
            adapter = LiveEvalAdapter(top_k=3)

            import asyncio

            for _ in range(3):
                result = adapter.score_run(_BENCHMARK, _RETRIEVED_RANKINGS, release_id=_FIXED_RELEASE_ID)
                asyncio.run(adapter._persist_run(result))

            runs_file = tmp_path / "eval-runs" / "runs.jsonl"
            lines = runs_file.read_text(encoding="utf-8").strip().split("\n")

            assert len(lines) == 3
            # Each line is valid JSON
            for line in lines:
                record = json.loads(line)
                assert "run_id" in record


class TestReleaseId:
    """Test release_id acquisition via git or fallback."""

    def test_release_id_from_git(self):
        """_get_release_id returns a non-empty string (git or fallback)."""
        release_id = _get_release_id()
        assert release_id is not None
        assert isinstance(release_id, str)
        assert len(release_id) > 0

    def test_release_id_fallback_unknown_when_git_unavailable(self):
        """When git fails, falls back to 'unknown' (settings has no app_version attr)."""
        import subprocess

        import app.services.eval_adapter as adapter_module

        # Patch subprocess.run to simulate git not being available
        with patch.object(
            subprocess, "run", side_effect=FileNotFoundError("git not found")
        ):
            release_id = _get_release_id()
            # Since settings has no app_version, fallback is "unknown"
            assert release_id == "unknown"
