"""Tests for citation validation and repair."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.citation_validator import (
    normalize_citation_brackets,
    parse_citations,
    parse_kms_citations,
    parse_wiki_citations,
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
    """FR-004: citation confidence scoring via lexical (containment) overlap.

    SUPERSEDES the original Jaccard-based expectations: for a short claim
    sentence against a long source, Jaccard's union term (which includes
    every unrelated source token) crushes the score even for a verbatim
    quote — e.g. a 15-token sentence quoted from a 200-token source maxes
    out around 0.08 Jaccard, well below any sane confidence threshold. The
    metric is now containment: |claim_tokens ∩ source_tokens| / |claim_tokens|,
    i.e. "what fraction of the claim's own tokens are found in the source."
    All expected values below are hand-computed from the token sets.
    """

    def test_high_overlap_citation_high_confidence(self):
        # content: "The capital of France is Paris [S1]."
        # source: "Paris is the capital of France and its largest city."
        # claim_tokens (strip punctuation): {"the","capital","of","france","is","paris","s1"} (7)
        # source_tokens: {"paris","is","the","capital","of","france","and","its","largest","city"}
        # intersection = 6 ("s1" not in source) → containment = 6/7 ≈ 0.8571
        content = "The capital of France is Paris [S1]."
        source_text = "Paris is the capital of France and its largest city."
        result = score_citations(content, [source_text])
        self.assertIn("S1", result.citation_confidence)
        self.assertAlmostEqual(result.citation_confidence["S1"], 6 / 7, places=4)

    def test_low_overlap_citation_low_confidence(self):
        # content: "The sky is green [S1]."
        # source: "Python is a programming language developed in 1991."
        # claim_tokens (strip punctuation "[]."): {"the","sky","is","green","s1"} (5)
        # source_tokens: {"python","is","a","programming","language","developed","in","1991"}
        # intersection = {"is"} → containment = 1/5 = 0.2
        content = "The sky is green [S1]."
        source_text = "Python is a programming language developed in 1991."
        result = score_citations(content, [source_text])
        self.assertIn("S1", result.citation_confidence)
        self.assertAlmostEqual(result.citation_confidence["S1"], 1 / 5, places=4)

    def test_partial_overlap_medium_confidence(self):
        # content: "Python is a programming language [S1]."
        # source: "Python is a general-purpose high-level programming language."
        # Python's string.punctuation strips hyphens:
        # claim_tokens: {"python","is","a","programming","language","s1"} (6)
        # source_tokens: {"python","is","a","generalpurpose","highlevel","programming","language"}
        # intersection = 5 ("s1" not in source) → containment = 5/6 ≈ 0.8333
        content = "Python is a programming language [S1]."
        source_text = "Python is a general-purpose high-level programming language."
        result = score_citations(content, [source_text])
        self.assertIn("S1", result.citation_confidence)
        self.assertAlmostEqual(result.citation_confidence["S1"], 5 / 6, places=4)

    def test_mixed_citations_individual_scores(self):
        # S1: "Dogs are mammals [S1]." vs source_s1
        # claim_tokens: {"dogs","are","mammals","s1"} (4)
        # source_tokens: {"dogs","belong","to","the","canine","family","and","are","domesticated","mammals"}
        # intersection = {"dogs","are","mammals"} → containment = 3/4 = 0.75
        # S2: "Cats are also mammals [S2]." vs source_s2 ("The capital of France is Paris.")
        # claim_tokens: {"cats","are","also","mammals","s2"} (5)
        # source_tokens: {"the","capital","of","france","is","paris"}
        # intersection = ∅ → containment = 0.0
        content = "Dogs are mammals [S1]. Cats are also mammals [S2]."
        source_s1 = "Dogs belong to the canine family and are domesticated mammals."
        source_s2 = "The capital of France is Paris."
        result = score_citations(content, [source_s1, source_s2])
        self.assertIn("S1", result.citation_confidence)
        self.assertIn("S2", result.citation_confidence)
        self.assertAlmostEqual(result.citation_confidence["S1"], 3 / 4, places=4)
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

    SUPERSEDES the original Jaccard/0.15 expectations (see
    TestScoreCitationsConfidence for why containment replaced Jaccard).
    Sentences with no [S#] citation and containment < 0.5 (UNVERIFIABLE_THRESHOLD)
    against all sources are flagged — but only sentences with >= 5 word tokens
    once markdown list/heading/quote decoration is stripped; shorter fragments
    (e.g. a numbered-list split leaving "2.") are skipped before scoring so
    they don't masquerade as unverifiable claims.
    """

    def test_uncited_low_overlap_sentence_in_unverifiable(self):
        # Only the first sentence has >= 5 word tokens; the other three
        # ("First, parsing." etc.) are 2-word fragments and are skipped
        # before scoring regardless of overlap.
        #  "The system processes input in three steps." (7 words) vs
        #  "Python is a programming language." → intersection=∅ → containment=0.0 < 0.5
        content = (
            "The system processes input in three steps. "
            "First, parsing. Second, validation. Third, transformation."
        )
        source_texts = ["Python is a programming language."]
        result = score_citations(content, source_texts)
        self.assertEqual(
            result.unverifiable_claims,
            ("The system processes input in three steps.",),
        )

    def test_cited_sentence_not_in_unverifiable(self):
        # Cited sentences are skipped regardless of overlap score (the [S#]
        # citation itself excludes them from consideration).
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

    def test_verbatim_sentence_from_long_source_not_flagged(self):
        # A sentence copied verbatim from a long, multi-sentence source has
        # containment 1.0 (every claim token appears in the source) — under
        # the old Jaccard metric this would score far below 0.15 purely
        # because the source has many other unrelated tokens.
        source_text = (
            "Mitochondria are membrane-bound organelles found in most eukaryotic cells. "
            "They generate most of the cell's supply of adenosine triphosphate, used as "
            "a source of chemical energy. Mitochondria have a double membrane structure, "
            "and the inner membrane is folded into structures called cristae that increase "
            "the surface area available for energy production."
        )
        verbatim_sentence = (
            "Mitochondria have a double membrane structure, and the inner membrane is "
            "folded into structures called cristae that increase the surface area "
            "available for energy production."
        )
        unrelated_sentence = (
            "The stock market rallied sharply after the central bank cut interest "
            "rates unexpectedly."
        )
        content = f"{verbatim_sentence} {unrelated_sentence}"
        result = score_citations(content, [source_text])
        self.assertNotIn(verbatim_sentence, result.unverifiable_claims)
        self.assertIn(unrelated_sentence, result.unverifiable_claims)

    def test_markdown_fragments_never_flagged(self):
        # A numbered-list split fragment ("2."), a markdown header, and a
        # table row must never appear in unverifiable_claims — the naive
        # sentence splitter turns markdown structure into pseudo-claims with
        # no real content. A genuine unrelated sentence in the same content
        # IS still flagged, proving the skip is fragment-specific rather than
        # suppressing the whole feature.
        content = (
            "2. "
            "# Overview Header Section Title. "
            "| Name | Value | Total |. "
            "This unrelated claim about rocket propulsion physics has "
            "absolutely no matching source content at all."
        )
        source_texts = [
            "Gardening requires regular watering, sunlight, and healthy soil "
            "composition for best plant growth results."
        ]
        result = score_citations(content, source_texts)
        self.assertNotIn("2.", result.unverifiable_claims)
        self.assertFalse(
            any(c.startswith("#") for c in result.unverifiable_claims),
            f"header fragment leaked into unverifiable_claims: {result.unverifiable_claims}",
        )
        self.assertFalse(
            any("|" in c for c in result.unverifiable_claims),
            f"table-row fragment leaked into unverifiable_claims: {result.unverifiable_claims}",
        )
        self.assertIn(
            "This unrelated claim about rocket propulsion physics has "
            "absolutely no matching source content at all.",
            result.unverifiable_claims,
        )


class TestFullwidthCitationNormalization(unittest.TestCase):
    """Task A: fullwidth CJK bracket citations (【S1】) must be treated as [S1]."""

    def test_normalize_citation_brackets_converts_all_prefixes(self):
        content = "See 【S1】 and 【M2】 and 【W3】 and 【K4】."
        self.assertEqual(
            normalize_citation_brackets(content),
            "See [S1] and [M2] and [W3] and [K4].",
        )

    def test_normalize_citation_brackets_leaves_ascii_and_empty_unchanged(self):
        self.assertEqual(normalize_citation_brackets("Already [S1] ascii."), "Already [S1] ascii.")
        self.assertEqual(normalize_citation_brackets(""), "")

    def test_parse_citations_recognizes_fullwidth(self):
        sources, memories = parse_citations("Claim 【S1】 and 【M2】.")
        self.assertEqual(sources, ["S1"])
        self.assertEqual(memories, ["M2"])

    def test_parse_wiki_and_kms_citations_recognize_fullwidth(self):
        self.assertEqual(parse_wiki_citations("See 【W3】."), ["W3"])
        self.assertEqual(parse_kms_citations("See 【K4】."), ["K4"])

    def test_validate_and_repair_normalizes_and_flags_normalized_citations(self):
        result = validate_and_repair_citations(
            "Claim 【S1】.", source_count=1, memory_count=0
        )
        self.assertIn("[S1]", result.repaired_content)
        self.assertNotIn("【S1】", result.repaired_content)
        self.assertEqual(result.valid_citations, ("S1",))
        self.assertFalse(result.invalid_stripped)
        self.assertTrue(result.normalized_citations)

    def test_validate_and_repair_no_normalization_flag_when_already_ascii(self):
        result = validate_and_repair_citations(
            "Claim [S1].", source_count=1, memory_count=0
        )
        self.assertFalse(result.normalized_citations)

    def test_validate_and_repair_strips_invalid_fullwidth_citation(self):
        # A fullwidth citation to a non-existent source is normalized AND
        # then stripped as invalid, same as the ASCII case.
        result = validate_and_repair_citations(
            "Claim 【S99】.", source_count=1, memory_count=0
        )
        self.assertNotIn("S99", result.repaired_content)
        self.assertEqual(result.invalid_citations, ("S99",))
        self.assertTrue(result.invalid_stripped)

    def test_score_citations_recognizes_fullwidth(self):
        content = "Paris is the capital of France 【S1】."
        source_text = "Paris is the capital of France."
        result = score_citations(content, [source_text])
        self.assertIn("S1", result.citation_confidence)


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
