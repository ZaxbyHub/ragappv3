"""Permanent tests for app.services.draft_quality (issue #436 §6, SPEC §13).

Pure unit tests -- no DB, no HTTP, no model calls. ``settings.draft_lint_rewrite_limit``
is patched with ``unittest.mock.patch.object`` matching the convention used across the
Draft Room test suite (see ``test_draft_pipeline.py``, ``test_draft_provider_policy.py``).
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

try:  # pragma: no cover - CI installs no lancedb; backend/conftest.py stubs it
    import lancedb  # noqa: F401
except ImportError:  # pragma: no cover
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

from app.config import settings
from app.services.draft_quality import (
    BLOCKED_BOILERPLATE,
    WaiverError,
    apply_bounded_rewrites,
    create_waiver,
    is_waiver_valid,
    mask_excluded_spans,
    run_deterministic_lint,
)

BOILERPLATE_PHRASE = "it is important to note that"


class TestBoilerplateHardFail(unittest.TestCase):
    def test_exact_boilerplate_phrase_is_a_blocker(self) -> None:
        text = f"Consider the data. {BOILERPLATE_PHRASE} results vary."
        report = run_deterministic_lint(text)
        boilerplate_findings = [
            f for f in report.findings if f.rule_id.startswith("blocked_boilerplate.")
        ]
        self.assertEqual(len(boilerplate_findings), 1)
        finding = boilerplate_findings[0]
        self.assertEqual(finding.severity, "blocker")
        self.assertEqual(
            text[finding.start : finding.end].lower(), BOILERPLATE_PHRASE
        )

    def test_every_curated_phrase_is_curated_and_hard_fails(self) -> None:
        for phrase in BLOCKED_BOILERPLATE:
            with self.subTest(phrase=phrase):
                text = f"Intro. {phrase} Outro."
                report = run_deterministic_lint(text)
                blockers = [f for f in report.findings if f.severity == "blocker"]
                self.assertTrue(
                    any(
                        text[f.start : f.end].lower() == phrase.lower()
                        for f in blockers
                    ),
                    f"phrase {phrase!r} did not hard-fail",
                )

    def test_phrase_glued_to_a_larger_word_is_not_matched(self) -> None:
        # Prefix glued: "xit is important to note that" -- the phrase's first
        # word boundary is not a real word boundary (x|i are both \w), so the
        # match must be rejected entirely.
        prefixed = f"x{BOILERPLATE_PHRASE} results vary."
        report = run_deterministic_lint(prefixed)
        self.assertEqual(
            [f for f in report.findings if f.severity == "blocker"], []
        )

        # Suffix glued: "...that" immediately followed by a word character.
        suffixed = f"Consider the data. {BOILERPLATE_PHRASE}x results vary."
        report = run_deterministic_lint(suffixed)
        self.assertEqual(
            [f for f in report.findings if f.severity == "blocker"], []
        )

        # Sanity: the same phrase with real word boundaries on both sides DOES
        # match, proving the glued cases above are genuinely exercising
        # word-boundary logic and not some unrelated masking effect.
        clean = f"Consider the data. {BOILERPLATE_PHRASE} results vary."
        report = run_deterministic_lint(clean)
        self.assertEqual(
            len([f for f in report.findings if f.severity == "blocker"]), 1
        )

    def test_matching_is_case_insensitive(self) -> None:
        text = f"Intro. {BOILERPLATE_PHRASE.upper()} outro."
        report = run_deterministic_lint(text)
        self.assertTrue(any(f.severity == "blocker" for f in report.findings))


class TestMaskingExclusions(unittest.TestCase):
    """SPEC §13.2: a boilerplate phrase inside an excluded span is NOT flagged."""

    def _assert_no_blocker(self, text: str) -> None:
        report = run_deterministic_lint(text)
        blockers = [f for f in report.findings if f.severity == "blocker"]
        self.assertEqual(blockers, [], f"unexpected blocker(s) in {text!r}: {blockers}")

    def test_phrase_inside_straight_quotes_is_excluded(self) -> None:
        self._assert_no_blocker(f'She said "{BOILERPLATE_PHRASE}" during review.')

    def test_phrase_inside_curly_quotes_is_excluded(self) -> None:
        self._assert_no_blocker(f"She said “{BOILERPLATE_PHRASE}” during review.")

    def test_phrase_inside_fenced_code_is_excluded(self) -> None:
        self._assert_no_blocker(f"Before.\n```\n{BOILERPLATE_PHRASE}\n```\nAfter.")

    def test_phrase_inside_inline_code_is_excluded(self) -> None:
        self._assert_no_blocker(f"Before `{BOILERPLATE_PHRASE}` after.")

    def test_phrase_inside_markdown_link_target_is_excluded(self) -> None:
        self._assert_no_blocker(f"See [this link]({BOILERPLATE_PHRASE}) for details.")

    def test_phrase_disguised_as_a_url_is_excluded(self) -> None:
        url_phrase = BOILERPLATE_PHRASE.replace(" ", "-")
        self._assert_no_blocker(f"Visit https://example.com/{url_phrase} today.")

    def test_phrase_inside_caller_supplied_locked_span_is_excluded(self) -> None:
        prefix = "Editorial note: "
        text = f"{prefix}{BOILERPLATE_PHRASE} remains in the approved quote."
        locked_start = len(prefix)
        locked_end = locked_start + len(BOILERPLATE_PHRASE)
        report = run_deterministic_lint(text, locked_spans=[(locked_start, locked_end)])
        blockers = [f for f in report.findings if f.severity == "blocker"]
        self.assertEqual(blockers, [])

    def test_citation_label_itself_is_masked_and_adjacent_phrase_still_detected(self) -> None:
        # SPEC §13.2 explicit coverage: "a blocked phrase that begins
        # immediately after an excluded span."
        text = f"[S1]{BOILERPLATE_PHRASE} follows the citation."
        masked = mask_excluded_spans(text)
        self.assertTrue(any(kind == "citation_label" for _, _, kind in masked.spans))
        report = run_deterministic_lint(text)
        blockers = [f for f in report.findings if f.severity == "blocker"]
        self.assertEqual(len(blockers), 1)
        self.assertEqual(text[blockers[0].start : blockers[0].end], BOILERPLATE_PHRASE)


class TestMaskingIsLengthPreserving(unittest.TestCase):
    def _assert_length_preserved(self, text: str) -> None:
        masked = mask_excluded_spans(text)
        self.assertEqual(len(masked.masked), len(text))

    def test_length_preserved_for_quotes(self) -> None:
        self._assert_length_preserved(f'"{BOILERPLATE_PHRASE}" more text.')

    def test_length_preserved_for_fenced_code(self) -> None:
        self._assert_length_preserved(f"```\n{BOILERPLATE_PHRASE}\n```\ntail")

    def test_length_preserved_for_inline_code(self) -> None:
        self._assert_length_preserved(f"`{BOILERPLATE_PHRASE}` tail")

    def test_length_preserved_for_link_target(self) -> None:
        self._assert_length_preserved(f"[x]({BOILERPLATE_PHRASE}) tail")

    def test_length_preserved_for_url(self) -> None:
        self._assert_length_preserved("Visit https://example.com/path?q=1 today.")

    def test_length_preserved_for_locked_span(self) -> None:
        text = "abcdef ghijkl"
        masked = mask_excluded_spans(text, locked_spans=[(0, 6)])
        self.assertEqual(len(masked.masked), len(text))

    def test_length_preserved_for_citation_label(self) -> None:
        self._assert_length_preserved(f"[D12]{BOILERPLATE_PHRASE} tail")

    def test_length_preserved_for_mixed_multibyte_text(self) -> None:
        self._assert_length_preserved(
            f'🎉 Café résumé 北京 "{BOILERPLATE_PHRASE}" and `{BOILERPLATE_PHRASE}` end.'
        )


class TestOffsetsSliceBackToOriginal(unittest.TestCase):
    def test_offsets_are_correct_with_multibyte_prefix(self) -> None:
        prefix = "🎉 Café résumé 北京 "
        text = f"{prefix}{BOILERPLATE_PHRASE} tail."
        report = run_deterministic_lint(text)
        blockers = [f for f in report.findings if f.severity == "blocker"]
        self.assertEqual(len(blockers), 1)
        finding = blockers[0]
        self.assertEqual(finding.start, len(prefix))
        self.assertEqual(text[finding.start : finding.end], BOILERPLATE_PHRASE)

    def test_offsets_are_correct_with_several_masked_spans_before_the_match(self) -> None:
        prefix = "🎉 Café résumé 北京 "
        text = (
            f'{prefix}`inline code` "a quoted string" '
            f"[link](https://example.com/x) [S1] "
            f"https://example.com/another/url "
            f"{BOILERPLATE_PHRASE} tail."
        )
        report = run_deterministic_lint(text)
        blockers = [f for f in report.findings if f.severity == "blocker"]
        self.assertEqual(len(blockers), 1)
        finding = blockers[0]
        self.assertEqual(text[finding.start : finding.end], BOILERPLATE_PHRASE)

    def test_offsets_correct_after_front_matter_block(self) -> None:
        text = f"---\ntitle: Doc\n---\n{BOILERPLATE_PHRASE} body."
        report = run_deterministic_lint(text)
        blockers = [f for f in report.findings if f.severity == "blocker"]
        self.assertEqual(len(blockers), 1)
        finding = blockers[0]
        self.assertEqual(text[finding.start : finding.end], BOILERPLATE_PHRASE)


class TestStraightApostropheAndInchMarksAreNotQuoteDelimiters(unittest.TestCase):
    def test_apostrophe_contractions_are_untouched(self) -> None:
        text = "It don't matter what the board's report says."
        masked = mask_excluded_spans(text)
        self.assertEqual(masked.masked, text)
        self.assertFalse(any(kind == "quote" for _, _, kind in masked.spans))

    def test_single_inch_mark_is_untouched(self) -> None:
        text = 'The panel is 15" wide and costs forty dollars.'
        masked = mask_excluded_spans(text)
        self.assertEqual(masked.masked, text)
        self.assertFalse(any(kind == "quote" for _, _, kind in masked.spans))

    def test_boilerplate_after_an_apostrophe_word_still_hard_fails(self) -> None:
        text = f"The board's decision stands. {BOILERPLATE_PHRASE} nothing changes."
        report = run_deterministic_lint(text)
        blockers = [f for f in report.findings if f.severity == "blocker"]
        self.assertEqual(len(blockers), 1)
        self.assertEqual(text[blockers[0].start : blockers[0].end], BOILERPLATE_PHRASE)


class TestAdvisoryRulesNeverBlock(unittest.TestCase):
    def test_review_vocabulary_is_advisory(self) -> None:
        text = "This robust, seamless approach will delve into the tapestry of options."
        report = run_deterministic_lint(text)
        vocab_findings = [f for f in report.findings if f.rule_id.startswith("review_vocabulary.")]
        self.assertTrue(vocab_findings, "expected at least one review_vocabulary finding")
        self.assertTrue(all(f.severity == "advisory" for f in vocab_findings))

    def test_hedging_is_advisory(self) -> None:
        text = "Arguably, it could be argued that results vary to some extent."
        report = run_deterministic_lint(text)
        hedge_findings = [
            f for f in report.findings if f.rule_id.startswith("review_vocabulary.hedging.")
        ]
        self.assertTrue(hedge_findings, "expected at least one hedging finding")
        self.assertTrue(all(f.severity == "advisory" for f in hedge_findings))

    def test_passive_voice_is_advisory(self) -> None:
        text = "The report was written by the committee. Mistakes were made throughout."
        report = run_deterministic_lint(text)
        passive_findings = [
            f for f in report.findings if f.rule_id == "readability_signal.passive_voice"
        ]
        self.assertTrue(passive_findings, "expected at least one passive-voice finding")
        self.assertTrue(all(f.severity == "advisory" for f in passive_findings))

    def test_readability_signal_is_advisory(self) -> None:
        long_sentence = (
            "This extraordinarily elaborate and needlessly verbose sentence "
            "continues onward through many clauses and qualifications in order "
            "to comfortably exceed the long-sentence word threshold that the "
            "readability heuristic in this module is configured to flag for "
            "editorial review purposes today."
        )
        report = run_deterministic_lint(long_sentence)
        long_findings = [
            f for f in report.findings if f.rule_id == "readability_signal.long_sentence"
        ]
        self.assertTrue(long_findings, "expected a long-sentence finding")
        self.assertTrue(all(f.severity == "advisory" for f in long_findings))

    def test_burstiness_uniform_sentence_length_is_advisory(self) -> None:
        # Five uniform-length sentences to trip the low-coefficient-of-
        # variation burstiness heuristic.
        sentence = "The cat sat on the mat today."
        text = " ".join([sentence] * 5)
        report = run_deterministic_lint(text)
        burst_findings = [
            f
            for f in report.findings
            if f.rule_id == "structure_signal.uniform_sentence_length"
        ]
        self.assertTrue(burst_findings, "expected a uniform-sentence-length finding")
        self.assertTrue(all(f.severity == "advisory" for f in burst_findings))

    def test_structural_repetition_repeated_openers_is_advisory(self) -> None:
        text = "However this happened. However that happened. However nothing else happened."
        report = run_deterministic_lint(text)
        structure_findings = [
            f for f in report.findings if f.rule_id == "structure_signal.repeated_openers"
        ]
        self.assertTrue(structure_findings, "expected a repeated-openers finding")
        self.assertTrue(all(f.severity == "advisory" for f in structure_findings))

    def test_no_advisory_rule_class_ever_produces_a_blocker(self) -> None:
        # Fire every advisory category in one document, plus one genuine
        # boilerplate blocker, and confirm the *only* blocker present is the
        # boilerplate one.
        text = (
            "However this is robust. However that is seamless. However nothing "
            "changes. Arguably, it seems that the tapestry of options was "
            "written by the committee. "
            f"{BOILERPLATE_PHRASE} "
            "The cat sat on the mat quietly."
        )
        report = run_deterministic_lint(text)
        blockers = [f for f in report.findings if f.severity == "blocker"]
        advisories = [f for f in report.findings if f.severity == "advisory"]
        self.assertTrue(advisories, "expected advisory findings to fire")
        self.assertTrue(
            all(f.rule_id.startswith("blocked_boilerplate.") for f in blockers)
        )
        self.assertTrue(len(blockers) >= 1)


class TestBoundedRewrites(unittest.TestCase):
    def test_rewrite_count_is_bounded_by_the_configured_limit(self) -> None:
        phrases = list(BLOCKED_BOILERPLATE)
        self.assertGreaterEqual(len(phrases), 3, "need at least 3 curated phrases for this test")
        text = ". ".join(f"{p}" for p in phrases[:3]) + "."
        with patch.object(settings, "draft_lint_rewrite_limit", 2):
            report = run_deterministic_lint(text)
            rewritten, applied = apply_bounded_rewrites(text, report)
        self.assertEqual(applied, 2)
        # Exactly one of the three curated phrases must still be present
        # verbatim (case-insensitive) in the rewritten text.
        remaining = sum(1 for p in phrases[:3] if p.lower() in rewritten.lower())
        self.assertEqual(remaining, 1)

    def test_rewrite_never_touches_advisory_spans(self) -> None:
        text = f"This robust approach will delve deep. {BOILERPLATE_PHRASE} it works."
        report = run_deterministic_lint(text)
        rewritten, applied = apply_bounded_rewrites(text, report)
        self.assertGreaterEqual(applied, 1)
        # The advisory vocabulary words are untouched by the rewrite.
        self.assertIn("robust", rewritten)
        self.assertIn("delve", rewritten)
        # The boilerplate phrase itself is gone (or altered) by the rewrite.
        self.assertNotIn(BOILERPLATE_PHRASE, rewritten.lower())

    def test_explicit_limit_argument_overrides_settings(self) -> None:
        phrases = list(BLOCKED_BOILERPLATE)
        text = ". ".join(phrases[:3]) + "."
        report = run_deterministic_lint(text)
        rewritten, applied = apply_bounded_rewrites(text, report, limit=1)
        self.assertEqual(applied, 1)

    def test_zero_limit_applies_no_rewrites(self) -> None:
        text = f"{BOILERPLATE_PHRASE} stays."
        report = run_deterministic_lint(text)
        rewritten, applied = apply_bounded_rewrites(text, report, limit=0)
        self.assertEqual(applied, 0)
        self.assertEqual(rewritten, text)


class TestWaivers(unittest.TestCase):
    def _boilerplate_finding(self, text: str):
        report = run_deterministic_lint(text, rule_version="v1")
        blockers = [f for f in report.findings if f.severity == "blocker"]
        self.assertEqual(len(blockers), 1)
        return blockers[0]

    def test_waiver_requires_actor(self) -> None:
        text = f"{BOILERPLATE_PHRASE} stands."
        finding = self._boilerplate_finding(text)
        with self.assertRaises(WaiverError):
            create_waiver(finding, text, actor="", reason="approved by editor", rule_version="v1")
        with self.assertRaises(WaiverError):
            create_waiver(finding, text, actor="   ", reason="approved by editor", rule_version="v1")

    def test_waiver_requires_reason(self) -> None:
        text = f"{BOILERPLATE_PHRASE} stands."
        finding = self._boilerplate_finding(text)
        with self.assertRaises(WaiverError):
            create_waiver(finding, text, actor="editor-1", reason="", rule_version="v1")
        with self.assertRaises(WaiverError):
            create_waiver(finding, text, actor="editor-1", reason="   ", rule_version="v1")

    def test_valid_waiver_records_actor_reason_rule_version_and_span_hash(self) -> None:
        text = f"{BOILERPLATE_PHRASE} stands."
        finding = self._boilerplate_finding(text)
        waiver = create_waiver(
            finding, text, actor="editor-1", reason="approved editorially", rule_version="v1"
        )
        self.assertEqual(waiver.actor, "editor-1")
        self.assertEqual(waiver.reason, "approved editorially")
        self.assertEqual(waiver.rule_version, "v1")
        self.assertTrue(waiver.text_sha256)
        self.assertTrue(is_waiver_valid(waiver, text, rule_version="v1"))

    def test_waiver_invalidated_when_span_text_changes(self) -> None:
        text = f"{BOILERPLATE_PHRASE} stands."
        finding = self._boilerplate_finding(text)
        waiver = create_waiver(
            finding, text, actor="editor-1", reason="approved editorially", rule_version="v1"
        )
        edited_text = (
            text[: finding.start] + "some other phrase entirely" + text[finding.end :]
        )
        self.assertFalse(is_waiver_valid(waiver, edited_text, rule_version="v1"))

    def test_waiver_invalidated_when_rule_version_changes(self) -> None:
        text = f"{BOILERPLATE_PHRASE} stands."
        finding = self._boilerplate_finding(text)
        waiver = create_waiver(
            finding, text, actor="editor-1", reason="approved editorially", rule_version="v1"
        )
        self.assertFalse(is_waiver_valid(waiver, text, rule_version="v2"))

    def test_waiver_survives_an_edit_elsewhere_in_the_document(self) -> None:
        text = f"{BOILERPLATE_PHRASE} stands."
        finding = self._boilerplate_finding(text)
        waiver = create_waiver(
            finding, text, actor="editor-1", reason="approved editorially", rule_version="v1"
        )
        edited_text = text + " An unrelated sentence was appended."
        self.assertTrue(is_waiver_valid(waiver, edited_text, rule_version="v1"))


if __name__ == "__main__":
    unittest.main()
