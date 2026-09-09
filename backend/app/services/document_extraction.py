"""
Parse-only text extraction seam.

Turns a file on disk into normalized plain text plus a small amount of
metadata. This module is deliberately inert: it reads the path it is given
through :class:`DocumentParser` and returns a value object. It performs no
segmentation, no vector work, no indexing, no database writes, and schedules
no background jobs, so callers can extract text without triggering ingestion.
"""

import logging
import mimetypes
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel

from app.services.document_artifacts import parse_elements_to_atoms, project_text
from app.services.document_processor import DocumentParser

logger = logging.getLogger(__name__)

# Extraction error messages are persisted to a database column and logged, so
# they are capped at this many characters and never carry parser output.
MAX_ERROR_MESSAGE_CHARS = 200

# Fallback when the extension maps to no known media type.
DEFAULT_MEDIA_TYPE = "application/octet-stream"

# Stable, machine-readable failure codes.
CODE_INPUT_FILE_MISSING = "input_file_missing"
CODE_INPUT_PARSE_FAILED = "input_parse_failed"

# Short, content-free reasons paired with each code.
_REASONS = {
    CODE_INPUT_FILE_MISSING: "input file is missing or unreadable",
    CODE_INPUT_PARSE_FAILED: "input could not be parsed",
}

# Bounded, non-content warning identifiers.
WARNING_EMPTY_DOCUMENT = "empty_document"

# ---------------------------------------------------------------------------
# Parse-quality diagnostics (issue #514 PRODUCT-ENH-06)
# ---------------------------------------------------------------------------

# Stable identifier for the diagnostics pipeline shape itself: bump when the
# derivation rules below change so consumers can tell snapshot generations
# apart. Distinct from the parser fingerprint (which tracks the installed
# unstructured version and feeds the generation hash).
EXTRACTION_DIAGNOSTICS_VERSION = "extraction-diagnostics-v1"

# A page whose extractable element text totals fewer than this many characters
# is listed in ``low_content_pages``. The threshold intentionally sits below a
# single recovered table/caption line so a page carrying real structure is not
# flagged merely for being terse — it exists to surface pages where the parser
# recovered little or nothing (e.g. scanned image regions).
LOW_CONTENT_PAGE_MIN_CHARS = 20

# Element categories counted as recovered table structures ("TableChunk" is a
# continuation slice of a split table, still a recovered table structure).
_TABLE_CATEGORIES = frozenset({"Table", "TableChunk"})
# Element categories counted as captions (unstructured spells the figure
# caption category "FigureCaption"; "Caption" is accepted for stub/test
# shapes and future category spellings).
_CAPTION_CATEGORIES = frozenset({"FigureCaption", "Caption"})


class DocumentExtractionError(Exception):
    """Raised when text extraction cannot produce a result.

    ``code`` is a stable machine-readable identifier (one of
    :data:`CODE_INPUT_FILE_MISSING` / :data:`CODE_INPUT_PARSE_FAILED`).

    The message is a redacted summary — the originating exception's class name
    plus a fixed reason — truncated to :data:`MAX_ERROR_MESSAGE_CHARS`. It
    never contains the underlying exception's message, a file path, or any
    document content, because callers persist and log it.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message[:MAX_ERROR_MESSAGE_CHARS])


class ExtractedDocument(BaseModel):
    """Result of a successful parse-only extraction."""

    text: str
    character_count: int
    media_type: str
    warnings: list[str]


def _redacted_message(code: str, exc: BaseException) -> str:
    """Build a bounded failure message from the exception class name only."""
    return f"{type(exc).__name__}: {_REASONS[code]}"


def _normalize(text: str) -> str:
    """Normalize line endings to ``\\n`` and strip trailing whitespace per line.

    Paragraph breaks (blank lines), quotation characters, numbers, headings and
    list boundaries are preserved verbatim: internal whitespace is not
    collapsed and no unicode normalization is applied.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in normalized.split("\n"))


def _diagnostic_element_category(element: Any) -> str:
    """Element category with the same fallback the atom adapter uses."""
    return getattr(element, "category", None) or type(element).__name__


def _diagnostic_element_page(element: Any) -> Optional[int]:
    """Element page number, or None when the parser produced no page metadata."""
    meta = getattr(element, "metadata", None)
    page = getattr(meta, "page_number", None) if meta is not None else None
    try:
        return int(page) if page is not None else None
    except (TypeError, ValueError):
        return None


def _diagnostic_element_text(element: Any) -> str:
    """Element text as a plain string (empty when absent or non-string)."""
    text = getattr(element, "text", None)
    return text if isinstance(text, str) else ""


def build_extraction_diagnostics(
    elements: Any,
    extraction_version: str = EXTRACTION_DIAGNOSTICS_VERSION,
    ocr_used: bool = False,
) -> dict:
    """Build structured parse-quality diagnostics from parser elements.

    Pure derivation over unstructured-shaped ``elements`` (``category``,
    ``text``, ``metadata.page_number``) — no DB, vector, or job side effects.
    The parse stage calls it right after elements are produced and persists
    the result as JSON on ``files.extraction_diagnostics`` so the per-file
    status payload can reveal parse omissions (e.g. a scanned page the parser
    recovered nothing from) even when every chunk embedded successfully.

    Derivation rules:

    - ``pages_total`` — distinct page numbers seen by the parser. When no
      element carries page metadata (txt/docx/html parses), the document is
      reported as a single logical page.
    - ``pages_with_text`` — pages whose element text is non-empty.
    - ``low_content_pages`` — pages whose total element text is below
      :data:`LOW_CONTENT_PAGE_MIN_CHARS` characters, ascending.
    - ``ocr_used`` — passed through verbatim from the caller's OCR seam
      (True when the image/OCR text path actually ran for this document);
      never inferred here.
    - ``tables_detected`` / ``captions_detected`` — counts of recovered
      table / caption elements (honest counts of what the parser produced,
      never estimates of what the source contained).

    Args:
        elements: iterable of parser elements (unstructured elements or
            duck-typed equivalents; ``None`` tolerated as an empty parse).
        extraction_version: stable diagnostics-pipeline identifier persisted
            alongside the counts.
        ocr_used: whether OCR contributed text for this document.

    Returns:
        Dict with ``pages_total``, ``pages_with_text``, ``low_content_pages``,
        ``ocr_used``, ``tables_detected``, ``captions_detected`` and
        ``extraction_version``.
    """
    page_chars: dict[int, int] = {}
    unpaged_chars = 0
    tables_detected = 0
    captions_detected = 0
    for element in elements or ():
        category = _diagnostic_element_category(element)
        text = _diagnostic_element_text(element)
        stripped_len = len(text.strip())
        if category in _TABLE_CATEGORIES:
            tables_detected += 1
        elif category in _CAPTION_CATEGORIES:
            captions_detected += 1
        page = _diagnostic_element_page(element)
        if page is None:
            unpaged_chars += stripped_len
        else:
            page_chars[page] = page_chars.get(page, 0) + stripped_len
    if not page_chars and elements:
        # No page metadata at all: the whole parse is one logical page. An
        # element-less parse keeps pages_total=0 (nothing was parsed).
        page_chars = {1: unpaged_chars}
    pages_total = len(page_chars)
    pages_with_text = sum(1 for chars in page_chars.values() if chars > 0)
    low_content_pages = sorted(
        page for page, chars in page_chars.items()
        if chars < LOW_CONTENT_PAGE_MIN_CHARS
    )
    return {
        "pages_total": pages_total,
        "pages_with_text": pages_with_text,
        "low_content_pages": low_content_pages,
        "ocr_used": bool(ocr_used),
        "tables_detected": tables_detected,
        "captions_detected": captions_detected,
        "extraction_version": extraction_version,
    }


class DocumentExtractionService:
    """Extracts normalized text from a document without ingesting it."""

    def __init__(self) -> None:
        self._parser = DocumentParser()

    def extract_text(self, path: Path) -> ExtractedDocument:
        """Parse ``path`` and return its normalized text and metadata.

        Raises:
            DocumentExtractionError: with ``code == "input_file_missing"`` when
                the path does not resolve to a readable file, or
                ``code == "input_parse_failed"`` for a parser failure
                (``DocumentParseError``) or any other unexpected error.
        """
        media_type = self._media_type(path)

        try:
            elements = self._parser.parse(str(path))
        except FileNotFoundError as exc:
            raise self._fail(CODE_INPUT_FILE_MISSING, exc) from exc
        except Exception as exc:
            # Covers DocumentParseError and any other parser-side failure.
            raise self._fail(CODE_INPUT_PARSE_FAILED, exc) from exc

        # Same joining/ordering as DocumentProcessor._process_document_file, so
        # paragraph order, headings and list boundaries match ingestion. Routing
        # through the shared document-atom projection keeps the parse-only output
        # and ingestion derived from one parser-neutral definition (issue #460);
        # the adapter is pure (no DB/vector/job side effects) and this service
        # remains deliberately inert.
        atoms = parse_elements_to_atoms(
            elements,
            file_id=0,
            generation_hash="extract",
            parser_fingerprint="",
        )
        text = project_text(atoms)

        warnings: list[str] = []
        if not text.strip():
            warnings.append(WARNING_EMPTY_DOCUMENT)
            logger.debug("Extraction produced no text (media_type=%s)", media_type)

        return ExtractedDocument(
            text=text,
            character_count=len(text),
            media_type=media_type,
            warnings=warnings,
        )

    @staticmethod
    def _media_type(path: Path) -> str:
        guessed, _ = mimetypes.guess_type(path.name)
        return guessed or DEFAULT_MEDIA_TYPE

    @staticmethod
    def _fail(code: str, exc: BaseException) -> DocumentExtractionError:
        message = _redacted_message(code, exc)
        logger.warning("Document extraction failed (code=%s, %s)", code, message)
        return DocumentExtractionError(code, message)
