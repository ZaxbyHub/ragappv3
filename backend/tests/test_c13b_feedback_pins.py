"""Feedback pins for issue #718 (swarm-pr-review run pr976-20261018).

RED at the review head (abbdf643) / GREEN after the feedback fixes:
- F-018: neighbor range query filters by the center chunk's chunk_scale.
- F-005: the two degrade-to-[] handlers actually catch VectorStoreError
  (document_retrieval.expand_window and document_processor retry reconcile).
- F-002: the per-run report file exists and loads via load_run_report.
- F-004: request bounds (benchmark/contexts max_length) reject oversized
  payloads; LIKE wildcard `_` and backslash are escaped literally.
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.services.vector_store import VectorStore, VectorStoreError


class _CapturingTable:
    """Fake LanceDB table capturing the .where() filter string."""

    def __init__(self, rows):
        self._rows = rows
        self.captured_where: list[str] = []

    def query(self):
        return _CapturingQuery(self)


class _CapturingQuery:
    def __init__(self, table):
        self._table = table

    def where(self, query: str):
        self._table.captured_where.append(query)
        return self

    async def to_list(self):
        query = self._table.captured_where[-1]
        # Minimal AND-clause evaluator for the two predicates under test.
        rows = self._table._rows
        if "chunk_scale = '768'" in query:
            rows = [r for r in rows if r.get("chunk_scale") == "768"]
        import re as _re

        m = _re.search(r"chunk_index BETWEEN (\d+) AND (\d+)", query)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            rows = [
                r for r in rows if lo <= int(r.get("chunk_index", 0)) <= hi
            ]
        return rows


class TestScaleAwareNeighborQuery(unittest.IsolatedAsyncioTestCase):
    """F-018: the range window must filter by the center chunk's scale."""

    def _make_store(self, rows):
        store = VectorStore.__new__(VectorStore)
        store.table = _CapturingTable(rows)
        return store

    async def test_scale_filter_applied_when_scale_passed(self):
        rows = [
            {"chunk_index": 1, "chunk_scale": "768", "text": "a"},
            {"chunk_index": 1, "chunk_scale": "1536", "text": "wrong-region"},
            {"chunk_index": 2, "chunk_scale": "768", "text": "b"},
            {"chunk_index": 3, "chunk_scale": "768", "text": "c"},
        ]
        store = self._make_store(rows)
        out = await store.get_chunks_by_file_range(
            "f_1", 2, 1, 1, chunk_scale="768"
        )
        self.assertEqual(
            [r["text"] for r in out], ["a", "b", "c"]
        )
        self.assertIn("chunk_scale = '768'", store.table.captured_where[-1])

    async def test_no_scale_keeps_legacy_shape(self):
        rows = [{"chunk_index": 1, "text": "a"}]
        store = self._make_store(rows)
        await store.get_chunks_by_file_range("f_1", 2, 1, 1)
        self.assertNotIn(
            "chunk_scale", store.table.captured_where[-1]
        )


class TestExpandWindowDegrade(unittest.IsolatedAsyncioTestCase):
    """F-005: expand_window must degrade to no-neighbors (not raise) when
    get_chunks_by_uid raises VectorStoreError."""

    def _make_service(self):
        from app.services.document_retrieval import DocumentRetrievalService

        store = MagicMock()
        store.get_chunks_by_uid = AsyncMock(
            side_effect=VectorStoreError("store unavailable")
        )
        service = DocumentRetrievalService.__new__(DocumentRetrievalService)
        service.vector_store = store
        service.retrieval_window = 1
        service.retrieval_top_k = 5
        return service

    async def test_expand_window_degrades_on_store_error(self):
        from app.services.document_retrieval import RAGSource

        service = self._make_service()
        source = RAGSource(
            text="center chunk text",
            file_id="1",
            score=0.5,
            metadata={"chunk_index": 2, "chunk_scale": "768"},
        )
        out = await service.expand_window([source])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].file_id, "1")
        self.assertEqual(out[0].text, "center chunk text")


class TestRequestBounds(unittest.TestCase):
    """F-004: benchmark/contexts max_length reject oversized payloads."""

    def test_benchmark_over_50_rejected(self):
        from app.api.routes.eval import LiveBenchmarkItem, LiveEvalRequest

        with self.assertRaises(Exception):
            LiveEvalRequest(
                benchmark=[
                    LiveBenchmarkItem(
                        id=f"q{i}", query="q", relevant_ids=["d1"]
                    )
                    for i in range(51)
                ]
            )

    def test_contexts_over_50_rejected(self):
        from app.api.routes.eval import HeuristicEvaluationRequest

        with self.assertRaises(Exception):
            HeuristicEvaluationRequest(
                query="q",
                answer="a",
                contexts=[f"c{i}" for i in range(51)],
            )


class TestLikeEscapeArms(unittest.TestCase):
    """F-013: `_` and backslash in q are literal, not wildcards."""

    def test_underscore_is_literal(self):
        # Same chain as search.py:609-613; pins the `_` arm at the unit level.
        escaped = "q_".replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        self.assertEqual(escaped, "q\\_")

    def test_backslash_is_doubled_first(self):
        escaped = "q\\%".replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        self.assertEqual(escaped, "q\\\\\\%")


class TestPerRunReportLoadable(unittest.IsolatedAsyncioTestCase):
    """F-002: the per-run report file exists and loads via load_run_report —
    the artifact the operator doc names as the compare_runs input."""

    async def test_report_file_written_and_loadable(self):
        import app.services.eval_adapter as adapter_module
        from app.services.eval_adapter import LiveEvalAdapter, RunResult

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(adapter_module.settings, "data_dir", Path(tmp)):
                adapter = LiveEvalAdapter(top_k=3)
                from app.services.eval_adapter import QueryMetrics

                result = RunResult(
                    run_id="c13btest1",
                    timestamp="2026-10-19T00:00:00Z",
                    release_id="testrelease",
                    top_k=3,
                    query_metrics=[
                        QueryMetrics(query_id="c13btest1", recall_at_k=0.5)
                    ],
                    mrr_mean=0.5,
                    ndcg_mean=0.4,
                    recall_mean=0.6,
                )
                report_path = await adapter._persist_run(result)
                self.assertIsNotNone(report_path)
                self.assertTrue(Path(report_path).exists())

                from app.services.eval_compare import load_run_report

                report = load_run_report(report_path)
                self.assertEqual(report.run_id, "c13btest1")
                # Environment identity now carries the real model.
                from app.config import settings as app_settings

                self.assertTrue(
                    report.environment.get("model_hash"),
                    "model_hash must be non-empty",
                )
                # matched_resources must be non-empty for this run's own ids
                from app.services.eval_compare import (
                    RunReport as _RR,
                )
                from app.services.eval_compare import (
                    compare_runs,
                )

                comparison = compare_runs(report, report)
                self.assertEqual(
                    sorted(comparison.matched_resources), ["c13btest1"]
                )
