"""Draft Room deterministic quality/lint engine (issue #436 §4, SPEC §13).

This module is dependency-light pure Python: no model calls, no network
access, and no database access. It implements the exact contract in
``specs/draft-room/SPEC.md`` §13 ("Deterministic quality policy"):

* **Masking** (§13.2) — before any rule scans a section, excluded spans
  (quotes, code, blockquotes, Markdown citation labels/link targets, URLs,
  and caller-supplied locked spans) are replaced with same-length filler so
  every downstream offset still refers to the *original* text.
* **Rule classes** (§13.1) — exactly one class is hard-fail/blocking:
  ``blocked_boilerplate``, an exact, curated, versioned phrase list matched
  with word-boundary awareness. The other three classes —
  ``review_vocabulary``, ``structure_signal``, ``readability_signal`` — are
  always ``severity="advisory"`` and can never block compilation or Ready
  (SPEC §13.1/§13.3, issue §20: broad style vocabulary must never be a hard
  blocker, and this module must never optimize for or reference an AI
  detector).
* **Bounded rewrite** (§13.3) — at most ``settings.draft_lint_rewrite_limit``
  deterministic, targeted rewrites of *exact* ``blocked_boilerplate`` spans.
  Advisory findings are never rewritten.
* **Waivers** (§13.3) — a human owner may waive a specific
  ``blocked_boilerplate`` rule/span with a non-empty reason. The shipped
  implementation is independent SQL in ``app.api.routes.draft_room`` plus
  ``app.services.draft_store.waive_finding``; this module does not implement
  waivers.

``LintFinding`` / ``LintReport`` are defined in :mod:`app.services.draft_prompts`
and imported here, not redefined.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from app.config import settings
from app.services.draft_prompts import LintFinding, LintReport

__all__ = [
    "BOILERPLATE_RULE_VERSION",
    "BLOCKED_BOILERPLATE",
    "REVIEW_VOCABULARY",
    "MaskedText",
    "mask_excluded_spans",
    "run_deterministic_lint",
    "restore_offsets",
    "apply_bounded_rewrites",
]


# ---------------------------------------------------------------------------
# Rule version + curated phrase lists (SPEC §13.1)
# ---------------------------------------------------------------------------

#: Tracks ``settings.draft_boilerplate_rule_version`` exactly. Any change to
#: :data:`BLOCKED_BOILERPLATE` must bump ``draft_boilerplate_rule_version``
#: in ``app.config`` so stored findings/waivers correctly invalidate.
BOILERPLATE_RULE_VERSION: str = settings.draft_boilerplate_rule_version

#: Exact, curated, versioned ``blocked_boilerplate`` phrases (SPEC §13.1
#: "Initial candidates"). Matching is case-insensitive and word-boundary
#: aware (see :func:`_lint_boilerplate`). Keys are lowercase phrases; values
#: are a deterministic, grammatically-neutral replacement used by
#: :func:`apply_bounded_rewrites` (never rewriting advisory findings).
BLOCKED_BOILERPLATE: dict[str, str] = {
    "in today's rapidly evolving landscape": "",
    "it is important to note that": "",
    "in the ever-evolving world of": "in",
    "this comprehensive guide will": "this guide will",
}

#: ``review_vocabulary`` — context-sensitive words/short phrases (SPEC
#: §13.1 examples plus common LLM-cliché additions). ADVISORY ONLY: never a
#: blocker, and never used to imply model authorship or evade a detector
#: (issue §20). These are curated for *review*, not automatic rejection.
REVIEW_VOCABULARY: frozenset[str] = frozenset(
    {
        "delve",
        "tapestry",
        "leverage",
        "game-changer",
        "game changer",
        "robust",
        "seamless",
        "seamlessly",
        "embark",
        "unlock",
        "elevate",
        "testament to",
        "boasts",
        "unparalleled",
        "cutting-edge",
        "cutting edge",
        "paradigm shift",
        "holistic",
        "synergy",
        "plethora",
        "myriad",
        "foster",
        "underscore",
        "underscores",
        "in conclusion",
        "dive into",
        "navigate the",
        "ever-changing",
    }
)

#: ``review_vocabulary.hedging`` — hedging phrases that soften a claim
#: without changing its content. Advisory only (SPEC does not list hedging
#: as a distinct rule class; it is treated as a vocabulary sub-signal here).
HEDGE_PHRASES: frozenset[str] = frozenset(
    {
        "it could be argued",
        "it seems that",
        "arguably",
        "to some extent",
        "in some cases",
        "may potentially",
        "could potentially",
        "somewhat",
        "perhaps",
    }
)

#: ``structure_signal.transition_density`` vocabulary.
_TRANSITION_WORDS: frozenset[str] = frozenset(
    {
        "however",
        "moreover",
        "furthermore",
        "additionally",
        "consequently",
        "in addition",
        "on the other hand",
        "as a result",
        "therefore",
        "meanwhile",
    }
)

_LONG_SENTENCE_WORD_THRESHOLD = 30


# ---------------------------------------------------------------------------
# Masking (SPEC §13.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MaskedText:
    """Length-preserving masked view of a section's Markdown text.

    ``masked`` is exactly ``len(text)`` characters long; every excluded
    character is replaced with a single space (newlines are preserved as
    newlines so paragraph/sentence structure survives for the advisory
    structural checks). Because masking never changes length, every offset
    computed against ``masked`` already IS the correct offset into the
    original text -- there is no coordinate remapping.
    """

    masked: str
    spans: tuple[tuple[int, int, str], ...] = field(default_factory=tuple)


_FENCE_RE = re.compile(r"(?<!\\)```")
_INLINE_CODE_RE = re.compile(r"(?<!\\)`[^`\n]*(?<!\\)`")
_BLOCKQUOTE_RE = re.compile(r"^[ \t]{0,3}>.*$", re.MULTILINE)
_STRAIGHT_DOUBLE_QUOTE_RE = re.compile(r'"[^"\n]*"')
_CURLY_DOUBLE_QUOTE_RE = re.compile(r"“[^”\n]*”")
_CURLY_SINGLE_QUOTE_RE = re.compile(r"‘[^’\n]*’")
_CITATION_LABEL_RE = re.compile(r"\[(?:S|M|W|K|D)\d+\]")
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]*)\)")
_URL_RE = re.compile(r"https?://\S+")
_FRONT_MATTER_RE = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.DOTALL)


def _find_fenced_code_blocks(text: str) -> list[tuple[int, int]]:
    """Pair up non-escaped ``` markers into (start, end) fenced-block spans.

    A marker preceded by a backslash (an escaped fence, e.g. ``\\```) is
    excluded by the regex's negative lookbehind and never starts or closes a
    block, per SPEC §13.2's explicit "escaped fences" test requirement. An
    unmatched trailing fence (odd count) is left unclosed and not masked --
    conservative behavior, since we cannot tell where it was meant to end.
    """
    positions = [m.start() for m in _FENCE_RE.finditer(text)]
    spans: list[tuple[int, int]] = []
    i = 0
    while i + 1 < len(positions):
        spans.append((positions[i], positions[i + 1] + 3))
        i += 2
    return spans


def mask_excluded_spans(
    text: str, *, locked_spans: Sequence[tuple[int, int]] = ()
) -> MaskedText:
    """Replace SPEC §13.2 excluded spans with same-length filler.

    Excluded, in matching order (each pass only matches against text not
    already masked by an earlier pass, so nested/adjacent Markdown -- e.g. a
    quote inside a blockquote, or a boilerplate phrase starting immediately
    after a code span -- resolves correctly and is never double-masked):

    1. front matter (a leading ``---`` ... ``---`` block);
    2. fenced code blocks;
    3. inline code spans;
    4. Markdown blockquote lines;
    5. direct quoted strings (straight ``"``/curly ``"..."``/curly single
       ``'...'``); a bare straight apostrophe/inch mark (``'``) is
       deliberately NOT treated as a quote delimiter -- pairing on it would
       misfire on contractions ("it's", "don't") and on inch marks (``15"
       wide``), which SPEC §13.2 calls out as required test coverage;
    6. Markdown link targets (the ``(...)`` part of ``[text](target)``);
    7. bracketed citation labels (``[S1]``, ``[M2]``, ``[W3]``, ``[K4]``,
       ``[D5]``) per ``citation_validator._CITATION_RE``;
    8. bare URLs;
    9. caller-supplied ``locked_spans`` (already-approved manuscript spans
       and brief-preserved text), applied last so they always win.

    Returns:
        MaskedText: ``masked`` is the same length as ``text``; ``spans``
        lists every excluded ``(start, end, kind)`` range actually masked,
        sorted by start offset.
    """
    n = len(text)
    scratch = list(text)
    spans: list[tuple[int, int, str]] = []

    def current() -> str:
        return "".join(scratch)

    def blank(start: int, end: int) -> None:
        for i in range(start, end):
            if scratch[i] != "\n":
                scratch[i] = " "

    def add(start: int, end: int, kind: str) -> None:
        if start >= end:
            return
        spans.append((start, end, kind))
        blank(start, end)

    fm_match = _FRONT_MATTER_RE.match(current())
    if fm_match:
        add(0, fm_match.end(), "front_matter")

    for start, end in _find_fenced_code_blocks(current()):
        add(start, end, "fenced_code")

    for m in _INLINE_CODE_RE.finditer(current()):
        add(m.start(), m.end(), "inline_code")

    for m in _BLOCKQUOTE_RE.finditer(current()):
        add(m.start(), m.end(), "blockquote")

    for pattern in (
        _STRAIGHT_DOUBLE_QUOTE_RE,
        _CURLY_DOUBLE_QUOTE_RE,
        _CURLY_SINGLE_QUOTE_RE,
    ):
        for m in pattern.finditer(current()):
            add(m.start(), m.end(), "quote")

    # Markdown link targets before bare citation labels: a citation-shaped
    # link label such as "[S1](https://...)" must still have its target
    # masked even though the bracket also matches the citation-label shape.
    for m in _MD_LINK_RE.finditer(current()):
        add(m.start(1), m.end(1), "link_target")

    for m in _CITATION_LABEL_RE.finditer(current()):
        add(m.start(), m.end(), "citation_label")

    for m in _URL_RE.finditer(current()):
        add(m.start(), m.end(), "url")

    for start, end in locked_spans:
        if 0 <= start < end <= n:
            add(start, end, "locked_span")

    return MaskedText(masked=current(), spans=tuple(sorted(spans, key=lambda s: (s[0], s[1]))))


# ---------------------------------------------------------------------------
# blocked_boilerplate (hard-fail, human-waivable) — SPEC §13.1/§13.3
# ---------------------------------------------------------------------------


def _boilerplate_pattern(phrase: str) -> re.Pattern[str]:
    escaped = re.escape(phrase)
    # Collapse literal escaped spaces to \s+ so a phrase still matches across
    # incidental whitespace variation (e.g. a hard line-wrap) while every
    # other character (including the escaped apostrophe) stays literal.
    escaped = escaped.replace(r"\ ", r"\s+")
    return re.compile(r"\b" + escaped + r"\b", re.IGNORECASE)


_BOILERPLATE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (phrase, _boilerplate_pattern(phrase)) for phrase in BLOCKED_BOILERPLATE
)


def _rule_id_for_phrase(phrase: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", phrase.lower()).strip("_")
    return f"blocked_boilerplate.{slug}"


def _lint_boilerplate(masked_text: str, *, section_id: str) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for phrase, pattern in _BOILERPLATE_PATTERNS:
        for m in pattern.finditer(masked_text):
            findings.append(
                LintFinding(
                    rule_id=_rule_id_for_phrase(phrase),
                    severity="blocker",
                    disposition="open",
                    section_id=section_id,
                    start=m.start(),
                    end=m.end(),
                    excerpt=masked_text[m.start() : m.end()],
                    message="Replace the stock construction without changing the claim.",
                )
            )
    return findings


# ---------------------------------------------------------------------------
# review_vocabulary (advisory only) — SPEC §13.1
# ---------------------------------------------------------------------------


def _lint_review_vocabulary(masked_text: str, *, section_id: str) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for word in sorted(REVIEW_VOCABULARY):
        pattern = re.compile(r"\b" + re.escape(word).replace(r"\ ", r"\s+") + r"\b", re.IGNORECASE)
        for m in pattern.finditer(masked_text):
            findings.append(
                LintFinding(
                    rule_id=f"review_vocabulary.{word.replace(' ', '_')}",
                    severity="advisory",
                    disposition="open",
                    section_id=section_id,
                    start=m.start(),
                    end=m.end(),
                    excerpt=masked_text[m.start() : m.end()],
                    message=(
                        "Context-sensitive word/phrase flagged for editorial review; "
                        "not a hard rule and never auto-blocked."
                    ),
                )
            )
    for phrase in sorted(HEDGE_PHRASES):
        pattern = re.compile(r"\b" + re.escape(phrase).replace(r"\ ", r"\s+") + r"\b", re.IGNORECASE)
        for m in pattern.finditer(masked_text):
            findings.append(
                LintFinding(
                    rule_id=f"review_vocabulary.hedging.{phrase.replace(' ', '_')}",
                    severity="advisory",
                    disposition="open",
                    section_id=section_id,
                    start=m.start(),
                    end=m.end(),
                    excerpt=masked_text[m.start() : m.end()],
                    message="Hedging language flagged for editorial review.",
                )
            )
    return findings


# ---------------------------------------------------------------------------
# Sentence splitting + shared text stats helpers
# ---------------------------------------------------------------------------

_SENTENCE_SPLIT_RE = re.compile(r"[^.!?\n]+(?:[.!?]+|\n|$)")
_WORD_RE = re.compile(r"[A-Za-z']+")
_VOWEL_GROUP_RE = re.compile(r"[aeiouy]+", re.IGNORECASE)
_PASSIVE_RE = re.compile(
    r"\b(?:is|are|was|were|be|been|being)\s+(?:\w+ly\s+)?([a-z]+ed|[a-z]+en)\b",
    re.IGNORECASE,
)


def _iter_sentences(text: str) -> list[tuple[int, int, str]]:
    """Return ``(start, end, sentence_text)`` for each sentence in ``text``.

    A lightweight regex splitter (not a full NLP sentence tokenizer) is
    sufficient here: these checks are advisory signals, not exact grammar.
    """
    sentences = []
    for m in _SENTENCE_SPLIT_RE.finditer(text):
        raw = m.group()
        stripped = raw.strip()
        if not stripped:
            continue
        start = m.start() + (len(raw) - len(raw.lstrip()))
        end = start + len(stripped)
        sentences.append((start, end, stripped))
    return sentences


def _count_syllables(word: str) -> int:
    groups = _VOWEL_GROUP_RE.findall(word)
    count = len(groups)
    if word.lower().endswith("e") and count > 1:
        count -= 1
    return max(count, 1)


def _flesch_reading_ease(text: str) -> float | None:
    sentences = [s for _, _, s in _iter_sentences(text) if s.strip()]
    words = _WORD_RE.findall(text)
    if not sentences or not words:
        return None
    syllables = sum(_count_syllables(w) for w in words)
    words_per_sentence = len(words) / len(sentences)
    syllables_per_word = syllables / len(words)
    return 206.835 - 1.015 * words_per_sentence - 84.6 * syllables_per_word


# ---------------------------------------------------------------------------
# structure_signal (advisory only) — SPEC §13.1
# ---------------------------------------------------------------------------


def _lint_structure_signal(masked_text: str, *, section_id: str) -> list[LintFinding]:
    findings: list[LintFinding] = []
    sentences = _iter_sentences(masked_text)

    # Repeated openers: same first word (case-insensitive) starting 3+
    # sentences.
    openers: dict[str, list[tuple[int, int, str]]] = {}
    for start, end, sentence in sentences:
        first_word_match = _WORD_RE.search(sentence)
        if not first_word_match:
            continue
        key = first_word_match.group().lower()
        openers.setdefault(key, []).append((start, end, sentence))
    for key, occurrences in openers.items():
        if len(occurrences) >= 3:
            start, end, sentence = occurrences[-1]
            findings.append(
                LintFinding(
                    rule_id="structure_signal.repeated_openers",
                    severity="advisory",
                    disposition="open",
                    section_id=section_id,
                    start=start,
                    end=end,
                    excerpt=sentence[:80],
                    message=(
                        f"{len(occurrences)} sentences open with '{key}'; "
                        "consider varying sentence openings."
                    ),
                )
            )

    # Triad overuse: "X, Y, and Z" list constructions.
    triad_matches = list(
        re.finditer(r"\b\w+,\s+\w+,?\s+and\s+\w+\b", masked_text, re.IGNORECASE)
    )
    if len(triad_matches) >= 3:
        last = triad_matches[-1]
        findings.append(
            LintFinding(
                rule_id="structure_signal.triad_overuse",
                severity="advisory",
                disposition="open",
                section_id=section_id,
                start=last.start(),
                end=last.end(),
                excerpt=masked_text[last.start() : last.end()],
                message=(
                    f"{len(triad_matches)} three-item list constructions found; "
                    "consider varying sentence structure."
                ),
            )
        )

    # Uniform sentence length / burstiness: low coefficient of variation
    # across sentence word counts signals mechanical rhythm.
    lengths = [len(_WORD_RE.findall(s)) for _, _, s in sentences if _WORD_RE.findall(s)]
    if len(lengths) >= 5:
        mean = sum(lengths) / len(lengths)
        variance = sum((n - mean) ** 2 for n in lengths) / len(lengths)
        stdev = variance**0.5
        coefficient_of_variation = (stdev / mean) if mean else 0.0
        if mean > 0 and coefficient_of_variation < 0.15:
            start, end, _ = sentences[0]
            findings.append(
                LintFinding(
                    rule_id="structure_signal.uniform_sentence_length",
                    severity="advisory",
                    disposition="open",
                    section_id=section_id,
                    start=start,
                    end=end,
                    excerpt=f"avg {mean:.1f} words/sentence, cv={coefficient_of_variation:.2f}",
                    message=(
                        "Sentence lengths are unusually uniform (low burstiness); "
                        "consider varying sentence length."
                    ),
                )
            )

    # Transition density: overuse of formal transition words/phrases.
    transition_matches = []
    for phrase in _TRANSITION_WORDS:
        pattern = re.compile(r"\b" + re.escape(phrase).replace(r"\ ", r"\s+") + r"\b", re.IGNORECASE)
        transition_matches.extend(pattern.finditer(masked_text))
    word_count = len(_WORD_RE.findall(masked_text))
    if word_count > 0 and len(transition_matches) / max(word_count, 1) > 0.02 and transition_matches:
        last = max(transition_matches, key=lambda m: m.start())
        findings.append(
            LintFinding(
                rule_id="structure_signal.transition_density",
                severity="advisory",
                disposition="open",
                section_id=section_id,
                start=last.start(),
                end=last.end(),
                excerpt=masked_text[last.start() : last.end()],
                message=(
                    f"{len(transition_matches)} formal transition words in "
                    f"{word_count} words; consider more natural connective language."
                ),
            )
        )

    return findings


# ---------------------------------------------------------------------------
# readability_signal (advisory only) — SPEC §13.1
# ---------------------------------------------------------------------------


def _lint_readability_signal(masked_text: str, *, section_id: str) -> list[LintFinding]:
    findings: list[LintFinding] = []
    sentences = _iter_sentences(masked_text)

    for start, end, sentence in sentences:
        for m in _PASSIVE_RE.finditer(sentence):
            findings.append(
                LintFinding(
                    rule_id="readability_signal.passive_voice",
                    severity="advisory",
                    disposition="open",
                    section_id=section_id,
                    start=start + m.start(),
                    end=start + m.end(),
                    excerpt=sentence[m.start() : m.end()],
                    message="Passive-voice construction; consider an active rewrite.",
                )
            )
        word_count = len(_WORD_RE.findall(sentence))
        if word_count > _LONG_SENTENCE_WORD_THRESHOLD:
            findings.append(
                LintFinding(
                    rule_id="readability_signal.long_sentence",
                    severity="advisory",
                    disposition="open",
                    section_id=section_id,
                    start=start,
                    end=end,
                    excerpt=sentence[:80],
                    message=f"Sentence is {word_count} words; consider splitting it.",
                )
            )

    flesch = _flesch_reading_ease(masked_text)
    if flesch is not None and flesch < 30:
        findings.append(
            LintFinding(
                rule_id="readability_signal.flesch_score",
                severity="advisory",
                disposition="open",
                section_id=section_id,
                start=0,
                end=0,
                excerpt=f"Flesch reading ease: {flesch:.1f} (very difficult)",
                message="Text scores as very difficult to read; consider simplifying.",
            )
        )

    return findings


# ---------------------------------------------------------------------------
# Entry point + offset restoration
# ---------------------------------------------------------------------------


def run_deterministic_lint(
    text: str,
    *,
    locked_spans: Sequence[tuple[int, int]] = (),
    rule_version: str = BOILERPLATE_RULE_VERSION,
    section_id: str = "",
) -> LintReport:
    """Run the full SPEC §13 deterministic lint contract over ``text``.

    Masks excluded spans first (§13.2), then runs all four rule classes
    against the masked text. Because :func:`mask_excluded_spans` is
    length-preserving, every offset produced against the masked text is
    already the correct offset into the *original*, unmasked ``text`` --
    :func:`restore_offsets` is applied before returning as the final,
    defensive reconciliation pass SPEC treats as mandatory, so findings are
    never accidentally emitted with masked-relative coordinates.

    Only ``blocked_boilerplate`` findings ever carry ``severity="blocker"``;
    every other rule class is ``severity="advisory"`` and can never block
    compilation or Ready (SPEC §13.1/§13.3).

    Args:
        text: the exact section Markdown to lint.
        locked_spans: caller-supplied ``(start, end)`` ranges (already
            approved manuscript spans, brief-preserved text) to exclude
            from matching, same as :func:`mask_excluded_spans`.
        rule_version: the ``blocked_boilerplate`` rule-list version recorded
            on every finding's report; defaults to the module's current
            :data:`BOILERPLATE_RULE_VERSION`, which tracks
            ``settings.draft_boilerplate_rule_version``.
        section_id: the section identifier to stamp on every finding. This
            module lints one text span at a time and has no notion of
            sections itself; callers that lint whole documents section by
            section should pass the real section id here (or ``model_copy``
            the resulting findings with a section id if lint runs once over
            an already-concatenated document).

    Returns:
        LintReport: ``rule_version`` plus every finding across all four
        rule classes, in section/document order.
    """
    masked = mask_excluded_spans(text, locked_spans=locked_spans)
    findings: list[LintFinding] = []
    findings.extend(_lint_boilerplate(masked.masked, section_id=section_id))
    findings.extend(_lint_review_vocabulary(masked.masked, section_id=section_id))
    findings.extend(_lint_structure_signal(masked.masked, section_id=section_id))
    findings.extend(_lint_readability_signal(masked.masked, section_id=section_id))
    findings.sort(key=lambda f: (f.start, f.end))
    report = LintReport(rule_version=rule_version, findings=findings)
    return restore_offsets(report, masked)


def restore_offsets(report: LintReport, masked: MaskedText) -> LintReport:
    """Reconcile a lint report's offsets/excerpts against the original text.

    Every rule in this module scans ``masked.masked``, which is guaranteed
    (by construction) to be exactly ``len(original_text)`` characters, so a
    finding's ``start``/``end`` are already valid offsets into the original
    text -- no coordinate translation is required. This function still
    performs the final reconciliation pass explicitly (rather than trusting
    that invariant implicitly) so that:

    * the excerpt stored on each finding is always re-sliced from
      ``masked.masked`` (never from any earlier intermediate buffer), and
    * a finding whose span was NOT already excluded by masking is
      guaranteed to expose true original characters, since matches can only
      occur outside excluded spans (they are blanked to non-matching
      filler) -- so re-slicing here can never surface a masked filler
      character in an excerpt.

    Args:
        report: a report produced by scanning ``masked.masked``.
        masked: the exact :class:`MaskedText` that was scanned.

    Returns:
        LintReport: a new report (findings are re-built, not mutated) with
        offsets unchanged and excerpts re-sliced from ``masked.masked``.
    """
    restored: list[LintFinding] = []
    for finding in report.findings:
        start = max(0, min(finding.start, len(masked.masked)))
        end = max(start, min(finding.end, len(masked.masked)))
        excerpt = masked.masked[start:end] if end > start else finding.excerpt
        restored.append(finding.model_copy(update={"start": start, "end": end, "excerpt": excerpt}))
    return LintReport(rule_version=report.rule_version, findings=restored)


# ---------------------------------------------------------------------------
# Bounded automated rewrite (SPEC §13.3: "at most two targeted rewrites")
# ---------------------------------------------------------------------------


def _rewrite_replacement(excerpt: str, replacement: str) -> str:
    if replacement and excerpt[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def apply_bounded_rewrites(
    text: str,
    report: LintReport,
    *,
    limit: int | None = None,
) -> tuple[str, int]:
    """Apply at most ``limit`` deterministic rewrites of exact boilerplate.

    Only findings with ``severity="blocker"``, ``disposition="open"``, and a
    ``rule_id`` starting with ``"blocked_boilerplate."`` are eligible --
    this function never touches an advisory finding. Eligible findings are
    rewritten lowest-offset-first up to ``limit``, applied to the text from
    the end of the string backward so earlier offsets stay valid as later
    ones shift. A finding whose recorded ``excerpt`` no longer matches the
    live text at its span (the text changed since the finding was computed)
    is skipped rather than applied, to avoid corrupting unrelated content.

    Args:
        text: the exact text the report's offsets refer to.
        report: a :class:`LintReport` (e.g. from :func:`run_deterministic_lint`).
        limit: maximum rewrites to apply; defaults to
            ``settings.draft_lint_rewrite_limit``.

    Returns:
        tuple[str, int]: the rewritten text, and the number of rewrites
        actually applied (``<= limit``).
    """
    if limit is None:
        limit = settings.draft_lint_rewrite_limit

    eligible = [
        f
        for f in report.findings
        if f.severity == "blocker"
        and f.disposition == "open"
        and f.rule_id.startswith("blocked_boilerplate.")
    ]
    eligible.sort(key=lambda f: f.start)
    eligible = eligible[: max(limit, 0)]

    new_text = text
    applied = 0
    for finding in sorted(eligible, key=lambda f: f.start, reverse=True):
        if new_text[finding.start : finding.end] != finding.excerpt:
            continue
        phrase_key = finding.excerpt.strip().lower()
        replacement_template = BLOCKED_BOILERPLATE.get(phrase_key)
        if replacement_template is None:
            continue
        replacement = _rewrite_replacement(finding.excerpt, replacement_template)
        new_text = new_text[: finding.start] + replacement + new_text[finding.end :]
        applied += 1
    return new_text, applied
