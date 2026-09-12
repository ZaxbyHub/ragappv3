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
    GoldenCase,
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


class TestArtifactMetrics(unittest.TestCase):
    """Issue #462 — artifact-level retrieval/citation/vision metrics."""

    def _runner(self, expected_artifact_ids, expected_vision_mode="either"):
        from tests.eval.eval_harness import GoldenCase

        cases = [
            GoldenCase(
                id="g-art",
                query="?",
                expected_artifact_ids=expected_artifact_ids,
                expected_vision_mode=expected_vision_mode,
            )
        ]
        return EvalRunner(cases, top_k=3)

    def test_artifact_recall_independent_of_chunk_ids(self):
        runner = self._runner(expected_artifact_ids=["art-a", "art-b"])
        runner.add_result(
            CaseResult(
                id="g-art",
                retrieved_artifact_ids=["art-a", "art-x"],
            )
        )
        metrics = runner.evaluate()
        m = metrics[0]
        self.assertAlmostEqual(m.artifact_recall_at_k, 0.5)
        # Chunk recall is separate and None here (no chunk expectations).
        self.assertIsNone(m.recall_at_k)

    def test_artifact_citation_validity(self):
        runner = self._runner(expected_artifact_ids=["art-a"])
        runner.add_result(
            CaseResult(
                id="g-art",
                retrieved_artifact_ids=["art-a"],
                cited_artifact_ids=["art-a", "art-bogus"],
            )
        )
        metrics = runner.evaluate()
        # 1 of 2 cited artifact ids is retrieved -> 0.5
        self.assertAlmostEqual(metrics[0].artifact_citation_validity, 0.5)

    def test_vision_use_rate_for_used_mode(self):
        runner = self._runner(
            expected_artifact_ids=["art-a", "art-b"], expected_vision_mode="used"
        )
        runner.add_result(
            CaseResult(
                id="g-art",
                retrieved_artifact_ids=["art-a", "art-b"],
                vision_used_artifact_ids=["art-a"],
            )
        )
        metrics = runner.evaluate()
        # 1 of 2 expected artifacts used by the VLM.
        self.assertAlmostEqual(metrics[0].vision_use_rate, 0.5)
        self.assertEqual(metrics[0].vision_degradation_rate, 0.0)

    def test_vision_degradation_rate(self):
        runner = self._runner(
            expected_artifact_ids=["art-a", "art-b"], expected_vision_mode="degraded"
        )
        runner.add_result(
            CaseResult(
                id="g-art",
                retrieved_artifact_ids=["art-a", "art-b"],
                vision_degraded_artifact_ids=["art-b"],
            )
        )
        metrics = runner.evaluate()
        self.assertAlmostEqual(metrics[0].vision_degradation_rate, 0.5)

    def test_retrieval_status_counts_in_summary(self):
        runner = self._runner(expected_artifact_ids=["art-a"])
        runner.add_result(
            CaseResult(id="g-art", retrieval_status="ok")
        )
        summary = runner.summarize()
        self.assertEqual(
            summary.get("retrieval_status_counts", {}).get("ok"), 1
        )

    def test_artifact_case_from_dict_parses_fields(self):
        from tests.eval.eval_harness import _case_from_dict

        case = _case_from_dict(
            {
                "id": "x",
                "query": "?",
                "expected_artifact_ids": ["a1"],
                "expected_modalities": ["image"],
                "expected_vision_mode": "used",
            }
        )
        self.assertEqual(case.expected_artifact_ids, ["a1"])
        self.assertEqual(case.expected_modalities, ["image"])
        self.assertEqual(case.expected_vision_mode, "used")

    def test_retrieval_unavailable_excluded_from_means(self):
        """Plan F: denominators exclude `unavailable` (outage != zero recall)."""
        from tests.eval.eval_harness import GoldenCase

        runner = EvalRunner(
            [
                GoldenCase(id="ok", query="?", expected_chunk_ids=["c1"]),
                GoldenCase(id="out", query="?", expected_chunk_ids=["c1"]),
            ],
            top_k=3,
        )
        runner.add_result(
            CaseResult(id="ok", retrieved_chunk_ids=["c1"], retrieval_status="ok")
        )
        runner.add_result(
            CaseResult(id="out", retrieved_chunk_ids=[], retrieval_status="unavailable")
        )
        summary = runner.summarize()
        # The ok case alone contributes (recall 1.0) even though the outage case
        # exists with 0 retrieved — outage must not drag the mean to 0.5.
        self.assertEqual(summary["recall_at_k_mean"], 1.0)
        self.assertEqual(summary["retrieval_status_counts"].get("unavailable"), 1)

    def test_modality_match_rate(self):
        from tests.eval.eval_harness import GoldenCase

        runner = EvalRunner(
            [
                GoldenCase(
                    id="g",
                    query="?",
                    expected_modalities=["image", "chart"],
                )
            ],
            top_k=3,
        )
        runner.add_result(
            CaseResult(
                id="g",
                retrieved_artifact_ids=["a1", "a2"],
                retrieved_modalities=["image", "table"],
            )
        )
        metrics = runner.evaluate()
        # 1 of 2 expected modalities (image) observed; chart absent.
        self.assertAlmostEqual(metrics[0].modality_match_rate, 0.5)


if __name__ == "__main__":
    unittest.main()


class TestRanCountFromPresence(unittest.TestCase):
    """EVAL-003 (issue #237): ran_count derives from result presence."""

    def _runner(self):
        cases = [
            GoldenCase(id="mem-1", query="q1", expected_memories=["M1"]),
            GoldenCase(id="nomatch-1", query="q2", expect_no_match=True),
            GoldenCase(id="art-1", query="q3", expected_artifact_ids=["a9"]),
            GoldenCase(id="absent-1", query="q4", expected_chunk_ids=["c1"]),
        ]
        return EvalRunner(cases, top_k=5)

    def test_specialized_completed_cases_count(self):
        runner = self._runner()
        runner.add_result(
            CaseResult(id="mem-1", cited_memory_labels=["M1"], retrieval_status="ok")
        )
        runner.add_result(
            CaseResult(id="nomatch-1", no_match_returned=True, retrieval_status="ok")
        )
        runner.add_result(
            CaseResult(id="art-1", retrieved_artifact_ids=["a9"], retrieval_status="ok")
        )
        summary = runner.summarize()
        self.assertEqual(summary["case_count"], 4)
        self.assertEqual(summary["ran_count"], 3)
        self.assertEqual(summary["retrieval_status_counts"], {"ok": 3})
        self.assertEqual(summary["memory_recall_mean"], 1.0)
        self.assertEqual(summary["no_match_correct_rate"], 1.0)
        self.assertEqual(summary["artifact_recall_at_k_mean"], 1.0)


class TestDuplicateGoldenIdRejection(unittest.TestCase):
    """EVAL-002 class (issue #237): golden ids must be unique."""

    def test_runner_rejects_duplicate_case_ids(self):
        with self.assertRaises(ValueError) as ctx:
            EvalRunner(
                [
                    GoldenCase(id="dup", query="a"),
                    GoldenCase(id="dup", query="b"),
                ]
            )
        self.assertIn("duplicate", str(ctx.exception))

    def test_load_jsonl_rejects_duplicate_case_ids(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "golden.jsonl"
            path.write_text(
                '{"id": "x", "query": "a"}' + chr(10) + '{"id": "x", "query": "b"}' + chr(10),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as ctx:
                load_jsonl(path)
            self.assertIn("duplicate", str(ctx.exception))


class TestHonestReportingKeys(unittest.TestCase):
    """AC7 (issue #237): denominators, uncertainty, definitions."""

    def _summary(self):
        runner = EvalRunner(
            [
                GoldenCase(id="a", query="qa", expected_chunk_ids=["c1"], expected_facts=["October 15"]),
                GoldenCase(id="b", query="qb", expected_facts=["VP of Finance"]),
                GoldenCase(id="c", query="qc", expected_chunk_ids=["c9"]),
            ],
            top_k=5,
        )
        runner.add_result(
            CaseResult(id="a", retrieved_chunk_ids=["c1"], answer="It is October 15.", retrieval_status="ok")
        )
        runner.add_result(
            CaseResult(id="b", answer="The VP of Finance signed it.", retrieval_status="ok")
        )
        return runner

    def test_new_keys_present_and_existing_keys_intact(self):
        summary = self._summary().summarize()
        for key in (
            "case_count", "ran_count", "top_k", "recall_at_k_mean", "mrr_mean",
            "ndcg_at_k_mean", "citation_validity_mean", "memory_recall_mean",
            "wiki_recall_mean", "fact_coverage_mean", "unsupported_citation_total",
            "no_match_correct_rate", "retrieval_status_counts",
        ):
            self.assertIn(key, summary)
        for key in ("metric_n", "metric_skipped", "uncertainty", "metric_definitions"):
            self.assertIn(key, summary)

    def test_metric_n_matches_contributions(self):
        runner = self._summary()
        summary = runner.summarize()
        metrics = {m.id: m for m in runner.evaluate()}
        for key, n in summary["metric_n"].items():
            attr = key[:-5] if key.endswith("_mean") else "no_match_correct"
            expected = sum(1 for m in metrics.values() if getattr(m, attr) is not None)
            self.assertEqual(n, expected, key)

    def test_uncertainty_brackets_and_documents_method(self):
        summary = self._summary().summarize()
        for key, entry in summary["uncertainty"].items():
            value = summary[key]
            self.assertIsInstance(entry["method"], str)
            self.assertTrue(entry["method"])
            self.assertLessEqual(entry["low"] - 1e-12, value)
            self.assertGreaterEqual(entry["high"] + 1e-12, value)

    def test_no_definition_labels_ragas(self):
        summary = self._summary().summarize()
        for key, definition in summary["metric_definitions"].items():
            self.assertIsInstance(definition, str)
            self.assertTrue(definition.strip())
            self.assertNotIn("ragas", definition.lower())

    def test_summarize_is_deterministic(self):
        runner = self._summary()
        import json as _json

        self.assertEqual(
            _json.dumps(runner.summarize(), sort_keys=True),
            _json.dumps(runner.summarize(), sort_keys=True),
        )
