"""
Parser-neutral "document atom" foundation for multimodal RAG (issue #460).

This module is deliberately *pure and side-effect free*: it defines validated
value objects, adapts parser output (currently Unstructured) into ordered typed
atoms, and projects atoms back to plain text. It performs no segmentation, no
vector work, no indexing, no database writes, no filesystem access, and schedules
no background jobs — so both the main ingestion path and the parse-only
:mod:`app.services.document_extraction` seam can share it without side effects.

The canonical term is **document atom**: the smallest unit of a parsed document
carrying a stable opaque identity, an ordinal, a checked kind, and immutable raw
evidence appropriate to that kind. Public/API consumers never receive storage
paths or raw parser objects.

Design notes (issue #460):
- Supported kinds in v1: ``title``, ``text``, ``list``, ``image``, ``chart``,
  ``table``, ``equation``, ``code``. Unknown parser categories degrade to the
  safe ``unknown`` kind with a warning — never a crash or an unchecked category.
- ``atom_id`` is opaque and stable *within one source generation*.
- Raw evidence is immutable; generated descriptions are NOT part of this PR and
  are never written into raw fields.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants / versioning
# ---------------------------------------------------------------------------

# Atom schema version. Bump when the persisted shape of an atom changes in a way
# that is not backward-readable. Part of the generation fingerprint so a schema
# change invalidates prior generations.
ATOM_SCHEMA_VERSION = 1

# Bounded warning/metadata lists, so a hostile parser cannot balloon storage.
MAX_WARNINGS_PER_ATOM = 32
MAX_METADATA_KEYS_PER_ATOM = 64

# Canonical parser identity used in generation fingerprints.
DEFAULT_PARSER_NAME = "unstructured"

# The set of extensions treated as standalone raster images across the whole
# ingestion pipeline (config allowlist, magic checks, content validation,
# processor dispatch). Single source of truth for "what is an image".
RASTER_IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp"}
)


class AtomKind(str, Enum):
    """Checked atom kind. ``UNKNOWN`` is the safe degradation target."""

    TITLE = "title"
    TEXT = "text"
    LIST = "list"
    IMAGE = "image"
    CHART = "chart"
    TABLE = "table"
    EQUATION = "equation"
    CODE = "code"
    UNKNOWN = "unknown"


# Map of Unstructured element categories to canonical atom kinds. Anything not
# listed degrades to AtomKind.UNKNOWN with a warning (never crashes).
_CATEGORY_TO_KIND: dict[str, AtomKind] = {
    "Title": AtomKind.TITLE,
    "NarrativeText": AtomKind.TEXT,
    "Text": AtomKind.TEXT,
    "UnformattedText": AtomKind.TEXT,
    "ListItem": AtomKind.LIST,
    "BulletedText": AtomKind.LIST,
    "Image": AtomKind.IMAGE,
    "Picture": AtomKind.IMAGE,
    "FigureCaption": AtomKind.TEXT,
    "Table": AtomKind.TABLE,
    "TableChunk": AtomKind.TABLE,
    "Equation": AtomKind.EQUATION,
    "CodeSnippet": AtomKind.CODE,
    "Chart": AtomKind.CHART,
}

# Categories that carry structured content we persist verbatim as raw evidence
# only (never synthesized descriptions).
_PRESERVE_VERBATIM = {AtomKind.TABLE, AtomKind.CODE, AtomKind.EQUATION}

# Bbox coordinate system value when points come from Unstructured coordinates.
_COORD_SYSTEM = "unstructured_points"


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DocumentAsset:
    """A validated binary asset extracted from a document.

    ``asset_id`` is the opaque identity referenced by atoms and vector metadata;
    ``rel_path`` is a validated relative storage path resolved only against the
    configured vault artifact root by the storage layer. No caller-supplied
    absolute path is ever accepted.
    """

    asset_id: str
    file_id: int
    generation_hash: str
    sha256: str
    rel_path: str
    mime_type: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    byte_size: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DocumentAtom:
    """A validated, ordered, typed unit of parsed document evidence.

    All fields are immutable (frozen). ``raw_text`` is the atomic raw evidence:
    for text-like kinds it is the element text; for tables it is the cell/HTML
    text; for equations it is the LaTeX; for images it is the OCR/searchable text.
    """

    atom_id: str
    schema_version: int
    file_id: int
    generation_hash: str
    ordinal: int
    kind: AtomKind
    raw_text: str
    page_number: Optional[int] = None
    bbox: Optional[dict[str, float]] = None
    bbox_coord_system: Optional[str] = None
    section_path: tuple[str, ...] = ()
    caption: Optional[str] = None
    parent_atom_id: Optional[str] = None
    asset_id: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    parser_fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain validated dict for persistence.

        ``metadata`` and ``warnings`` are bounded copies so a hostile parser
        cannot balloon the persisted payload. ``section_path`` becomes a list.
        """
        return {
            "atom_id": self.atom_id,
            "schema_version": self.schema_version,
            "file_id": self.file_id,
            "generation_hash": self.generation_hash,
            "ordinal": self.ordinal,
            "kind": self.kind.value,
            "raw_text": self.raw_text,
            "page_number": self.page_number,
            "bbox": self.bbox,
            "bbox_coord_system": self.bbox_coord_system,
            "section_path": list(self.section_path),
            "caption": self.caption,
            "parent_atom_id": self.parent_atom_id,
            "asset_id": self.asset_id,
            "metadata": _bounded_metadata(self.metadata),
            "warnings": list(self.warnings)[:MAX_WARNINGS_PER_ATOM],
            "parser_fingerprint": self.parser_fingerprint,
        }


@dataclass(frozen=True)
class ParsedDocument:
    """The single internal result of parsing, before chunking.

    Every format adapter (documents, images, spreadsheets, schemas) produces one
    of these so ``process_file`` and ``process_existing_file`` share one pipeline
    (issue #460 — no parallel parser path).
    """

    atoms: tuple[DocumentAtom, ...]
    raw_projection: str = ""
    normalized_projection: str = ""
    parser_fingerprint: str = ""
    assets: tuple[DocumentAsset, ...] = ()


# ---------------------------------------------------------------------------
# Fingerprinting / identity
# ---------------------------------------------------------------------------


def compute_generation_hash(
    source_file_hash: str,
    *,
    parser_name: str = DEFAULT_PARSER_NAME,
    parser_version: str = "",
    config_version: str = "",
    schema_version: int = ATOM_SCHEMA_VERSION,
) -> str:
    """Deterministic fingerprint of one source generation.

    Keyed by the file-byte hash plus the parser implementation/config/schema
    versions that affect atom production. Uses a canonical JSON encoding so the
    same inputs always yield the same fingerprint regardless of dict ordering.
    """
    payload = json.dumps(
        {
            "source_file_hash": source_file_hash,
            "parser_name": parser_name,
            "parser_version": parser_version,
            "config_version": config_version,
            "schema_version": schema_version,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def make_atom_id(file_id: int, generation_hash: str, ordinal: int) -> str:
    """Opaque, deterministic, generation-local atom id.

    Stable within one source generation: the same (file, generation, ordinal)
    always yields the same id, so reprocessing an unchanged generation is
    idempotent. IDs are intentionally generation-local and may change across
    generations (a later inserted element re-sequences ordinals).
    """
    payload = json.dumps(
        [str(file_id), generation_hash, ordinal],
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Text projection
# ---------------------------------------------------------------------------


def raw_join(atoms: Iterable[DocumentAtom]) -> str:
    """Un-normalized ordered join used by main ingestion.

    Reproduces ``DocumentProcessor._process_document_file``'s historical
    ``"\\n".join(str(e) for e in elements)`` so parent-window offset matching and
    contextual chunking behavior are preserved byte-for-byte.
    """
    return "\n".join(a.raw_text for a in atoms)


def _normalize(text: str) -> str:
    """Normalize line endings to ``\\n`` and strip trailing whitespace per line.

    Mirrors ``document_extraction._normalize`` exactly so the parse-only seam and
    ingestion share one definition of normalized text.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in normalized.split("\n"))


def project_text(atoms: Iterable[DocumentAtom]) -> str:
    """Pure normalized text projection of an ordered atom list.

    Used by :class:`DocumentExtractionService` and by ingestion where normalized
    text is required. Equals ``_normalize(raw_join(atoms))``; ordering is the
    atom order.
    """
    return _normalize(raw_join(atoms))


# ---------------------------------------------------------------------------
# Adapter: Unstructured -> atoms
# ---------------------------------------------------------------------------


def _element_category(element: Any) -> str:
    return getattr(element, "category", None) or type(element).__name__


def _element_page_number(element: Any) -> Optional[int]:
    meta = getattr(element, "metadata", None)
    if meta is None:
        return None
    page = getattr(meta, "page_number", None)
    try:
        return int(page) if page is not None else None
    except (TypeError, ValueError):
        return None


def _element_bbox(element: Any) -> tuple[Optional[dict[str, float]], Optional[str]]:
    """Normalize element coordinates to an axis-aligned bbox.

    Mirrors ``SemanticChunker._capture_bbox`` semantics (and is equally safe when
    Unstructured is stubbed in CI): returns ``(None, None)`` on absence/malformed.
    """
    meta = getattr(element, "metadata", None)
    if meta is None:
        return None, None
    coords = getattr(meta, "coordinates", None)
    if coords is None:
        return None, None
    points = getattr(coords, "points", None)
    if not points or len(points) < 2:
        return None, None
    try:
        xs = [float(p[0]) for p in points]
        ys = [float(p[1]) for p in points]
    except (TypeError, ValueError, IndexError):
        return None, None
    bbox = {
        "left": min(xs),
        "top": min(ys),
        "right": max(xs),
        "bottom": max(ys),
    }
    return bbox, _COORD_SYSTEM


def _element_section_path(element: Any) -> tuple[str, ...]:
    meta = getattr(element, "metadata", None)
    if meta is None:
        return ()
    section = getattr(meta, "section", None)
    if section:
        return (str(section),)
    page_name = getattr(meta, "page_name", None)
    if page_name:
        return (str(page_name),)
    filename = getattr(meta, "filename", None)
    if filename:
        return (str(filename),)
    return ()


def _element_caption(element: Any) -> Optional[str]:
    meta = getattr(element, "metadata", None)
    if meta is None:
        return None
    caption = getattr(meta, "caption", None) or getattr(meta, "text_as_html", None)
    return str(caption) if caption else None


def _bounded_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Return a bounded copy of arbitrary parser metadata.

    Keeps only the first :data:`MAX_METADATA_KEYS_PER_ATOM` keys and coerces
    values to JSON-serializable primitives. Non-serializable values are dropped
    (a parser quirk must never break persistence).
    """
    out: dict[str, Any] = {}
    for key, value in list(metadata.items())[:MAX_METADATA_KEYS_PER_ATOM]:
        try:
            json.dumps(value)
            out[str(key)] = value
        except (TypeError, ValueError):
            continue
    return out


def parse_elements_to_atoms(
    elements: Iterable[Any],
    *,
    file_id: int,
    generation_hash: str,
    parser_fingerprint: str,
) -> list[DocumentAtom]:
    """Adapt raw parser elements into ordered, validated :class:`DocumentAtom`.

    Unknown categories degrade to :data:`AtomKind.UNKNOWN` with a warning rather
    than crashing or persisting an unchecked category. ``raw_text`` is captured
    from ``str(element)`` *before* any metadata access so a malformed element can
    never change the projected text.
    """
    atoms: list[DocumentAtom] = []
    for ordinal, element in enumerate(elements):
        warnings: list[str] = []
        try:
            raw_text = str(element)
        except Exception as exc:  # noqa: BLE001 — element str() must not crash
            logger.warning("Element str() failed at ordinal %d: %s", ordinal, exc)
            raw_text = ""
            warnings.append("element_str_failed")

        category = _element_category(element)
        kind = _CATEGORY_TO_KIND.get(category, AtomKind.UNKNOWN)
        if kind is AtomKind.UNKNOWN and category not in _CATEGORY_TO_KIND:
            warnings.append(f"unknown_category:{category}")

        bbox, coord_system = _element_bbox(element)

        atoms.append(
            DocumentAtom(
                atom_id=make_atom_id(file_id, generation_hash, ordinal),
                schema_version=ATOM_SCHEMA_VERSION,
                file_id=file_id,
                generation_hash=generation_hash,
                ordinal=ordinal,
                kind=kind,
                raw_text=raw_text,
                page_number=_element_page_number(element),
                bbox=bbox,
                bbox_coord_system=coord_system,
                section_path=_element_section_path(element),
                caption=_element_caption(element),
                parser_fingerprint=parser_fingerprint,
                metadata=_bounded_metadata(_element_extra_metadata(element)),
                warnings=warnings,
            )
        )
    return atoms


def _element_extra_metadata(element: Any) -> dict[str, Any]:
    """Best-effort, bounded extra metadata an element exposes.

    Only a small allowlist of known-safe scalar attributes are copied so we never
    persist raw parser objects. Failures are swallowed (non-fatal).
    """
    out: dict[str, Any] = {}
    meta = getattr(element, "metadata", None)
    for attr in ("languages", "filetype", "data_source", "orig_elements", "is_continuation"):
        value = getattr(meta, attr, None)
        if value is not None:
            try:
                json.dumps(value)
                out[attr] = value
            except (TypeError, ValueError):
                continue
    return out


def validate_generation_atoms(atoms: Iterable[DocumentAtom]) -> Optional[str]:
    """Validate a full generation's atom list before publication.

    Returns an error string if invalid, else ``None``. Enforces: ordinals exactly
    ``0..n-1``, unique ids, non-empty raw text requirement for verbatim kinds,
    resolvable parent references within the generation, and JSON-serializability.
    """
    atoms = list(atoms)
    seen_ids: set[str] = set()
    for ordinal, atom in enumerate(atoms):
        if atom.ordinal != ordinal:
            return f"atom ordinal out of order at {ordinal}: got {atom.ordinal}"
        if atom.atom_id in seen_ids:
            return f"duplicate atom_id {atom.atom_id}"
        seen_ids.add(atom.atom_id)
        if atom.kind in _PRESERVE_VERBATIM and not atom.raw_text.strip():
            return f"verbatim atom {atom.atom_id} has empty raw text"
        if not isinstance(atom.ordinal, int) or atom.ordinal < 0:
            return f"invalid ordinal on atom {atom.atom_id}"
        try:
            json.dumps(atom.to_dict())
        except (TypeError, ValueError):
            return f"atom {atom.atom_id} is not JSON-serializable"
    valid_ids = {a.atom_id for a in atoms}
    for atom in atoms:
        parent = atom.parent_atom_id
        if parent is not None and parent not in valid_ids:
            return f"atom {atom.atom_id} parent {parent} outside generation"
    return None
