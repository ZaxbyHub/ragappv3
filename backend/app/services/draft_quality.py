"""Draft Room deterministic quality/lint engine (issue #436 §4, SPEC §13).

This module is dependency-light pure Python: no model calls, no network
access, and no database access. It implements the exact contract in
``specs/draft-room/SPEC.md`` §13 ("Deterministic quality policy"):

* **Masking** (§13.2) — before any rule scans a section, excluded spans
  (quotes, code, blockquotes, Markdown citation labels/link targets, URLs,
  and caller-supplied locked spans) are replaced with same-length filler so
  every downstream offset still refers to the *original* text.
* **Rule classes** (§13.1, amended by issue #1150) — two blocker families.
  ``blocked_boilerplate`` is an exact, curated, versioned phrase list matched
  with word-boundary awareness (versioned by
  ``settings.draft_boilerplate_rule_version`` as before). The deterministic
  quality-gate family — total and per-section length tolerance, paragraph
  duplication (hash and similarity), cross-section duplicate sentences,
  heading structure, process-commentary leakage, and a trailing source line —
  emits ``severity="blocker"`` findings versioned by
  ``settings.draft_quality_rule_version`` (recorded per finding so waivers
  stay bound to the rule set they were granted against). The advisory classes
  — ``review_vocabulary``, ``structure_signal``, ``readability_signal`` and
  the length near-band warning — can never block compilation or Ready (SPEC
  §13.1/§13.3, issue §20: broad style vocabulary must never be a hard
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
from typing import Optional, Sequence

from app.config import settings
from app.services.draft_prompts import LintFinding, LintReport

__all__ = [
    "BOILERPLATE_RULE_VERSION",
    "QUALITY_GATES_RULE_VERSION",
    "BLOCKED_BOILERPLATE",
    "REVIEW_VOCABULARY",
    "SectionInput",
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


_FENCE_MARKER_RE = re.compile(r"[ \t]{0,3}(`{3,}|~{3,})(.*)$")
_BLOCKQUOTE_RE = re.compile(r"^[ \t]{0,3}>.*$", re.MULTILINE)
_STRAIGHT_DOUBLE_QUOTE_RE = re.compile(r'"[^"\n]*"')
_CURLY_DOUBLE_QUOTE_RE = re.compile(r"“[^”\n]*”")
_CURLY_SINGLE_QUOTE_RE = re.compile(r"‘[^’\n]*’")
_CITATION_LABEL_RE = re.compile(r"\[(?:S|M|W|K|D)\d+\]")
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]*)\)")
_URL_RE = re.compile(r"https?://\S+")
_FRONT_MATTER_RE = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.DOTALL)


def _find_fenced_code_blocks(text: str) -> list[tuple[int, int]]:
    """Pair CommonMark fenced-code markers into (start, end) block spans.

    An opener is a line with at most three leading spaces followed by a run
    of 3+ backticks or 3+ tildes (plus an info string, which for backtick
    fences must not itself contain a backtick — CommonMark §4.4). The closer
    is a line containing only a run of the same marker character at least as
    long as the opener. A marker preceded by a backslash (an escaped fence,
    e.g. ``\\```) never opens or closes a block, per SPEC §13.2's explicit
    "escaped fences" test requirement. An unmatched opener is left unclosed
    and not masked — conservative behavior, since we cannot tell where it
    was meant to end.
    """
    spans: list[tuple[int, int]] = []
    opener: Optional[tuple[str, int, int]] = None  # (marker char, length, offset)
    offset = 0
    for raw_line in text.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        line_start = offset
        offset += len(raw_line)
        if opener is not None:
            stripped = line.lstrip(" \t")
            # CommonMark §4.4: a closing fence may be followed only by spaces
            # or tabs, so the marker must be compared with trailing whitespace
            # stripped (the opener side already tolerates it via the info
            # string).
            closer = stripped.rstrip(" \t")
            indent = len(line) - len(stripped)
            if (
                indent <= 3
                and closer
                and set(closer) == {opener[0]}
                and len(closer) >= opener[1]
                and not closer.startswith("\\")
            ):
                spans.append((opener[2], line_start + len(line)))
                opener = None
            continue
        match = _FENCE_MARKER_RE.match(line)
        if not match:
            continue
        marker = match.group(1)
        if marker.startswith("\\"):
            continue
        char, length = marker[0], len(marker)
        info = match.group(2)
        if char == "`" and "`" in info:
            # CommonMark §4.4: a backtick fence's info string may not contain
            # a backtick, so a prose line like "use ```x``` here" is not an
            # opener (the inline scanner still masks its code spans).
            continue
        opener = (char, length, line_start)
    return spans


def _find_inline_code_spans(text: str) -> list[tuple[int, int]]:
    """Find single-line inline code spans delimited by equal-length runs.

    A span opens at a backtick run of length ``n >= 1`` not preceded by a
    backslash and closes at the next run of exactly ``n`` backticks on the
    same line (CommonMark §6.1, conservative: never crossing a newline, so
    an unmatched run stays literal prose exactly as before).
    """
    spans: list[tuple[int, int]] = []
    line_start = 0
    for raw_line in text.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        i = 0
        while i < len(line):
            if line[i] != "`" or (i > 0 and line[i - 1] == "\\"):
                i += 1
                continue
            run_start = i
            while i < len(line) and line[i] == "`":
                i += 1
            run_len = i - run_start
            closer = line.find("`" * run_len, i)
            if closer == -1:
                continue
            spans.append((line_start + run_start, line_start + closer + run_len))
            i = closer + run_len
        line_start += len(raw_line)
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

    for start, end in _find_inline_code_spans(current()):
        add(start, end, "inline_code")

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
# Deterministic quality gates (issue #1150, SPEC §13.1 as amended)
# ---------------------------------------------------------------------------

#: Version of the gate rule set (length/duplication/heading/process rules).
#: Tracks ``settings.draft_quality_rule_version`` exactly; a change to a
#: gate rule below should bump that setting so NEW findings carry the new
#: identity. (Waivers bind to the version stored on their finding row;
#: nothing enforces the bump at runtime.)
QUALITY_GATES_RULE_VERSION: str = settings.draft_quality_rule_version

#: Relative total-length bands: a blocker outside +/-25 percent of the brief's
#: target, an advisory warning outside +/-15 percent (only when the blocker
#: did not fire; exactly-at-band always passes).
_LENGTH_BLOCKER_BAND = 0.25
_LENGTH_NEAR_BAND = 0.15

#: A section blocks only above max(1.5 x budget, budget + floor words); the
#: absolute floor keeps tiny sections from tripping the relative arm alone.
_SECTION_OVER_BUDGET_RATIO = 1.5

#: Paragraphs shorter than this (word basis) are exempt from the similarity
#: rule: near-identical short paragraphs are usually legitimate constructions,
#: and word-set Jaccard is unstable on small sets.
_SIMILARITY_MIN_WORDS = 12

#: Word-set Jaccard at or above this counts as a duplicate paragraph.
_SIMILARITY_THRESHOLD = 0.9

#: A sentence of this many words or more appearing in more than one section
#: is a cross-section duplicate.
_CROSS_SECTION_MIN_WORDS = 8

#: Degenerate-output caps for the duplicate rules (issue #1150 feedback):
#: at most this many paragraphs enter the pairwise similarity comparison,
#: and each duplicate rule emits at most this many findings (document
#: order, deterministic). Repetition-loop model output otherwise produced
#: hundreds of thousands of blockers in one lint pass.
_MAX_SIMILARITY_PARAGRAPHS = 400
_MAX_DUPLICATE_FINDINGS = 100

#: Curated, versioned pipeline-instruction leakage patterns (QUALITY_GATES_
#: RULE_VERSION). The 2026-11-15 deployment audit watched outline acceptance
#: checks and verification instructions come back as prose ("verification was
#: not performed" statements); these patterns catch that transcription class.
PROCESS_COMMENTARY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (phrase, re.compile(pattern, re.IGNORECASE))
    for phrase, pattern in (
        ("verification_was_not_performed", r"verification (?:was|is) not performed"),
        # Anchored to pipeline vocabulary: a bare "acceptance check" blocked
        # ordinary QA prose ("Each release passes an acceptance check before
        # it ships"), so the pattern must name the draft's own checks.
        (
            "acceptance_check",
            r"acceptance checks? (?:for|of) th(?:is|e) "
            r"(?:section|draft|outline|piece)",
        ),
        # Self-reference required: "As an AI researcher, she ..." is prose.
        ("as_an_ai", r"\bI(?:'m| am) an? AI\b|\b(?:as|being) an AI\b[,.]?(?:\s+I\b)"),
        # Anchored to pipeline artifacts (the acceptance_check noun family):
        # a bare "I cannot verify ..." blocked ordinary first-person prose
        # ("I could not verify the figures with the registrar").
        (
            "cannot_verify",
            r"I (?:cannot|can't|could not|couldn't) verify "
            r"(?:this|that|the) (?:section|draft|outline|piece)\b",
        ),
        (
            "no_access",
            r"I (?:don't|do not) have access to (?:this|that|the) "
            r"(?:section|draft|outline|piece)\b",
        ),
        ("outline_instructs", r"the outline (?:instructs|says|requires)"),
        ("per_the_brief", r"per the brief's instructions"),
        ("not_fact_checked", r"was not fact[- ]checked"),
        ("evidence_labels_placeholder", r"evidence_labels\[\]"),
        ("must_preserve_placeholder", r"must_preserve\[\]"),
    )
)

#: The raw final non-empty line is a trailing source attribution when it
#: matches this shape (per-element bracket handling: `Source: [D1]`,
#: `Source: D12`, `Source: [S1], [W2].`). Checked on the RAW line because
#: citation masking blanks the label text inside it.
_TRAILING_SOURCE_RE = re.compile(
    r"^Source:\s*(?:\[?[A-Za-z][0-9]{0,3}\]?)"
    r"(?:\s*,\s*\[?[A-Za-z][0-9]{0,3}\]?)*\s*\.?$"
)

_ATX_HEADING_RE = re.compile(r"^#{1,6} .*$", re.MULTILINE)
_H1_RE = re.compile(r"^# .*$", re.MULTILINE)
# Unicode letters and digits (any script), joined on apostrophes so
# contractions ("don't") count as one word. Digits are kept deliberately:
# dropping them made paragraphs differing only in a number read as
# duplicates, and a digits-only token such as "2021" still counts toward
# the length rules (issue #1150 feedback round: ASCII-only tokenizer
# silently disabled every word-based rule for non-Latin drafts).
_WORD_RE = re.compile(r"[^\W_']+(?:['\u2019][^\W_']+)*", re.UNICODE)


@dataclass(frozen=True)
class SectionInput:
    """One outline section's deterministic identity for the gate rules.

    ``section_id``/``heading`` come from the outline entry and
    ``budget_words`` is the entry's ``target_words``. Candidate heading
    blocks map to these descriptors IN ORDER (heading text is a tiebreak
    when the desks reworded or duplicated a heading); a candidate with no
    ATX headings disables the section-scoped rules entirely.
    """

    section_id: str
    heading: str
    budget_words: int


def _count_words(text: str) -> int:
    """Length-rule word basis: letter/apostrophe runs over masked text.

    Shares the standard exclusion masking (code, quotes, citations, front
    matter) so markup never counts as prose.
    """
    return len(_WORD_RE.findall(mask_excluded_spans(text).masked))


def _normalize_words(text: str) -> str:
    """Case-folded, whitespace-collapsed, punctuation-stripped match key."""
    return " ".join(word.casefold() for word in _WORD_RE.findall(text))


@dataclass(frozen=True)
class _TextBlock:
    start: int
    end: int
    text: str


def _split_blocks(masked_text: str) -> list[_TextBlock]:
    """Non-empty blank-line-separated blocks with original offsets."""
    blocks: list[_TextBlock] = []
    for match in re.finditer(r"[^\n\s][^\n]*(?:\n(?!\s*\n)[^\n]*)*", masked_text):
        raw = match.group(0)
        if raw.strip():
            blocks.append(_TextBlock(match.start(), match.end(), raw))
    return blocks


def _mask_texts_everywhere(text: str, texts: Sequence[str]) -> str:
    """Blank EVERY occurrence of each protected text, same length.

    SPEC §13.1 exempts must-preserve spans at ALL occurrences from the
    duplicate rules, so the duplicate view is built from a locked-free
    masked base and then blanks every occurrence of each protected text
    (exact and whitespace-normalized matching).
    """
    if not texts:
        return text
    chars = list(text)
    for protected in texts:
        if not (protected and protected.strip()):
            continue
        exact = protected.strip()
        pattern = re.compile(
            r"\s+".join(re.escape(part) for part in exact.split())
        )
        for match in pattern.finditer(text):
            for i in range(match.start(), match.end()):
                chars[i] = " "
    return "".join(chars)


def _section_blocks(
    masked_text: str, sections: Sequence[SectionInput]
) -> list[tuple[SectionInput, _TextBlock]]:
    """Map heading-delimited blocks to section inputs.

    Mapping is by HEADING ORDINAL — the index over heading blocks only,
    never the index over all blocks (a body paragraph between headings
    would otherwise shift every later section onto the wrong budget).
    Boundaries are discovered from ATX heading LINES, not blank-line
    blocks: valid Markdown does not require a blank line before a heading,
    and a heading glued to the preceding body must still start its own
    section (issue #1150 feedback round 18). Heading text is matched first
    so a document-level H1 title or sub-headings do not consume a section
    slot; when no heading text matches, the first ``len(sections)``
    heading lines map in order.
    """
    if not sections:
        return []
    heading_spans: list[tuple[int, str]] = [
        (m.start(), m.group(1))
        for m in re.finditer(r"^[ \t]*(#{1,6} .+)$", masked_text, re.MULTILINE)
    ]
    if not heading_spans:
        return []

    normalized_sections = {
        _normalize_words(section.heading): section
        for section in sections
        if _normalize_words(section.heading)
    }
    boundaries: list[tuple[int, SectionInput]] = []
    matched: list[tuple[int, SectionInput]] = []
    used: set[str] = set()
    for start, line in heading_spans:
        section = normalized_sections.get(_normalize_words(_heading_text(line)))
        if section is not None and section.section_id not in used:
            matched.append((start, section))
            used.add(section.section_id)
    if matched:
        # Text-match base: headings whose text names a planned section start
        # that section. Reworded section headings must NOT lose their
        # boundary (issue #1150 feedback round 2 — a partial text match used
        # to discard every unmatched planned section, merging its body into
        # the previous section's budget), so each remaining planned section
        # is mapped from carrier candidates inside its OWN document segment:
        # the span between the matched boundaries of its planned neighbours.
        # Carrier candidates are at the matched heading level and never name
        # a planned section: deeper levels are sub-headings, and a repeated
        # heading text is a reference, not a new section. A document title
        # before the first matched heading therefore belongs only to the
        # segment of a section planned BEFORE that first match; unmatched
        # sections cannot steal carriers across an exact match, and extra
        # candidates inside a segment (sub-headings at the matched level)
        # leave the first candidate carrying the section — a deterministic
        # choice, with the residual that a front-matter heading inside the
        # segment is indistinguishable from a reworded section heading.
        matched_sorted = sorted(matched, key=lambda item: item[0])
        first_start = matched_sorted[0][0]
        matched_level = _heading_level(
            next(line for start, line in heading_spans if start == first_start)
        )
        taken_starts = {start for start, _ in matched}
        planned_pos = {
            section.section_id: idx for idx, section in enumerate(sections)
        }
        matched_pos = {
            section.section_id: planned_pos[section.section_id]
            for _, section in matched
        }
        candidates = [
            (start, line)
            for start, line in heading_spans
            if start not in taken_starts
            and _heading_level(line) == matched_level
            and _normalize_words(_heading_text(line)) not in normalized_sections
        ]
        boundaries.extend(matched)
        for section in sections:
            if section.section_id in used:
                continue
            pos = planned_pos[section.section_id]
            before = [
                start
                for start, sec in matched_sorted
                if matched_pos[sec.section_id] < pos
            ]
            after = [
                start
                for start, sec in matched_sorted
                if matched_pos[sec.section_id] > pos
            ]
            seg_lo = max(before) if before else 0
            seg_hi = min(after) if after else len(masked_text)
            in_segment = [
                (start, line)
                for start, line in candidates
                if seg_lo <= start < seg_hi
            ]
            if in_segment:
                carrier_start, _carrier_line = in_segment[0]
                candidates.remove(in_segment[0])
                boundaries.append((carrier_start, section))
        # Carriers may sit between matched boundaries; extents are built
        # from neighbours, so boundaries must be in document order.
        boundaries.sort(key=lambda item: item[0])
    else:
        for ordinal, (start, _line) in enumerate(heading_spans):
            if ordinal < len(sections):
                boundaries.append((start, sections[ordinal]))
            else:
                boundaries.append((start, sections[-1]))

    out: list[tuple[SectionInput, _TextBlock]] = []
    for i, (start, section_input) in enumerate(boundaries):
        end = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(masked_text)
        out.append((section_input, _TextBlock(start, end, masked_text[start:end])))
    return out


def _heading_text(line: str) -> str:
    """The heading words of an ATX heading line, ``#`` markers stripped."""
    return line.strip().lstrip("#").strip()


def _heading_level(line: str) -> int:
    """The ATX level (number of leading ``#``) of a heading line."""
    stripped = line.strip()
    return len(stripped) - len(stripped.lstrip("#"))


def _split_sentences(block_text: str) -> list[tuple[int, int, str]]:
    """Sentence spans within one block (offsets relative to the block)."""
    spans: list[tuple[int, int, str]] = []
    for match in re.finditer(r"[^.!?\n]+[.!?]?", block_text):
        sentence = match.group(0)
        if sentence.strip():
            spans.append((match.start(), match.end(), sentence))
    return spans


def _finding(
    *,
    rule_id: str,
    severity: str,
    section_id: str,
    start: int,
    end: int,
    masked_text: str,
    message: str,
) -> LintFinding:
    return LintFinding(
        rule_id=rule_id,
        severity=severity,  # type: ignore[arg-type]
        disposition="open",
        section_id=section_id,
        start=start,
        end=max(end, start + 1),
        excerpt=masked_text[start : max(end, start + 1)],
        message=message,
        rule_version=QUALITY_GATES_RULE_VERSION,
    )


def _lint_quality_gates(
    text: str,
    masked: MaskedText,
    *,
    dup_masked: MaskedText,
    section_id: str,
    target_words: Optional[int],
    sections: Sequence[SectionInput],
    piece_title: str,
) -> list[LintFinding]:
    """The issue-#1150 blocker family (SPEC §13.1 as amended).

    Length/heading/commentary rules read ``masked`` (all exclusions); the
    duplicate and cross-section rules read ``dup_masked``, where
    must-preserve spans are blanked at ALL occurrences (SPEC §13.1).
    """
    findings: list[LintFinding] = []
    masked_text = masked.masked
    dup_text = dup_masked.masked

    # -- total length ----------------------------------------------------
    # Band edges use scaled-integer comparisons (total * 100 vs target * band
    # percent) so an exactly-at-band count is exact in binary floating point:
    # 125 words against 100 is exactly +25 percent and passes BOTH rules.
    if target_words is not None and target_words > 0:
        total = _count_words(text)
        scaled = total * 100
        blocker_low = int(target_words * (100 - _LENGTH_BLOCKER_BAND * 100))
        blocker_high = int(target_words * (100 + _LENGTH_BLOCKER_BAND * 100))
        near_low = int(target_words * (100 - _LENGTH_NEAR_BAND * 100))
        near_high = int(target_words * (100 + _LENGTH_NEAR_BAND * 100))
        if scaled < blocker_low or scaled > blocker_high:
            findings.append(
                _finding(
                    rule_id="length.total_words",
                    severity="blocker",
                    section_id=section_id,
                    start=0,
                    end=len(masked_text),
                    masked_text=masked_text,
                    message=(
                        f"candidate is {total} words against a {target_words}-word "
                        f"target (outside +/-{_LENGTH_BLOCKER_BAND:.0%})"
                    ),
                )
            )
        elif (
            (scaled < near_low or scaled > near_high)
            and blocker_low < scaled < blocker_high
        ):
                findings.append(
                    _finding(
                        rule_id="length.total_words_near",
                        severity="advisory",
                        section_id=section_id,
                        start=0,
                        end=len(masked_text),
                        masked_text=masked_text,
                        message=(
                            f"candidate is {total} words against a "
                            f"{target_words}-word target (outside +/-"
                            f"{_LENGTH_NEAR_BAND:.0%})"
                        ),
                    )
                )

    # -- section budgets -------------------------------------------------
    # Budgets read the STANDARD masked view: the duplicate view blanks every
    # must-preserve occurrence, which would shrink section word counts and
    # suppress legitimate over-budget blockers (issue #1150 feedback round 2).
    # The duplicate view is reserved for the duplicate rules below.
    budget_section_map = _section_blocks(masked_text, sections)
    if budget_section_map:
        floor = max(int(settings.draft_quality_section_floor_words), 0)
        for section_input, block in budget_section_map:
            budget = section_input.budget_words
            if budget <= 0:
                continue
            words = len(_WORD_RE.findall(block.text))
            threshold = max(
                _SECTION_OVER_BUDGET_RATIO * budget, budget + floor
            )
            if words > threshold:
                findings.append(
                    _finding(
                        rule_id="length.section_over_budget",
                        severity="blocker",
                        section_id=section_input.section_id,
                        start=block.start,
                        end=block.end,
                        masked_text=masked_text,
                        message=(
                            f"section {section_input.section_id!r} is {words} "
                            f"words against a {budget}-word budget (over "
                            f"max(1.5x budget, budget + {floor}))"
                        ),
                    )
                )

    # -- duplication (hash + similarity) on the duplicate view ------------
    paragraphs: list[_TextBlock] = []
    for block in _split_blocks(dup_text):
        first_line, sep, remainder = block.text.partition("\n")
        if _ATX_HEADING_RE.match(first_line.strip()):
            # A heading block participates only with its non-heading
            # remainder (a heading glued to its body is one block, and the
            # heading line itself is not prose). The paragraph anchor is the
            # REMAINDER's own offset: a finding span or waiver bound must
            # cover the duplicated prose, never the unrelated heading line.
            if remainder.strip():
                paragraphs.append(
                    _TextBlock(
                        block.start + len(first_line) + len(sep),
                        block.end,
                        remainder,
                    )
                )
            continue
        paragraphs.append(block)
    seen_hash: dict[str, _TextBlock] = {}
    hash_emitted = 0
    for block in paragraphs:
        if hash_emitted >= _MAX_DUPLICATE_FINDINGS:
            break
        key = _normalize_words(block.text)
        if len(key.split()) < 2:
            continue
        if key in seen_hash:
            hash_emitted += 1
            first = seen_hash[key]
            findings.append(
                _finding(
                    rule_id="duplication.paragraph_hash",
                    severity="blocker",
                    section_id=section_id,
                    start=block.start,
                    end=block.end,
                    masked_text=masked_text,
                    message=(
                        "paragraph repeats the paragraph starting at offset "
                        f"{first.start} verbatim (normalized)"
                    ),
                )
            )
        else:
            seen_hash[key] = block
    # Degenerate repetition-loop output can be huge; cap the pairwise
    # comparisons and the emitted findings so one pathological candidate
    # cannot produce hundreds of thousands of blockers in one lint pass
    # (issue #1150 feedback round). Caps are deterministic (document order).
    if len(paragraphs) > _MAX_SIMILARITY_PARAGRAPHS:
        # Fail closed (issue #1150 feedback round 4): the bounded comparison
        # below cannot cover paragraphs beyond the cap, and the SPEC §13.1
        # duplicate contract has no size cutoff — a candidate too large to
        # fully evaluate blocks instead of silently passing its unchecked
        # tail.
        unchecked = paragraphs[_MAX_SIMILARITY_PARAGRAPHS]
        findings.append(
            _finding(
                rule_id="duplication.similarity_beyond_cap",
                severity="blocker",
                section_id=section_id,
                start=unchecked.start,
                end=unchecked.end,
                masked_text=masked_text,
                message=(
                    f"candidate has {len(paragraphs)} paragraphs; the "
                    "similarity gate compares at most the first "
                    f"{_MAX_SIMILARITY_PARAGRAPHS}, so the duplicate "
                    "contract cannot be evaluated whole — split or shorten "
                    "the draft"
                ),
            )
        )
    similarity_candidates = [
        (block, key)
        for block in paragraphs[:_MAX_SIMILARITY_PARAGRAPHS]
        for key in [_normalize_words(block.text)]
        if len(_WORD_RE.findall(block.text)) >= _SIMILARITY_MIN_WORDS
    ]
    similarity_emitted = 0
    for i in range(len(similarity_candidates)):
        for j in range(i + 1, len(similarity_candidates)):
            if similarity_emitted >= _MAX_DUPLICATE_FINDINGS:
                break
            a_block, a_key = similarity_candidates[i]
            b_block, b_key = similarity_candidates[j]
            if a_key == b_key:
                continue  # already reported by the hash rule (same words)
            a_words = frozenset(a_key.split())
            b_words = frozenset(b_key.split())
            if not a_words or not b_words:
                continue
            union = a_words | b_words
            jaccard = len(a_words & b_words) / len(union) if union else 0.0
            if jaccard >= _SIMILARITY_THRESHOLD and a_block.text.strip() != b_block.text.strip():
                similarity_emitted += 1
                findings.append(
                    _finding(
                        rule_id="duplication.paragraph_similarity",
                        severity="blocker",
                        section_id=section_id,
                        start=b_block.start,
                        end=b_block.end,
                        masked_text=masked_text,
                        message=(
                            f"paragraph is {jaccard:.2f} similar by word set to "
                            f"the paragraph starting at offset {a_block.start}"
                        ),
                    )
                )

    # -- cross-section duplicate sentences -------------------------------
    # Runs on the duplicate view: must-preserve sentences are exempt at ALL
    # occurrences (SPEC §13.1), so a preserved sentence repeated across
    # sections never blocks.
    cross_section_map = _section_blocks(dup_text, sections)
    if cross_section_map:
        occurrences: dict[str, tuple[str, int, int]] = {}
        cross_emitted = 0
        for section_input, block in cross_section_map:
            if cross_emitted >= _MAX_DUPLICATE_FINDINGS:
                break
            body_start = block.start
            for s_start, s_end, sentence in _split_sentences(block.text):
                if cross_emitted >= _MAX_DUPLICATE_FINDINGS:
                    break
                if _ATX_HEADING_RE.match(sentence.strip()):
                    continue
                key = _normalize_words(sentence)
                if len(key.split()) < _CROSS_SECTION_MIN_WORDS:
                    continue
                absolute = (body_start + s_start, body_start + s_end)
                if key in occurrences:
                    first_section = occurrences[key][0]
                    if first_section != section_input.section_id:
                        cross_emitted += 1
                        findings.append(
                            _finding(
                                rule_id="duplication.cross_section_sentence",
                                severity="blocker",
                                section_id=section_input.section_id,
                                start=absolute[0],
                                end=absolute[1],
                                masked_text=masked_text,
                                message=(
                                    "sentence of "
                                    f"{len(key.split())} words repeats section "
                                    f"{first_section!r} verbatim"
                                ),
                            )
                        )
                else:
                    occurrences[key] = (
                        section_input.section_id, absolute[0], absolute[1]
                    )

    # -- heading structure -----------------------------------------------
    h1_matches = [m for m in _H1_RE.finditer(masked_text) if m.group(0).strip()]
    # Section inputs justify their own headings: an outline that plans N
    # sections may carry N H1 section headings, so the document-level defect
    # is H1s beyond the expected structure (or any second H1 when the caller
    # supplied no section model).
    expected_h1 = max(len(tuple(sections)), 1)
    if len(h1_matches) > expected_h1:
        # Anchor the FIRST heading beyond the allowance: with N planned
        # sections, h1_matches[1] is an allowed section heading, and binding
        # the waiver span to it would invalidate the wrong heading.
        extra = h1_matches[expected_h1]
        findings.append(
            _finding(
                rule_id="structure.multiple_h1",
                severity="blocker",
                section_id=section_id,
                start=extra.start(),
                end=extra.end(),
                masked_text=masked_text,
                message=f"candidate carries {len(h1_matches)} H1 headings",
            )
        )
    if piece_title.strip():
        title_key = _normalize_words(piece_title)
        if title_key:
            for match in _ATX_HEADING_RE.finditer(masked_text):
                heading = match.group(0).lstrip("#").strip()
                if heading and _normalize_words(heading) == title_key:
                    findings.append(
                        _finding(
                            rule_id="structure.heading_repeats_title",
                            severity="blocker",
                            section_id=section_id,
                            start=match.start(),
                            end=match.end(),
                            masked_text=masked_text,
                            message=(
                                "heading repeats the piece title "
                                f"{piece_title!r}"
                            ),
                        )
                    )

    # -- process commentary ------------------------------------------------
    for phrase, pattern in PROCESS_COMMENTARY_PATTERNS:
        for match in pattern.finditer(masked_text):
            findings.append(
                _finding(
                    rule_id=f"process_commentary.{phrase}",
                    severity="blocker",
                    section_id=section_id,
                    start=match.start(),
                    end=match.end(),
                    masked_text=masked_text,
                    message=(
                        "pipeline-instruction text leaked into the candidate: "
                        f"{match.group(0)!r}"
                    ),
                )
            )

    # -- trailing source line (RAW final non-empty line) -------------------
    stripped_tail = text.rstrip()
    if stripped_tail:
        final_start = stripped_tail.rfind("\n") + 1
        final_line = stripped_tail[final_start:]
        if _TRAILING_SOURCE_RE.match(final_line.strip()):
            findings.append(
                LintFinding(
                    rule_id="process.trailing_source_line",
                    severity="blocker",
                    disposition="open",
                    section_id=section_id,
                    start=final_start,
                    end=len(stripped_tail),
                    excerpt=final_line,
                    message=(
                        "final line is a source attribution the prompts never "
                        f"asked for: {final_line.strip()!r}"
                    ),
                    rule_version=QUALITY_GATES_RULE_VERSION,
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
    target_words: Optional[int] = None,
    sections: Sequence[SectionInput] = (),
    piece_title: str = "",
    preserve_texts: Sequence[str] = (),
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
    # Duplicate view: exclusion masking WITHOUT locked spans, then every
    # occurrence of each protected text (locked-span texts + the outline's
    # must_preserve texts) blanked — SPEC §13.1 exempts must_preserve spans
    # at ALL occurrences from the duplicate rules. Same length as the
    # candidate, so finding offsets stay valid against the original text.
    dup_base = mask_excluded_spans(text, locked_spans=())
    protected_texts = list(preserve_texts) + [
        text[start:end] for start, end in locked_spans
    ]
    dup_masked = MaskedText(
        masked=_mask_texts_everywhere(dup_base.masked, protected_texts),
        spans=dup_base.spans,
    )
    findings: list[LintFinding] = []
    findings.extend(_lint_boilerplate(masked.masked, section_id=section_id))
    findings.extend(_lint_review_vocabulary(masked.masked, section_id=section_id))
    findings.extend(_lint_structure_signal(masked.masked, section_id=section_id))
    findings.extend(_lint_readability_signal(masked.masked, section_id=section_id))
    findings.extend(
        _lint_quality_gates(
            text,
            masked,
            dup_masked=dup_masked,
            section_id=section_id,
            target_words=target_words,
            sections=tuple(sections),
            piece_title=piece_title,
        )
    )
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
    locked_spans: Sequence[tuple[int, int]] = (),
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

    The replacement template is resolved from a whitespace-normalized key so
    a match the detector found across incidental whitespace variation (a hard
    line-wrap, a tab) rewrites exactly like its single-space form; the rewrite
    itself still replaces the exact detected span, so surrounding text and
    span-exactness are untouched. A span overlapping any ``locked_spans``
    range is never rewritten, and after each applied rewrite every locked
    span's text is re-verified to still be present — a rewrite that would
    consume preserved wording is reverted before it can stick.

    Args:
        text: the exact text the report's offsets refer to.
        report: a :class:`LintReport` (e.g. from :func:`run_deterministic_lint`).
        limit: maximum rewrites to apply; defaults to
            ``settings.draft_lint_rewrite_limit``.
        locked_spans: caller-supplied ``(start, end)`` ranges whose text must
            survive verbatim (already-approved manuscript spans).

    Returns:
        tuple[str, int]: the rewritten text, and the number of rewrites
        actually applied (``<= limit``).
    """
    if limit is None:
        limit = settings.draft_lint_rewrite_limit

    locked_pairs = [
        (start, end)
        for start, end in locked_spans
        if 0 <= start < end <= len(text)
    ]
    locked_texts = [text[start:end] for start, end in locked_pairs]

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
        if any(
            not (finding.end <= ls or finding.start >= le)
            for ls, le in locked_pairs
        ):
            continue
        # Resolve the template on a whitespace-normalized key so a match the
        # detector found across a line-wrap rewrites like the single-space
        # form (detection and resolution must agree on the same equivalence).
        phrase_key = re.sub(r"\s+", " ", finding.excerpt.strip().lower())
        replacement_template = BLOCKED_BOILERPLATE.get(phrase_key)
        if replacement_template is None:
            continue
        replacement = _rewrite_replacement(finding.excerpt, replacement_template)
        candidate = (
            new_text[: finding.start] + replacement + new_text[finding.end :]
        )
        if any(locked not in candidate for locked in locked_texts):
            continue
        new_text = candidate
        applied += 1
    return new_text, applied
