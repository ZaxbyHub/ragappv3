"""Citation validation and repair for assistant responses.

Parses ``[S#]`` (document), ``[M#]`` (memory), and ``[W#]`` (wiki) citation
labels from assistant output, validates them against the available
source/memory/wiki labels, and repairs or strips invalid references before the
response is streamed to the client or persisted to chat history.

Design goals:
- Never modify content during token streaming (UX guarantee).
- Run a single repair pass on the *complete* assistant text before save.
- Be deterministic and side-effect free so unit tests remain stable.
- Backward compatible: existing callers that only use S/M continue to work.
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

# Match [S<digits>], [M<digits>], [W<digits>], and [K<digits>] anywhere in text.
# Case-sensitive: S/M/W/K only — lowercase variants are treated as plain text.
_CITATION_RE = re.compile(r"\[(S|M|W|K)(\d+)\]")

# Some deployed LLMs emit fullwidth CJK brackets (U+3010/U+3011) around
# citation labels instead of ASCII brackets, e.g. 【S1】 instead of [S1].
# Same case-sensitivity as _CITATION_RE.
_FULLWIDTH_CITATION_RE = re.compile(r"【(S|M|W|K)(\d+)】")


def normalize_citation_brackets(content: str) -> str:
    """Convert fullwidth 【S1】-style citations to ASCII [S1].

    ``_CITATION_RE`` (and everything downstream of it — validation, repair,
    parsing, scoring) only recognizes ASCII brackets. Some deployed LLMs emit
    the fullwidth CJK bracket variant instead, which would otherwise count as
    an uncited claim and can never be stripped/normalized. Call this before
    any citation parsing so all downstream logic sees ASCII.
    """
    if not content:
        return content
    return _FULLWIDTH_CITATION_RE.sub(r"[\1\2]", content)


@dataclass(frozen=True)
class CitationValidationResult:
    """Outcome of validating citations in an assistant response."""

    repaired_content: str
    valid_citations: Tuple[str, ...]
    invalid_citations: Tuple[str, ...]
    invalid_stripped: bool
    has_evidence: bool
    has_any_citation: bool
    uncited_factual_warning: bool
    invalid_wiki_citations: Tuple[str, ...] = field(default=())
    invalid_kms_citations: Tuple[str, ...] = field(default=())
    # Per-citation confidence scores [0.0, 1.0] — populated by score_citations().
    citation_confidence: Dict[str, float] = field(default_factory=dict)
    # Answer sentences that have no citation AND low overlap with all sources.
    unverifiable_claims: Tuple[str, ...] = field(default_factory=lambda: ())
    # True when normalize_citation_brackets() changed the content (e.g. a
    # fullwidth 【S1】 was converted to ASCII [S1]). Callers must treat this
    # the same as invalid_stripped when deciding whether repaired_content
    # needs to replace the raw streamed text — pure normalization leaves
    # invalid_stripped False even though the content changed.
    normalized_citations: bool = False


def _label_set(prefix: str, count: int) -> set[str]:
    """Build the set of labels available for the given prefix and count."""
    return {f"{prefix}{i}" for i in range(1, count + 1)}


def _looks_factual(content: str) -> bool:
    """Heuristic: does the content look like it contains factual claims?

    True when the content has more than two sentences and is not a "no-match"
    refusal. Used to decide whether an answer with zero citations should be
    flagged when document/memory/wiki evidence is present.
    """
    if not content or len(content) < 80:
        return False
    lower = content.lower()
    refusal_phrases = (
        "not available in the retrieved",
        "i don't have enough",
        "no relevant",
        "the retrieved documents",
    )
    if any(p in lower for p in refusal_phrases):
        return False
    # Count sentence-terminating punctuation.
    return sum(1 for ch in content if ch in ".!?") >= 2


def validate_and_repair_citations(
    content: str,
    *,
    source_count: int,
    memory_count: int,
    wiki_count: int = 0,
    kms_count: int = 0,
) -> CitationValidationResult:
    """Validate ``[S#]``, ``[M#]``, ``[W#]``, and ``[K#]`` citations in ``content``.

    Args:
        content: Complete assistant response text.
        source_count: Number of document sources available (label range S1..SN).
        memory_count: Number of memories available (label range M1..MN).
        wiki_count: Number of wiki evidence items available (range W1..WN).
            Defaults to 0 for backward compatibility — W citations will be
            treated as invalid when wiki_count is 0.
        kms_count: Number of KMS evidence items available (range K1..KN).
            Defaults to 0 for backward compatibility — K citations will be
            treated as invalid when kms_count is 0.

    Returns:
        CitationValidationResult with the repaired content, the set of valid
        and invalid labels found, and flags about evidence/warning state.
    """
    if not content:
        return CitationValidationResult(
            repaired_content="",
            valid_citations=(),
            invalid_citations=(),
            invalid_stripped=False,
            has_evidence=source_count > 0 or memory_count > 0 or wiki_count > 0 or kms_count > 0,
            has_any_citation=False,
            uncited_factual_warning=False,
            invalid_wiki_citations=(),
            invalid_kms_citations=(),
        )

    original_content = content
    content = normalize_citation_brackets(content)
    normalized_citations = content != original_content

    valid_s = _label_set("S", source_count)
    valid_m = _label_set("M", memory_count)
    valid_w = _label_set("W", wiki_count)
    valid_k = _label_set("K", kms_count)

    valid: List[str] = []
    invalid: List[str] = []
    invalid_wiki: List[str] = []
    invalid_kms: List[str] = []

    def _replacer(match: re.Match) -> str:
        prefix, num = match.group(1), match.group(2)
        label = f"{prefix}{num}"
        is_valid = (
            (prefix == "S" and label in valid_s)
            or (prefix == "M" and label in valid_m)
            or (prefix == "W" and label in valid_w)
            or (prefix == "K" and label in valid_k)
        )
        if is_valid:
            valid.append(label)
            return match.group(0)
        invalid.append(label)
        if prefix == "W":
            invalid_wiki.append(label)
        elif prefix == "K":
            invalid_kms.append(label)
        # Strip the invalid citation. Leave a single space so words don't merge.
        return ""

    repaired = _CITATION_RE.sub(_replacer, content)
    if invalid:
        # Only tidy whitespace when an invalid citation was actually stripped,
        # and do it non-destructively. Collapse runs of spaces/tabs only
        # mid-line (preceded by a non-space char) so leading code indentation is
        # preserved, and only drop spaces/tabs — never newlines — left before
        # sentence punctuation. Content with no stripped citations is returned
        # verbatim (modulo a trailing strip), so code blocks are never mangled.
        repaired = re.sub(r"(?<=\S)[ \t]{2,}", " ", repaired)
        repaired = re.sub(r"[ \t]+([.,;:!?])", r"\1", repaired)
    repaired = repaired.strip()

    has_evidence = (
        source_count > 0 or memory_count > 0 or wiki_count > 0 or kms_count > 0
    )
    has_any_citation = bool(valid)
    uncited_factual_warning = (
        has_evidence
        and not has_any_citation
        and _looks_factual(repaired)
    )

    return CitationValidationResult(
        repaired_content=repaired,
        valid_citations=tuple(dict.fromkeys(valid)),
        invalid_citations=tuple(dict.fromkeys(invalid)),
        invalid_stripped=bool(invalid),
        has_evidence=has_evidence,
        has_any_citation=has_any_citation,
        uncited_factual_warning=uncited_factual_warning,
        invalid_wiki_citations=tuple(dict.fromkeys(invalid_wiki)),
        invalid_kms_citations=tuple(dict.fromkeys(invalid_kms)),
        normalized_citations=normalized_citations,
    )


def parse_citations(content: str) -> Tuple[List[str], List[str]]:
    """Return (sources_cited, memories_cited) labels as encountered, deduped.

    Useful for tests and for trace instrumentation. Order matches first
    occurrence in ``content``. Signature unchanged for backward compatibility.
    [W#] citations are ignored here — use parse_wiki_citations() instead.
    """
    content = normalize_citation_brackets(content or "")
    sources: List[str] = []
    memories: List[str] = []
    for m in _CITATION_RE.finditer(content):
        label = f"{m.group(1)}{m.group(2)}"
        if m.group(1) == "S" and label not in sources:
            sources.append(label)
        elif m.group(1) == "M" and label not in memories:
            memories.append(label)
    return sources, memories


def parse_wiki_citations(content: str) -> List[str]:
    """Return [W#] labels found in content, deduped, in first-occurrence order."""
    content = normalize_citation_brackets(content or "")
    wikis: List[str] = []
    for m in _CITATION_RE.finditer(content):
        if m.group(1) == "W":
            label = f"W{m.group(2)}"
            if label not in wikis:
                wikis.append(label)
    return wikis


def parse_kms_citations(content: str) -> List[str]:
    """Return [K#] labels found in content, deduped, in first-occurrence order."""
    content = normalize_citation_brackets(content or "")
    kms: List[str] = []
    for m in _CITATION_RE.finditer(content):
        if m.group(1) == "K":
            label = f"K{m.group(2)}"
            if label not in kms:
                kms.append(label)
    return kms


def labels_for_sources(sources: Iterable[dict]) -> List[str]:
    """Return the source_label values for an iterable of source dicts."""
    out: List[str] = []
    for s in sources:
        label = s.get("source_label") if isinstance(s, dict) else None
        if label:
            out.append(label)
    return out


def labels_for_memories(memories: Iterable[dict]) -> List[str]:
    """Return the memory_label values for an iterable of memory dicts."""
    out: List[str] = []
    for m in memories:
        label = m.get("memory_label") if isinstance(m, dict) else None
        if label:
            out.append(label)
    return out


def _tokenize(text: str) -> Set[str]:
    """Lowercase token set for a text: strip punctuation, split on whitespace."""
    table = str.maketrans("", "", string.punctuation)
    cleaned = text.translate(table).lower()
    return set(cleaned.split())


# -------------------------------------------------------------------------- #
# Sentence-split helpers (mirrors context_distiller logic for consistency)
# -------------------------------------------------------------------------- #
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def _split_sentences(text: str) -> List[str]:
    """Split text into sentence strings, stripping leading whitespace."""
    if not text:
        return []
    return [s.strip() for s in _SENTENCE_RE.split(text) if s.strip()]


# -------------------------------------------------------------------------- #
# Containment-based citation confidence
# -------------------------------------------------------------------------- #

#: Containment threshold below which an uncited sentence is "unverifiable":
#: at least half the claim's unique tokens must appear in some source. Uses
#: containment rather than Jaccard because a short (~15-token) claim sentence
#: compared against a long (~200-token) source caps Jaccard around 0.08 even
#: for a verbatim quote, since Jaccard's denominator includes every unrelated
#: token in the source. Containment only divides by the claim's own token
#: count, so it isn't punished for the source being long.
UNVERIFIABLE_THRESHOLD = 0.5


def _sentence_overlap(claim_sentence: str, source_text: str) -> float:
    """Containment overlap between a claim sentence and a source text.

    |claim_tokens ∩ source_tokens| / |claim_tokens|. Returns 0.0 when the
    claim has no tokens. Uses the full source_text (all distilled content for
    that source index). Optionally a caller may supply a tighter span via
    ``source_span`` when provenance character offsets are available — but for
    the base case we compare against the full source text so every cited
    sentence has at least one overlapping source by construction.
    """
    claim_tokens = _tokenize(claim_sentence)
    if not claim_tokens:
        return 0.0
    source_tokens = _tokenize(source_text)
    return len(claim_tokens & source_tokens) / len(claim_tokens)


def _best_overlap_for_claim(
    claim_sentence: str, source_texts: Sequence[str]
) -> float:
    """Maximum containment overlap between a claim sentence and any source text."""
    if not source_texts:
        return 0.0
    return max(_sentence_overlap(claim_sentence, src) for src in source_texts)


def _compute_citation_confidence(
    content: str,
    valid_citations: Sequence[str],
    source_texts: Sequence[str],
) -> Dict[str, float]:
    """Compute containment-based confidence per valid citation label.

    Algorithm:
      1. Split the answer content into sentences (sentence-boundary aware).
      2. For each valid [S#] label, find the *first* sentence that contains it
         → that is the "claim sentence" for this citation.
      3. Look up the cited source text (sources[source_num - 1]).
      4. Score = containment(claim_sentence tokens, source_text tokens) —
         the fraction of the claim's own tokens found in the source.

    Returns a dict mapping label (e.g. "S1") → confidence score [0.0, 1.0].
    """
    if not valid_citations or not source_texts:
        return {}

    sentences = _split_sentences(content)
    label_to_sentence: Dict[str, str] = {}
    for sentence in sentences:
        for match in _CITATION_RE.finditer(sentence):
            label = f"{match.group(1)}{match.group(2)}"
            if label not in label_to_sentence:
                label_to_sentence[label] = sentence

    confidences: Dict[str, float] = {}
    for label in valid_citations:
        prefix = label[0]
        if prefix != "S":
            continue
        try:
            idx = int(label[1:]) - 1
        except ValueError:
            continue
        if idx < 0 or idx >= len(source_texts):
            continue
        claim = label_to_sentence.get(label, "")
        if not claim:
            continue
        source_text = source_texts[idx]
        confidences[label] = _sentence_overlap(claim, source_text)

    return confidences


def _strip_leading_markdown(sentence: str) -> str:
    """Strip leading list/heading/quote/table decoration from a sentence.

    Handles numbered-list prefixes (``"2. "``) and leading ``#``/``*``/``-``/
    ``>``/``|`` characters, so the remaining word count reflects actual
    content rather than markdown structure.
    """
    stripped = re.sub(r"^\d+\.\s*", "", sentence.strip())
    return stripped.lstrip("#*->|").strip()


def _find_unverifiable_claims(
    content: str,
    valid_citations: Sequence[str],
    source_texts: Sequence[str],
    threshold: float = UNVERIFIABLE_THRESHOLD,
) -> List[str]:
    """Return answer sentences (claims) that have no citation and low source overlap.

    Unverifiable claims are factual-sounding sentences that:
      - Have no [S#]/[M#]/[W#]/[K#] citation, AND
      - Score below ``threshold`` containment overlap against *every*
        retrieved source.

    Non-claim fragments are skipped before scoring — markdown table rows,
    headers, and anything too short to carry real content once list/heading/
    quote decoration is stripped. The naive ``_SENTENCE_RE`` split turns
    markdown structure (e.g. a numbered list) into pseudo-claims like "2."
    that would otherwise always score below threshold and get flagged.

    Returns sentences in document order (first-occurrence order).
    """
    if not _looks_factual(content) or not source_texts:
        return []

    cited_labels: Set[str] = set()
    for m in _CITATION_RE.finditer(content):
        cited_labels.add(f"{m.group(1)}{m.group(2)}")

    unverifiable: List[str] = []
    seen_uncited: Set[str] = set()
    for sentence in _split_sentences(content):
        if _CITATION_RE.search(sentence):
            continue
        if sentence in seen_uncited:
            continue
        seen_uncited.add(sentence)
        # Skip non-claim fragments: markdown table rows, headers, and
        # anything too short to carry a real claim once list/heading/quote
        # decoration is stripped. The "|" check also skips prose sentences
        # that merely contain a pipe — an accepted under-flagging tradeoff
        # for this advisory surface.
        if sentence.startswith("#") or "|" in sentence:
            continue
        if len(_strip_leading_markdown(sentence).split()) < 5:
            continue
        best = _best_overlap_for_claim(sentence, source_texts)
        if best < threshold:
            unverifiable.append(sentence)

    return unverifiable


def score_citations(
    content: str,
    source_texts: Sequence[str],
    sentence_provenance: Optional[Sequence[object]] = None,
) -> CitationValidationResult:
    """Compute citation confidence scores and unverifiable-claims list.

    This is a pure, side-effect-free extension to ``validate_and_repair_citations``.
    It adds:
      - ``citation_confidence``: containment overlap score per valid [S#] citation.
      - ``unverifiable_claims``: answer sentences with no citation AND low overlap.

    Callers that only need valid/invalid citation labels should continue calling
    ``validate_and_repair_citations`` directly.

    Args:
        content: Complete assistant response text.
        source_texts: Texts of the retrieved document sources, in order
            (sources[0] → S1, sources[1] → S2, …).
        sentence_provenance: Optional task-2.1 sentence provenance list
            (SentenceProvenance NamedTuples). When supplied the function may use
            provenance character offsets to narrow the source span used for
            overlap scoring. Currently unused in v1 (reserved for future use);
            the full source text is used for lexical overlap.

    Returns:
        A CitationValidationResult (created from a minimal valid result) that
        carries ``citation_confidence`` and ``unverifiable_claims``. No invalid
        citations are stripped, but fullwidth 【S#】 citations are normalized to
        ASCII, so ``repaired_content`` may differ from the input (and
        ``normalized_citations`` reports it); use
        ``validate_and_repair_citations`` separately when repair is also needed.
    """
    normalized = normalize_citation_brackets(content)
    normalized_changed = normalized != content
    content = normalized
    if not content:
        return CitationValidationResult(
            repaired_content="",
            valid_citations=(),
            invalid_citations=(),
            invalid_stripped=False,
            has_evidence=bool(source_texts),
            has_any_citation=False,
            uncited_factual_warning=False,
            citation_confidence={},
            unverifiable_claims=(),
        )

    valid_s = {f"S{i}" for i in range(1, len(source_texts) + 1)}
    valid: List[str] = []
    for m in _CITATION_RE.finditer(content):
        label = f"{m.group(1)}{m.group(2)}"
        if label not in valid and m.group(1) == "S" and label in valid_s:
            valid.append(label)
    valid_citations = tuple(dict.fromkeys(valid))

    citation_confidence = _compute_citation_confidence(
        content, valid_citations, source_texts
    )
    unverifiable = _find_unverifiable_claims(
        content, valid_citations, source_texts
    )

    return CitationValidationResult(
        repaired_content=content,
        valid_citations=valid_citations,
        invalid_citations=(),
        invalid_stripped=False,
        has_evidence=bool(source_texts),
        has_any_citation=bool(valid_citations),
        uncited_factual_warning=False,
        citation_confidence=citation_confidence,
        unverifiable_claims=tuple(unverifiable),
        normalized_citations=normalized_changed,
    )


def repair_against_sources_and_memories(
    content: str,
    sources: Sequence[dict],
    memories: Sequence[dict],
    wiki_evidence: Optional[Sequence[dict]] = None,
    kms_evidence: Optional[Sequence[dict]] = None,
) -> CitationValidationResult:
    """Convenience: derive counts from source/memory/wiki/kms dicts, then validate.

    Sources are expected to use 1-based ``source_label`` like ``S1``.
    Memories are expected to use 1-based ``memory_label`` like ``M1``.
    Wiki evidence items are expected to use 1-based ``wiki_label`` like ``W1``.
    KMS evidence items are expected to use 1-based ``kms_label`` like ``K1``.
    Counts default to the maximum index assigned across the inputs so
    sparse labelings (e.g. only S2 and S4) still validate correctly.
    """

    def _max_index(items: Sequence[dict], prefix: str, key: str) -> int:
        n = 0
        for it in items:
            if not isinstance(it, dict):
                continue
            label = it.get(key)
            if not isinstance(label, str):
                continue
            if label.startswith(prefix) and label[len(prefix):].isdigit():
                n = max(n, int(label[len(prefix):]))
        return n

    wiki_count = _max_index(wiki_evidence or [], "W", "wiki_label")
    kms_count = _max_index(kms_evidence or [], "K", "kms_label")

    return validate_and_repair_citations(
        content,
        source_count=_max_index(sources, "S", "source_label"),
        memory_count=_max_index(memories, "M", "memory_label"),
        wiki_count=wiki_count,
        kms_count=kms_count,
    )


__all__ = [
    "CitationValidationResult",
    "normalize_citation_brackets",
    "validate_and_repair_citations",
    "parse_citations",
    "parse_wiki_citations",
    "parse_kms_citations",
    "labels_for_sources",
    "labels_for_memories",
    "repair_against_sources_and_memories",
    "score_citations",
    "UNVERIFIABLE_THRESHOLD",
]
