"""Loader and deterministic scoring helpers for the Draft Room gold corpus.

This module is **test-support code**. It lives under ``backend/tests/`` on
purpose: nothing under ``backend/app/`` may import it, because it holds the
gold answers for the Draft Room factual-preservation evaluation. A production
feature that could read this module could also read the answer key.

Everything here is an **oracle-backed deterministic metric**. No function in
this module calls a language model, performs a network request, or consults a
learned judge. Every score is a pure function of (a) the curated manifest that
ships next to the fixture documents and (b) the candidate text handed in by the
caller. The same inputs always produce the same outputs, on any machine, in any
process order.

Layout::

    backend/tests/fixtures/draft_room/manifest.json   curated expectations
    backend/tests/fixtures/draft_room/*.md, *.txt     synthetic source documents
    backend/tests/draft_room/gold_corpus.py           this module

Stdlib only. No third-party dependency is introduced by this file.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

__all__ = [
    "AUTHORITIES",
    "CATEGORIES",
    "DEFAULT_FIXTURES_DIR",
    "MANIFEST_FILENAME",
    "REQUIRED_HIGH_STAKES_CATEGORIES",
    "ROLES",
    "STATUSES",
    "CitationCheck",
    "CitationMatchResult",
    "ExactQuote",
    "ExpectedContradiction",
    "GoldCorpus",
    "GoldCorpusError",
    "GoldDocument",
    "InjectionString",
    "LockedSpan",
    "MajorEditResult",
    "NearQuoteTrap",
    "Proposition",
    "PropositionPreservationResult",
    "QuoteFidelityResult",
    "Scenario",
    "UnsupportedClaimResult",
    "load_corpus",
    "normalize_document_text",
    "normalize_for_match",
    "normalize_quotes",
    "score_citation_accuracy",
    "score_major_edit_outcome",
    "score_proposition_preservation",
    "score_quote_fidelity",
    "score_unsupported_claim_rate",
    "split_sentences",
]

MANIFEST_FILENAME = "manifest.json"
DEFAULT_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "draft_room"

#: Exact literal role values. These mirror the Draft Room database CHECK
#: constraint; nothing outside this set may appear in the manifest.
ROLES = ("manuscript", "reference", "style", "background", "challenge")

#: Exact literal authority values.
AUTHORITIES = ("primary", "official", "secondary", "user_asserted", "unknown")

#: Expected-status vocabulary for curated propositions.
STATUSES = (
    "supported",
    "contradicted",
    "ambiguous",
    "stale",
    "unsupported",
    "opinion",
)

#: Category vocabulary for curated propositions.
CATEGORIES = (
    "name",
    "number",
    "date",
    "obligation",
    "safety",
    "causality",
    "quotation",
    "other",
)

#: Categories that the high-stakes subset of propositions must cover in full.
REQUIRED_HIGH_STAKES_CATEGORIES = (
    "name",
    "number",
    "date",
    "obligation",
    "safety",
    "causality",
    "quotation",
)

#: Statuses whose propositions must NOT be asserted by a faithful rewrite.
DISTRACTOR_STATUSES = ("contradicted", "unsupported", "stale", "opinion")

_DOUBLE_QUOTES = "“”„‟«»″"
_SINGLE_QUOTES = "‘’‚‛′"
_DASHES = "‐‑‒–—―−"
_WHITESPACE_RE = re.compile(r"\s+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+")
_BLOCK_OPENER_RE = re.compile(r"\s*(?:#{1,6}\s|>\s|[-*+]\s|\d+\.\s|\||---|\*\*)")


class GoldCorpusError(ValueError):
    """Raised when the gold corpus manifest is malformed or inconsistent."""


# ---------------------------------------------------------------------------
# text normalization
# ---------------------------------------------------------------------------


def normalize_document_text(raw: bytes) -> str:
    """Decode fixture bytes into the canonical text that manifest offsets index.

    Inputs: ``raw`` — the exact bytes of a fixture file.
    Outputs: the UTF-8 decoding with ``\\r\\n`` and lone ``\\r`` folded to
    ``\\n``. Every ``start``/``end`` offset in the manifest is a character
    offset into this string.

    Deterministic and oracle-backed: this is a pure decoding rule, not a
    judgement of any kind.
    """
    return raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def normalize_quotes(text: str) -> str:
    """Fold typographic quotation marks onto their ASCII equivalents.

    Inputs: ``text`` — any string.
    Outputs: the same string with curly/angled double quotes rewritten to
    ``"``, curly single quotes rewritten to ``'``, and non-breaking spaces
    rewritten to ordinary spaces. Dashes, commas, semicolons, digits and
    wording are left untouched, so a near-quote trap that differs by
    punctuation or numeral form is still distinguishable from the real quote.

    Deterministic and oracle-backed: a fixed character mapping, not an LLM
    judge.
    """
    out = []
    for char in text:
        if char in _DOUBLE_QUOTES:
            out.append('"')
        elif char in _SINGLE_QUOTES:
            out.append("'")
        elif char == "\u00a0":
            out.append(" ")
        else:
            out.append(char)
    return "".join(out)


def normalize_for_match(text: str) -> str:
    """Fold text into the canonical form used for anchor and passage matching.

    Inputs: ``text`` — any string.
    Outputs: a casefolded string with typographic quotes folded to ASCII, every
    Unicode dash folded to ``-``, and every run of whitespace collapsed to a
    single space, with leading/trailing whitespace stripped.

    This normalization is intentionally more permissive than
    :func:`normalize_quotes`: it exists so that a rewrite may re-wrap lines and
    change capitalisation without being scored as having dropped a fact. It is
    never used for quote fidelity.

    Deterministic and oracle-backed: a fixed character mapping plus whitespace
    collapsing. No model is involved.
    """
    folded = normalize_quotes(text)
    folded = "".join("-" if char in _DASHES else char for char in folded)
    return _WHITESPACE_RE.sub(" ", folded).strip().casefold()


def split_sentences(text: str) -> tuple[str, ...]:
    """Split text into comparable sentence units.

    Inputs: ``text`` — any string.
    Outputs: a tuple of non-empty, whitespace-normalised sentence strings.

    Blocks are formed first: a blank line ends a block, and so does a line that
    opens a Markdown structure (heading, block quote, bullet, numbered item, or
    table row). Continuation lines are joined with a single space, so a
    hard-wrapped paragraph is not shredded at its line breaks. Each block is
    then split on ``.``, ``!``, ``?`` or ``;`` followed by whitespace.

    Deterministic and oracle-backed: a fixed regular-expression rule, not an
    LLM judge.
    """
    units: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if not buffer:
            return
        block = _WHITESPACE_RE.sub(" ", " ".join(buffer)).strip()
        buffer.clear()
        if not block:
            return
        for piece in _SENTENCE_SPLIT_RE.split(block):
            candidate = piece.strip()
            if candidate:
                units.append(candidate)

    for line in text.split("\n"):
        if not line.strip():
            flush()
            continue
        if _BLOCK_OPENER_RE.match(line):
            flush()
        buffer.append(line.strip())
    flush()
    return tuple(units)


# ---------------------------------------------------------------------------
# typed records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GoldDocument:
    """One synthetic source document in the corpus."""

    id: str
    path: str
    title: str
    role: str
    authority: str
    as_of_date: str | None
    vault: str
    scenario_ids: tuple[str, ...]
    sha256: str
    byte_length: int
    char_length: int


@dataclass(frozen=True)
class LockedSpan:
    """A span of a document whose wording may not be altered by a rewrite."""

    id: str
    document_id: str
    start: int
    end: int
    sha256: str
    reason: str


@dataclass(frozen=True)
class ExactQuote:
    """A quotation that must survive byte-identically (modulo quote glyphs)."""

    id: str
    document_id: str
    text: str
    start: int
    end: int
    sha256: str
    reason: str


@dataclass(frozen=True)
class NearQuoteTrap:
    """A passage that is almost a certified quote but differs materially."""

    id: str
    document_id: str
    text: str
    start: int
    end: int
    sha256: str
    trap_for_quote_id: str
    reason: str


@dataclass(frozen=True)
class Proposition:
    """A curated factual claim with its adjudicated expected status."""

    id: str
    text: str
    document_ids: tuple[str, ...]
    expected_status: str
    high_stakes: bool
    required: bool
    category: str
    anchors: tuple[str, ...]


@dataclass(frozen=True)
class ContradictionEvidence:
    """A literal passage proving one side of an expected contradiction."""

    document_id: str
    quote: str
    start: int
    end: int


@dataclass(frozen=True)
class ExpectedContradiction:
    """Two or more curated claims that genuinely disagree."""

    id: str
    proposition_ids: tuple[str, ...]
    document_ids: tuple[str, ...]
    topic: str
    reason: str
    evidence: tuple[ContradictionEvidence, ...]


@dataclass(frozen=True)
class InjectionString:
    """A prompt-injection payload literally embedded in a source document."""

    id: str
    document_id: str
    role: str
    payload: str
    kind: str
    start: int
    end: int


@dataclass(frozen=True)
class Scenario:
    """One of the ten evaluation scenarios the corpus must cover."""

    id: str
    description: str
    document_ids: tuple[str, ...]


@dataclass(frozen=True)
class GoldCorpus:
    """The fully validated corpus: metadata, records, and document texts."""

    root: Path
    schema_version: str
    rubric_version: str
    corpus_id: str
    documents: tuple[GoldDocument, ...]
    scenarios: tuple[Scenario, ...]
    locked_spans: tuple[LockedSpan, ...]
    exact_quotes: tuple[ExactQuote, ...]
    near_quote_traps: tuple[NearQuoteTrap, ...]
    propositions: tuple[Proposition, ...]
    contradictions: tuple[ExpectedContradiction, ...]
    injections: tuple[InjectionString, ...]
    reviewers: Mapping[str, object]
    texts: Mapping[str, str] = field(repr=False)

    # -- lookups ---------------------------------------------------------
    def document(self, document_id: str) -> GoldDocument:
        """Return the document with ``document_id`` or raise ``GoldCorpusError``."""
        for doc in self.documents:
            if doc.id == document_id:
                return doc
        raise GoldCorpusError(f"unknown document id: {document_id!r}")

    def text(self, document_id: str) -> str:
        """Return the normalized text of ``document_id``."""
        try:
            return self.texts[document_id]
        except KeyError:
            raise GoldCorpusError(f"unknown document id: {document_id!r}") from None

    def proposition(self, proposition_id: str) -> Proposition:
        """Return the proposition with ``proposition_id`` or raise."""
        for prop in self.propositions:
            if prop.id == proposition_id:
                return prop
        raise GoldCorpusError(f"unknown proposition id: {proposition_id!r}")

    def documents_by_role(self, role: str) -> tuple[GoldDocument, ...]:
        """Return every document carrying ``role``."""
        return tuple(doc for doc in self.documents if doc.role == role)

    def propositions_by_status(self, status: str) -> tuple[Proposition, ...]:
        """Return every proposition whose ``expected_status`` is ``status``."""
        return tuple(p for p in self.propositions if p.expected_status == status)

    @property
    def high_stakes_propositions(self) -> tuple[Proposition, ...]:
        """Every proposition flagged ``high_stakes``."""
        return tuple(p for p in self.propositions if p.high_stakes)

    @property
    def required_propositions(self) -> tuple[Proposition, ...]:
        """Every proposition flagged ``required``."""
        return tuple(p for p in self.propositions if p.required)


# ---------------------------------------------------------------------------
# loading / validation
# ---------------------------------------------------------------------------


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise GoldCorpusError(message)


def _get(mapping: Mapping[str, object], key: str, where: str) -> object:
    if key not in mapping:
        raise GoldCorpusError(f"{where}: missing key {key!r}")
    return mapping[key]


def _as_str_tuple(value: object, where: str) -> tuple[str, ...]:
    _require(isinstance(value, list), f"{where}: expected a list")
    assert isinstance(value, list)  # narrowing for type checkers
    for item in value:
        _require(isinstance(item, str), f"{where}: expected a list of strings")
    return tuple(str(item) for item in value)


def _check_unique(ids: Iterable[str], where: str) -> None:
    seen: set[str] = set()
    for identifier in ids:
        _require(identifier not in seen, f"{where}: duplicate id {identifier!r}")
        seen.add(identifier)


def _check_span(text: str, start: int, end: int, where: str) -> str:
    _require(
        isinstance(start, int) and isinstance(end, int),
        f"{where}: start and end must be integers",
    )
    _require(0 <= start < end <= len(text), f"{where}: span {start}..{end} out of bounds")
    return text[start:end]


def load_corpus(root: Path | str | None = None) -> GoldCorpus:
    """Read, validate and return the Draft Room gold corpus.

    Inputs: ``root`` — the fixtures directory containing ``manifest.json`` and
    the source documents. Defaults to ``backend/tests/fixtures/draft_room``.

    Outputs: a fully populated :class:`GoldCorpus` of frozen dataclass records
    plus the normalized text of every document.

    Raises :class:`GoldCorpusError` if the manifest is missing a required key,
    uses a role/authority/status/category outside the fixed vocabularies,
    repeats an id, references an id that does not exist, records a span that
    falls outside its document, records a span whose ``sha256`` does not match
    the spanned substring, or records a document ``sha256`` that does not match
    the file bytes.

    Deterministic and oracle-backed: validation is structural. No model is
    consulted and no heuristic is applied.
    """
    base = Path(root) if root is not None else DEFAULT_FIXTURES_DIR
    manifest_path = base / MANIFEST_FILENAME
    _require(manifest_path.is_file(), f"manifest not found: {manifest_path}")

    with manifest_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    _require(isinstance(data, dict), "manifest root must be a JSON object")

    schema_version = str(_get(data, "schema_version", "manifest"))
    rubric_version = str(_get(data, "rubric_version", "manifest"))
    corpus_id = str(_get(data, "corpus_id", "manifest"))
    _require(bool(schema_version), "manifest: schema_version must be non-empty")
    _require(bool(rubric_version), "manifest: rubric_version must be non-empty")

    # -- documents -------------------------------------------------------
    raw_documents = _get(data, "documents", "manifest")
    _require(isinstance(raw_documents, list) and raw_documents, "manifest: documents must be a non-empty list")
    documents: list[GoldDocument] = []
    texts: dict[str, str] = {}
    for entry in raw_documents:
        where = f"document {entry.get('id', '<no id>')!r}"
        _require(isinstance(entry, dict), "manifest: each document must be an object")
        doc_id = str(_get(entry, "id", where))
        rel_path = str(_get(entry, "path", where))
        role = str(_get(entry, "role", where))
        authority = str(_get(entry, "authority", where))
        _require(role in ROLES, f"{where}: role {role!r} not in {ROLES}")
        _require(authority in AUTHORITIES, f"{where}: authority {authority!r} not in {AUTHORITIES}")
        _require(not Path(rel_path).is_absolute(), f"{where}: path must be relative")
        _require(".." not in Path(rel_path).parts, f"{where}: path must not escape the corpus root")

        file_path = base / rel_path
        _require(file_path.is_file(), f"{where}: file not found: {rel_path}")
        raw = file_path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        recorded = str(_get(entry, "sha256", where))
        _require(digest == recorded, f"{where}: sha256 mismatch (file {digest}, manifest {recorded})")

        text = normalize_document_text(raw)
        as_of = entry.get("as_of_date")
        _require(as_of is None or isinstance(as_of, str), f"{where}: as_of_date must be a string or null")
        if isinstance(as_of, str):
            _require(
                bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of)),
                f"{where}: as_of_date {as_of!r} is not an ISO date",
            )

        documents.append(
            GoldDocument(
                id=doc_id,
                path=rel_path,
                title=str(entry.get("title", "")),
                role=role,
                authority=authority,
                as_of_date=as_of,
                vault=str(_get(entry, "vault", where)),
                scenario_ids=_as_str_tuple(_get(entry, "scenario_ids", where), where),
                sha256=recorded,
                byte_length=int(_get(entry, "byte_length", where)),
                char_length=int(_get(entry, "char_length", where)),
            )
        )
        texts[doc_id] = text

    _check_unique((doc.id for doc in documents), "documents")
    _check_unique((doc.path for doc in documents), "document paths")
    known_docs = {doc.id for doc in documents}

    for doc in documents:
        _require(
            doc.byte_length == len((base / doc.path).read_bytes()),
            f"document {doc.id!r}: byte_length mismatch",
        )
        _require(doc.char_length == len(texts[doc.id]), f"document {doc.id!r}: char_length mismatch")

    # -- scenarios -------------------------------------------------------
    scenarios: list[Scenario] = []
    for entry in _as_list(data, "scenarios"):
        where = f"scenario {entry.get('id', '<no id>')!r}"
        doc_ids = _as_str_tuple(_get(entry, "document_ids", where), where)
        for doc_id in doc_ids:
            _require(doc_id in known_docs, f"{where}: unknown document id {doc_id!r}")
        scenarios.append(
            Scenario(
                id=str(_get(entry, "id", where)),
                description=str(_get(entry, "description", where)),
                document_ids=doc_ids,
            )
        )
    _check_unique((s.id for s in scenarios), "scenarios")
    known_scenarios = {s.id for s in scenarios}
    for doc in documents:
        for scenario_id in doc.scenario_ids:
            _require(
                scenario_id in known_scenarios,
                f"document {doc.id!r}: unknown scenario id {scenario_id!r}",
            )

    # -- locked spans ----------------------------------------------------
    locked_spans: list[LockedSpan] = []
    for entry in _as_list(data, "locked_spans"):
        where = f"locked span {entry.get('id', '<no id>')!r}"
        doc_id = str(_get(entry, "document_id", where))
        _require(doc_id in known_docs, f"{where}: unknown document id {doc_id!r}")
        start = int(_get(entry, "start", where))
        end = int(_get(entry, "end", where))
        spanned = _check_span(texts[doc_id], start, end, where)
        recorded = str(_get(entry, "sha256", where))
        actual = hashlib.sha256(spanned.encode("utf-8")).hexdigest()
        _require(actual == recorded, f"{where}: sha256 mismatch (span {actual}, manifest {recorded})")
        locked_spans.append(
            LockedSpan(
                id=str(_get(entry, "id", where)),
                document_id=doc_id,
                start=start,
                end=end,
                sha256=recorded,
                reason=str(_get(entry, "reason", where)),
            )
        )
    _check_unique((s.id for s in locked_spans), "locked_spans")

    # -- exact quotes ----------------------------------------------------
    exact_quotes: list[ExactQuote] = []
    for entry in _as_list(data, "exact_quotes"):
        where = f"exact quote {entry.get('id', '<no id>')!r}"
        doc_id = str(_get(entry, "document_id", where))
        _require(doc_id in known_docs, f"{where}: unknown document id {doc_id!r}")
        start = int(_get(entry, "start", where))
        end = int(_get(entry, "end", where))
        spanned = _check_span(texts[doc_id], start, end, where)
        quote_text = str(_get(entry, "text", where))
        _require(spanned == quote_text, f"{where}: text does not match the spanned substring")
        recorded = str(_get(entry, "sha256", where))
        actual = hashlib.sha256(quote_text.encode("utf-8")).hexdigest()
        _require(actual == recorded, f"{where}: sha256 mismatch")
        exact_quotes.append(
            ExactQuote(
                id=str(_get(entry, "id", where)),
                document_id=doc_id,
                text=quote_text,
                start=start,
                end=end,
                sha256=recorded,
                reason=str(_get(entry, "reason", where)),
            )
        )
    _check_unique((q.id for q in exact_quotes), "exact_quotes")
    known_quotes = {q.id for q in exact_quotes}

    # -- near-quote traps ------------------------------------------------
    traps: list[NearQuoteTrap] = []
    for entry in _as_list(data, "near_quote_traps"):
        where = f"near-quote trap {entry.get('id', '<no id>')!r}"
        doc_id = str(_get(entry, "document_id", where))
        _require(doc_id in known_docs, f"{where}: unknown document id {doc_id!r}")
        start = int(_get(entry, "start", where))
        end = int(_get(entry, "end", where))
        spanned = _check_span(texts[doc_id], start, end, where)
        trap_text = str(_get(entry, "text", where))
        _require(spanned == trap_text, f"{where}: text does not match the spanned substring")
        recorded = str(_get(entry, "sha256", where))
        actual = hashlib.sha256(trap_text.encode("utf-8")).hexdigest()
        _require(actual == recorded, f"{where}: sha256 mismatch")
        trap_for = str(_get(entry, "trap_for_quote_id", where))
        _require(trap_for in known_quotes, f"{where}: unknown quote id {trap_for!r}")
        _require(trap_text != _quote_text(exact_quotes, trap_for), f"{where}: trap is identical to its quote")
        traps.append(
            NearQuoteTrap(
                id=str(_get(entry, "id", where)),
                document_id=doc_id,
                text=trap_text,
                start=start,
                end=end,
                sha256=recorded,
                trap_for_quote_id=trap_for,
                reason=str(_get(entry, "reason", where)),
            )
        )
    _check_unique((t.id for t in traps), "near_quote_traps")

    # -- propositions ----------------------------------------------------
    propositions: list[Proposition] = []
    for entry in _as_list(data, "expected_propositions"):
        where = f"proposition {entry.get('id', '<no id>')!r}"
        status = str(_get(entry, "expected_status", where))
        category = str(_get(entry, "category", where))
        _require(status in STATUSES, f"{where}: expected_status {status!r} not in {STATUSES}")
        _require(category in CATEGORIES, f"{where}: category {category!r} not in {CATEGORIES}")
        doc_ids = _as_str_tuple(_get(entry, "document_ids", where), where)
        _require(bool(doc_ids), f"{where}: document_ids must be non-empty")
        for doc_id in doc_ids:
            _require(doc_id in known_docs, f"{where}: unknown document id {doc_id!r}")
        anchors = _as_str_tuple(_get(entry, "anchors", where), where)
        _require(bool(anchors), f"{where}: anchors must be non-empty")
        high_stakes = _get(entry, "high_stakes", where)
        required = _get(entry, "required", where)
        _require(isinstance(high_stakes, bool), f"{where}: high_stakes must be a boolean")
        _require(isinstance(required, bool), f"{where}: required must be a boolean")
        propositions.append(
            Proposition(
                id=str(_get(entry, "id", where)),
                text=str(_get(entry, "text", where)),
                document_ids=doc_ids,
                expected_status=status,
                high_stakes=bool(high_stakes),
                required=bool(required),
                category=category,
                anchors=anchors,
            )
        )
    _check_unique((p.id for p in propositions), "expected_propositions")
    known_props = {p.id for p in propositions}

    # -- contradictions --------------------------------------------------
    contradictions: list[ExpectedContradiction] = []
    for entry in _as_list(data, "expected_contradictions"):
        where = f"contradiction {entry.get('id', '<no id>')!r}"
        prop_ids = _as_str_tuple(_get(entry, "proposition_ids", where), where)
        _require(len(prop_ids) >= 2, f"{where}: needs at least two proposition ids")
        for prop_id in prop_ids:
            _require(prop_id in known_props, f"{where}: unknown proposition id {prop_id!r}")
        doc_ids = _as_str_tuple(_get(entry, "document_ids", where), where)
        _require(len(doc_ids) >= 2, f"{where}: needs at least two document ids")
        for doc_id in doc_ids:
            _require(doc_id in known_docs, f"{where}: unknown document id {doc_id!r}")
        evidence: list[ContradictionEvidence] = []
        for item in _get(entry, "evidence", where):
            _require(isinstance(item, dict), f"{where}: each evidence item must be an object")
            ev_doc = str(_get(item, "document_id", where))
            _require(ev_doc in known_docs, f"{where}: unknown evidence document id {ev_doc!r}")
            start = int(_get(item, "start", where))
            end = int(_get(item, "end", where))
            spanned = _check_span(texts[ev_doc], start, end, where)
            quote = str(_get(item, "quote", where))
            _require(spanned == quote, f"{where}: evidence quote does not match its span")
            evidence.append(
                ContradictionEvidence(document_id=ev_doc, quote=quote, start=start, end=end)
            )
        _require(len(evidence) >= 2, f"{where}: needs at least two evidence passages")
        contradictions.append(
            ExpectedContradiction(
                id=str(_get(entry, "id", where)),
                proposition_ids=prop_ids,
                document_ids=doc_ids,
                topic=str(_get(entry, "topic", where)),
                reason=str(_get(entry, "reason", where)),
                evidence=tuple(evidence),
            )
        )
    _check_unique((c.id for c in contradictions), "expected_contradictions")

    # -- injections ------------------------------------------------------
    injections: list[InjectionString] = []
    for entry in _as_list(data, "injection_strings"):
        where = f"injection {entry.get('id', '<no id>')!r}"
        doc_id = str(_get(entry, "document_id", where))
        _require(doc_id in known_docs, f"{where}: unknown document id {doc_id!r}")
        role = str(_get(entry, "role", where))
        _require(role in ROLES, f"{where}: role {role!r} not in {ROLES}")
        payload = str(_get(entry, "payload", where))
        _require(bool(payload), f"{where}: payload must be non-empty")
        start = int(_get(entry, "start", where))
        end = int(_get(entry, "end", where))
        spanned = _check_span(texts[doc_id], start, end, where)
        _require(spanned == payload, f"{where}: payload does not match its span")
        injections.append(
            InjectionString(
                id=str(_get(entry, "id", where)),
                document_id=doc_id,
                role=role,
                payload=payload,
                kind=str(_get(entry, "kind", where)),
                start=start,
                end=end,
            )
        )
    _check_unique((i.id for i in injections), "injection_strings")

    reviewers = _get(data, "reviewers", "manifest")
    _require(isinstance(reviewers, dict), "manifest: reviewers must be an object")
    assert isinstance(reviewers, dict)
    for key in ("process", "reviewer_a", "reviewer_b", "adjudicator", "sign_off"):
        _require(key in reviewers, f"manifest reviewers: missing key {key!r}")

    return GoldCorpus(
        root=base,
        schema_version=schema_version,
        rubric_version=rubric_version,
        corpus_id=corpus_id,
        documents=tuple(documents),
        scenarios=tuple(scenarios),
        locked_spans=tuple(locked_spans),
        exact_quotes=tuple(exact_quotes),
        near_quote_traps=tuple(traps),
        propositions=tuple(propositions),
        contradictions=tuple(contradictions),
        injections=tuple(injections),
        reviewers=reviewers,
        texts=texts,
    )


def _as_list(data: Mapping[str, object], key: str) -> list:
    value = _get(data, key, "manifest")
    _require(isinstance(value, list), f"manifest: {key} must be a list")
    assert isinstance(value, list)
    for item in value:
        _require(isinstance(item, dict), f"manifest: each {key} entry must be an object")
    return value


def _quote_text(quotes: Sequence[ExactQuote], quote_id: str) -> str:
    for quote in quotes:
        if quote.id == quote_id:
            return quote.text
    raise GoldCorpusError(f"unknown quote id: {quote_id!r}")


# ---------------------------------------------------------------------------
# scoring results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PropositionPreservationResult:
    """Outcome of :func:`score_proposition_preservation`."""

    total: int
    preserved_ids: tuple[str, ...]
    missing_ids: tuple[str, ...]
    required_total: int
    required_preserved_ids: tuple[str, ...]
    required_missing_ids: tuple[str, ...]
    preservation_rate: float
    required_preservation_rate: float


@dataclass(frozen=True)
class UnsupportedClaimResult:
    """Outcome of :func:`score_unsupported_claim_rate`."""

    asserted_supported_ids: tuple[str, ...]
    asserted_distractor_ids: tuple[str, ...]
    asserted_by_status: Mapping[str, tuple[str, ...]]
    total_asserted: int
    unsupported_claim_rate: float


@dataclass(frozen=True)
class QuoteFidelityResult:
    """Outcome of :func:`score_quote_fidelity`."""

    total: int
    matched_ids: tuple[str, ...]
    missing_ids: tuple[str, ...]
    trap_hit_ids: tuple[str, ...]
    fidelity_rate: float


@dataclass(frozen=True)
class CitationCheck:
    """Outcome of resolving one ``(label, passage)`` citation."""

    label: str
    passage: str
    resolved_document_id: str | None
    document_resolved: bool
    passage_found: bool
    exact_offset: int


@dataclass(frozen=True)
class CitationMatchResult:
    """Outcome of :func:`score_citation_accuracy`."""

    total: int
    checks: tuple[CitationCheck, ...]
    valid_count: int
    accuracy: float


@dataclass(frozen=True)
class MajorEditResult:
    """Outcome of :func:`score_major_edit_outcome`."""

    total_sentences: int
    changed_sentences: int
    unchanged_sentences: int
    changed_indexes: tuple[int, ...]
    fraction_changed: float
    similarity_threshold: float


# ---------------------------------------------------------------------------
# scoring functions
# ---------------------------------------------------------------------------


def _asserted(candidate_normalized: str, proposition: Proposition) -> bool:
    return all(
        normalize_for_match(anchor) in candidate_normalized for anchor in proposition.anchors
    )


def score_proposition_preservation(
    candidate_text: str,
    propositions: Sequence[Proposition],
) -> PropositionPreservationResult:
    """Measure which expected propositions survived into a candidate rewrite.

    Inputs:
      ``candidate_text`` — the rewritten/composed text produced by the system
      under evaluation.
      ``propositions`` — the curated propositions to look for. Callers normally
      pass the ``supported`` subset (for example
      ``corpus.propositions_by_status("supported")``) or
      ``corpus.high_stakes_propositions``.

    Outputs: a :class:`PropositionPreservationResult` naming which proposition
    ids were preserved, which went missing, the same split restricted to the
    ``required`` subset, and both rates in ``[0.0, 1.0]``. Empty input yields a
    rate of ``1.0`` (nothing was required, so nothing was lost).

    A proposition counts as preserved when **every** one of its curated anchor
    strings appears in the candidate under :func:`normalize_for_match` (case,
    line wrapping, quote glyphs and dash glyphs are ignored; wording, numerals
    and punctuation are not).

    This is an oracle-backed deterministic metric. The anchors are curated by
    two human reviewers with product-owner adjudication and stored in the
    manifest; scoring is literal substring containment. It is **not** an LLM
    judge and it never calls a model.
    """
    normalized = normalize_for_match(candidate_text)
    preserved: list[str] = []
    missing: list[str] = []
    required_preserved: list[str] = []
    required_missing: list[str] = []

    for prop in propositions:
        if _asserted(normalized, prop):
            preserved.append(prop.id)
            if prop.required:
                required_preserved.append(prop.id)
        else:
            missing.append(prop.id)
            if prop.required:
                required_missing.append(prop.id)

    total = len(propositions)
    required_total = len(required_preserved) + len(required_missing)
    return PropositionPreservationResult(
        total=total,
        preserved_ids=tuple(preserved),
        missing_ids=tuple(missing),
        required_total=required_total,
        required_preserved_ids=tuple(required_preserved),
        required_missing_ids=tuple(required_missing),
        preservation_rate=(len(preserved) / total) if total else 1.0,
        required_preservation_rate=(
            (len(required_preserved) / required_total) if required_total else 1.0
        ),
    )


def score_unsupported_claim_rate(
    candidate_text: str,
    propositions: Sequence[Proposition],
) -> UnsupportedClaimResult:
    """Measure how much of what a candidate asserts is not grounded in the corpus.

    Inputs:
      ``candidate_text`` — the rewritten/composed text under evaluation.
      ``propositions`` — the curated propositions to test against. Callers
      normally pass ``corpus.propositions`` so that both the grounded claims
      and the planted distractors are in scope.

    Outputs: an :class:`UnsupportedClaimResult` listing the ids of asserted
    ``supported`` propositions, the ids of asserted distractor propositions
    (``contradicted``, ``unsupported``, ``stale`` or ``opinion``), the same
    distractors bucketed by status, the total number of curated claims the
    candidate asserted, and ``unsupported_claim_rate`` = asserted distractors
    divided by total asserted claims. A candidate that asserts nothing scores
    ``0.0``.

    Note that ``ambiguous`` propositions are deliberately excluded from both
    numerator and denominator: the curation record adjudicated them as not
    decidable from the corpus, so asserting or omitting one is neither credited
    nor penalised here.

    This is an oracle-backed deterministic metric over curated anchors. It is
    **not** an LLM judge and it never calls a model.
    """
    normalized = normalize_for_match(candidate_text)
    supported_hits: list[str] = []
    distractor_hits: list[str] = []
    by_status: dict[str, list[str]] = {status: [] for status in DISTRACTOR_STATUSES}

    for prop in propositions:
        if prop.expected_status == "ambiguous":
            continue
        if not _asserted(normalized, prop):
            continue
        if prop.expected_status == "supported":
            supported_hits.append(prop.id)
        else:
            distractor_hits.append(prop.id)
            by_status[prop.expected_status].append(prop.id)

    total_asserted = len(supported_hits) + len(distractor_hits)
    return UnsupportedClaimResult(
        asserted_supported_ids=tuple(supported_hits),
        asserted_distractor_ids=tuple(distractor_hits),
        asserted_by_status={k: tuple(v) for k, v in by_status.items()},
        total_asserted=total_asserted,
        unsupported_claim_rate=(len(distractor_hits) / total_asserted) if total_asserted else 0.0,
    )


def score_quote_fidelity(
    candidate_text: str,
    quotes: Sequence[ExactQuote],
    traps: Sequence[NearQuoteTrap] = (),
) -> QuoteFidelityResult:
    """Measure whether certified quotations survived exactly.

    Inputs:
      ``candidate_text`` — the rewritten/composed text under evaluation.
      ``quotes`` — the certified quotations that must survive, normally
      ``corpus.exact_quotes``.
      ``traps`` — optional near-quote decoys, normally
      ``corpus.near_quote_traps``. Any trap literally present in the candidate
      is reported in ``trap_hit_ids``.

    Outputs: a :class:`QuoteFidelityResult` with the matched and missing quote
    ids, the ids of any near-quote traps the candidate reproduced, and
    ``fidelity_rate`` = matched / total (``1.0`` when there are no quotes).

    Matching is exact after :func:`normalize_quotes` only: typographic quotation
    marks and non-breaking spaces are folded to their ASCII forms on both sides,
    so a rewrite that re-typesets ``"`` as ``“`` still passes. Nothing else is
    normalised — a changed comma, a changed dash, a dropped article or a numeral
    substitution is a miss, which is exactly what the near-quote traps in the
    corpus are built to detect.

    This is an oracle-backed deterministic metric. It is **not** an LLM judge
    and it never calls a model.
    """
    normalized_candidate = normalize_quotes(candidate_text)
    matched: list[str] = []
    missing: list[str] = []
    for quote in quotes:
        if normalize_quotes(quote.text) in normalized_candidate:
            matched.append(quote.id)
        else:
            missing.append(quote.id)

    trap_hits = tuple(
        trap.id for trap in traps if normalize_quotes(trap.text) in normalized_candidate
    )
    total = len(quotes)
    return QuoteFidelityResult(
        total=total,
        matched_ids=tuple(matched),
        missing_ids=tuple(missing),
        trap_hit_ids=trap_hits,
        fidelity_rate=(len(matched) / total) if total else 1.0,
    )


def _resolve_label(corpus: GoldCorpus, label: str) -> str | None:
    needle = label.strip()
    for doc in corpus.documents:
        if needle in (doc.id, doc.path, doc.title):
            return doc.id
    folded = normalize_for_match(needle)
    if not folded:
        return None
    for doc in corpus.documents:
        if folded in (
            normalize_for_match(doc.id),
            normalize_for_match(doc.path),
            normalize_for_match(doc.title),
        ):
            return doc.id
    return None


def score_citation_accuracy(
    corpus: GoldCorpus,
    citations: Sequence[tuple[str, str]],
) -> CitationMatchResult:
    """Measure whether cited passages really occur in the documents they cite.

    Inputs:
      ``corpus`` — a loaded :class:`GoldCorpus`.
      ``citations`` — a sequence of ``(label, passage)`` pairs taken from the
      candidate output. ``label`` is whatever the system emitted to name its
      source: a document id, a corpus-relative path, or a document title.
      ``passage`` is the text the candidate attributed to that source.

    Outputs: a :class:`CitationMatchResult` containing one
    :class:`CitationCheck` per citation and ``accuracy`` = the fraction of
    citations that both resolved to a real corpus document **and** whose
    passage occurs in that document. An empty citation list scores ``1.0``.

    ``passage_found`` uses :func:`normalize_for_match`, so a citation is not
    penalised for re-wrapping lines or changing case. ``exact_offset`` is the
    character offset of the passage in the document's normalized text when the
    passage appears literally, and ``-1`` otherwise.

    This is an oracle-backed deterministic metric: it is literal containment
    against the fixture bytes. It is **not** an LLM judge and it never calls a
    model.
    """
    checks: list[CitationCheck] = []
    valid = 0
    for label, passage in citations:
        doc_id = _resolve_label(corpus, label)
        if doc_id is None:
            checks.append(
                CitationCheck(
                    label=label,
                    passage=passage,
                    resolved_document_id=None,
                    document_resolved=False,
                    passage_found=False,
                    exact_offset=-1,
                )
            )
            continue
        text = corpus.text(doc_id)
        found = normalize_for_match(passage) in normalize_for_match(text)
        offset = text.find(passage) if passage else -1
        checks.append(
            CitationCheck(
                label=label,
                passage=passage,
                resolved_document_id=doc_id,
                document_resolved=True,
                passage_found=found,
                exact_offset=offset,
            )
        )
        if found:
            valid += 1

    total = len(citations)
    return CitationMatchResult(
        total=total,
        checks=tuple(checks),
        valid_count=valid,
        accuracy=(valid / total) if total else 1.0,
    )


def score_major_edit_outcome(
    manuscript_text: str,
    candidate_text: str,
    similarity_threshold: float = 0.9,
) -> MajorEditResult:
    """Measure how much of a manuscript a rewrite substantively changed.

    Inputs:
      ``manuscript_text`` — the original manuscript, normally
      ``corpus.text("doc_manuscript_field_report")``.
      ``candidate_text`` — the rewritten text under evaluation.
      ``similarity_threshold`` — a manuscript sentence counts as *unchanged*
      when some candidate sentence matches it with a ``difflib`` similarity
      ratio at or above this value. Must be in ``(0.0, 1.0]``; defaults to
      ``0.9``.

    Outputs: a :class:`MajorEditResult` with the number of manuscript
    sentences, how many were substantively changed, the indexes of those
    sentences (into :func:`split_sentences` of the manuscript), and
    ``fraction_changed`` in ``[0.0, 1.0]``. An empty manuscript scores ``0.0``.

    Sentences are compared after :func:`normalize_for_match`, so re-wrapping and
    capitalisation are not counted as edits.

    This is an oracle-backed deterministic metric built on ``difflib``. It is
    **not** an LLM judge and it never calls a model.
    """
    if not 0.0 < similarity_threshold <= 1.0:
        raise ValueError("similarity_threshold must be in (0.0, 1.0]")

    source_sentences = split_sentences(manuscript_text)
    candidate_sentences = [normalize_for_match(s) for s in split_sentences(candidate_text)]

    changed: list[int] = []
    for index, sentence in enumerate(source_sentences):
        needle = normalize_for_match(sentence)
        best = 0.0
        for other in candidate_sentences:
            ratio = difflib.SequenceMatcher(None, needle, other).ratio()
            if ratio > best:
                best = ratio
                if best >= 1.0:
                    break
        if best < similarity_threshold:
            changed.append(index)

    total = len(source_sentences)
    return MajorEditResult(
        total_sentences=total,
        changed_sentences=len(changed),
        unchanged_sentences=total - len(changed),
        changed_indexes=tuple(changed),
        fraction_changed=(len(changed) / total) if total else 0.0,
        similarity_threshold=similarity_threshold,
    )
