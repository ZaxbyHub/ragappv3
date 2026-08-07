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
