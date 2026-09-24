"""Loader and validator for the deterministic retrieval gold corpus.

Test-support code only: this module must never import anything from
``backend/app`` (enforced by the contract-test suite, mirroring the
draft-room ``gold_corpus.py`` purity rule).

The corpus lives under ``backend/tests/fixtures/retrieval_gold``:

* ``manifest.json`` — document inventory with one sha256 per document,
  computed over the UTF-8 encoding of the *normalized* text (CRLF and lone
  CR folded to LF), so the manifest holds identically on LF and CRLF
  checkouts. The ``normalization.policy`` field declares this and the
  contract tests assert it.
* ``cases.json`` — query/expected-span cases. Every span carries its
  verbatim text (and a sha256 over that text); the loader re-derives every
  span from the document bytes and rejects any drift. ``cases.json`` is
  deliberately not byte-hashed in the manifest: it is protected
  structurally, which keeps the frozen ranking-degradation probe's
  sanctioned rewrite of a temp copy loader-valid.

Chunking policy (used by the tie guard and the retrieval harness): the
document text is split on blank lines into paragraphs, consecutive
paragraphs are merged while the running length stays within the scale
limit, an over-long paragraph is split on line breaks, and an over-long
line is split on sentence boundaries. The policy is deterministic and
shared by both scales ("768" and "1536") via the ``limit`` argument.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DEFAULT_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "retrieval_gold"

DOCUMENT_ROLES = (
    "controlling_spec",
    "superseded_spec",
    "competing_spec",
    "safety",
    "distributor_guidance",
    "site_memo",
    "timeline",
    "glossary",
    "permit",
    "opinion",
)

CASE_KINDS = (
    "exact_fact",
    "revision_marker",
    "vendor_confusion",
    "memo_quote",
    "morphological_variant",
    "not_in_corpus",
)

SCALE_LIMITS = {"768": 768, "1536": 1536}
SCALE_NAMES = tuple(SCALE_LIMITS)


class RetrievalGoldCorpusError(ValueError):
    """Raised when the retrieval gold corpus fails validation."""


def normalize_text(raw: bytes) -> str:
    """Decode UTF-8 and fold CRLF and lone CR to LF (the declared policy)."""
    text = raw.decode("utf-8")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def normalized_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_text(text: str, limit: int) -> List[str]:
    """Deterministically split normalized document text into chunk units.

    Split on blank lines, merge consecutive paragraphs while the running
    length stays within ``limit``, split over-long paragraphs on line
    breaks, and split over-long lines on sentence boundaries.
    """
    paragraphs = [part.strip("\n") for part in text.split("\n\n")]
    paragraphs = [p for p in paragraphs if p.strip()]
    units: List[str] = []
    buffer = ""
    for paragraph in paragraphs:
        candidate = paragraph if not buffer else buffer + "\n" + paragraph
        if len(candidate) <= limit:
            buffer = candidate
            continue
        if buffer:
            units.append(buffer)
            buffer = ""
        if len(paragraph) <= limit:
            units.append(paragraph)
            continue
        line_buffer = ""
        for line in paragraph.split("\n"):
            line_candidate = line if not line_buffer else line_buffer + "\n" + line
            if len(line_candidate) <= limit:
                line_buffer = line_candidate
                continue
            if line_buffer:
                units.append(line_buffer)
                line_buffer = ""
            if len(line) <= limit:
                units.append(line)
                continue
            sentence_buffer = ""
            for sentence in line.split(". "):
                sent_candidate = (
                    sentence if not sentence_buffer else sentence_buffer + ". " + sentence
                )
                if len(sent_candidate) <= limit:
                    sentence_buffer = sent_candidate
                    continue
                if sentence_buffer:
                    units.append(sentence_buffer)
                    sentence_buffer = ""
                units.append(sentence)
            if sentence_buffer:
                units.append(sentence_buffer)
        if line_buffer:
            units.append(line_buffer)
    if buffer:
        units.append(buffer)
    return [unit for unit in units if unit.strip()]


@dataclass(frozen=True)
class GoldDocument:
    id: str
    path: str
    title: str
    role: str
    sha256: str
    char_length: int
    text: str
    superseded_by: Optional[str] = None


@dataclass(frozen=True)
class GoldSpan:
    start: int
    end: int
    text: str
    sha256: str


@dataclass(frozen=True)
class GoldTrapSpan:
    document: str
    start: int
    end: int
    text: str
    sha256: str


@dataclass(frozen=True)
class GoldCase:
    id: str
    kind: str
    query: str
    expected_doc: str
    expected_span: Optional[GoldSpan]
    must_not_match: Tuple[str, ...] = field(default_factory=tuple)
    trap_span: Optional[GoldTrapSpan] = None
    also_expected: Tuple[str, ...] = field(default_factory=tuple)
    notes: str = ""


@dataclass(frozen=True)
class RetrievalGoldCorpus:
    documents: Tuple[GoldDocument, ...]
    cases: Tuple[GoldCase, ...]
    normalization_policy: str

    def document(self, path: str) -> GoldDocument:
        for document in self.documents:
            if document.path == path:
                return document
        raise RetrievalGoldCorpusError(f"unknown document path: {path}")

    def text(self, path: str) -> str:
        return self.document(path).text

    def case(self, case_id: str) -> GoldCase:
        for case in self.cases:
            if case.id == case_id:
                return case
        raise RetrievalGoldCorpusError(f"unknown case id: {case_id}")

    def documents_by_role(self, role: str) -> Tuple[GoldDocument, ...]:
        return tuple(d for d in self.documents if d.role == role)

    def cases_by_kind(self, kind: str) -> Tuple[GoldCase, ...]:
        return tuple(c for c in self.cases if c.kind == kind)

    @property
    def trap_cases(self) -> Tuple[GoldCase, ...]:
        return tuple(c for c in self.cases if c.must_not_match)

    def probe_case(self) -> GoldCase:
        """The first case with both expected_doc and must_not_match.

        This is the case the frozen ranking-degradation probe selects and
        the only case whose trap document must carry the expected span at
        identical offsets (the loader-clean swap property).
        """
        for case in self.cases:
            if case.kind != "not_in_corpus" and case.expected_doc and case.must_not_match:
                return case
        raise RetrievalGoldCorpusError("no probe case (expected_doc + must_not_match) in corpus")


def _require(mapping: dict, key: str, where: str):
    if key not in mapping:
        raise RetrievalGoldCorpusError(f"{where}: missing key {key!r}")
    return mapping[key]


def _check_span(span: dict, text: str, where: str, offset_guard: bool = True) -> GoldSpan:
    start = _require(span, "start", where)
    end = _require(span, "end", where)
    span_text = _require(span, "text", where)
    if (
        type(start) is not int
        or type(end) is not int
        or not isinstance(span_text, str)
    ):
        # exact int types: bool is an int subclass and must not pass
        raise RetrievalGoldCorpusError(f"{where}: span fields have wrong types")
    if not 0 <= start < end <= len(text):
        raise RetrievalGoldCorpusError(
            f"{where}: span {start}..{end} out of bounds (document length {len(text)})"
        )
    actual = text[start:end]
    if actual != span_text:
        raise RetrievalGoldCorpusError(
            f"{where}: span text not verbatim at offsets {start}..{end} "
            f"(document has {actual[:40]!r}, case records {span_text[:40]!r})"
        )
    recorded_sha = span.get("sha256")
    expected_sha = normalized_sha256(span_text)
    if recorded_sha is not None and recorded_sha != expected_sha:
        raise RetrievalGoldCorpusError(
            f"{where}: span sha256 mismatch (recomputed {expected_sha}, recorded {recorded_sha})"
        )
    if offset_guard and text.find(span_text) != start:
        raise RetrievalGoldCorpusError(
            f"{where}: span text occurs in the document before its recorded start offset"
        )
    return GoldSpan(start=start, end=end, text=span_text, sha256=expected_sha)


def _validate_path(path: str, where: str) -> None:
    if not isinstance(path, str) or not path:
        raise RetrievalGoldCorpusError(f"{where}: path must be a non-empty string")
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise RetrievalGoldCorpusError(f"{where}: path must be relative and contained: {path!r}")
    if "|" in path:
        # "|" is the retrieval harness's chunk-id separator
        # (f"{path}|{scale}|{index}"); a path containing it would corrupt the
        # round-trip between manifest entries and store chunk ids.
        raise RetrievalGoldCorpusError(f"{where}: path cannot contain '|': {path!r}")


def load_corpus(root: Optional[Path] = None) -> RetrievalGoldCorpus:
    """Load and validate the corpus.

    Fixture resolution order: the explicit ``root`` argument, then the
    ``RETRIEVAL_GOLD_ROOT`` environment variable (the seam the frozen
    ranking-degradation probe sets for a fresh pytest process), then the
    default fixtures directory.
    """
    if root is not None:
        base = Path(root)
    else:
        base = Path(os.environ.get("RETRIEVAL_GOLD_ROOT", DEFAULT_FIXTURES_DIR))
    manifest_path = base / "manifest.json"
    if not manifest_path.is_file():
        raise RetrievalGoldCorpusError(f"manifest not found: {manifest_path}")

    with manifest_path.open("r", encoding="utf-8") as fh:
        try:
            manifest = json.load(fh)
        except json.JSONDecodeError as exc:
            raise RetrievalGoldCorpusError(f"manifest: invalid JSON: {exc}") from exc

    where = "manifest"
    if _require(manifest, "schema_version", where) != "1.0.0":
        raise RetrievalGoldCorpusError(f"{where}: unsupported schema_version")
    _require(manifest, "corpus_id", where)
    normalization = _require(manifest, "normalization", where)
    policy = _require(normalization, "policy", where)
    if policy != "crlf-to-lf":
        raise RetrievalGoldCorpusError(
            f"{where}: normalization.policy must be 'crlf-to-lf' (recorded {policy!r})"
        )
    roles = tuple(_require(manifest, "roles", where))
    unknown_roles = [role for role in roles if role not in DOCUMENT_ROLES]
    if unknown_roles:
        raise RetrievalGoldCorpusError(
            f"{where}: roles outside the canonical vocabulary: {unknown_roles!r}"
        )
    documents_raw = _require(manifest, "documents", where)
    if not documents_raw or not isinstance(documents_raw, list):
        raise RetrievalGoldCorpusError(f"{where}: documents must be a non-empty list")

    seen_doc_ids: set = set()
    seen_paths: set = set()
    documents: List[GoldDocument] = []
    for entry in documents_raw:
        entry_where = f"manifest.documents[{len(documents)}]"
        doc_id = _require(entry, "id", entry_where)
        path = _require(entry, "path", entry_where)
        _validate_path(path, entry_where)
        if doc_id in seen_doc_ids:
            raise RetrievalGoldCorpusError(f"{entry_where}: duplicate document id {doc_id!r}")
        if path in seen_paths:
            raise RetrievalGoldCorpusError(f"{entry_where}: duplicate document path {path!r}")
        seen_doc_ids.add(doc_id)
        seen_paths.add(path)
        role = _require(entry, "role", entry_where)
        if role not in roles:
            raise RetrievalGoldCorpusError(f"{entry_where}: unknown role {role!r}")
        doc_file = base / path
        # Containment is checked on the RESOLVED path: platform quirks (e.g.
        # Windows drive-relative "C:foo.md" paths, whose is_absolute() is
        # False) must not let a manifest entry read outside the fixtures root.
        if not doc_file.is_file() or not doc_file.resolve().is_relative_to(
            base.resolve()
        ):
            raise RetrievalGoldCorpusError(
                f"{entry_where}: fixture file missing or outside the fixtures root: {path}"
            )
        text = normalize_text(doc_file.read_bytes())
        digest = normalized_sha256(text)
        recorded = _require(entry, "sha256", entry_where)
        if recorded != digest:
            raise RetrievalGoldCorpusError(
                f"{entry_where}: sha256 mismatch (file {digest}, manifest {recorded})"
            )
        char_length = _require(entry, "char_length", entry_where)
        if char_length != len(text):
            raise RetrievalGoldCorpusError(
                f"{entry_where}: char_length mismatch (file {len(text)}, manifest {char_length})"
            )
        words = len(text.split())
        if not 300 <= words <= 1500:
            raise RetrievalGoldCorpusError(
                f"{entry_where}: word count {words} outside the 300-1500 band"
            )
        documents.append(
            GoldDocument(
                id=doc_id,
                path=path,
                title=_require(entry, "title", entry_where),
                role=role,
                sha256=digest,
                char_length=char_length,
                text=text,
                superseded_by=entry.get("superseded_by"),
            )
        )

    for document in documents:
        if document.superseded_by is not None and document.superseded_by not in seen_doc_ids:
            raise RetrievalGoldCorpusError(
                f"manifest: superseded_by target {document.superseded_by!r} is not a document id"
            )

    texts: Dict[str, str] = {document.path: document.text for document in documents}

    cases_path = base / "cases.json"
    if not cases_path.is_file():
        raise RetrievalGoldCorpusError(f"cases file not found: {cases_path}")
    with cases_path.open("r", encoding="utf-8") as fh:
        try:
            cases_doc = json.load(fh)
        except json.JSONDecodeError as exc:
            raise RetrievalGoldCorpusError(f"cases: invalid JSON: {exc}") from exc
    cases_where = "cases"
    if _require(cases_doc, "schema_version", cases_where) != "1.0.0":
        raise RetrievalGoldCorpusError(
            f"{cases_where}: unsupported schema_version"
        )
    cases_corpus_id = _require(cases_doc, "corpus_id", cases_where)
    if cases_corpus_id != manifest.get("corpus_id"):
        raise RetrievalGoldCorpusError(
            f"{cases_where}: corpus_id {cases_corpus_id!r} does not match the manifest's"
        )
    kinds = tuple(_require(cases_doc, "case_kinds", cases_where) or ())
    if not kinds:
        kinds = CASE_KINDS
    unknown_kinds = [kind for kind in kinds if kind not in CASE_KINDS]
    if unknown_kinds:
        raise RetrievalGoldCorpusError(
            f"{cases_where}: case_kinds outside the canonical vocabulary: {unknown_kinds!r}"
        )
    cases_raw = _require(cases_doc, "cases", "cases")

    seen_case_ids: set = set()
    cases: List[GoldCase] = []
    for entry in cases_raw:
        c_where = f"cases[{len(cases)}]"
        case_id = _require(entry, "id", c_where)
        if case_id in seen_case_ids:
            raise RetrievalGoldCorpusError(f"{c_where}: duplicate case id {case_id!r}")
        seen_case_ids.add(case_id)
        kind = _require(entry, "kind", c_where)
        if kind not in kinds:
            raise RetrievalGoldCorpusError(f"{c_where}: unknown kind {kind!r}")
        query = _require(entry, "query", c_where)
        if not isinstance(query, str) or not query.strip():
            raise RetrievalGoldCorpusError(f"{c_where}: query must be non-empty text")

        if kind == "not_in_corpus":
            if entry.get("expected_doc") not in ("", None) or entry.get("must_not_match"):
                raise RetrievalGoldCorpusError(
                    f"{c_where}: not_in_corpus case must carry no expected doc or traps"
                )
            cases.append(
                GoldCase(
                    id=case_id,
                    kind=kind,
                    query=query,
                    expected_doc="",
                    expected_span=None,
                    notes=entry.get("notes", ""),
                )
            )
            continue

        expected_doc = _require(entry, "expected_doc", c_where)
        if expected_doc not in texts:
            raise RetrievalGoldCorpusError(f"{c_where}: unknown expected_doc {expected_doc!r}")
        expected_text = texts[expected_doc]
        span_raw = _require(entry, "expected_span", c_where)
        expected_span = _check_span(span_raw, expected_text, f"{c_where}.expected_span")

        must_not_raw = _require(entry, "must_not_match", c_where)
        if not isinstance(must_not_raw, list):
            raise RetrievalGoldCorpusError(f"{c_where}: must_not_match must be a list")
        for trap in must_not_raw:
            if trap == expected_doc:
                raise RetrievalGoldCorpusError(
                    f"{c_where}: trap document equals expected document"
                )
            if trap not in texts:
                raise RetrievalGoldCorpusError(f"{c_where}: unknown trap document {trap!r}")

        trap_span = None
        if entry.get("trap_span") is not None:
            trap_raw = _require(entry, "trap_span", c_where)
            trap_doc = _require(trap_raw, "document", f"{c_where}.trap_span")
            if trap_doc not in must_not_raw:
                raise RetrievalGoldCorpusError(
                    f"{c_where}: trap_span.document {trap_doc!r} is not in must_not_match"
                )
            span = _check_span(trap_raw, texts[trap_doc], f"{c_where}.trap_span")
            if span.text == expected_span.text:
                raise RetrievalGoldCorpusError(
                    f"{c_where}: trap span text equals expected span text"
                )
            if expected_span.text in texts[trap_doc]:
                raise RetrievalGoldCorpusError(
                    f"{c_where}: expected span text occurs in trap document {trap_doc!r} "
                    "(must_not_match with trap_span must be absent from the trap document)"
                )
            trap_span = GoldTrapSpan(
                document=trap_doc, start=span.start, end=span.end, text=span.text, sha256=span.sha256
            )

        also_raw = entry.get("also_expected", [])
        if not isinstance(also_raw, list):
            raise RetrievalGoldCorpusError(f"{c_where}: also_expected must be a list")
        for extra in also_raw:
            if extra not in texts:
                raise RetrievalGoldCorpusError(f"{c_where}: unknown also_expected document {extra!r}")

        cases.append(
            GoldCase(
                id=case_id,
                kind=kind,
                query=query,
                expected_doc=expected_doc,
                expected_span=expected_span,
                must_not_match=tuple(must_not_raw),
                trap_span=trap_span,
                also_expected=tuple(also_raw),
                notes=entry.get("notes", ""),
            )
        )

    if not 20 <= len(cases) <= 50:
        raise RetrievalGoldCorpusError(
            f"cases: count {len(cases)} outside the 20-50 band"
        )
    traps = [case for case in cases if case.must_not_match]
    if len(traps) < len(cases) / 3:
        raise RetrievalGoldCorpusError(
            f"cases: {len(traps)} trap cases below the one-third floor ({len(cases)}/3)"
        )
    trap_span_cases = [case for case in traps if case.trap_span is not None]
    if len(trap_span_cases) * 2 <= len(traps):
        raise RetrievalGoldCorpusError(
            "cases: trap_span cases must be the clear majority of trap cases"
        )
    if not any(case.kind == "not_in_corpus" for case in cases):
        raise RetrievalGoldCorpusError("cases: at least one not_in_corpus case is required")

    # Tier-1 contract: every trap case WITHOUT a trap_span (doc-level trap)
    # must carry its expected span in its trap document at the SAME offsets —
    # this is what guarantees the frozen ranking-degradation probe's swap is
    # loader-clean. trap_span cases are exempt (their expected span text must
    # be ABSENT from the trap document instead, checked during case loading).
    for case in cases:
        if case.kind == "not_in_corpus" or not case.must_not_match or case.trap_span is not None:
            continue
        for trap in case.must_not_match:
            if texts[trap].find(case.expected_span.text) != case.expected_span.start:
                raise RetrievalGoldCorpusError(
                    f"cases: tier-1 case {case.id} trap document {trap!r} does not carry the "
                    "expected span at identical offsets (loader-clean swap property)"
                )

    seen_units: Dict[str, str] = {}
    for document in documents:
        for scale, limit in SCALE_LIMITS.items():
            for unit in chunk_text(document.text, limit):
                if unit in seen_units and seen_units[unit] != document.path:
                    raise RetrievalGoldCorpusError(
                        f"tie guard: identical {scale}-scale chunk unit in "
                        f"{seen_units[unit]} and {document.path}: {unit[:50]!r}"
                    )
                seen_units[unit] = document.path

    return RetrievalGoldCorpus(
        documents=tuple(documents),
        cases=tuple(cases),
        normalization_policy=policy,
    )
