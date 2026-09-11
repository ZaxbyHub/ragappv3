"""Issue #517 acceptance checks: deterministic quality/lint contracts.

Discriminating regression tests (expected RED at HEAD d9e3460) for the
draft-review findings DRAFT-017, DRAFT-018 and DRAFT-019:

* **AC8 (DRAFT-017)** — ``draft_quality`` masking only understands
  triple-backtick fences and single-backtick inline spans: a ``~~~`` fenced
  block (and 4+-backtick fences) containing a blocked boilerplate phrase is
  flagged and rewritten even though fenced code must never be touched.
  Includes the positive control (P3): the same phrase in plain prose IS
  rewritten.
* **AC9 (DRAFT-018)** — the pipeline's lint call sites never pass
  ``locked_spans``, so a boilerplate phrase inside an input's approved/locked
  span is rewritten away in the persisted revision when the same phrase
  appears unlocked elsewhere.
* **AC10 (DRAFT-019)** — ``_boilerplate_pattern`` matches across ``\\s+`` but
  ``apply_bounded_rewrites`` looks the replacement up by the raw excerpt key,
  so a line-wrapped occurrence is flagged yet never rewritten.

AC8/AC10 are pure unit tests on ``run_deterministic_lint`` +
``apply_bounded_rewrites``; AC9 drives a real compile through the shared
harness in ``test_issue517_facts_acceptance``.
"""

import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

try:  # pragma: no cover - CI installs no lancedb; backend/conftest.py stubs it
    import lancedb  # noqa: F401
except ImportError:  # pragma: no cover
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

from app.services.draft_quality import apply_bounded_rewrites, run_deterministic_lint
from tests.draft_room.test_issue517_facts_acceptance import (
    FakeModel,
    FakeRetriever,
    Issue517PipelineBase,
    draft_section_json,
    fact_json,
    happy_responses,
    no_edits_json,
    outline_json,
)

BOILERPLATE = "it is important to note that"
LANDSCAPE = "in today's rapidly evolving landscape"


def _boilerplate_findings(text: str, report):
    return [
        f
        for f in report.findings
        if f.severity == "blocker"
        and f.rule_id.startswith("blocked_boilerplate.")
    ]


# ── AC8 (DRAFT-017): tilde/multi-backtick fences must be excluded ────────────


class TestFenceMaskingCoversTildeAndMultiBacktick(unittest.TestCase):
    """Fenced code must be excluded from matching, whatever the fence style."""

    def test_tilde_fenced_block_is_not_flagged_and_not_rewritten(self):
        text = f"Before.\n~~~\n{BOILERPLATE}\n~~~\nAfter."

        print("AC8 CHECK: FAIL", flush=True)
        report = run_deterministic_lint(text)
        self.assertEqual(
            _boilerplate_findings(text, report),
            [],
            "a blocked phrase inside a ~~~ fenced code block must be excluded "
            "from boilerplate matching",
        )
        rewritten, applied = apply_bounded_rewrites(text, report)
        self.assertEqual(applied, 0)
        self.assertEqual(
            rewritten,
            text,
            "a ~~~ fenced code block must survive lint + bounded rewrite "
            "byte-identical",
        )

    def test_double_backtick_inline_span_is_not_flagged_and_not_rewritten(self):
        text = f"Before ``{BOILERPLATE}`` after."

        print("AC8 CHECK: FAIL", flush=True)
        report = run_deterministic_lint(text)
        self.assertEqual(
            _boilerplate_findings(text, report),
            [],
            "a blocked phrase inside a multi-backtick inline code span must "
            "be excluded from boilerplate matching",
        )
        rewritten, applied = apply_bounded_rewrites(text, report)
        self.assertEqual(applied, 0)
        self.assertEqual(
            rewritten,
            text,
            "a multi-backtick inline code span must survive lint + bounded "
            "rewrite byte-identical",
        )

    def test_positive_control_plain_prose_phrase_is_rewritten(self):
        """P3 preserving control: plain-prose boilerplate IS still rewritten."""
        text = f"Consider the data. {BOILERPLATE} results vary."
        report = run_deterministic_lint(text)
        self.assertEqual(len(_boilerplate_findings(text, report)), 1)
        rewritten, applied = apply_bounded_rewrites(text, report)
        self.assertGreaterEqual(applied, 1)
        self.assertNotIn(BOILERPLATE, rewritten.lower())
        self.assertIn("Consider the data.", rewritten)
        self.assertIn("results vary.", rewritten)


# ── AC10 (DRAFT-019): line-wrapped boilerplate must be rewritten too ─────────


class TestLineWrappedBoilerplateRewritten(unittest.TestCase):
    def test_wrapped_and_single_space_forms_are_both_rewritten(self):
        # The FIRST occurrence is hard line-wrapped INSIDE the phrase (the
        # break sits between "rapidly" and "evolving"); the second is the
        # plain single-space form.
        wrapped_phrase = "in today's rapidly\n evolving landscape"
        wrapped = f"Intro {wrapped_phrase} the plan continues."
        single = f"Second {LANDSCAPE} mention stands."
        text = f"{wrapped}\n{single}"
        self.assertIn(wrapped_phrase, text)
        self.assertIn(LANDSCAPE, text)

        print("AC10 CHECK: FAIL", flush=True)
        report = run_deterministic_lint(text)
        self.assertEqual(
            len(_boilerplate_findings(text, report)),
            2,
            "both the line-wrapped and single-space occurrences must be "
            "flagged (the pattern already matches across \\s+)",
        )
        rewritten, applied = apply_bounded_rewrites(text, report)
        self.assertEqual(
            applied,
            2,
            "both occurrences must be rewritten — a line-wrapped match must "
            "not survive merely because its raw excerpt is not a literal "
            "phrase-list key",
        )
        self.assertIsNone(
            re.search(
                re.escape(LANDSCAPE).replace(r"\ ", r"\s+"),
                rewritten,
                re.IGNORECASE,
            ),
            "neither the wrapped nor the single-space occurrence may remain",
        )
        # Surrounding prose is untouched.
        self.assertIn("the plan continues.", rewritten)
        self.assertIn("mention stands.", rewritten)


# ── AC9 (DRAFT-018): locked spans must protect approved text through lint ────


class TestLockedSpanSurvivesPipelineLint(Issue517PipelineBase):
    """A phrase inside an input's locked/approved span must survive compile.

    The input manuscript locks the span covering one occurrence of the
    boilerplate phrase; the draft desk emits that phrase twice — once as the
    approved span, once in plain prose. After compile, the locked occurrence
    must still be present byte-identical in the persisted revision while the
    unlocked occurrence is rewritten away.
    """

    MANUSCRIPT = (
        "Board guidance follows. The approved motto is "
        f"{LANDSCAPE} per the board. The internal review window for "
        "charter amendments is thirty days."
    )

    async def test_locked_occurrence_survives_and_unlocked_is_rewritten(self):
        manuscript = self.MANUSCRIPT
        span_start = manuscript.index(LANDSCAPE)
        span_end = span_start + len(LANDSCAPE)
        # Lock the approved span on the input, as the route's span-locking
        # endpoint would persist it.
        self.conn.execute(
            "UPDATE draft_inputs SET locked_spans_json = ? WHERE id = ?",
            (
                json.dumps([{"start": span_start, "end": span_end}]),
                self.input_id,
            ),
        )
        self.conn.commit()

        draft_markdown = (
            f"Preserved motto: {LANDSCAPE}.\n\n"
            f"Plain prose: {LANDSCAPE} appears again."
        )
        model = FakeModel(
            happy_responses(
                outline=[outline_json(must_preserve=(LANDSCAPE,))],
                draft=[draft_section_json(markdown=draft_markdown)],
                copy=[no_edits_json()],
                standards=[no_edits_json()],
                fact=[fact_json(proposition="Preserved motto")],
            )
        )
        await self._run(
            model=model,
            retriever=FakeRetriever(facet_query=manuscript),
        )

        revision = self._current_revision()
        self.assertIsNotNone(revision, "fixture: a revision must be stored")
        content = revision["content_md"]

        print("AC9 CHECK: FAIL", flush=True)
        self.assertEqual(
            content.count(LANDSCAPE),
            1,
            "the locked/approved occurrence of the boilerplate phrase must "
            "survive compile byte-identical while the unlocked occurrence is "
            "rewritten; persisted revision content was: "
            f"{content!r}",
        )

        # Unit-level contract: a candidate-relative locked span handed to the
        # lint engine excludes that span from matching (this part is expected
        # to hold already; it is pinned so the fix keeps it true).
        locked_start = draft_markdown.index(LANDSCAPE)
        locked_end = locked_start + len(LANDSCAPE)
        report = run_deterministic_lint(
            draft_markdown, locked_spans=[(locked_start, locked_end)]
        )
        offenders = [
            f
            for f in _boilerplate_findings(draft_markdown, report)
            if f.start < locked_end and f.end > locked_start
        ]
        self.assertEqual(
            offenders,
            [],
            "run_deterministic_lint must exclude a caller-supplied locked "
            "span from boilerplate matching",
        )
        self.assertEqual(
            len(_boilerplate_findings(draft_markdown, report)),
            1,
            "the unlocked occurrence outside the locked span must still be "
            "flagged",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
