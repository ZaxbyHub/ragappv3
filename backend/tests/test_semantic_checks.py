"""Tests for deterministic semantic-preservation checks (issue #237, AC11)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.semantic_checks import semantic_fact_preserved


class TestSemanticFactPreserved(unittest.TestCase):
    def _expect(self, fact, answer, preserved, label):
        result = semantic_fact_preserved(answer=answer, fact=fact)
        self.assertEqual(
            bool(result.preserved),
            preserved,
            f"{label}: preserved={result.preserved} reasons={result.failure_reasons}",
        )
        if not preserved:
            self.assertTrue(result.failure_reasons)

    def test_literal_containment_passes(self):
        self._expect(
            "The deadline is October 15",
            "The deadline is October 15 according to the plan.",
            True,
            "literal",
        )

    def test_paraphrase_passes_without_literal_substring(self):
        self._expect(
            "The report was approved by the VP of Finance",
            "According to the archive, the VP of Finance approved the report.",
            True,
            "paraphrase",
        )

    def test_not_negation_contradiction_fails(self):
        self._expect(
            "The service supports offline mode",
            "The service does not support offline mode.",
            False,
            "not-negation",
        )

    def test_no_negation_contradiction_fails(self):
        self._expect(
            "The pipeline emits metrics",
            "The pipeline emits no metrics.",
            False,
            "no-negation",
        )

    def test_numeric_contradiction_fails(self):
        self._expect(
            "The budget is 5000 dollars",
            "The budget is 7500 dollars.",
            False,
            "number-contradiction",
        )

    def test_consistent_numbers_pass_non_literally(self):
        self._expect(
            "The budget is 5000 dollars",
            "The approved budget totals 5000 dollars.",
            True,
            "number-consistent",
        )

    def test_code_identifier_contradiction_fails(self):
        self._expect(
            "Return code ERR-4021 means retry",
            "Return code ERR-9999 means retry.",
            False,
            "code-contradiction",
        )

    def test_consistent_code_identifier_passes(self):
        # Sufficient content-word overlap AND a consistent code identifier:
        # isolation-tested apart from the overlap rule (which is covered by
        # the paraphrase fixtures).
        self._expect(
            "Return code ERR-4021 means retry",
            "Return code ERR-4021 means retry the request eventually.",
            True,
            "code-consistent",
        )

    def test_empty_fact_is_vacuously_preserved(self):
        self._expect("", "anything", True, "empty-fact")

    def test_unrelated_answer_fails_on_overlap(self):
        self._expect(
            "The deadline is October 15",
            "Penguins swim very fast in cold water.",
            False,
            "unrelated",
        )

    def test_both_negated_passes(self):
        self._expect(
            "The service does not support offline mode",
            "Offline mode is not supported by the service.",
            True,
            "both-negated",
        )


if __name__ == "__main__":
    unittest.main()
