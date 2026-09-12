"""Tests for the versioned golden-dataset schema (issue #237, AC5)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from tests.eval.eval_harness import CaseResult, GoldenCase
from tests.eval.golden_schema import (
    GOLDEN_SCHEMA_VERSION,
    dump_cases,
    dump_results,
    parse_cases,
    parse_results,
)


def _full_case() -> GoldenCase:
    return GoldenCase(
        id="case-1",
        query="What is the deadline?",
        vault_id=3,
        expected_chunk_ids=["c-1", "c-2"],
        expected_source_labels=["S1"],
        expected_facts=["October 15"],
        expected_memories=["M1"],
        expected_wiki_labels=["W1"],
        expect_no_match=False,
        expected_artifact_ids=["art-1"],
        expected_modalities=["text"],
        expected_vision_mode="either",
    )


def _full_result() -> CaseResult:
    return CaseResult(
        id="case-1",
        retrieved_chunk_ids=["c-1"],
        retrieved_source_labels=["S1"],
        answer="October 15.",
        cited_source_labels=["S1"],
        cited_memory_labels=["M1"],
        cited_wiki_labels=["W1"],
        invalid_citations=["S9"],
        no_match_returned=False,
        retrieved_artifact_ids=["art-1"],
        cited_artifact_ids=["art-1"],
        retrieved_modalities=["text"],
        retrieval_status="ok",
        vision_used_artifact_ids=["art-1"],
        vision_degraded_artifact_ids=[],
    )


class TestRoundTrip(unittest.TestCase):
    def test_cases_round_trip_preserves_every_field(self):
        cases = [_full_case(), GoldenCase(id="case-2", query="q2")]
        parsed = parse_cases(dump_cases(cases))
        self.assertEqual(parsed, cases)

    def test_results_round_trip_preserves_every_field(self):
        results = [_full_result(), CaseResult(id="case-2")]
        parsed = parse_results(dump_results(results))
        self.assertEqual(parsed, results)

    def test_every_line_carries_schema_version(self):
        text = dump_cases([_full_case()])
        for line in text.strip().splitlines():
            self.assertIn('"schema_version"', line)


class TestVersionRejection(unittest.TestCase):
    def test_unknown_schema_version_rejected(self):
        line = '{"id": "x", "query": "q", "schema_version": "999"}'
        with self.assertRaises(ValueError) as ctx:
            parse_cases(line)
        self.assertIn("schema_version", str(ctx.exception))

    def test_version_constant_is_nonempty(self):
        self.assertTrue(GOLDEN_SCHEMA_VERSION)


class TestDuplicateRejection(unittest.TestCase):
    def test_duplicate_case_ids_rejected(self):
        one = dump_cases([GoldenCase(id="x", query="a")]).strip()
        with self.assertRaises(ValueError) as ctx:
            parse_cases(one + "\n" + one)
        self.assertIn("duplicate", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
