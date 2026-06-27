"""Unit tests for the RAG eval harness metric math (P3.5)."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests.eval.eval_harness import (
    CaseResult,
    EvalRunner,
    citation_validity,
    fact_coverage,
    load_jsonl,
    mean_reciprocal_rank,
    ndcg_at_k,
    recall_at_k,
)


class TestMetricPrimitives(unittest.TestCase):
    def test_recall_at_k_perfect(self):
        self.assertEqual(recall_at_k(["a", "b", "c"], ["a", "b"], k=5), 1.0)

    def test_recall_at_k_partial(self):
        self.assertAlmostEqual(
            recall_at_k(["a", "x"], ["a", "b"], k=5), 0.5
        )

    def test_recall_at_k_respects_top_k(self):
        self.assertAlmostEqual(
            recall_at_k(["x", "a"], ["a"], k=1), 0.0
        )

    def test_mrr_first_hit(self):
        self.assertEqual(mean_reciprocal_rank(["x", "a", "b"], ["a"]), 0.5)

    def test_mrr_no_hit(self):
        self.assertEqual(mean_reciprocal_rank(["x", "y"], ["a"]), 0.0)

    def test_ndcg_perfect(self):
        self.assertAlmostEqual(
            ndcg_at_k(["a", "b"], ["a", "b"], k=2), 1.0
        )

    def test_ndcg_partial(self):
        # First spot misses, second hits → DCG smaller than ideal.
        v = ndcg_at_k(["x", "a"], ["a"], k=2)
        self.assertGreater(v, 0.0)
        self.assertLess(v, 1.0)

    def test_citation_validity_no_citations_is_vacuous(self):
        self.assertEqual(citation_validity([], ["S1"]), 1.0)

    def test_citation_validity_invalid(self):
        self.assertAlmostEqual(
            citation_validity(["S1", "S99"], ["S1"]), 0.5
        )

    def test_fact_coverage_substring_match(self):
        self.assertAlmostEqual(
            fact_coverage("We shipped on October 15.", ["October 15", "Q4"]),
            0.5,
        )


class TestEvalRunner(unittest.TestCase):
    def setUp(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        self.golden_path = Path(path)
        self.golden_path.write_text(
            "\n".join(
                json.dumps(c)
                for c in [
                    {
                        "id": "g1",
                        "query": "?",
                        "expected_chunk_ids": ["c-a", "c-b"],
                        "expected_source_labels": ["S1", "S2"],
                        "expected_facts": ["foo"],
                    },
                    {
                        "id": "g2",
                        "query": "?",
                        "expect_no_match": True,
                    },
                ]
            )
        )

    def tearDown(self) -> None:
        try:
            self.golden_path.unlink()
        except OSError:
            pass

    def test_load_jsonl_round_trip(self):
        cases = load_jsonl(self.golden_path)
        self.assertEqual(len(cases), 2)
        self.assertEqual(cases[0].id, "g1")
        self.assertTrue(cases[1].expect_no_match)

    def test_summary_aggregates_only_runs(self):
        cases = load_jsonl(self.golden_path)
        runner = EvalRunner(cases, top_k=2)
        # Only run g1 — g2 is intentionally left without a result.
        runner.add_result(
            CaseResult(
                id="g1",
                retrieved_chunk_ids=["c-a", "c-x"],
                retrieved_source_labels=["S1"],
                answer="foo lives here [S1]",
                cited_source_labels=["S1"],
            )
        )
        summary = runner.summarize()
        self.assertEqual(summary["case_count"], 2)
        # recall@k for g1 = 1/2; the unrun case is None and excluded.
        self.assertAlmostEqual(summary["recall_at_k_mean"], 0.5)
        # citation validity = 1.0 (S1 cited and available).
        self.assertEqual(summary["citation_validity_mean"], 1.0)

    def test_no_match_correctness(self):
        cases = load_jsonl(self.golden_path)
        runner = EvalRunner(cases, top_k=2)
        runner.add_result(CaseResult(id="g2", no_match_returned=True))
        summary = runner.summarize()
        self.assertEqual(summary["no_match_correct_rate"], 1.0)

    def test_to_json_writes_machine_readable_report(self):
        cases = load_jsonl(self.golden_path)
        runner = EvalRunner(cases, top_k=5)
        runner.add_result(
            CaseResult(
                id="g1",
                retrieved_chunk_ids=["c-a", "c-b"],
                retrieved_source_labels=["S1", "S2"],
                answer="foo here [S1] [S2]",
                cited_source_labels=["S1", "S2"],
            )
        )
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        out = Path(path)
        try:
            runner.to_json(out)
            data = json.loads(out.read_text())
            self.assertIn("summary", data)
            self.assertIn("cases", data)
            self.assertEqual(data["summary"]["case_count"], 2)
        finally:
            out.unlink(missing_ok=True)

    def test_unknown_result_id_raises(self):
        cases = load_jsonl(self.golden_path)
        runner = EvalRunner(cases)
        with self.assertRaises(KeyError):
            runner.add_result(CaseResult(id="nope"))


if __name__ == "__main__":
    unittest.main()
