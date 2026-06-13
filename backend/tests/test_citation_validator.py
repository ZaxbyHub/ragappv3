"""Tests for citation validation and repair."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.citation_validator import (
    parse_citations,
    repair_against_sources_and_memories,
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


if __name__ == "__main__":
    unittest.main()
