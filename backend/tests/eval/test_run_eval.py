"""CLI smoke tests for the eval harness runner (issue #237; PRR-024f).

Exercises ``run_eval.main`` end to end — argument parsing, ``--manifest``
split validation, and the ``--out`` run_report envelope — and proves the
envelope is loadable by ``app.services.eval_compare.load_run_report``.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.services.eval_compare import load_run_report
from tests.eval.run_eval import main

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Two cases (one covered, one not) keep the run partial so ran_count < case_count.
RESULT_LINES = [
    json.dumps(
        {
            "id": "case-001",
            "retrieved_chunk_ids": ["chunk-deadline-1", "chunk-deadline-2"],
            "retrieved_source_labels": ["S1", "S2"],
            "answer": "The deadline is October 15 in Q4.",
            "cited_source_labels": ["S1"],
            "no_match_returned": False,
            "retrieval_status": "ok",
        }
    ),
    json.dumps(
        {
            "id": "case-005",
            "no_match_returned": True,
            "retrieval_status": "ok",
        }
    ),
]


class TestRunEvalMain(unittest.TestCase):
    def test_main_writes_loadable_report_with_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            results = tmp / "results.jsonl"
            results.write_text(
                chr(10).join(RESULT_LINES) + chr(10), encoding="utf-8"
            )
            out = tmp / "report.json"
            exit_code = main(
                [
                    "--golden",
                    str(FIXTURES / "golden.jsonl"),
                    "--results",
                    str(results),
                    "--manifest",
                    str(FIXTURES / "dataset_manifest.json"),
                    "--out",
                    str(out),
                ]
            )
            self.assertEqual(exit_code, 0)
            report = json.loads(out.read_text(encoding="utf-8"))
            # Manifest splits recorded and disjoint.
            self.assertEqual(len(report["splits"]["tuning"]), 10)
            self.assertEqual(len(report["splits"]["held-out"]), 5)
            self.assertFalse(
                set(report["splits"]["tuning"]) & set(report["splits"]["held-out"])
            )
            # Envelope loads through the comparison service.
            run_report = _load(out)
            self.assertEqual(run_report["run_report"]["run_id"], "run-eval")
            self.assertIn(
                "recall_at_k_mean", run_report["run_report"]["intervals"]
            )
            # load_run_report accepts the emitted envelope file directly.
            envelope = tmp / "envelope.json"
            envelope.write_text(
                json.dumps(report["run_report"]), encoding="utf-8"
            )
            loaded = load_run_report(envelope)
            self.assertEqual(loaded.run_id, "run-eval")

    def test_main_rejects_bad_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            results = tmp / "results.jsonl"
            results.write_text(chr(10).join(RESULT_LINES) + chr(10), encoding="utf-8")
            manifest = tmp / "bad_manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": "1",
                        "tuning_case_ids": ["case-001"],
                        "held_out_case_ids": ["case-001"],  # overlap
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                main(
                    [
                        "--golden",
                        str(FIXTURES / "golden.jsonl"),
                        "--results",
                        str(results),
                        "--manifest",
                        str(manifest),
                    ]
                )


def _load(out: Path) -> dict:
    return json.loads(out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
