"""Tests for the report contract and baseline comparison (issue #237, AC7/AC9)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests.eval.report_contract import (
    MEAN_METRIC_KEYS,
    METRIC_DEFINITIONS,
    UNCERTAINTY_METHOD,
)

ENV = {
    "code_hash": "c0ffee01",
    "model_hash": "m0d3l02",
    "config_hash": "c0nf1g03",
    "corpus_hash": "c0rpu504",
}


class TestReportContract(unittest.TestCase):
    def test_mean_metric_keys_are_frozen_and_defined(self):
        self.assertEqual(len(MEAN_METRIC_KEYS), 15)
        self.assertEqual(set(MEAN_METRIC_KEYS), set(METRIC_DEFINITIONS))
        for definition in METRIC_DEFINITIONS.values():
            self.assertTrue(definition.strip())

    def test_uncertainty_method_is_documented(self):
        self.assertTrue(UNCERTAINTY_METHOD)


class TestCompareRuns(unittest.TestCase):
    def _load(self, metrics, resources):
        from app.services.eval_compare import RunReport

        return RunReport(
            run_id="r",
            environment=dict(ENV),
            metrics=metrics,
            resources=resources,
        )

    def test_identical_runs_produce_zero_delta_everywhere(self):
        from app.services.eval_compare import compare_runs

        metrics = {"recall_at_k_mean": 0.75, "mrr_mean": 0.8}
        comparison = compare_runs(
            self._load(metrics, ["a", "b"]), self._load(metrics, ["a", "b"])
        )
        self.assertEqual(sorted(comparison.deltas), sorted(metrics))
        for delta in comparison.deltas.values():
            self.assertIsNotNone(delta.delta)
            self.assertEqual(delta.delta, 0.0)
            self.assertLessEqual(delta.baseline_low, delta.baseline)
            self.assertGreaterEqual(delta.baseline_high, delta.baseline)
            self.assertLessEqual(delta.candidate_low, delta.candidate)
            self.assertGreaterEqual(delta.candidate_high, delta.candidate)

    def test_controlled_regression_moves_only_affected_metric(self):
        from app.services.eval_compare import compare_runs

        base = {"recall_at_k_mean": 0.75, "mrr_mean": 0.8}
        regressed = dict(base, recall_at_k_mean=0.5)
        comparison = compare_runs(
            self._load(base, ["a"]), self._load(regressed, ["a"])
        )
        self.assertAlmostEqual(comparison.deltas["recall_at_k_mean"].delta, -0.25)
        self.assertEqual(comparison.deltas["mrr_mean"].delta, 0.0)

    def test_matched_resources_is_sorted_intersection(self):
        from app.services.eval_compare import compare_runs

        comparison = compare_runs(
            self._load({}, ["b", "a"]), self._load({}, ["a", "c", "b"])
        )
        self.assertEqual(comparison.matched_resources, ["a", "b"])

    def test_missing_metric_gets_none_delta_not_dropped(self):
        from app.services.eval_compare import compare_runs

        comparison = compare_runs(
            self._load({"x": 1.0}, []), self._load({}, [])
        )
        self.assertIn("x", comparison.deltas)
        self.assertIsNone(comparison.deltas["x"].delta)

    def test_load_run_report_round_trip(self):
        import json

        from app.services.eval_compare import load_run_report

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "run.json"
            payload = {
                "run_id": "run-1",
                "environment": ENV,
                "metrics": {"recall_at_k_mean": 0.5},
                "resources": ["case-1"],
                "intervals": {"recall_at_k_mean": {"low": 0.4, "high": 0.6}},
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            report = load_run_report(path)
        self.assertEqual(report.run_id, "run-1")
        self.assertEqual(report.metrics["recall_at_k_mean"], 0.5)
        self.assertEqual(report.intervals["recall_at_k_mean"]["low"], 0.4)

    def test_load_run_report_rejects_missing_identity(self):
        import json

        from app.services.eval_compare import load_run_report

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "run.json"
            payload = {
                "run_id": "run-1",
                "environment": {"code_hash": "c"},
                "metrics": {},
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_run_report(path)

    def test_environment_fingerprint_is_deterministic(self):
        from app.services.eval_compare import environment_fingerprint

        a = environment_fingerprint("rel", {"m": 1}, {"c": 2}, ["r1", "r0"])
        b = environment_fingerprint("rel", {"m": 1}, {"c": 2}, ["r0", "r1"])
        self.assertEqual(a, b)  # resource order must not matter
        c = environment_fingerprint("rel2", {"m": 1}, {"c": 2}, ["r1", "r0"])
        self.assertNotEqual(a["code_hash"], c["code_hash"])


if __name__ == "__main__":
    unittest.main()
