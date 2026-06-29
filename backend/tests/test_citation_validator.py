"""Tests for citation validation and repair."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.citation_validator import (
    parse_citations,
    repair_against_sources_and_memories,
    score_citations,
    validate_and_repair_citations,
)


class TestValidateAndRepair(unittest.TestCase):
    def test_empty(self):
        result = validate_and_repair_citations("", source_count=0, memory_count=0)
        self.assertEqual(result.repaired_content, "")
        self.assertFalse(result.has_any_citation)
        self.assertFalse(result.uncited_factual_warning)

    def test_valid_only(self):
        result = validate_and_repair_citations(
            "Claim [S1] and another [M1].", source_count=2, memory_count=2
        )
        self.assertEqual(result.repaired_content, "Claim [S1] and another [M1].")
        self.assertEqual(set(result.valid_citations), {"S1", "M1"})
        self.assertEqual(result.invalid_citations, ())
        self.assertFalse(result.invalid_stripped)

    def test_strip_invalid_source(self):
        result = validate_and_repair_citations(
            "Real claim [S99] should drop.", source_count=2, memory_count=0
        )
        self.assertNotIn("[S99]", result.repaired_content)
        self.assertEqual(result.invalid_citations, ("S99",))
        self.assertTrue(result.invalid_stripped)

    def test_strip_invalid_memory(self):
        result = validate_and_repair_citations(
            "From memory [M5].", source_count=0, memory_count=1
        )
        self.assertNotIn("[M5]", result.repaired_content)
        self.assertEqual(result.invalid_citations, ("M5",))

    def test_strip_invalid_collapses_double_space_left_behind(self):
        # The space pair left where "[S99]" sat is collapsed to one space.
        result = validate_and_repair_citations(
            "Real claim [S99] should drop.", source_count=2, memory_count=0
        )
        self.assertEqual(result.repaired_content, "Real claim should drop.")

    def test_strip_invalid_preserves_code_indentation(self):
        # An invalid citation in prose must not collapse indentation of a
        # following code block (regression for the destructive whitespace pass).
        content = "See [S99] below:\n    def foo():\n        return 1"
        result = validate_and_repair_citations(
            content, source_count=1, memory_count=0
        )
        self.assertNotIn("[S99]", result.repaired_content)
        self.assertIn("\n    def foo():", result.repaired_content)
        self.assertIn("\n        return 1", result.repaired_content)

    def test_clean_content_with_indentation_unchanged(self):
        # No invalid citations → content returned verbatim (only outer strip),
        # so indentation and intentional internal spacing are never mangled.
        content = "Here is code:\n    x = 1\n        y = 2"
        result = validate_and_repair_citations(
            content, source_count=1, memory_count=0
        )
        self.assertEqual(result.repaired_content, content)
        self.assertFalse(result.invalid_stripped)

    def test_mixed_valid_and_invalid(self):
        result = validate_and_repair_citations(
            "Good [S1] bad [S99] memory [M1] bad [M9].",
            source_count=1,
            memory_count=1,
        )
        self.assertIn("[S1]", result.repaired_content)
        self.assertIn("[M1]", result.repaired_content)
        self.assertNotIn("[S99]", result.repaired_content)
        self.assertNotIn("[M9]", result.repaired_content)
        self.assertEqual(set(result.valid_citations), {"S1", "M1"})
        self.assertEqual(set(result.invalid_citations), {"S99", "M9"})

    def test_uncited_factual_warning_when_evidence(self):
        # Long enough to look factual + has evidence + zero citations.
        long_text = (
            "The system processes input in three steps. First, parsing happens. "
            "Second, validation. Third, transformation. Each step is deterministic."
        )
        result = validate_and_repair_citations(
            long_text, source_count=2, memory_count=0
        )
        self.assertTrue(result.uncited_factual_warning)
        self.assertFalse(result.has_any_citation)

    def test_no_warning_when_no_evidence(self):
        result = validate_and_repair_citations(
            "I just made this up.", source_count=0, memory_count=0
        )
        self.assertFalse(result.uncited_factual_warning)
        self.assertFalse(result.has_evidence)

    def test_no_warning_for_refusal(self):
        result = validate_and_repair_citations(
            "The information is not available in the retrieved documents.",
            source_count=2,
            memory_count=0,
        )
        self.assertFalse(result.uncited_factual_warning)


class TestParseCitations(unittest.TestCase):
    def test_separate_namespaces(self):
        sources, memories = parse_citations("[S1] and [M1] and [S2]")
        self.assertEqual(sources, ["S1", "S2"])
        self.assertEqual(memories, ["M1"])

    def test_dedup(self):
        sources, _ = parse_citations("[S1] [S1] [S1]")
        self.assertEqual(sources, ["S1"])


class TestRepairAgainstSourcesAndMemories(unittest.TestCase):
    def test_sparse_label_indices(self):
        # Source labels S2 and S4 only — must not flag those as invalid even
        # though source_count is technically 2 if we counted naively.
        sources = [
            {"source_label": "S2", "id": "x"},
            {"source_label": "S4", "id": "y"},
        ]
        result = repair_against_sources_and_memories(
            "Use [S2] and [S4] together.", sources=sources, memories=[]
        )
        self.assertIn("[S2]", result.repaired_content)
        self.assertIn("[S4]", result.repaired_content)
        self.assertEqual(result.invalid_citations, ())

    def test_memory_label_collision_with_source(self):
        # [S1] and [M1] are NOT the same — must validate against the right
        # label space.
        sources = [{"source_label": "S1", "id": "a"}]
        memories = [{"memory_label": "M1", "id": "b"}]
        result = repair_against_sources_and_memories(
            "Doc [S1], memory [M1], invalid memory [M2].",
            sources=sources,
            memories=memories,
        )
        self.assertIn("[S1]", result.repaired_content)
        self.assertIn("[M1]", result.repaired_content)
        self.assertNotIn("[M2]", result.repaired_content)
        self.assertEqual(result.invalid_citations, ("M2",))


class TestScoreCitationsConfidence(unittest.TestCase):
    """FR-004: citation confidence scoring via lexical (Jaccard) overlap.

    Jaccard = |claim_tokens ∩ source_tokens| / |claim_tokens ∪ source_tokens|
    All expected values below are hand-computed from the token sets.
    """

    def test_high_overlap_citation_high_confidence(self):
        # content: "The capital of France is Paris [S1]."
        # source: "Paris is the capital of France and its largest city."
        # claim_tokens (strip punctuation): {"the","capital","of","france","is","paris","s1"}
        # source_tokens: {"paris","is","the","capital","of","france","and","its","largest","city"}
        # intersection = 6, union = 11 → Jaccard = 6/11 ≈ 0.5455
        content = "The capital of France is Paris [S1]."
        source_text = "Paris is the capital of France and its largest city."
        result = score_citations(content, [source_text])
        self.assertIn("S1", result.citation_confidence)
        self.assertAlmostEqual(result.citation_confidence["S1"], 6 / 11, places=4)

    def test_low_overlap_citation_low_confidence(self):
        # content: "The sky is green [S1]."
        # source: "Python is a programming language developed in 1991."
        # Python's string.punctuation strips trailing digits too:
        # claim_tokens (strip punctuation "[]1,"): {"the","sky","is","green","s"}
        # source_tokens (strip punctuation "-", trailing "1" from "1991"): {"python","is","a","programming","language","developed","in","199"}
        # intersection = {"is"} → size 1, union = 12 → Jaccard = 1/12 ≈ 0.0833
        content = "The sky is green [S1]."
        source_text = "Python is a programming language developed in 1991."
        result = score_citations(content, [source_text])
        self.assertIn("S1", result.citation_confidence)
        self.assertAlmostEqual(result.citation_confidence["S1"], 1 / 12, places=4)

    def test_partial_overlap_medium_confidence(self):
        # content: "Python is a programming language [S1]."
        # source: "Python is a general-purpose high-level programming language."
        # Python's string.punctuation strips hyphens:
        # claim_tokens: {"python","is","a","programming","language","s"}
        # source_tokens: {"python","is","a","generalpurpose","highlevel","programming","language"}
        # intersection = 5, union = 8 → Jaccard = 5/8 = 0.625
        content = "Python is a programming language [S1]."
        source_text = "Python is a general-purpose high-level programming language."
        result = score_citations(content, [source_text])
        self.assertIn("S1", result.citation_confidence)
        self.assertAlmostEqual(result.citation_confidence["S1"], 5 / 8, places=4)

    def test_mixed_citations_individual_scores(self):
        # S1: "Dogs are mammals [S1]." vs source_s1
        # claim_tokens: {"dogs","are","mammals","s1"}
        # source_tokens: {"dogs","belong","to","the","canine","family","and","are","domesticated","mammals"}
        # intersection = {"dogs","are","mammals"} → size 3, union = 11 → Jaccard = 3/11 ≈ 0.2727
        # S2: "Cats are also mammals [S2]." vs source_s2 ("The capital of France is Paris.")
        # claim_tokens: {"cats","are","also","mammals","s2"}
        # source_tokens: {"the","capital","of","france","is","paris"}
        # intersection = ∅ → Jaccard = 0.0
        content = "Dogs are mammals [S1]. Cats are also mammals [S2]."
        source_s1 = "Dogs belong to the canine family and are domesticated mammals."
        source_s2 = "The capital of France is Paris."
        result = score_citations(content, [source_s1, source_s2])
        self.assertIn("S1", result.citation_confidence)
        self.assertIn("S2", result.citation_confidence)
        self.assertAlmostEqual(result.citation_confidence["S1"], 3 / 11, places=4)
        self.assertAlmostEqual(result.citation_confidence["S2"], 0.0, places=4)

    def test_empty_content_returns_empty_confidence(self):
        result = score_citations("", ["some text"])
        self.assertEqual(result.citation_confidence, {})
        self.assertEqual(result.unverifiable_claims, ())

    def test_no_sources_returns_empty_confidence(self):
        content = "Something [S1]."
        result = score_citations(content, [])
        self.assertEqual(result.citation_confidence, {})


class TestUnverifiableClaims(unittest.TestCase):
    """FR-004: unverifiable-claims list — answer sentences with no citation and low source overlap.

    Sentences with no [S#] citation and Jaccard < 0.15 against all sources are flagged.
    Expected values hand-computed: each sentence's tokens vs "python is a programming language"
    token set {"python","is","a","programming","language"}.
    """

    def test_uncited_low_overlap_sentence_in_unverifiable(self):
        # All four sentences score Jaccard < 0.15 against the Python source:
        #  "The system processes input in three steps." → overlap={"in"} → 1/10=0.1 < 0.15
        #  "First, parsing."  → overlap={"a"} → 1/7≈0.143 < 0.15
        #  "Second, validation." → overlap=∅ → 0.0 < 0.15
        #  "Third, transformation." → overlap=∅ → 0.0 < 0.15
        content = (
            "The system processes input in three steps. "
            "First, parsing. Second, validation. Third, transformation."
        )
        source_texts = ["Python is a programming language."]
        result = score_citations(content, source_texts)
        self.assertEqual(
            result.unverifiable_claims,
            (
                "The system processes input in three steps.",
                "First, parsing.",
                "Second, validation.",
                "Third, transformation.",
            ),
        )

    def test_cited_sentence_not_in_unverifiable(self):
        # Both cited sentences score Jaccard >= 0.15 against their respective sources:
        #  "The sky is blue [S1]" vs "The sky is blue on clear days."
        #    → overlap={"the","sky","is","blue"} → 4/9≈0.444 ≥ 0.15
        #  "Dogs are mammals [S2]" vs "Dogs are domesticated mammals."
        #    → overlap={"dogs","are","mammals"} → 3/8=0.375 ≥ 0.15
        # → unverifiable_claims must be exactly empty.
        content = "The sky is blue [S1]. Dogs are mammals [S2]."
        source_s1 = "The sky is blue on clear days."
        source_s2 = "Dogs are domesticated mammals."
        result = score_citations(content, [source_s1, source_s2])
        self.assertEqual(result.unverifiable_claims, ())

    def test_cited_high_overlap_not_in_unverifiable(self):
        content = "Paris is the capital of France [S1]."
        source_text = "Paris is the capital of France."
        result = score_citations(content, [source_text])
        self.assertEqual(result.unverifiable_claims, ())

    def test_short_refusal_not_in_unverifiable(self):
        # Short refusal-style text should NOT be flagged as unverifiable.
        content = "I don't have enough information to answer that."
        source_texts = ["Some unrelated document."]
        result = score_citations(content, source_texts)
        self.assertEqual(result.unverifiable_claims, ())

    def test_no_sources_means_no_unverifiable(self):
        # When there are no sources, we can't verify anything — but we also
        # shouldn't return unverifiable claims (there is no evidence baseline).
        content = "This is a factual claim without citation."
        result = score_citations(content, [])
        # No sources → unverifiable list should be empty
        self.assertEqual(result.unverifiable_claims, ())


class TestBackwardCompatibility(unittest.TestCase):
    """ADDITIVE constraint: existing validate_and_repair_citations callers keep working."""

    def test_validate_and_repair_unchanged_fields(self):
        result = validate_and_repair_citations(
            "Claim [S1] and another [M1].",
            source_count=2,
            memory_count=2,
        )
        # Core fields are present and correct
        self.assertEqual(result.repaired_content, "Claim [S1] and another [M1].")
        self.assertEqual(set(result.valid_citations), {"S1", "M1"})
        self.assertFalse(result.invalid_stripped)
        self.assertTrue(result.has_any_citation)
        # New fields exist with defaults
        self.assertEqual(result.citation_confidence, {})
        self.assertEqual(result.unverifiable_claims, ())


if __name__ == "__main__":
    unittest.main()
