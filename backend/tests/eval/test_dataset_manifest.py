"""Tests for tuning/held-out dataset manifests (issue #237, AC8)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from tests.eval.dataset_manifest import (
    DatasetManifest,
    load_manifest,
    split_cases,
    validate_manifest,
)
from tests.eval.eval_harness import GoldenCase, load_jsonl

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CASES = load_jsonl(FIXTURES / "golden.jsonl")


class TestFixtureManifest(unittest.TestCase):
    def test_fixture_manifest_validates_against_golden_fixture(self):
        manifest = load_manifest(FIXTURES / "dataset_manifest.json")
        validate_manifest(manifest, CASES)

    def test_fixture_splits_are_disjoint_and_cover_all_cases(self):
        manifest = load_manifest(FIXTURES / "dataset_manifest.json")
        splits = split_cases(CASES, manifest)
        self.assertTrue(splits["tuning"])
        self.assertTrue(splits["held-out"])
        self.assertEqual(
            len(splits["tuning"]) + len(splits["held-out"]), len(CASES)
        )


class TestValidation(unittest.TestCase):
    def _cases(self):
        return [
            GoldenCase(id="a", query="qa"),
            GoldenCase(id="b", query="qb"),
        ]

    def test_overlap_rejected(self):
        manifest = DatasetManifest(
            schema_version="1", tuning_case_ids=["a", "b"], held_out_case_ids=["b"]
        )
        with self.assertRaises(ValueError) as ctx:
            validate_manifest(manifest, self._cases())
        self.assertTrue(
            "overlap" in str(ctx.exception)
            or "duplicate" in str(ctx.exception)
            or "disjoint" in str(ctx.exception)
        )

    def test_unknown_id_rejected(self):
        manifest = DatasetManifest(
            schema_version="1", tuning_case_ids=["a"], held_out_case_ids=["zzz"]
        )
        with self.assertRaises(ValueError) as ctx:
            validate_manifest(manifest, self._cases())
        self.assertIn("unknown", str(ctx.exception).lower())

    def test_empty_split_rejected(self):
        manifest = DatasetManifest(
            schema_version="1", tuning_case_ids=["a", "b"], held_out_case_ids=[]
        )
        with self.assertRaises(ValueError):
            validate_manifest(manifest, self._cases())

    def test_load_manifest_rejects_wrong_version(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "manifest.json"
            path.write_text(
                '{"schema_version": "9", "tuning_case_ids": [], "held_out_case_ids": []}',
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_manifest(path)


if __name__ == "__main__":
    unittest.main()
