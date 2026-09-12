"""Deterministic semantic-preservation checks (issue #237, AC11 / FULL-ENH-02).

Complements literal matching (``fact_coverage``) with lexical-structural
semantics: a correct paraphrase passes without literal substring presence,
while negation, numeric and code-identifier contradictions fail. Pure
stdlib, no embeddings and no LLM — these are decision rules over normalized
tokens, deterministic by construction.

Decision order for ``semantic_fact_preserved(answer, fact)``:

1. Empty/whitespace-only fact is vacuously preserved.
2. Literal containment of the normalized fact in the normalized answer passes.
3. A negation-polarity mismatch (one side negates, the other does not) fails.
4. A fact number absent from the answer fails (numeric contradiction).
5. A fact code identifier (``WORD-123`` shape) absent from the answer fails.
6. Otherwise the fact-anchored content-word recall
   ``|content_words(fact) ∩ content_words(answer)| / |content_words(fact)|``
   decides, with threshold ``>= 0.6``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Tuple

#: Fact-anchored content-word recall threshold (see module docstring).
CONTENT_WORD_RECALL_THRESHOLD = 0.6

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_NUMBER_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
_CODE_TOKEN_RE = re.compile(r"^[a-z]+-[0-9]+$")

_NEGATION_MARKERS = frozenset(
    {
        "not",
        "no",
        "never",
        "without",
        "cannot",
        "cant",
        "dont",
        "doesnt",
        "isnt",
        "arent",
        "wasnt",
        "werent",
        "wont",
        "neither",
        "nor",
    }
)

_STOPWORDS = frozenset(
    {
        "a", "an", "the", "and", "or", "but", "if", "because", "so", "than",
        "too", "very", "of", "in", "on", "at", "by", "for", "with", "about",
        "against", "between", "into", "through", "to", "from", "up", "down",
        "out", "off", "over", "under", "again", "further", "then", "once",
        "here", "there", "when", "where", "why", "how", "all", "any", "both",
        "each", "few", "more", "most", "other", "some", "such", "only",
        "own", "same", "s", "t", "can", "will", "just", "should", "now",
        "is", "are", "was", "were", "be", "been", "being", "have", "has",
        "had", "having", "do", "does", "did", "doing", "would", "could",
        "might", "must", "shall", "may", "it", "its", "this", "that",
        "these", "those", "i", "you", "he", "she", "we", "they", "them",
        "his", "her", "their", "our", "your", "me", "him", "us", "what",
        "which", "who", "whom", "as", "also", "according",
    }
)


@dataclass(frozen=True)
class SemanticCheckResult:
    """Outcome of one semantic-preservation check."""

    preserved: bool
    failure_reasons: List[str] = field(default_factory=list)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().casefold())


def _tokens(text: str) -> List[str]:
    return _TOKEN_RE.findall(_normalize(text))


def _classify(tokens: List[str]) -> Tuple[set, set, set, set]:
    """Split tokens into (content words, numbers, code ids, negation markers)."""
    content: set = set()
    numbers: set = set()
    code: set = set()
    negations: set = set()
    for token in tokens:
        if token in _NEGATION_MARKERS:
            negations.add(token)
        elif _CODE_TOKEN_RE.match(token):
            code.add(token)
        elif _NUMBER_RE.match(token):
            numbers.add(token)
        elif token not in _STOPWORDS:
            content.add(token)
    return content, numbers, code, negations


def semantic_fact_preserved(answer: str, fact: str) -> SemanticCheckResult:
    """Check whether ``answer`` semantically preserves ``fact``.

    Deterministic decision rules — see the module docstring for the exact
    order. Failure results always carry at least one human-readable reason.
    """
    reasons: List[str] = []
    if not _normalize(fact):
        return SemanticCheckResult(preserved=True, failure_reasons=reasons)

    if _normalize(fact) in _normalize(answer):
        return SemanticCheckResult(preserved=True, failure_reasons=reasons)

    fact_content, fact_numbers, fact_code, fact_neg = _classify(_tokens(fact))
    ans_content, ans_numbers, ans_code, ans_neg = _classify(_tokens(answer))

    if bool(fact_neg) != bool(ans_neg):
        reasons.append(
            "negation mismatch: fact and answer disagree on negation "
            f"(fact markers={sorted(fact_neg)}, answer markers={sorted(ans_neg)})"
        )
        return SemanticCheckResult(preserved=False, failure_reasons=reasons)

    missing_numbers = fact_numbers - ans_numbers
    if missing_numbers:
        reasons.append(
            f"numeric contradiction: fact numbers {sorted(missing_numbers)} "
            f"absent from answer"
        )
        return SemanticCheckResult(preserved=False, failure_reasons=reasons)

    missing_code = fact_code - ans_code
    if missing_code:
        reasons.append(
            f"code identifier contradiction: fact identifiers "
            f"{sorted(missing_code)} absent from answer"
        )
        return SemanticCheckResult(preserved=False, failure_reasons=reasons)

    if not fact_content:
        # Nothing but numbers/code/negation markers — already checked above.
        return SemanticCheckResult(preserved=True, failure_reasons=reasons)

    overlap = fact_content & ans_content
    ratio = len(overlap) / len(fact_content)
    if ratio < CONTENT_WORD_RECALL_THRESHOLD:
        reasons.append(
            f"insufficient content-word overlap: fact-anchored recall "
            f"{ratio:.3f} < {CONTENT_WORD_RECALL_THRESHOLD} "
            f"(missing={sorted(fact_content - ans_content)})"
        )
        return SemanticCheckResult(preserved=False, failure_reasons=reasons)

    return SemanticCheckResult(preserved=True, failure_reasons=reasons)


__all__ = [
    "CONTENT_WORD_RECALL_THRESHOLD",
    "SemanticCheckResult",
    "semantic_fact_preserved",
]
