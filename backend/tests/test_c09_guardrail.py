"""Guardrail census for issue #714 — per-sentence support scoring must cover
every label family supplied via the support registry, while the document
([S#]) path, the unverifiable-claims rule, and the S-scoped result fields stay
byte-identical.

RED at base, GREEN after the fix: at base ``score_citations`` accepted no
``support_texts_by_label`` argument at all (TypeError), and its valid-label
universe was derived from the document text list only.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.citation_validator import score_citations  # noqa: E402


class TestSupportRegistryGuardrail(unittest.TestCase):
    """Pins the multi-family support-scoring contract at the library seam."""

    def test_all_four_families_score_with_a_registry(self):
        content = (
            "Paris is the capital of France [S1]. "
            "The capital of France is Paris [M1]. "
            "Zebras roam the savanna [W1]. "
            "The pythagorean theorem relates triangle sides [K1]."
        )
        result = score_citations(
            content,
            ["Paris is the capital of France."],
            support_texts_by_label={
                "M1": "The capital of France is Paris",
                "W1": "Zebras roam the savanna in family groups",
                "K1": "The pythagorean theorem relates the sides of a right triangle.",
            },
        )
        conf = result.citation_confidence
        self.assertIn("S1", conf)
        self.assertIn("M1", conf)
        self.assertIn("W1", conf)
        self.assertIn("K1", conf)
        self.assertGreater(conf["M1"], 0.0)
        self.assertGreater(conf["W1"], 0.0)
        self.assertGreater(conf["K1"], 0.0)

    def test_phantom_label_never_scores(self):
        # M9 is cited in the text but has no registry entry (never offered);
        # registry membership is the validity gate, so it must NOT appear.
        result = score_citations(
            "Some claim about nothing [M9].",
            [],
            support_texts_by_label={"M1": "unrelated text"},
        )
        self.assertNotIn("M9", result.citation_confidence)
        self.assertNotIn("M1", result.citation_confidence)  # uncited in content

    def test_empty_registry_body_scores_exactly_zero(self):
        result = score_citations(
            "A claim sentence [W1].",
            [],
            support_texts_by_label={"W1": ""},
        )
        self.assertEqual(result.citation_confidence.get("W1"), 0.0)

    def test_zero_overlap_registry_label_flags_zero_not_omits(self):
        result = score_citations(
            "Zebras roam the savanna [W1].",
            [],
            support_texts_by_label={"W1": "Quantum entanglement couples particle states."},
        )
        self.assertEqual(result.citation_confidence.get("W1"), 0.0)

    def test_s_path_values_unchanged_when_registry_supplied(self):
        content = "Paris is the capital of France [S1]."
        source = "Paris is the capital of France."
        without = score_citations(content, [source])
        with_registry = score_citations(
            content,
            [source],
            support_texts_by_label={"M1": "The capital of France is Paris"},
        )
        self.assertEqual(
            without.citation_confidence.get("S1"),
            with_registry.citation_confidence.get("S1"),
        )
        # The registry label is uncited in this content, so nothing else moves.
        self.assertEqual(
            set(without.citation_confidence), set(with_registry.citation_confidence)
        )

    def test_result_fields_stay_s_scoped_while_confidence_is_multi_family(self):
        result = score_citations(
            "Paris is the capital of France [S1]. Memory says so [M1].",
            ["Paris is the capital of France."],
            support_texts_by_label={"M1": "Memory says the capital is Paris"},
        )
        # valid_citations/has_any_citation remain document-[S#]-scoped (the
        # single production caller reads only these plus the confidence dict).
        self.assertEqual(result.valid_citations, ("S1",))
        self.assertTrue(result.has_any_citation)
        self.assertIn("S1", result.citation_confidence)
        self.assertIn("M1", result.citation_confidence)

    def test_unverifiable_claims_stay_document_scoped(self):
        # An uncited low-overlap sentence must keep failing against the
        # DOCUMENT texts only; adding a registry must not change the verdict.
        content = (
            "Paris is the capital of France [S1]. "
            "Xylophones resonate at ultrasonic frequencies."
        )
        without = score_citations(content, ["Paris is the capital of France."])
        with_registry = score_citations(
            content,
            ["Paris is the capital of France."],
            support_texts_by_label={"M1": "Xylophones resonate at ultrasonic frequencies."},
        )
        self.assertEqual(
            list(without.unverifiable_claims), list(with_registry.unverifiable_claims)
        )
        self.assertEqual(len(with_registry.unverifiable_claims), 1)


if __name__ == "__main__":
    unittest.main()
