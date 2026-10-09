"""
Document processing service with orchestration, status tracking, and deduplication.

Provides DocumentProcessor class that coordinates parsing, chunking, and schema extraction
while tracking processing status in SQLite and handling file deduplication.
"""

import asyncio
import hashlib
import io
import json
import logging
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple, TypeVar

import pandas as pd

from ..config import settings
from ..models.database import SQLiteConnectionPool, get_pool
from ..utils.file_utils import compute_file_hash
from ..utils.retry import with_retry
from . import artifact_store, near_duplicates
from .chunk_enrichment import ChunkEnrichmentService
from .chunking import (
    EmbeddingSemanticChunker,
    ProcessedChunk,
    SemanticChunker,
    compute_parent_windows,
)
from .contextual_chunking import ContextualChunker
from .document_artifacts import (
    ATOM_SCHEMA_VERSION,
    DEFAULT_PARSER_NAME,
    RASTER_IMAGE_EXTENSIONS,
    AtomKind,
    DocumentAsset,
    DocumentAtom,
    ParsedDocument,
    compute_generation_hash,
    make_atom_id,
    parse_elements_to_atoms,
    project_text,
)
from .document_progress import (
    PHASE_CANCELLED,
    PHASE_CHUNKING,
    PHASE_EMBEDDING,
    PHASE_EXTRACTING_TEXT,
    PHASE_INDEXED,
    PHASE_PARSING,
    PHASE_WRITING_INDEX,
    clear_progress,
    set_phase,
    set_wiki_pending,
    write_session,
)
from .embedding_cache import (
    embedding_cache_key,
)
from .embedding_cache import (
    lookup as embedding_cache_lookup,
)
from .embedding_cache import (
    store as embedding_cache_store,
)
from .embeddings import EmbeddingError, EmbeddingService
from .image_processor import ImageProcessingResult, _process_image_sync
from .image_search import build_searchable_text
from .llm_client import LLMClient
from .schema_parser import SchemaParser
from .vector_store import VectorStore, VectorStoreError

logger = logging.getLogger(__name__)

_STAGE_TIMING_FIELDS = (
    # issue #704 (T1-25-KR-16): enrichment_ms was declared here but never
    # recorded — enrichment runs post-index in the enrichment worker, outside
    # every ingest entry point's measured span, so the logged value was
    # structurally always 0.0. Dropped rather than wired: wiring it inside
    # the entry points would time "deciding whether to enqueue enrichment",
    # not enrichment itself.
    "parse_ms",
    "chunk_ms",
    "contextual_ms",
    "parent_window_ms",
    "embedding_ms",
    "vector_write_ms",
    "optimize_ms",
    "sqlite_finalize_ms",
)


def _new_stage_timings() -> dict[str, float]:
    return {field: 0.0 for field in _STAGE_TIMING_FIELDS}


def _parser_version() -> str:
    """Best-effort version of the installed Unstructured parser.

    Returns ``"unknown"`` under reduced CI dependencies (unstructured stubbed);
    the value feeds the generation fingerprint so a parser upgrade invalidates
    prior generations.
    """
    try:
        import unstructured

        return getattr(unstructured, "__version__", "unknown")
    except Exception:  # noqa: BLE001 - availability is best-effort
        return "unknown"


def _compile_generation(file_hash: str) -> tuple[str, str]:
    """Compute the generation fingerprint and parser-fingerprint for ingestion.

    Returns ``(generation_hash, parser_fingerprint)``. The generation is keyed by
    the file-byte hash plus parser implementation/config/schema versions (issue
    #460), so an unchanged file+parser reprocesses idempotently and any
    change retires the old generation.
    """
    # Coerced to str so a mocked/None config value can never crash the
    # json.dumps inside compute_generation_hash with a non-serializable object
    # (e.g. a MagicMock when tests patch settings).
    config_version = str(getattr(settings, "document_parsing_strategy", "") or "")
    generation_hash = compute_generation_hash(
        file_hash,
        parser_name=DEFAULT_PARSER_NAME,
        parser_version=str(_parser_version()),
        config_version=config_version,
        schema_version=ATOM_SCHEMA_VERSION,
    )
    parser_fingerprint = f"{DEFAULT_PARSER_NAME}:{_parser_version()}"
    return generation_hash, parser_fingerprint


# ── Per-file retry coordination (issue #513 W15) ────────────────────────────
# Module-level so concurrent retry_failed_chunks calls for the SAME file
# serialize even when issued through different DocumentProcessor instances
# (route handler + background worker). Reference-counted: an entry is created
# on first use and pruned once no coroutine holds it, so the registry cannot
# grow unboundedly and a fresh asyncio.Lock is created per usage burst (locks
# bind to one event loop; pruning avoids cross-loop reuse across sequential
# event loops, e.g. repeated asyncio.run in tests).
_retry_locks: Dict[int, asyncio.Lock] = {}
_retry_lock_holders: Dict[int, int] = {}
# A plain threading.Lock (not asyncio) guards the registry: the critical
# sections contain no await and must be usable from any event loop.
_retry_locks_guard = threading.Lock()


@asynccontextmanager
async def _per_file_retry_lock(file_id: int) -> Iterator[None]:
    """Serialize chunk-retry work per file; prune the lock when unreferenced."""
    with _retry_locks_guard:
        lock = _retry_locks.get(file_id)
        if lock is None:
            lock = asyncio.Lock()
            _retry_locks[file_id] = lock
        _retry_lock_holders[file_id] = _retry_lock_holders.get(file_id, 0) + 1
    try:
        async with lock:
            yield
    finally:
        with _retry_locks_guard:
            remaining = _retry_lock_holders.get(file_id, 0) - 1
            if remaining <= 0:
                _retry_lock_holders.pop(file_id, None)
                # Prune only when this is still the registered entry and no
                # holder remains; waiters keep the entry alive via the holder
                # count they took on entry.
                if _retry_locks.get(file_id) is lock:
                    del _retry_locks[file_id]
            else:
                _retry_lock_holders[file_id] = remaining


def is_enrichment_enabled_for_vault(vault_id: Optional[int]) -> bool:
    """Return the effective enrichment setting for a vault.

    Resolution order:
    1. If vault_id is None, fall back to global settings.chunk_enrichment_enabled.
    2. Look up vaults.enrichment_enabled for the given vault_id.
       - NULL  → inherit global settings.chunk_enrichment_enabled
       - 1     → True (vault-level override: ON)
       - 0     → False (vault-level override: OFF)
    """
    if vault_id is None:
        return settings.chunk_enrichment_enabled

    try:
        pool = get_pool(str(settings.sqlite_path), max_size=1)
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT enrichment_enabled FROM vaults WHERE id = ?",
                (vault_id,),
            ).fetchone()
        if row is None:
            # Vault not found — treat as global
            return settings.chunk_enrichment_enabled
        override = row[0]
        if override is None:
            return settings.chunk_enrichment_enabled
        return bool(override)
    except Exception:
        # DB error — conservatively fall back to global, but never silently:
        # an operator's explicit vault opt-out is being overridden here, so
        # the fallback is logged (issue #697 / T1-25-KR-17).
        logger.warning(
            "Vault enrichment override lookup failed for vault_id=%s; "
            "falling back to the global chunk_enrichment_enabled=%s setting",
            vault_id,
            settings.chunk_enrichment_enabled,
            exc_info=True,
        )
        return settings.chunk_enrichment_enabled


def is_enrichment_enabled_for_file(file_id: int, vault_id: int) -> bool:
    """Return the effective enrichment setting for a file.

    Resolution order:
    1. Look up files.enrichment_enabled for the given file_id.
       - 1     → True (file-level override: ON)
       - 0     → False (file-level override: OFF)
       - NULL  → inherit from vault (fall through to step 2)
    2. Look up vaults.enrichment_enabled for the given vault_id
       (via is_enrichment_enabled_for_vault), which itself falls through
       to global settings.chunk_enrichment_enabled when vault override is NULL.

    Uses a single JOIN query for efficiency: file override + vault override
    in one round-trip.
    """
    try:
        pool = get_pool(str(settings.sqlite_path), max_size=1)
        with pool.connection() as conn:
            row = conn.execute(
                """
                SELECT f.enrichment_enabled AS file_override,
                       v.enrichment_enabled AS vault_override
                FROM files f
                JOIN vaults v ON v.id = f.vault_id
                WHERE f.id = ?
                """,
                (file_id,),
            ).fetchone()
        if row is None:
            # File not found — fall back to vault resolution
            return is_enrichment_enabled_for_vault(vault_id)

        file_override = row["file_override"] if isinstance(row, sqlite3.Row) else row[0]
        vault_override = (
            row["vault_override"] if isinstance(row, sqlite3.Row) else row[1]
        )

        # File-level override takes precedence
        if file_override is not None:
            return bool(file_override)

        # No file override: fall through to vault resolution
        # Build a synthetic vault row for is_enrichment_enabled_for_vault logic
        if vault_override is None:
            return settings.chunk_enrichment_enabled
        return bool(vault_override)
    except Exception:
        # DB error — conservatively fall back to vault resolution, but never
        # silently: an operator's explicit file-level opt-out may be
        # overridden here, so the fallback is logged (issue #697 /
        # T1-25-KR-17).
        logger.warning(
            "File enrichment override lookup failed for file_id=%s (vault_id=%s); "
            "falling back to vault/global enrichment resolution",
            file_id,
            vault_id,
            exc_info=True,
        )
        return is_enrichment_enabled_for_vault(vault_id)


def _add_elapsed_ms(timings: dict[str, float], field: str, started_at: float) -> None:
    timings[field] += (time.monotonic() - started_at) * 1000


def _merge_vector_timings(
    timings: dict[str, float], vector_timings: Optional[dict[str, float]]
) -> None:
    if not vector_timings:
        return
    timings["vector_write_ms"] += vector_timings.get("vector_write_ms", 0.0)
    timings["optimize_ms"] += vector_timings.get("optimize_ms", 0.0)


@dataclass
class ProcessedDocument:
    """
    Result of processing a document file.

    Attributes:
        file_id: The database ID of the processed file
        chunks: List of processed chunks from the document
    """

    file_id: int
    chunks: List[ProcessedChunk]
    document_text: str = ""
    file_hash: str = ""
    file_path: str = ""
    vault_id: int = 0


class DuplicateFileError(Exception):
    """Exception raised when a file with the same hash already exists and is indexed."""

    pass


class DocumentProcessingError(Exception):
    """Exception raised when document processing fails due to database errors."""

    pass


class EmbeddingDimensionChangedError(DocumentProcessingError):
    """An ingest was refused because the live index dimension differs.

    Classified to the stable ``DIMENSION_CHANGED`` ingest-error code so the
    persisted (user-visible) message carries the reindex remediation instead
    of the generic PARSE_FAILED reason (issue #691).
    """

    pass


class DocumentParseError(Exception):
    """Exception raised when document parsing fails."""

    pass


# --- Ingestion error redaction (issue #562 / C26) ---------------------------
#
# Strings persisted to files.error_message, files.phase_message and
# enrichment_error are returned to any vault reader, so they are built from a
# stable code plus a fixed, content-free reason — never from the raw exception
# text or the server-side path, which stay in the server log only. Same
# convention as document_extraction.py's stable codes.

INGEST_ERROR_PARSER_UNAVAILABLE = "PARSER_UNAVAILABLE"
INGEST_ERROR_PARSE_FAILED = "PARSE_FAILED"
# issue #703 review: a timed-out (or in-flight-refused) parse persists a
# distinct stable code so operators can tell a deadline hit from a
# content failure.
INGEST_ERROR_PARSE_TIMEOUT = "PARSE_TIMEOUT"
INGEST_ERROR_FILE_MISSING = "FILE_MISSING"
INGEST_ERROR_ENRICHMENT_FAILED = "ENRICHMENT_FAILED"
INGEST_ERROR_DIMENSION_CHANGED = "DIMENSION_CHANGED"
# issue #704 (T1-25-KR-02): an embedding-provider outage and a vector-store
# rejection are not parse failures; persisting them as PARSE_FAILED hid the
# actual failure provenance from operators (#562 asked for distinguishable
# codes).
INGEST_ERROR_EMBEDDING_FAILED = "EMBEDDING_FAILED"
INGEST_ERROR_VECTOR_STORE_FAILED = "VECTOR_STORE_FAILED"

_INGEST_ERROR_REASONS = {
    INGEST_ERROR_PARSER_UNAVAILABLE: "document parser is unavailable",
    INGEST_ERROR_PARSE_FAILED: "document could not be parsed",
    INGEST_ERROR_PARSE_TIMEOUT: "document parsing exceeded its time limit",
    INGEST_ERROR_FILE_MISSING: "uploaded file is missing from storage",
    INGEST_ERROR_ENRICHMENT_FAILED: (
        "content enrichment failed; the indexed document is unaffected"
    ),
    INGEST_ERROR_DIMENSION_CHANGED: (
        "embedding dimension changed; run the admin reindex job to migrate "
        "the index, then re-ingest this file"
    ),
    INGEST_ERROR_EMBEDDING_FAILED: (
        "the embedding provider failed; the document itself parsed correctly"
    ),
    INGEST_ERROR_VECTOR_STORE_FAILED: (
        "the vector store rejected the write; the document itself parsed correctly"
    ),
}

# Matches document_extraction.MAX_ERROR_MESSAGE_CHARS: persisted messages are
# bounded and content-free, so the cap never truncates a reason mid-word.
_INGEST_ERROR_MAX_CHARS = 200

# Bound on the __cause__ walk so pathological (or cyclic) exception chains
# cannot make classification unbounded.
_INGEST_ERROR_CAUSE_DEPTH = 5


def classify_ingest_error(exc: BaseException) -> str:
    """Map an ingestion failure to a stable, user-facing error code.

    The parser wrapper re-raises underlying failures as
    ``DocumentParseError(...) from e``, so the motivating failure families
    (a missing parser module, a vanished file) usually sit on the ``__cause__``
    chain rather than at the top level; the walk stays bounded.
    """
    current: Optional[BaseException] = exc
    depth = 0
    while current is not None and depth <= _INGEST_ERROR_CAUSE_DEPTH:
        # Errors that carry an explicit ingest code (parse deadline,
        # OCR-unavailable images) classify by that code first (issue
        # #703 review).
        code = getattr(current, "ingest_error_code", None)
        if code:
            return code
        if isinstance(current, EmbeddingDimensionChangedError):
            return INGEST_ERROR_DIMENSION_CHANGED
        # issue #704 (T1-25-KR-02): failure PROVENANCE must survive
        # classification — an embedding outage or a vector-store rejection is
        # not a parse failure, and an operator has to tell them apart from the
        # persisted code alone. Dimension changes stay first (more specific).
        if isinstance(current, EmbeddingError):
            return INGEST_ERROR_EMBEDDING_FAILED
        if isinstance(current, VectorStoreError):
            return INGEST_ERROR_VECTOR_STORE_FAILED
        if isinstance(current, ImportError):
            return INGEST_ERROR_PARSER_UNAVAILABLE
        if isinstance(current, (FileNotFoundError, FileExistsError)):
            return INGEST_ERROR_FILE_MISSING
        current = current.__cause__
        depth += 1
    return INGEST_ERROR_PARSE_FAILED


def format_ingest_error(code: str) -> str:
    """Build a persisted message: stable code + fixed content-free reason."""
    return f"{code}: {_INGEST_ERROR_REASONS[code]}"[:_INGEST_ERROR_MAX_CHARS]


def redact_ingest_error(exc: BaseException) -> str:
    """Build the persisted message for a caught ingestion failure."""
    return format_ingest_error(classify_ingest_error(exc))


class DocumentParser:
    """
    Parser for extracting text elements from documents using unstructured.io.

    Supports various formats: PDF, DOCX, TXT, HTML, and more.
    Uses configurable strategy from settings (default: fast for speed).
    """

    def parse(self, file_path: str) -> List[Any]:
        """
        Parse a document and extract text elements.

        Args:
            file_path: Path to the document file to parse.

        Returns:
            List of extracted text elements from the document.

        Raises:
            FileNotFoundError: If the specified file does not exist.
            DocumentParseError: If parsing fails for any reason.
        """
        # Validate file exists
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Document file not found: {file_path}")

        if not path.is_file():
            raise FileNotFoundError(f"Path is not a file: {file_path}")

        try:
            # Lazy import: unstructured can hang at module level when it
            # tries to download models or reach a network resource.
            from unstructured.partition.auto import partition

            # Use unstructured with configured strategy from settings
            elements = partition(
                filename=str(path), strategy=settings.document_parsing_strategy
            )
            return elements
        except Exception as e:
            # Wrap exceptions with clear, actionable message
            raise DocumentParseError(
                f"Failed to parse document '{file_path}': {str(e)}"
            ) from e


class SpreadsheetParser:
    """
    Parser for CSV, XLS, and XLSX spreadsheet files.

    Converts tabular data into RAG-ready text chunks. Each chunk covers a
    configurable number of rows and always includes the column header line
    as context, so retrieved chunks are self-contained without needing
    surrounding rows for interpretation.

    Sheet name and column list are prepended to every chunk so the LLM
    can orient itself when answering questions about the data.
    """

    # Default number of rows to attempt per chunk. Adaptively reduced for wide sheets.
    ROWS_PER_CHUNK: int = 50
    # Maximum characters per chunk (embedding service limit is 8192)
    MAX_CHUNK_CHARS: int = 8192

    def _calculate_adaptive_rows_per_chunk(
        self, sample_df, headers: list, sheet_name: str
    ) -> int:
        """
        Calculate how many rows fit in MAX_CHUNK_CHARS by sampling the data.

        Uses the first few rows to estimate average row size, then calculates
        how many rows can fit before hitting the MAX_CHUNK_CHARS limit.

        Args:
            sample_df: DataFrame with first few rows for size estimation
            headers: Column names
            sheet_name: Sheet name for logging

        Returns:
            Estimated number of rows per chunk (minimum 1)
        """
        if sample_df.empty:
            return self.ROWS_PER_CHUNK

        # Estimate header and sheet overhead
        header_str = " | ".join(str(h) for h in headers)
        header_overhead = len(f"Sheet: {sheet_name}\nColumns: {header_str}\n\n")

        # Estimate average row size from samples
        total_row_chars = 0
        row_count = 0

        for _, row in sample_df.iterrows():
            row_parts = [
                f"{col}: {val}" for col, val in zip(headers, row) if str(val).strip()
            ]
            if row_parts:
                row_text = " | ".join(row_parts)
                total_row_chars += len(row_text) + 1  # +1 for newline
                row_count += 1

        if row_count == 0:
            return self.ROWS_PER_CHUNK

        avg_row_chars = total_row_chars / row_count
        available_chars = self.MAX_CHUNK_CHARS - header_overhead

        if available_chars <= 0:
            return 1

        # Calculate rows that fit, with safety margin
        estimated_rows = max(1, int(available_chars / avg_row_chars) - 2)
        result = min(self.ROWS_PER_CHUNK, estimated_rows)

        logger.debug(
            "Sheet '%s': avg_row=%d chars, available=%d, estimated rows=%d",
            sheet_name,
            int(avg_row_chars),
            available_chars,
            result,
        )

        return result

    def _split_row_by_columns(
        self,
        col_val_pairs: List[tuple],
        sheet_name: str,
        row_idx: int,
        total_rows: int,
        total_col_count: int,
    ) -> List[dict]:
        """
        Split a single wide row into multiple chunks by column groups.
        Each chunk includes only columns that fit within MAX_CHUNK_CHARS.

        Args:
            col_val_pairs: List of (col_name, str_value) tuples for non-empty cells
            sheet_name: Sheet name for chunk prefix
            row_idx: 0-based row index (for metadata)
            total_rows: Total rows in sheet (for metadata)
            total_col_count: Total columns in sheet (for metadata)

        Returns:
            List of chunk dicts, one per column group. No data loss unless a single
            cell value exceeds MAX_CHUNK_CHARS (which then gets truncated).
        """
        if not col_val_pairs:
            return []

        result = []
        group_pairs = []  # Current column group: list of (col, val) tuples

        for col, val in col_val_pairs:
            # Test adding this column to current group
            group_pairs.append((col, val))
            test_chunk_text = self._build_column_group_text(group_pairs, sheet_name)

            if len(test_chunk_text) > self.MAX_CHUNK_CHARS:
                group_pairs.pop()  # revert the probe append
                if not group_pairs:
                    # Single column exceeds limit: truncate this cell's value only
                    header_section = f"Sheet: {sheet_name}\nColumns: {col}\n\n{col}: "
                    available = self.MAX_CHUNK_CHARS - len(header_section)
                    truncated_val = val[: max(0, available)]
                    logger.warning(
                        "Column '%s' value exceeds max chunk size; truncating from %d to %d chars.",
                        col,
                        len(val),
                        len(truncated_val),
                    )
                    result.append(
                        self._make_column_group_chunk(
                            [(col, truncated_val)],
                            sheet_name,
                            row_idx,
                            total_rows,
                            total_col_count,
                            len(result),
                        )
                    )
                    # group_pairs stays empty, continue to next column
                else:
                    # Current group is full; flush it and start new group with this column
                    result.append(
                        self._make_column_group_chunk(
                            group_pairs,
                            sheet_name,
                            row_idx,
                            total_rows,
                            total_col_count,
                            len(result),
                        )
                    )
                    # Validate that single column fits by itself before starting new group
                    single_col_text = self._build_column_group_text(
                        [(col, val)], sheet_name
                    )
                    if len(single_col_text) > self.MAX_CHUNK_CHARS:
                        # Single column exceeds limit; truncate it
                        header_section = (
                            f"Sheet: {sheet_name}\nColumns: {col}\n\n{col}: "
                        )
                        available = self.MAX_CHUNK_CHARS - len(header_section)
                        truncated_val = val[: max(0, available)]
                        logger.warning(
                            "Column '%s' value exceeds max chunk size; truncating from %d to %d chars.",
                            col,
                            len(val),
                            len(truncated_val),
                        )
                        group_pairs = [(col, truncated_val)]
                    else:
                        group_pairs = [(col, val)]
            else:
                # Column fits; already appended above
                pass

        # Flush any remaining columns
        if group_pairs:
            result.append(
                self._make_column_group_chunk(
                    group_pairs,
                    sheet_name,
                    row_idx,
                    total_rows,
                    total_col_count,
                    len(result),
                )
            )

        return result

    def _build_column_group_text(
        self, col_val_pairs: List[tuple], sheet_name: str
    ) -> str:
        """Build the text for a column group (helper for _split_row_by_columns)."""
        col_str = " | ".join(col for col, _ in col_val_pairs)
        row_str = " | ".join(f"{col}: {val}" for col, val in col_val_pairs)
        return f"Sheet: {sheet_name}\nColumns: {col_str}\n\n{row_str}"

    def _make_column_group_chunk(
        self,
        col_val_pairs: List[tuple],
        sheet_name: str,
        row_idx: int,
        total_rows: int,
        total_col_count: int,
        group_idx: int,
    ) -> dict:
        """Create a chunk dict for a column group (helper for _split_row_by_columns)."""
        return {
            "text": self._build_column_group_text(col_val_pairs, sheet_name),
            "metadata": {
                "sheet_name": sheet_name,
                "row_start": row_idx,
                "row_end": row_idx,
                "total_rows": total_rows,
                "column_count": total_col_count,
                "col_group": group_idx,
                "source_type": "spreadsheet",
            },
        }

    # Byte-order marks that unambiguously identify a CSV's codec before
    # any fallback guessing runs (issue #703 / T1-25-K-05).
    _CSV_BOM_CODECS = (
        (b"\xff\xfe\x00\x00", "utf-32"),
        (b"\x00\x00\xfe\xff", "utf-32"),
        (b"\xef\xbb\xbf", "utf-8-sig"),
        (b"\xff\xfe", "utf-16"),
        (b"\xfe\xff", "utf-16"),
    )

    # A NUL byte in the sniff window means binary content or BOM-less
    # UTF-16 (whose ASCII text decodes as "valid" UTF-8 with interleaved
    # NULs); route those to the gated buffered path instead of streaming.
    _CSV_SNIFF_BYTES = 4096

    @classmethod
    def _decode_csv_buffer(cls, data: bytes) -> str:
        """Buffered decode for the non-streaming legs (BOM'd or non-UTF-8).

        An unambiguous BOM selects its codec (an undecodable body falls
        through to the plain chain on the BOM-stripped bytes); otherwise
        strict UTF-8, then a lossy cp1252 leg. Every leg passes the
        shared decode gates so mis-decodes fail accurately instead of
        indexing mojibake (issue #703 review).
        """
        text: Optional[str] = None
        for bom, codec in cls._CSV_BOM_CODECS:
            if data.startswith(bom):
                try:
                    text = data.decode(codec)
                except UnicodeDecodeError:
                    data = data[len(bom) :]
                break
        if text is None:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = data.decode("cp1252", errors="replace")
        return SchemaParser._validated(text, what="CSV file")

    @classmethod
    def _read_csv_with_encoding_fallback(cls, file_path: str) -> "pd.DataFrame":
        """Read a CSV through a real-world encoding fallback chain.

        The common case streams straight from the path (no whole-file
        copies): strict UTF-8 covers plain ASCII, UTF-8, and — via
        pandas' own handling — files pandas accepts. Only two cases
        pay the buffered path: an explicit BOM (selects the codec), and
        a streaming ``UnicodeDecodeError`` (falls back to a lossy
        cp1252 leg — the encoding of Excel's default "CSV (Comma
        delimited)" Windows export). Every buffered leg keeps the
        issue-#513 NA-preservation flags (``dtype=str``,
        ``keep_default_na=False``) and passes the shared decode gates.
        """
        with open(file_path, "rb") as handle:
            sniff = handle.read(cls._CSV_SNIFF_BYTES)
        if (
            not any(sniff.startswith(bom) for bom, _ in cls._CSV_BOM_CODECS)
            and b"\x00" not in sniff
        ):
            try:
                return pd.read_csv(file_path, dtype=str, keep_default_na=False).fillna(
                    ""
                )
            except UnicodeDecodeError:
                pass
        data = Path(file_path).read_bytes()
        text = cls._decode_csv_buffer(data)
        return pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False).fillna(
            ""
        )

    def parse(self, file_path: str) -> List[dict]:
        """
        Parse a spreadsheet file and return a list of chunk dicts.

        Each returned dict has the structure:
            {
                "text": str,        # RAG-ready text block for this chunk
                "metadata": dict,   # Sheet name, row range, column count, etc.
            }

        Args:
            file_path: Absolute or relative path to the .csv, .xls, or .xlsx file.

        Returns:
            List of chunk dicts. Returns an empty list if the file has no
            readable data (empty sheets are skipped, not errored).

        Raises:
            FileNotFoundError: If the file does not exist.
            DocumentParseError: If pandas fails to read the file.
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Spreadsheet file not found: {file_path}")

        ext = path.suffix.lower()

        try:
            if ext == ".csv":
                # Single-sheet: wrap in dict to unify the loop below.
                # keep_default_na=False (issue #513 W3): pandas' default NA-token
                # coercion turns literal cell strings like "NA"/"N/A"/"NULL" into
                # NaN, which ``.fillna("")`` then empties and the non-empty cell
                # filter drops — destroying literal text. With the flag, empties
                # stay "" and every literal value round-trips. The encoding
                # fallback below keeps the flag on every leg.
                df = self._read_csv_with_encoding_fallback(file_path)
                sheets: dict = {"Sheet1": df}
            elif ext in {".xls", ".xlsx"}:
                xf = pd.ExcelFile(file_path)
                # Same NA-preservation rationale as the CSV branch (issue #513 W3).
                sheets = {
                    name: xf.parse(name, dtype=str, keep_default_na=False).fillna("")
                    for name in xf.sheet_names
                }
            else:
                raise DocumentParseError(
                    f"SpreadsheetParser received unsupported extension '{ext}'. "
                    f"Expected .csv, .xls, or .xlsx."
                )
        except DocumentParseError:
            raise
        except Exception as e:
            raise DocumentParseError(
                f"Failed to read spreadsheet '{file_path}': {e}"
            ) from e

        chunks: List[dict] = []

        for sheet_name, df in sheets.items():
            if df.empty:
                logger.debug("Skipping empty sheet '%s' in '%s'", sheet_name, file_path)
                continue

            headers = list(df.columns)
            header_str = " | ".join(str(h) for h in headers)
            total_rows = len(df)

            # Calculate adaptive row count for this sheet based on column width
            rows_per_chunk = self._calculate_adaptive_rows_per_chunk(
                df.iloc[0 : min(5, total_rows)], headers, sheet_name
            )

            start = 0
            while start < total_rows:
                # Try to add rows up to rows_per_chunk, but adjust if needed
                end = min(start + rows_per_chunk, total_rows)
                batch = df.iloc[start:end]
                rows_text_lines = []

                for _, row in batch.iterrows():
                    # Only include cells that have non-empty values to keep
                    # chunks compact. The header line already names every column.
                    row_parts = [
                        f"{col}: {val}"
                        for col, val in zip(headers, row)
                        if str(val).strip()
                    ]
                    if row_parts:
                        rows_text_lines.append(" | ".join(row_parts))

                if not rows_text_lines:
                    # All rows in this batch were entirely empty — skip
                    start = end
                    continue

                chunk_text = (
                    f"Sheet: {sheet_name}\n"
                    f"Columns: {header_str}\n\n" + "\n".join(rows_text_lines)
                )

                # If chunk exceeds max chars, reduce row count or split by columns
                if len(chunk_text) > self.MAX_CHUNK_CHARS:
                    if end - start <= 1:
                        # Single row exceeds limit; split into column groups
                        single_row = df.iloc[start]
                        col_val_pairs = [
                            (col, str(val))
                            for col, val in zip(headers, single_row)
                            if str(val).strip()
                        ]
                        col_group_chunks = self._split_row_by_columns(
                            col_val_pairs, sheet_name, start, total_rows, len(headers)
                        )
                        chunks.extend(col_group_chunks)
                        start = end
                        continue
                    else:
                        # Multi-row batch exceeds limit; reduce row count and retry
                        rows_per_chunk = max(1, rows_per_chunk // 2)
                        continue

                chunks.append(
                    {
                        "text": chunk_text,
                        "metadata": {
                            "sheet_name": sheet_name,
                            "row_start": start,
                            "row_end": end - 1,
                            "total_rows": total_rows,
                            "column_count": len(headers),
                            "source_type": "spreadsheet",
                            "col_group": None,
                        },
                    }
                )
                start = end

        return chunks


class IngestCancelledError(Exception):
    """An in-flight ingest was cancelled by the user (issue #783).

    Not a failure: it must never land ``files.status='error'`` (both ingest
    paths re-raise it before their generic handlers) and must never be
    retried — both worker transports route it to the cancellation unwind
    (cleanup + terminal ``cancelled`` status) instead of the failure path.
    Carries the ``file_id`` so the unwind can land the terminal phase even
    on the scan/sync path, where the task item itself has ``file_id=None``
    (the processor assigns the row id mid-run).
    """

    def __init__(self, message: str, file_id: Optional[int] = None) -> None:
        super().__init__(message)
        self.file_id = file_id


# --- Parse deadline + per-file in-flight registry (issue #703) --------------
#
# The schema, spreadsheet, image and general document parse paths all run
# synchronous parser work through asyncio.to_thread under the same
# settings.document_parse_timeout deadline. Cancelling the await cannot
# kill the worker thread, so a timed-out parse keeps running; the retry
# transports requeue after 1s/2s/4s — far below the 300s default timeout —
# which used to stack concurrent parse attempts of the SAME file on the
# shared default executor (T1-25-S2-08). The process-wide registry below
# refuses a new parse of a file whose previous parse has not finished.


class ParseDeadlineError(DocumentProcessingError):
    """A parse exceeded settings.document_parse_timeout (issue #703).

    Distinct from the base class so the retry transports wait out the
    timeout window before requeueing instead of burning the
    short-backoff ladder while the abandoned worker still runs, and so
    the persisted cause can name the timeout (PARSE_TIMEOUT).
    """


class ParseInFlightError(DocumentProcessingError):
    """A parse was refused because the same file already has one in
    flight (issue #703). Same transport treatment as
    ParseDeadlineError.
    """


# Parses run on a dedicated bounded pool rather than the shared default
# executor, so slow parses queue here instead of delaying the route/DB
# to_thread work that also shares the default pool (issue #703 review).
_PARSE_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ingest-parse")

_PARSE_IN_FLIGHT_GUARD = threading.Lock()
_PARSE_IN_FLIGHT: set = set()

_PARSE_T = TypeVar("_PARSE_T")


def _parse_registry_key(file_path: str) -> str:
    """Normalize a parse input path so retries always collide on one key."""
    return os.path.normcase(os.path.abspath(file_path))


def _reserve_parse_slot(file_path: str) -> str:
    """Atomically reserve the per-file parse slot or refuse the parse.

    Registry key lifetime is the worker's lifetime: the reservation is
    released only by the worker itself when the parse returns or raises
    (and by process restart, since the registry is memory-only). A parse
    whose thread never returns therefore refuses that file's retries
    until restart — deliberately, because a wall-clock TTL shorter than
    a legitimate long parse would re-open the overlap this guard exists
    to prevent.
    """
    key = _parse_registry_key(file_path)
    with _PARSE_IN_FLIGHT_GUARD:
        if key in _PARSE_IN_FLIGHT:
            error = ParseInFlightError(
                "Another parse of this file is still in flight (a timed-out "
                f"parse has not finished yet): {file_path}"
            )
            # Same deadline family as the timeout itself: the refusal
            # persists the distinct PARSE_TIMEOUT code, not a generic
            # parse failure (issue #703 review round 6, F2).
            error.ingest_error_code = INGEST_ERROR_PARSE_TIMEOUT
            raise error
        _PARSE_IN_FLIGHT.add(key)
    return key


def _release_parse_slot(key: str) -> None:
    with _PARSE_IN_FLIGHT_GUARD:
        _PARSE_IN_FLIGHT.discard(key)


def _deadline_error(stage: str, file_path: str) -> ParseDeadlineError:
    error = ParseDeadlineError(
        f"{stage} timed out after {settings.document_parse_timeout}s: {file_path}"
    )
    error.ingest_error_code = INGEST_ERROR_PARSE_TIMEOUT
    return error


async def _parse_with_deadline(
    file_path: str,
    parse_fn: Callable[[], _PARSE_T],
    *,
    stage: str,
    stage_timings: Optional[dict[str, float]] = None,
    timing_key: Optional[str] = None,
) -> _PARSE_T:
    """Run a synchronous parse in a worker thread under the parse deadline.

    Wraps ``asyncio.to_thread`` in ``asyncio.wait_for`` against
    ``settings.document_parse_timeout`` and refuses to start while
    another parse of the same file is in flight (see _reserve_parse_slot
    for the lifetime/semantics). The worker releases the slot in its
    ``finally``; if the deadline fires first, the abandoned thread keeps
    the slot until it exits, so retries are refused rather than piled
    on. The timeout value is read from settings live at every call.
    """
    key = _reserve_parse_slot(file_path)
    started_at = time.monotonic()

    def _tracked() -> _PARSE_T:
        try:
            return parse_fn()
        finally:
            _release_parse_slot(key)

    # submit() (not run_in_executor) so we hold the CONCURRENT future:
    # its cancel() is the precise queued-vs-running discriminator the
    # timeout handler needs (issue #703 owner review). The asyncio-side
    # future reads cancelled in both cases once wait_for cancels it.
    concurrent = _PARSE_EXECUTOR.submit(_tracked)
    fut = asyncio.wrap_future(concurrent)
    try:
        return await asyncio.wait_for(
            fut,
            timeout=settings.document_parse_timeout,
        )
    except asyncio.TimeoutError:
        # cancel() returns True only when the queued work item was
        # dropped (or is dropped now): _tracked never runs, so its
        # finally would never release the slot — release it here. A
        # running worker cannot be cancelled, so its own finally
        # releases the slot when it exits.
        if concurrent.cancel():
            _release_parse_slot(key)
        raise _deadline_error(stage, file_path) from None
    finally:
        if stage_timings is not None and timing_key is not None:
            _add_elapsed_ms(stage_timings, timing_key, started_at)


class DocumentProcessor:
    """
    Orchestrates document processing with status tracking and deduplication.

    Coordinates DocumentParser, SemanticChunker, and SchemaParser to process
    files while maintaining processing status in SQLite and handling duplicates.
    """

    # File extensions that should use SchemaParser instead of DocumentParser
    SCHEMA_EXTENSIONS = {".sql", ".ddl"}

    # File extensions that should use SpreadsheetParser
    SPREADSHEET_EXTENSIONS = {".csv", ".xls", ".xlsx"}

    # File extensions that should use image processing (OCR + metadata).
    # Derived from the canonical raster set so dispatch and the upload allowlist
    # can never drift apart (issue #460, final-critic finding 2). Includes .tif.
    IMAGE_EXTENSIONS = set(RASTER_IMAGE_EXTENSIONS)

    def __init__(
        self,
        chunk_size_chars: int = 2000,
        chunk_overlap_chars: int = 200,
        vector_store: Optional[VectorStore] = None,
        embedding_service: Optional[EmbeddingService] = None,
        pool: Optional["SQLiteConnectionPool"] = None,
        llm_client: Optional[LLMClient] = None,
        contextual_chunker: Optional[ContextualChunker] = None,
        write_semaphore: Optional[asyncio.Semaphore] = None,
    ):
        """
        Initialize the document processor.

        Args:
            chunk_size_chars: Target chunk size in characters for semantic chunking
            chunk_overlap_chars: Overlap between chunks in characters
            vector_store: VectorStore instance for storing chunk embeddings
            embedding_service: EmbeddingService instance for generating embeddings
            pool: SQLiteConnectionPool instance for database connections
            llm_client: LLMClient instance for contextual chunking (optional)
            contextual_chunker: Pre-configured ContextualChunker instance (optional)
            write_semaphore: Optional asyncio.Semaphore for SQLite write contention (default None)
        """
        self.parser = DocumentParser()
        # Construction-time chunk params are retained only as a fallback for
        # callers that bypass settings. The active chunker is rebuilt per
        # document via ``_get_chunker()`` so admin Settings UI changes to
        # chunk_size_chars / chunk_overlap_chars take effect immediately for
        # the next ingested document without restart.
        self._chunk_size_fallback = chunk_size_chars
        self._chunk_overlap_fallback = chunk_overlap_chars
        self.chunker = SemanticChunker(
            chunk_size_chars=chunk_size_chars, chunk_overlap_chars=chunk_overlap_chars
        )
        self.schema_parser = SchemaParser()
        self.spreadsheet_parser = SpreadsheetParser()
        # Fallback to creating a pool from settings if not provided
        if pool is None:
            pool = get_pool(str(settings.sqlite_path), max_size=2)
        self.pool = pool
        self.vector_store = vector_store
        self.embedding_service = embedding_service
        self._llm_client = llm_client
        self._contextual_chunker = contextual_chunker
        self._write_semaphore = write_semaphore
        self._chunk_enrichment_service: Optional[ChunkEnrichmentService] = None
        # In-flight cancel requests (issue #783): file ids the cancel route
        # marked. Checked between ingest steps; cleared on unwind, on the
        # 409 refusal path, and on every re-enqueue so a cancelled ingest
        # never poisons a later ingest of the same row.
        self._cancel_requested: set = set()
        # issue #704 review (PRR-006): detached (referenced) enrichment-status
        # writes spawned from a cancelled scope; done-callbacks discard.
        self._detached_status_tasks: set = set()

    def set_llm_client(self, llm_client: Optional[LLMClient]) -> None:
        """Rebind optional ingestion LLM work to a different live client."""
        if self._llm_client is llm_client:
            return
        self._llm_client = llm_client
        self._contextual_chunker = None
        self._chunk_enrichment_service = None

    async def _persist_extraction_diagnostics(
        self, file_id: Optional[int], diagnostics: Optional[dict]
    ) -> None:
        """Persist parse-quality diagnostics on ``files.extraction_diagnostics``
        (issue #514 PRODUCT-ENH-06).

        Best-effort by contract, mirroring the phase-progress writes: the
        diagnostics are advisory parse metadata and must never abort indexing,
        so DB/pool failures log and drop. Runs under the shared write permit
        (``_write_session``) like every other status/commit write — a raw
        pooled checkout here would bypass the SQLite write serialization the
        remaining writers rely on. Lazy import because ``document_extraction``
        imports this module (the producer lives there per its documented home).
        """
        if file_id is None or diagnostics is None:
            return
        try:
            async with self._write_session() as conn:
                conn.execute(
                    "UPDATE files SET extraction_diagnostics = ? WHERE id = ?",
                    (json.dumps(diagnostics), file_id),
                )
                conn.commit()
        except (sqlite3.Error, RuntimeError) as exc:
            # RuntimeError = expected pool-checkout failure (exhaustion/closed
            # pool) — same best-effort contract as set_phase (issue #513 W2).
            logger.warning(
                "Extraction diagnostics persist failed for file_id=%s: %s",
                file_id,
                exc,
            )

    @asynccontextmanager
    async def _write_session(self) -> Iterator[sqlite3.Connection]:
        """Yield a pooled connection under the shared SQLite write permit.

        Single safe pattern for every status/commit write (issue #513 W1 /
        RC-3). Since issue #704 (T1-25-KR-09) this delegates to
        ``document_progress.write_session`` — the one implementation of the
        permit-first / checkout-inside / release-order pattern — which the
        module-level progress helpers (``set_phase`` / ``clear_progress`` /
        ``set_wiki_pending``) also route through, so every committing writer
        in this file family serializes on the same permit.
        """
        async with write_session(self.pool, self._write_semaphore) as conn:
            yield conn

    def _get_chunker(self) -> "SemanticChunker | EmbeddingSemanticChunker":
        """Return a chunker configured with the live settings values.

        Reading ``settings.chunk_size_chars`` / ``settings.chunk_overlap_chars``
        at call time lets admins change chunking via the Settings UI and have
        the next ingested document use the new values without restarting.
        Falls back to the values passed to ``__init__`` when settings are unset.

        Honors ``settings.semantic_chunking_strategy``: 'embedding' selects the
        cosine-similarity ``EmbeddingSemanticChunker`` (requires an embedding
        service; falls back to title-based chunking with a warning when none is
        available), while the default 'title' keeps the existing behavior.
        """
        # ``is not None`` guards (issue #513 W4): an explicitly configured zero
        # is a VALUE, not "unset" — ``or``-fallbacks silently swallowed
        # chunk_overlap_chars=0 (falsy-zero fallthrough).
        size = (
            settings.chunk_size_chars
            if settings.chunk_size_chars is not None
            else self._chunk_size_fallback
        )
        overlap = (
            settings.chunk_overlap_chars
            if settings.chunk_overlap_chars is not None
            else self._chunk_overlap_fallback
        )
        if settings.semantic_chunking_strategy == "embedding":
            if self.embedding_service is None:
                logger.warning(
                    "semantic_chunking_strategy='embedding' but no embedding "
                    "service is available; falling back to title-based chunking"
                )
            else:
                existing = getattr(self, "chunker", None)
                if (
                    isinstance(existing, EmbeddingSemanticChunker)
                    and existing.max_chunk_size == size
                ):
                    return existing
                self.chunker = EmbeddingSemanticChunker(
                    embedding_service=self.embedding_service,
                    max_chunk_size=size,
                )
                return self.chunker
        # Reuse the cached chunker if it already matches; rebuild when settings change.
        existing = getattr(self, "chunker", None)
        if (
            isinstance(existing, SemanticChunker)
            and getattr(existing, "chunk_size", None) == size
            and getattr(existing, "chunk_overlap", None) == overlap
        ):
            return existing
        self.chunker = SemanticChunker(
            chunk_size_chars=size, chunk_overlap_chars=overlap
        )
        return self.chunker

    def _get_contextual_chunker(self) -> Optional[ContextualChunker]:
        """
        Lazily create a ContextualChunker when needed.

        Returns:
            ContextualChunker instance if contextual_chunking_enabled is True
            and llm_client exists, None otherwise.
        """
        if not settings.contextual_chunking_enabled:
            return None
        if self._llm_client is None:
            logger.warning("Contextual chunking enabled but no LLM client available")
            return None
        if self._contextual_chunker is None:
            self._contextual_chunker = ContextualChunker(self._llm_client)
        return self._contextual_chunker

    def _validate_chunk_sizes(self, texts: List[str], source_filename: str) -> None:
        """
        Validate that chunks don't exceed embedding service's max text length.

        Logs warnings for chunks exceeding the limit.

        Args:
            texts: List of chunk texts to validate
            source_filename: Source filename for logging context
        """
        if not self.embedding_service:
            return

        max_len = getattr(self.embedding_service, "MAX_TEXT_LENGTH", 8192)
        raw_prefix = getattr(self.embedding_service, "embedding_doc_prefix", "") or ""
        prefix_len = len(raw_prefix)
        effective_max = max_len - prefix_len
        oversized = []

        for i, text in enumerate(texts):
            if len(text) > effective_max:
                oversized.append((i, len(text)))

        if oversized:
            logger.warning(
                "Document '%s' has %d chunk(s) exceeding effective embedding length (%d chars after prefix): %s. "
                "With fail_fast=False embed_batch returns None placeholders at these positions "
                "(dropped from the embedded set; the enrichment path treats any None as a chunk-level failure); "
                "fail_fast=True raises.",
                source_filename,
                len(oversized),
                effective_max,
                ", ".join(f"chunk {i} ({size} chars)" for i, size in oversized),
            )

    def _get_chunk_enrichment_service(
        self, vault_id: Optional[int] = None, file_id: Optional[int] = None
    ) -> Optional[ChunkEnrichmentService]:
        """Lazily create a ChunkEnrichmentService when needed.

        Uses the effective enrichment setting at file > vault > global priority.
        """
        if file_id is not None and vault_id is not None:
            if not is_enrichment_enabled_for_file(file_id, vault_id):
                return None
        elif not is_enrichment_enabled_for_vault(vault_id):
            return None
        if self._llm_client is None:
            logger.warning("Chunk enrichment enabled but no LLM client available")
            return None
        if self._chunk_enrichment_service is None:
            fields = [f.strip() for f in settings.chunk_enrichment_fields.split(",")]
            self._chunk_enrichment_service = ChunkEnrichmentService(
                llm_client=self._llm_client,
                concurrency=settings.chunk_enrichment_concurrency,
                enrichment_fields=fields,
            )
        return self._chunk_enrichment_service

    async def _verify_vector_rows_visible(
        self, file_id: int, vector_target: object | None = None
    ) -> None:
        """Ensure a vector-enabled ingest has visible LanceDB rows before indexing.

        ``vector_target`` (issue #513 W13) optionally scopes the visibility
        check to a dimension-rebuild target table instead of the live index.
        """
        if self.vector_store is None:
            return

        if vector_target is not None:
            visible_rows = await self.vector_store.count_by_file(
                str(file_id), target=vector_target
            )
        else:
            visible_rows = await self.vector_store.count_by_file(str(file_id))
        if visible_rows <= 0:
            _vis_error = DocumentProcessingError(
                f"Vector store visibility check failed: file_id={file_id} has zero LanceDB rows"
            )
            # issue #704 (PRR-001): a zero-rows visibility failure IS a
            # vector-store rejection — without this code it persisted as
            # PARSE_FAILED and an operator could not tell it from a parse
            # failure.
            _vis_error.ingest_error_code = INGEST_ERROR_VECTOR_STORE_FAILED
            raise _vis_error

    async def _ensure_live_dimension_compatible(self, embedding_dim: int) -> None:
        """Refuse single-file ingests that would need a dimension migration.

        When the live ``chunks`` table exists at a DIFFERENT embedding
        dimension than this ingest's embeddings, dimension migration is
        exclusively the admin reindex job's business (``_reindex_embed_all``
        re-embeds the whole corpus into a staged rebuild table before its
        validated swap). A single-file ingest must never open that rebuild
        itself: the staged table would hold only this file's rows, so the
        swap would destroy every other indexed file's vectors while their
        rows still claim ``status='indexed'`` (issue #691). Fail closed with
        an actionable error instead: the raw exception names both dimensions
        and the reindex remediation (server log), while the persisted
        user-visible message is the stable ``DIMENSION_CHANGED`` code.

        Best-effort by contract: when the store is absent, does not expose
        the dimension probe (test doubles, alternative backends), the probe
        fails, or the probe returns a non-numeric dimension, this returns
        without raising and the ordinary write path surfaces any real
        mismatch.
        """
        if self.vector_store is None:
            return
        get_live_dim = getattr(self.vector_store, "get_live_embedding_dim", None)
        if get_live_dim is None:
            return
        try:
            live_dim = await get_live_dim()
            if live_dim is None or int(live_dim) == int(embedding_dim):
                return
        except Exception:  # noqa: BLE001 - probe must never fail the ingest
            logger.warning(
                "Live embedding-dimension probe failed for dim=%s; proceeding "
                "without the compatibility check",
                embedding_dim,
                exc_info=True,
            )
            return
        raise EmbeddingDimensionChangedError(
            f"Embedding dimension changed: incoming embeddings have dimension "
            f"{int(embedding_dim)} but the live vector index was built at "
            f"dimension {int(live_dim)}. Refusing this single-file ingest: a "
            f"dimension migration rebuilds the whole index and must never be "
            f"triggered by one file's ingest. Run the admin reindex job "
            f"(Reindex) to migrate the index, then re-ingest this file "
            f"(issue #691)."
        )

    @staticmethod
    def _build_chunk_uid(file_id: int, chunk: ProcessedChunk) -> str:
        """Build a chunk_uid consistent with vector store record construction."""
        chunk_scale = chunk.metadata.get("chunk_scale", "default")
        if settings.multi_scale_indexing_enabled and chunk_scale != "default":
            chunk_index_value = chunk.metadata.get("chunk_index", chunk.chunk_index)
            if isinstance(chunk_index_value, str) and "_" in chunk_index_value:
                return f"{file_id}_{chunk_index_value}"
            return f"{file_id}_{chunk_scale}_{chunk.chunk_index}"
        return f"{file_id}_{chunk.chunk_index}"

    @staticmethod
    def _enrichment_has_content(enrichment: Any) -> bool:
        return bool(
            enrichment
            and (
                getattr(enrichment, "summary", "")
                or getattr(enrichment, "questions", None)
                or getattr(enrichment, "entities", None)
                or getattr(enrichment, "aliases", None)
            )
        )

    @staticmethod
    def _search_text_with_enrichment(
        chunk: ProcessedChunk, enrichment: Any = None
    ) -> str:
        """Build bounded searchable text while preserving raw evidence separately."""
        parts = [chunk.text]
        if not DocumentProcessor._enrichment_has_content(enrichment):
            return chunk.text

        summary = getattr(enrichment, "summary", "")
        questions = list(getattr(enrichment, "questions", []) or [])[:5]
        entities = list(getattr(enrichment, "entities", []) or [])[:10]
        aliases = list(getattr(enrichment, "aliases", []) or [])[:10]

        if summary:
            parts.append(f"Summary: {summary[:1000]}")
        if questions:
            parts.append("Questions: " + " ".join(str(q)[:240] for q in questions))
        if entities or aliases:
            names = [str(item)[:120] for item in entities + aliases]
            parts.append("Entities: " + " ".join(names))
        return "\n".join(parts)

    def _candidate_chunks_for_enrichment(
        self, file_id: int, chunks: List[ProcessedChunk]
    ) -> List[tuple[ProcessedChunk, str]]:
        """Choose canonical chunks for post-index enrichment.

        Prefer default-scale chunks when present. For multi-scale-only indexes,
        enrich at most one representative per parent window so duplicate scales
        do not multiply LLM calls.
        """
        if not chunks:
            return []

        selected_chunks = self._select_chunks_for_enrichment(chunks)
        return [
            (chunk, self._build_chunk_uid(file_id, chunk)) for chunk in selected_chunks
        ]

    def _select_chunks_for_enrichment(
        self, chunks: List[ProcessedChunk]
    ) -> List[ProcessedChunk]:
        """Choose chunks to enrich without requiring a file-specific chunk UID."""
        if not chunks:
            return []

        default_chunks = [
            chunk
            for chunk in chunks
            if chunk.metadata.get("chunk_scale", "default") == "default"
        ]
        source_chunks = default_chunks or chunks
        selected: List[ProcessedChunk] = []
        seen: set[tuple[Any, ...]] = set()

        for chunk in source_chunks:
            if (
                chunk.parent_window_start is not None
                and chunk.parent_window_end is not None
            ):
                key = ("parent", chunk.parent_window_start, chunk.parent_window_end)
            elif default_chunks:
                key = ("default", chunk.chunk_index)
            else:
                key = (
                    "chunk",
                    chunk.metadata.get("chunk_scale", "default"),
                    chunk.metadata.get("chunk_index", chunk.chunk_index),
                    chunk.chunk_index,
                )
            if key in seen:
                continue
            seen.add(key)
            selected.append(chunk)
        return selected

    async def set_enrichment_status(
        self, file_id: int, status: str, error_message: Optional[str] = None
    ) -> Optional[str]:
        """Update enrichment state without changing files.status.

        Async since issue #704 (T1-25-KR-09): the write routes through the
        shared write permit like every other committing write.
        """
        if self.pool is None:
            return None
        updated_at = datetime.now(UTC).isoformat()
        try:
            async with write_session(self.pool, self._write_semaphore) as conn:
                conn.execute(
                    """
                    UPDATE files
                    SET enrichment_status = ?,
                        enrichment_error = ?,
                        enrichment_updated_at = ?
                    WHERE id = ?
                    """,
                    (status, error_message, updated_at, file_id),
                )
                conn.commit()
            return updated_at
        except sqlite3.Error as e:
            logger.warning(
                "Failed to update enrichment status for file_id=%s: %s", file_id, e
            )
            return None

    async def _mark_enrichment_stale_if_current_job(
        self, file_id: int, processing_started_at: Optional[str]
    ) -> None:
        """Finish the same enrichment attempt if it becomes stale mid-flight."""
        if self.pool is None or processing_started_at is None:
            return
        try:
            async with write_session(self.pool, self._write_semaphore) as conn:
                conn.execute(
                    """
                    UPDATE files
                    SET enrichment_status = 'error',
                        enrichment_error = ?,
                        enrichment_updated_at = ?
                    WHERE id = ?
                      AND enrichment_status = 'processing'
                      AND enrichment_updated_at = ?
                    """,
                    (
                        "Enrichment job became stale before completion; base index remains available",
                        datetime.now(UTC).isoformat(),
                        file_id,
                        processing_started_at,
                    ),
                )
                conn.commit()
        except sqlite3.Error as e:
            logger.warning(
                "Failed to mark stale enrichment for file_id=%s: %s", file_id, e
            )

    def _is_enrichment_job_current(self, file_id: int, file_hash: str) -> bool:
        """Return True only if this queued enrichment still matches the live file row.

        A truthfully-``partial`` file row is current (issue #693 /
        T1-25-KR-03, dedup half): its enrichment must not be declared stale
        merely because the ingest reported partial success.
        """
        if self.pool is None:
            return False
        try:
            with self.pool.connection() as conn:
                row = conn.execute(
                    "SELECT file_hash, status FROM files WHERE id = ?",
                    (file_id,),
                ).fetchone()
        except sqlite3.Error as e:
            logger.warning(
                "Failed to validate enrichment job for file_id=%s: %s", file_id, e
            )
            return False

        if row is None:
            logger.info("Skipping stale enrichment for deleted file_id=%s", file_id)
            return False
        current_hash = row["file_hash"] if isinstance(row, sqlite3.Row) else row[0]
        current_status = row["status"] if isinstance(row, sqlite3.Row) else row[1]
        if current_hash != file_hash or current_status not in ("indexed", "partial"):
            logger.info(
                "Skipping stale enrichment for file_id=%s: current status/hash no longer match queued job",
                file_id,
            )
            return False
        return True

    def _build_vector_record(
        self,
        *,
        file_id: int,
        vault_id: int,
        file_hash: str,
        chunk: ProcessedChunk,
        embedding: List[float],
        sparse_emb: Any,
        document_text: str,
        enrichment: Any = None,
    ) -> Dict[str, Any]:
        chunk_scale = chunk.metadata.get("chunk_scale", "default")
        chunk_uid = self._build_chunk_uid(file_id, chunk)
        chunk_metadata = chunk.metadata.copy()
        chunk_metadata["chunk_uid"] = chunk_uid
        chunk_metadata["file_id"] = str(file_id)
        chunk_metadata["chunk_count"] = chunk.metadata.get("total_chunks") or 1
        chunk_metadata["chunk_scale"] = chunk_scale
        chunk_metadata["raw_text"] = chunk.raw_text or chunk.text

        if self._enrichment_has_content(enrichment):
            chunk_metadata["enrichment"] = enrichment.to_dict()

        if chunk.parent_window_start is not None:
            chunk_metadata["parent_window_start"] = chunk.parent_window_start
        if chunk.parent_window_end is not None:
            chunk_metadata["parent_window_end"] = chunk.parent_window_end
        if chunk.chunk_position is not None:
            chunk_metadata["chunk_position"] = chunk.chunk_position
        if (
            chunk.parent_window_start is not None
            and chunk.parent_window_end is not None
            and document_text
        ):
            chunk_metadata["parent_window_text"] = document_text[
                chunk.parent_window_start : chunk.parent_window_end
            ]

        if settings.reupload_safe_order:
            record_id = f"{file_id}_{file_hash[:8]}_{chunk_scale}_{chunk.chunk_index}"
        else:
            record_id = chunk_uid

        record = {
            "id": record_id,
            "text": self._search_text_with_enrichment(chunk, enrichment),
            "file_id": str(file_id),
            "chunk_index": chunk.chunk_index,
            "vault_id": str(vault_id),
            "chunk_scale": chunk_scale,
            "parent_doc_id": str(file_id),
            "parent_window_start": chunk.parent_window_start,
            "parent_window_end": chunk.parent_window_end,
            "chunk_position": chunk.chunk_position,
            "metadata": json.dumps(chunk_metadata),
            "embedding": embedding,
        }
        if sparse_emb is not None:
            try:
                record["sparse_embedding"] = json.dumps(sparse_emb)
            except (TypeError, ValueError) as e:
                logger.warning(f"Failed to serialize sparse embedding: {e}")
                record["sparse_embedding"] = None
        return record

    @staticmethod
    def _build_failed_chunk_metadata(
        chunk: ProcessedChunk, document_text: str
    ) -> Dict[str, Any]:
        """Build the rebuild metadata JSON for a failed chunk (Issue #396).

        Captures everything ``_rebuild_vector_record_from_stored`` needs to
        reconstruct a valid vector record later without re-parsing the source:
        raw_text, parent_window offsets + text, chunk_position, page_number,
        chunk_bbox, chunk_scale, chunk_uid. Enrichment is intentionally omitted
        (a chunk that failed embedding never reached the enrichment stage).
        """
        meta = {
            "raw_text": chunk.raw_text or chunk.text,
            "chunk_index": chunk.chunk_index,
            "chunk_scale": chunk.metadata.get("chunk_scale", "default"),
            "chunk_uid": chunk.chunk_uid,
            "chunk_position": chunk.chunk_position,
            "parent_window_start": chunk.parent_window_start,
            "parent_window_end": chunk.parent_window_end,
            "page_number": chunk.metadata.get("page_number"),
            "chunk_bbox": chunk.metadata.get("chunk_bbox"),
            "total_chunks": chunk.metadata.get("total_chunks"),
            # Optional exact provenance (image/spreadsheet/schema paths, issue
            # #460): carried so a chunk-scoped retry rebuilds with the same
            # atom/asset provenance as the first-pass record.
            "atom_id": chunk.metadata.get("atom_id"),
            "atom_kind": chunk.metadata.get("atom_kind"),
            "asset_id": chunk.metadata.get("asset_id"),
            "generation_hash": chunk.metadata.get("generation_hash"),
        }
        if (
            chunk.parent_window_start is not None
            and chunk.parent_window_end is not None
            and document_text
        ):
            meta["parent_window_text"] = document_text[
                chunk.parent_window_start : chunk.parent_window_end
            ]
        return meta

    def _persist_failed_chunks(
        self,
        file_id: int,
        chunks: List[ProcessedChunk],
        failed_indices: set,
        document_text: str,
        conn: sqlite3.Connection,
        error_reason: Optional[str] = None,
    ) -> None:
        """Persist failed-chunk identity + rebuild metadata (Issue #396).

        Clears any prior ``failed_chunks`` rows for this file first (idempotent
        re-ingest), then inserts one row per failed chunk. Called inside the
        caller's transaction; does not commit.
        """
        conn.execute("DELETE FROM failed_chunks WHERE file_id = ?", (file_id,))
        for idx in sorted(failed_indices):
            if idx >= len(chunks):
                continue
            chunk = chunks[idx]
            meta_json = json.dumps(
                self._build_failed_chunk_metadata(chunk, document_text)
            )
            conn.execute(
                "INSERT INTO failed_chunks "
                "(file_id, chunk_index, chunk_text, chunk_metadata, error_reason) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    file_id,
                    chunk.chunk_index,
                    chunk.text,
                    meta_json,
                    error_reason,
                ),
            )

    @staticmethod
    def _rebuild_chunk_from_stored(
        stored_row: Any,
    ) -> tuple[ProcessedChunk, Dict[str, Any]]:
        """Reconstruct a ProcessedChunk + its metadata dict from a failed_chunks row.

        Returns (chunk, stored_meta) so the caller can read parent_window_text
        and other rebuild-only fields from stored_meta separately. [N4]
        """
        stored_meta: Dict[str, Any] = json.loads(stored_row["chunk_metadata"])
        chunk_meta = {
            "chunk_scale": stored_meta.get("chunk_scale", "default"),
            "chunk_index": stored_meta.get("chunk_index", stored_row["chunk_index"]),
            "total_chunks": stored_meta.get("total_chunks") or 1,
            "raw_text": stored_meta.get("raw_text"),
            "page_number": stored_meta.get("page_number"),
            "chunk_bbox": stored_meta.get("chunk_bbox"),
            "chunk_uid": stored_meta.get("chunk_uid"),
            # Exact provenance preserved through chunk-scoped retry (issue #460).
            "atom_id": stored_meta.get("atom_id"),
            "atom_kind": stored_meta.get("atom_kind"),
            "asset_id": stored_meta.get("asset_id"),
            "generation_hash": stored_meta.get("generation_hash"),
        }
        chunk = ProcessedChunk(
            text=stored_row["chunk_text"],
            metadata=chunk_meta,
            chunk_index=stored_row["chunk_index"],
            chunk_uid=stored_meta.get("chunk_uid"),
            raw_text=stored_meta.get("raw_text"),
            parent_window_start=stored_meta.get("parent_window_start"),
            parent_window_end=stored_meta.get("parent_window_end"),
            chunk_position=stored_meta.get("chunk_position"),
        )
        return chunk, stored_meta

    def _rebuild_vector_record_from_stored(
        self,
        *,
        file_id: int,
        vault_id: int,
        file_hash: str,
        stored_row: Any,
        stored_meta: Dict[str, Any],
        chunk: ProcessedChunk,
        embedding: List[float],
    ) -> Dict[str, Any]:
        """Rebuild a vector record for a previously-failed chunk (Issue #396).

        Unlike ``_build_vector_record``, this does NOT require ``document_text``
        or an ``enrichment`` object — ``parent_window_text`` is read from the
        stored JSON, and enrichment is omitted (retried chunks re-enter the
        enrichment queue separately). [C1.1, N4]
        """
        chunk_scale = chunk.metadata.get("chunk_scale", "default")
        chunk_metadata = chunk.metadata.copy()
        chunk_metadata["chunk_uid"] = chunk.chunk_uid or self._build_chunk_uid(
            file_id, chunk
        )
        chunk_metadata["file_id"] = str(file_id)
        chunk_metadata["chunk_count"] = chunk.metadata.get("total_chunks") or 1
        chunk_metadata["chunk_scale"] = chunk_scale
        chunk_metadata["raw_text"] = chunk.raw_text or chunk.text
        if chunk.parent_window_start is not None:
            chunk_metadata["parent_window_start"] = chunk.parent_window_start
        if chunk.parent_window_end is not None:
            chunk_metadata["parent_window_end"] = chunk.parent_window_end
        if chunk.chunk_position is not None:
            chunk_metadata["chunk_position"] = chunk.chunk_position
        parent_window_text = stored_meta.get("parent_window_text")
        if isinstance(parent_window_text, str) and parent_window_text.strip():
            chunk_metadata["parent_window_text"] = parent_window_text

        # Mirror the reupload_safe_order id branch from _build_vector_record. [N4]
        if settings.reupload_safe_order:
            record_id = f"{file_id}_{file_hash[:8]}_{chunk_scale}_{chunk.chunk_index}"
        else:
            record_id = chunk_metadata["chunk_uid"]

        return {
            "id": record_id,
            "text": chunk.text,  # no enrichment on retry
            "file_id": str(file_id),
            "chunk_index": chunk.chunk_index,
            "vault_id": str(vault_id),
            "chunk_scale": chunk_scale,
            "parent_doc_id": str(file_id),
            "parent_window_start": chunk.parent_window_start,
            "parent_window_end": chunk.parent_window_end,
            "chunk_position": chunk.chunk_position,
            "metadata": json.dumps(chunk_metadata),
            "embedding": embedding,
        }

    async def retry_failed_chunks(self, file_id: int) -> Dict[str, Any]:
        """Re-embed and re-index only the failed chunks for an indexed file.

        Returns a summary dict: {retried, succeeded, still_failing,
        failed_chunk_indices}. Raises ``ValueError`` when the file is not in a
        retryable state (status not 'indexed'/'partial') so the caller can map
        to 409. [C1.2, C1.3, C1.4, N1, N3] 'partial' must stay eligible: the
        upload/reindex path (``process_existing_file``) lands below-threshold
        partial failures in status 'partial' with failed_chunks rows (frozen
        C27), and stranding those rows behind a 409 would defeat the
        chunk-scoped recovery this issue exists to provide.

        Concurrency (issue #513 W15): retries for the same file serialize on a
        module-level per-file lock, so a concurrent double-retry recovers each
        vector exactly once and increments chunk_count exactly once. Accounting
        is authoritative: after the reconciliation/vector write, chunk_count is
        taken from the vector store's live count for the file when a count API
        exists, otherwise derived from the rows actually cleared/written —
        repeated retries are no-ops.

        Stale-generation suppression (issue #513 W16): immediately before the
        vector write the file row is re-read; if the generation identity
        (file_hash / active_generation_hash), the row itself, or retry
        eligibility changed while this retry awaited the embedder, nothing is
        written and no counter is mutated (truthful no-op outcome).
        """
        if self.embedding_service is None or self.vector_store is None:
            raise RuntimeError("Embedding service or vector store unavailable")

        async with _per_file_retry_lock(file_id):
            return await self._retry_failed_chunks_locked(file_id)

    def _retry_staleness_reason(
        self, file_id: int, file_hash: str, generation_hash: Optional[str]
    ) -> Optional[str]:
        """Re-read the file row; return why a pending retry write is stale, or None.

        The re-read happens after every await that precedes the vector write
        (embed loop), so a newer generation that completed meanwhile suppresses
        this retry's publication entirely (issue #513 W16 / C15).
        """
        conn = self.pool.get_connection()
        try:
            current = conn.execute(
                "SELECT file_hash, status, active_generation_hash FROM files "
                "WHERE id = ?",
                (file_id,),
            ).fetchone()
        finally:
            self.pool.release_connection(conn)

        if current is None:
            return "file row no longer exists"
        current_hash = str(current["file_hash"] or "")
        current_status = str(current["status"] or "")
        current_generation = current["active_generation_hash"]
        if current_hash != file_hash:
            return (
                f"file_hash changed mid-retry ({file_hash[:8]}… -> "
                f"{current_hash[:8]}…); a newer generation owns the file"
            )
        if current_generation != generation_hash:
            return "active_generation_hash changed mid-retry"
        if current_status not in ("indexed", "partial"):
            return f"status '{current_status}' is no longer retry-eligible"
        return None

    def request_cancel(self, file_id: int) -> None:
        """Mark an in-flight ingest as cancel-requested (issue #783).

        Idempotent; the between-steps gates observe the request at their next
        seam and convert the ingest into an :class:`IngestCancelledError`.
        """
        self._cancel_requested.add(file_id)

    def clear_cancel(self, file_id: int) -> None:
        """Drop a stale cancel request (re-enqueue / unwind / 409 refusal)."""
        self._cancel_requested.discard(file_id)

    def is_cancel_requested(self, file_id: int) -> bool:
        return file_id in self._cancel_requested

    def _raise_if_cancelled(self, file_id: Optional[int]) -> None:
        """Between-steps cancellation gate (issue #783).

        Called at the two seams of BOTH ingest paths — after parse/chunking
        (before the expensive embedding) and immediately before the vector
        write (next to ``_raise_if_file_row_missing``, the issue-#692
        staleness gate) — so a cancelled ingest writes no vectors and
        publishes no atoms.
        """
        if file_id is not None and file_id in self._cancel_requested:
            raise IngestCancelledError(
                f"Ingest cancelled by user (file_id={file_id})",
                file_id=file_id,
            )

    async def rollback_cancelled_ingest(
        self, file_id: int, *, run_deletes: bool = True
    ) -> None:
        """Idempotent unwind of a cancelled ingest generation (issue #783).

        Deletes any vectors and atom rows this generation wrote (safe no-ops
        when the cancel landed before any write), lands the terminal
        ``cancelled`` status via a guarded UPDATE (never overwrites
        ``indexed``/``partial``), sets the terminal phase and clears the
        transient progress counters in one write, and purges the per-chunk
        ``failed_chunks`` rows written during embedding (a cancelled row can
        never reach the chunk-retry endpoint, which requires indexed/partial
        — issue #783 review PRR-032). Never raises: a cleanup failure is
        logged, the failed vector delete is queued on the
        ``vector_delete_pending`` sweep (the same tombstone the delete route
        uses, issue #783 review PRR-002), and the status guard still stands.
        ``run_deletes=False`` (issue #783 review F-001): the caller is a
        staged-rebuild re-embed — the cancel skips the file but must NOT
        destroy the document's previously indexed live vectors/atoms, which
        the rebuild neither owns nor re-creates.
        """
        if not run_deletes:
            self.clear_cancel(file_id)
            await clear_progress(
                self.pool,
                file_id,
                phase=PHASE_CANCELLED,
                phase_message="Cancelled by user",
            )
            return
        # Status-aware (PRR-004 review): deletes run when the row is or
        # becomes 'cancelled' — an already-cancelled row (the route raced
        # ahead of this unwind) is exactly the orphan-cleanup case — and
        # skip for every other survivor (indexed/partial/error/missing).
        deletes_run = False
        try:
            # issue #704 (T1-25-KR-09): permit-held session like every other
            # committing write in this file.
            async with self._write_session() as conn:
                row = conn.execute(
                    "SELECT status FROM files WHERE id = ?", (file_id,)
                ).fetchone()
                cur_status = None if row is None else row["status"]
                # Survivor-explicit gate: only a definitively-terminal
                # survivor (or a deleted row) skips the deletes; anything
                # else — pending/processing/cancelled/unknown — is treated
                # as cancellable so an unreadable status can never strand
                # orphan artifacts (issue #783 review PRR-004).
                if cur_status in ("indexed", "partial", "error"):
                    deletes_run = False  # survivor keeps its content
                elif cur_status is None:
                    deletes_run = False  # row gone: #692 discard owns cleanup
                elif cur_status == "cancelled":
                    # The route (or a sibling unwind) already accepted the
                    # cancel: this generation's artifacts are orphans.
                    deletes_run = True
                else:
                    cur = conn.execute(
                        "UPDATE files SET status = 'cancelled', "
                        "processed_at = CURRENT_TIMESTAMP "
                        "WHERE id = ? AND status IN ('pending', 'processing')",
                        (file_id,),
                    )
                    deletes_run = cur.rowcount > 0
                if deletes_run:
                    conn.execute(
                        "DELETE FROM document_atoms WHERE file_id = ?", (file_id,)
                    )
                    conn.execute(
                        "DELETE FROM failed_chunks WHERE file_id = ?", (file_id,)
                    )
                conn.commit()
        except Exception:  # noqa: BLE001 — cleanup must never raise
            logger.warning(
                "cancel cleanup: sqlite cleanup failed for file_id=%s",
                file_id,
                exc_info=True,
            )
        if not deletes_run:
            # Survivor row (indexed/partial/error) or deleted row: keep the
            # terminal phase truthful without touching another writer's
            # artifacts.
            await clear_progress(
                self.pool,
                file_id,
                phase=PHASE_CANCELLED,
                phase_message="Cancelled by user",
            )
            self.clear_cancel(file_id)
            return
        try:
            if self.vector_store is not None:
                await self.vector_store.delete_by_file(str(file_id))
        except Exception:  # noqa: BLE001 — cleanup must never raise
            logger.warning(
                "cancel cleanup: vector delete failed for file_id=%s; "
                "queueing on vector_delete_pending sweep",
                file_id,
                exc_info=True,
            )
            try:
                async with self._write_session() as conn:
                    # INSERT .. SELECT: a no-op when the row itself is gone.
                    conn.execute(
                        "INSERT OR IGNORE INTO vector_delete_pending "
                        "(file_id, vault_id) "
                        "SELECT id, vault_id FROM files WHERE id = ?",
                        (file_id,),
                    )
                    conn.commit()
            except Exception:  # noqa: BLE001 — cleanup must never raise
                logger.warning(
                    "cancel cleanup: vector_delete_pending insert failed for "
                    "file_id=%s",
                    file_id,
                    exc_info=True,
                )
        await clear_progress(
            self.pool,
            file_id,
            phase=PHASE_CANCELLED,
            phase_message="Cancelled by user",
        )
        self.clear_cancel(file_id)

    def _raise_if_file_row_missing(self, file_id: int) -> None:
        """Abort an in-flight generation when its ``files`` row was deleted.

        A vault delete racing this worker must not end with the vector store
        holding chunks for a row (and vault) that no longer exists, with no
        tombstone (issue #692 / T1-21-S2-10). Mirrors the row-gone branch of
        ``_retry_staleness_reason`` at the last durable write: called right
        before the vector write, it closes the whole parse/embed span. The
        narrower guard-to-``add_chunks`` window it cannot close is covered by
        the post-write compensating discard in
        :meth:`_discard_vectors_if_row_gone`.
        """
        conn = self.pool.get_connection()
        try:
            row = conn.execute(
                "SELECT 1 FROM files WHERE id = ?", (file_id,)
            ).fetchone()
        finally:
            self.pool.release_connection(conn)
        if row is None:
            raise DocumentProcessingError(
                "File row removed mid-ingest (vault deleted); "
                "discarding this generation"
            )

    async def _discard_vectors_if_row_gone(
        self, file_id: int, *, target_kwargs: Optional[dict] = None
    ) -> None:
        """Post-write compensation for the residual staleness window.

        The pre-write gate above cannot cover the awaits between itself and
        ``add_chunks`` (dimension check, ``init_table``). Re-reading the row
        after the write and deleting the just-written chunks when the row is
        gone closes that window mechanically instead of relying on the
        disclosed bound (issue #692 review follow-up F-004). Raises the same
        staleness error so the caller's failure path runs; the raised error
        is a no-op on the already-deleted row.
        """
        # get_connection_async: the async checkout surface used across this
        # module (#645 AC2 — no sync pooled checkouts inside async defs).
        conn = await self.pool.get_connection_async()
        try:
            row = conn.execute(
                "SELECT 1 FROM files WHERE id = ?", (file_id,)
            ).fetchone()
        finally:
            self.pool.release_connection(conn)
        if row is not None:
            return
        try:
            deleted = await self.vector_store.delete_by_file(
                str(file_id), **(target_kwargs or {})
            )
            logger.warning(
                "Discarded %d vector chunk(s) written for file_id=%s after "
                "its row was removed mid-ingest",
                deleted,
                file_id,
            )
        except Exception:  # noqa: BLE001 — compensation must not mask the gate
            logger.exception(
                "Failed to discard vectors written for removed file_id=%s; "
                "the next vault/vector reconciliation should sweep them",
                file_id,
            )
        raise DocumentProcessingError(
            "File row removed mid-ingest (vault deleted); wrote and "
            "discarded this generation"
        )

    async def _live_vector_count(self, file_id: int) -> Optional[int]:
        """Live vector count for a file, or None when unavailable (W15).

        Uses the vector store's count API when it exists; any failure logs a
        warning and returns None so the caller falls back to the derived count
        (non-fatal, plan W15 edge case).
        """
        count_fn = getattr(self.vector_store, "count_by_file", None)
        if count_fn is None:
            return None
        try:
            return int(await count_fn(str(file_id)))
        except Exception:  # noqa: BLE001 - count is an optimization, not a gate
            logger.warning(
                "Vector count after chunk retry failed for file_id=%s; "
                "falling back to the derived chunk_count",
                file_id,
                exc_info=True,
            )
            return None

    async def _retry_failed_chunks_locked(self, file_id: int) -> Dict[str, Any]:
        # issue #704 (T1-25-KR-09): permit-held session like every other
        # committing write in this file.
        async with self._write_session() as conn:
            row = conn.execute(
                "SELECT id, vault_id, file_hash, status, chunks_failed, "
                "chunk_count, active_generation_hash FROM files WHERE id = ?",
                (file_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"File {file_id} not found")
            if row["status"] not in ("indexed", "partial"):
                raise ValueError(
                    f"File {file_id} status is '{row['status']}', must be "
                    "'indexed' or 'partial'"
                )
            vault_id = int(row["vault_id"])
            file_hash = str(row["file_hash"] or "")
            generation_hash = row["active_generation_hash"]
            previous_chunk_count = row["chunk_count"] or 0

            stored_rows = conn.execute(
                "SELECT id, chunk_index, chunk_text, chunk_metadata, attempts "
                "FROM failed_chunks WHERE file_id = ? ORDER BY chunk_index",
                (file_id,),
            ).fetchall()

        if not stored_rows:
            return {
                "retried": 0,
                "succeeded": 0,
                "still_failing": 0,
                "failed_chunk_indices": [],
            }

        # [N3] Pre-check: skip chunks already in LanceDB (reconcile a prior
        # partial success where LanceDB write succeeded but SQLite DELETE failed).
        to_embed: list[tuple[Any, ProcessedChunk, Dict[str, Any]]] = []
        already_indexed_ids: list[int] = []  # failed_chunks row ids to clear
        for srow in stored_rows:
            chunk, stored_meta = self._rebuild_chunk_from_stored(srow)
            chunk_scale = chunk.metadata.get("chunk_scale", "default")
            if settings.reupload_safe_order:
                record_id = (
                    f"{file_id}_{file_hash[:8]}_{chunk_scale}_{chunk.chunk_index}"
                )
            else:
                record_id = chunk.chunk_uid or self._build_chunk_uid(file_id, chunk)
            existing = await self.vector_store.get_chunks_by_uid([record_id])
            if existing:
                already_indexed_ids.append(int(srow["id"]))
                continue
            to_embed.append((srow, chunk, stored_meta))

        # Embed + build records per chunk. [N1] embed_batch(fail_fast=False)
        # returns None placeholders on failure rather than raising.
        records: list[Dict[str, Any]] = []
        succeeded_row_ids: list[int] = []
        still_failing: list[tuple[int, str, int]] = []  # (chunk_index, reason, row_id)
        for srow, chunk, stored_meta in to_embed:
            embs, failed = await self.embedding_service.embed_batch(
                [chunk.text], fail_fast=False
            )
            if failed or not embs or embs[0] is None:
                reason = "embedding returned None"
                still_failing.append(
                    (int(srow["chunk_index"]), reason, int(srow["id"]))
                )
                continue
            record = self._rebuild_vector_record_from_stored(
                file_id=file_id,
                vault_id=vault_id,
                file_hash=file_hash,
                stored_row=srow,
                stored_meta=stored_meta,
                chunk=chunk,
                embedding=embs[0],
            )
            records.append(record)
            succeeded_row_ids.append(int(srow["id"]))

        # [W16] Generation guard AFTER the embedding awaits, immediately before
        # the vector write: a newer generation that completed while this retry
        # was parked mid-await must see ZERO publication from the old one.
        if records:
            stale_reason = self._retry_staleness_reason(
                file_id, file_hash, generation_hash
            )
            if stale_reason is not None:
                logger.info(
                    "Suppressing stale chunk retry for file_id=%s: %s "
                    "(nothing written, no counters mutated)",
                    file_id,
                    stale_reason,
                )
                return {
                    "retried": len(to_embed),
                    "succeeded": 0,
                    "still_failing": 0,
                    "failed_chunk_indices": [],
                    "skipped_stale_generation": True,
                }
            # [N3] LanceDB write FIRST (not transactional with SQLite), then
            # SQLite cleanup in its own try/except. On SQLite failure the next
            # retry's pre-check reconciles already-indexed chunks.
            await self.vector_store.add_chunks(records)

        # [W15] Authoritative chunk_count: prefer the store's live count when a
        # count API exists (computed BEFORE opening the cleanup connection so
        # no pooled connection is held across the vector-store await).
        live_count: Optional[int] = None
        if records or already_indexed_ids:
            live_count = await self._live_vector_count(file_id)

        async with self._write_session() as conn:
            try:
                clear_ids = already_indexed_ids + succeeded_row_ids
                if clear_ids:
                    placeholders = ",".join("?" * len(clear_ids))
                    conn.execute(
                        f"DELETE FROM failed_chunks WHERE id IN ({placeholders})",
                        clear_ids,
                    )
                for _chunk_index, reason, row_id in still_failing:
                    conn.execute(
                        "UPDATE failed_chunks SET attempts = attempts + 1, error_reason = ? "
                        "WHERE id = ?",
                        (reason, row_id),
                    )
                if records or already_indexed_ids:
                    if live_count is None:
                        # Derived from the ACTUAL rows cleared/written by this
                        # attempt: cleared rows are (reconciled + written) vectors
                        # that are now live but were never counted (they had failed
                        # embedding), so the derived count converges to the same
                        # value the live count would report.
                        live_count = (
                            previous_chunk_count
                            + len(already_indexed_ids)
                            + len(records)
                        )
                    # issue #704 (T1-25-K-02): a recovered file must leave the
                    # 'partial' state — when no failed chunks remain, promote the
                    # row back to 'indexed' so it rejoins unscoped retrieval and
                    # KMS. Any still-failing chunk keeps the current status.
                    conn.execute(
                        "UPDATE files SET chunks_failed = "
                        "(SELECT COUNT(*) FROM failed_chunks WHERE file_id = ?), "
                        "partial_embeddings = (SELECT CASE WHEN COUNT(*) > 0 "
                        "THEN 1 ELSE 0 END FROM failed_chunks WHERE file_id = ?), "
                        "chunk_count = ?, "
                        "status = CASE WHEN (SELECT COUNT(*) FROM failed_chunks "
                        "WHERE file_id = ?) = 0 THEN 'indexed' ELSE status END "
                        "WHERE id = ? AND status IN ('indexed', 'partial')",
                        (file_id, file_id, int(live_count), file_id, file_id),
                    )
                else:
                    conn.execute(
                        "UPDATE files SET chunks_failed = "
                        "(SELECT COUNT(*) FROM failed_chunks WHERE file_id = ?), "
                        "partial_embeddings = (SELECT CASE WHEN COUNT(*) > 0 "
                        "THEN 1 ELSE 0 END FROM failed_chunks WHERE file_id = ?) "
                        "WHERE id = ? "
                        "AND status IN ('indexed', 'partial')",
                        (file_id, file_id, file_id),
                    )
                conn.commit()
            except sqlite3.Error:
                conn.rollback()
                logger.warning(
                    "SQLite cleanup after retry of file %d failed; LanceDB already "
                    "updated. Next retry will reconcile.",
                    file_id,
                    exc_info=True,
                )

        return {
            "retried": len(to_embed),
            "succeeded": len(records),
            "still_failing": len(still_failing),
            "failed_chunk_indices": [ci for ci, _r, _rid in still_failing],
        }

    async def _swap_in_enriched_vector_records(
        self, records: List[Dict[str, Any]], old_ids: List[str]
    ) -> None:
        if self.vector_store is None:
            return
        add_then_delete = getattr(self.vector_store, "add_chunks_then_delete_ids", None)
        if add_then_delete is not None:
            await add_then_delete(records, old_ids)
            return
        # Test doubles may not implement the add-first primitive. Keep the
        # fallback add-first as well so failures do not remove the base index.
        await self.vector_store.add_chunks(records)
        for old_id in old_ids:
            delete_by_id = getattr(self.vector_store, "delete_by_id", None)
            if delete_by_id is not None:
                await delete_by_id(old_id)

    def should_enqueue_enrichment(
        self,
        chunks: List[ProcessedChunk],
        vault_id: Optional[int] = None,
        file_id: Optional[int] = None,
    ) -> bool:
        """Return True when post-index enrichment is configured and possible.

        Uses the effective enrichment setting at file > vault > global priority.
        """
        if file_id is not None and vault_id is not None:
            enabled = is_enrichment_enabled_for_file(file_id, vault_id)
        else:
            enabled = is_enrichment_enabled_for_vault(vault_id)
        return bool(
            enabled
            and self._llm_client is not None
            and self.embedding_service is not None
            and self.vector_store is not None
            and self._select_chunks_for_enrichment(chunks)
        )

    def _embedding_cache_identity(self) -> Tuple[str, str, str, int]:
        """Immutable embedding-contract identity for cache keys (issue #513 W24).

        Returns (model_id, model_revision, doc_prefix, dim). Model identity and
        the effective document prefix come from the embedding service when it
        exposes them (its properties read live settings), otherwise directly
        from settings; the revision component additionally binds the concrete
        embedder implementation (class identity + provider mode) so swapping
        the embedder invalidates cached vectors. Deterministic for a given
        service/settings state — required for cross-run cache hits; any change
        to any component changes the key (invalidation by construction).
        """
        service = self.embedding_service
        model_id = str(
            getattr(service, "embedding_model", None) or settings.embedding_model
        )
        try:
            provider_mode = str(getattr(service, "provider_mode", "") or "")
        except Exception:  # noqa: BLE001 - identity must never fail the embed path
            provider_mode = ""
        # The serving endpoint is part of the identity (issue #698): two
        # endpoints serving the same model id can hold different weights
        # (re-served/re-quantized), so without the URL the persistent cache
        # silently reuses stale vectors across an endpoint change. Note:
        # changing this format intentionally invalidates every pre-existing
        # cache row (a one-time cold cache — the table is a rebuildable
        # cache, not user data).
        model_revision = (
            f"{type(service).__name__}:{provider_mode}:{settings.ollama_embedding_url}"
        )
        prefix_obj = getattr(service, "embedding_doc_prefix", None)
        doc_prefix = (
            str(prefix_obj)
            if prefix_obj is not None
            else str(settings.embedding_doc_prefix or "")
        )
        try:
            dim = int(getattr(service, "embedding_dim", None) or settings.embedding_dim)
        except (TypeError, ValueError):
            dim = int(settings.embedding_dim)
        return model_id, model_revision, doc_prefix, dim

    async def _embed_with_cache(self, texts: List[str]) -> List[List[float]]:
        """Embed texts through the persistent embedding cache (issue #513 W24).

        Cache hits (byte-identical text under the same immutable embedding
        contract) reuse the stored vector and skip the provider entirely — an
        enrichment-only retry over unchanged text performs ZERO provider embeds
        (C32). Only misses are sent to ``embed_batch(fail_fast=False)`` and the
        successful vectors are stored back. Error semantics match the previous
        uncached call: a batch failure or an embedding-count mismatch raises
        ``DocumentProcessingError``.
        """
        model_id, model_revision, doc_prefix, dim = self._embedding_cache_identity()
        keys = [
            embedding_cache_key(model_id, model_revision, doc_prefix, dim, text)
            for text in texts
        ]
        cached = embedding_cache_lookup(keys)
        embeddings: List[Optional[List[float]]] = [cached.get(key) for key in keys]
        miss_positions = [i for i, emb in enumerate(embeddings) if emb is None]
        if miss_positions:
            miss_texts = [texts[i] for i in miss_positions]
            embeddings_result = await self.embedding_service.embed_batch(
                miss_texts, fail_fast=False
            )
            miss_embeddings, failed_batch_indices = embeddings_result
            if failed_batch_indices:
                failed_positions = [
                    i for i, emb in enumerate(miss_embeddings) if emb is None
                ]
                raise DocumentProcessingError(
                    f"Embedding failed for enriched chunks: batches "
                    f"{failed_batch_indices}, failed text positions "
                    f"{failed_positions}"
                )
            if len(miss_embeddings) != len(miss_texts):
                raise DocumentProcessingError(
                    f"Enriched embedding count mismatch: expected {len(miss_texts)}, got {len(miss_embeddings)}"
                )
            stored: List[Tuple[str, List[float]]] = []
            for i, emb in zip(miss_positions, miss_embeddings):
                if emb is None:
                    continue
                embeddings[i] = emb
                stored.append((keys[i], emb))
            # Off the event loop (issue #698): store() executes the SQLite
            # INSERT plus the entry-cap prune (an unindexed ORDER BY scan at
            # the default 50k cap measured 400-600 ms) — every comparable
            # sync seam in this file is offloaded via asyncio.to_thread.
            await asyncio.to_thread(embedding_cache_store, stored)
        missing_positions = [i for i, emb in enumerate(embeddings) if emb is None]
        if missing_positions:
            raise DocumentProcessingError(
                f"Enriched embedding failed for text positions {missing_positions} "
                f"(invalid or unembeddable texts: expected {len(texts)}, got "
                f"{len(texts) - len(missing_positions)})"
            )
        return embeddings  # type: ignore[return-value]  # no None members past the guard

    async def run_enrichment_job(
        self,
        *,
        file_id: int,
        file_path: str,
        vault_id: int,
        file_hash: str,
        chunks: List[ProcessedChunk],
        document_text: str,
    ) -> None:
        """Run optional chunk enrichment after the base file is already indexed."""
        if not self._is_enrichment_job_current(file_id, file_hash):
            return

        enrichment_service = self._get_chunk_enrichment_service(vault_id, file_id)
        if enrichment_service is None:
            await self.set_enrichment_status(file_id, "complete")
            return

        candidates = self._candidate_chunks_for_enrichment(file_id, chunks)
        if not candidates:
            await self.set_enrichment_status(file_id, "complete")
            return

        source_filename = Path(file_path).name
        processing_started_at = await self.set_enrichment_status(file_id, "processing")
        try:
            chunk_dicts = [
                {
                    "chunk_uid": chunk_uid,
                    "text": chunk.text,
                    "metadata": chunk.metadata,
                }
                for chunk, chunk_uid in candidates
            ]
            enrichments = await enrichment_service.enrich_chunks(
                chunk_dicts, document_title=source_filename
            )
            enrichment_by_uid = {
                enrichment.chunk_id: enrichment
                for enrichment in enrichments
                if self._enrichment_has_content(enrichment)
            }
            if not enrichment_by_uid:
                await self.set_enrichment_status(file_id, "complete")
                return

            enrichment_texts = [
                self._search_text_with_enrichment(
                    chunk,
                    enrichment_by_uid.get(self._build_chunk_uid(file_id, chunk)),
                )
                for chunk in chunks
            ]
            self._validate_chunk_sizes(enrichment_texts, source_filename)
            # [W24] Persistent embedding reuse: byte-identical texts under the
            # same immutable embedding contract are served from the disk-backed
            # cache; only misses hit the provider (issue #513 C32).
            embeddings = await self._embed_with_cache(enrichment_texts)
            if len(embeddings) != len(chunks):
                raise DocumentProcessingError(
                    f"Enriched embedding count mismatch: expected {len(chunks)}, got {len(embeddings)}"
                )

            records = [
                self._build_vector_record(
                    file_id=file_id,
                    vault_id=vault_id,
                    file_hash=file_hash,
                    chunk=chunk,
                    embedding=embedding,
                    sparse_emb=None,
                    document_text=document_text,
                    enrichment=enrichment_by_uid.get(
                        self._build_chunk_uid(file_id, chunk)
                    ),
                )
                for chunk, embedding in zip(chunks, embeddings)
            ]
            old_ids = [str(record["id"]) for record in records]
            for record in records:
                record["id"] = f"{record['id']}__enriched"
            if not self._is_enrichment_job_current(file_id, file_hash):
                await self._mark_enrichment_stale_if_current_job(
                    file_id, processing_started_at
                )
                return
            await self._swap_in_enriched_vector_records(records, old_ids)
            await self._verify_vector_rows_visible(file_id)
            await self.set_enrichment_status(file_id, "complete")
        except asyncio.CancelledError:
            # The status write must survive a pending cancellation (the
            # sync-era write always completed; issue #704 made it async).
            # Run it as a REFERENCED detached task: a bare asyncio.shield
            # orphan loses its only reference when this handler re-raises,
            # so a second cancel or loop close could destroy it mid-write
            # ("Task was destroyed but it is pending"), leaking the permit
            # and a pool slot and leaving enrichment_status='processing'
            # forever (issue #704 review, PRR-006).
            status_task = asyncio.ensure_future(
                self.set_enrichment_status(
                    file_id,
                    "error",
                    "Enrichment job cancelled before completion",
                )
            )
            self._detached_status_tasks.add(status_task)
            status_task.add_done_callback(self._detached_status_tasks.discard)
            raise
        except Exception as e:
            # Raw exception stays in the server log; enrichment_error is
            # returned to vault readers (issue #562). The document is already
            # parsed and indexed at this point, so a parse-failure code would
            # be contradictory — use the dedicated enrichment code.
            logger.warning(
                "Post-index enrichment failed for file_id=%s: %s", file_id, e
            )
            await self.set_enrichment_status(
                file_id, "error", format_ingest_error(INGEST_ERROR_ENRICHMENT_FAILED)
            )

    @with_retry(
        max_attempts=3, retry_exceptions=(sqlite3.Error,), raise_last_exception=True
    )
    def _check_duplicate(
        self, file_hash: str, conn: sqlite3.Connection, vault_id: int
    ) -> Optional[sqlite3.Row]:
        """
        Check if a file with the given hash already exists and is live.

        A ``partial`` row owns its content slot exactly like an ``indexed``
        one (issue #693 / T1-05-K2-09): until it reaches a terminal state,
        re-ingesting the same content must be reported as a duplicate rather
        than creating a second document.

        Args:
            file_hash: The hash of the file to check
            conn: Database connection
            vault_id: The vault ID to check for duplicates in (defaults to 1)

        Returns:
            The existing file row if found and live (indexed or partial),
            None otherwise
        """
        cursor = conn.execute(
            "SELECT * FROM files WHERE file_hash = ? AND vault_id = ? "
            "AND status IN ('indexed', 'partial')",
            (file_hash, vault_id),
        )
        return cursor.fetchone()

    @with_retry(
        max_attempts=3, retry_exceptions=(sqlite3.Error,), raise_last_exception=True
    )
    def _check_duplicate_in_flight(
        self, file_hash: str, conn: sqlite3.Connection, vault_id: int
    ) -> Optional[sqlite3.Row]:
        """
        Check for any existing file with this hash in any non-terminal state.

        Used by the async upload route to collapse two concurrent uploads of the
        same content to a single ingestion. ``_check_duplicate`` only matches
        ``status='indexed'`` (its legacy semantic for synchronous callers); the
        async route inserts rows with ``status='pending'`` first, so it needs to
        reject duplicates at every stage of the pipeline, not just indexed.

        Args:
            file_hash: The hash of the file to check.
            conn: Database connection.
            vault_id: Vault to scope the check to.

        Returns:
            The existing row when one of {pending, processing, indexed,
            partial} matches, else None. Rows in 'error' state are
            intentionally NOT matched — re-uploading a previously-failed
            file should be allowed. A ``partial`` row owns its content slot
            until it reaches a terminal state (issue #693 / T1-05-K2-09).
        """
        cursor = conn.execute(
            """
            SELECT * FROM files
            WHERE file_hash = ?
              AND vault_id = ?
              AND status IN ('pending', 'processing', 'indexed', 'partial')
            ORDER BY id DESC
            LIMIT 1
            """,
            (file_hash, vault_id),
        )
        return cursor.fetchone()

    def _insert_or_get_file_record(
        self,
        file_path: str,
        file_hash: str,
        conn: sqlite3.Connection,
        vault_id: int,
        source: str = "upload",
        email_subject: Optional[str] = None,
        email_sender: Optional[str] = None,
    ) -> int:
        """
        Insert a new file record or update existing one, returning the file ID.

        Transient ``sqlite3.Error`` failures (lock contention) are retried by
        the decorated single-attempt body; only after the retries are exhausted
        is the surviving error rolled back and re-typed here, so the external
        contract (``DocumentProcessingError`` after exhaustion) is unchanged
        (issue #704, T1-25-KR-11 — the retry previously could never fire
        because the interior except re-typed the error first).

        Args:
            file_path: Path to the file
            file_hash: Computed hash of the file
            conn: Database connection
            vault_id: The vault ID for the file (defaults to 1)
            source: Source of the file ('upload', 'scan', 'email')
            email_subject: Subject line for email-sourced files
            email_sender: Sender address for email-sourced files

        Returns:
            The file ID (database row ID)

        Raises:
            DocumentProcessingError: If database operations fail
        """
        try:
            return self._insert_or_get_file_record_once(
                file_path,
                file_hash,
                conn,
                vault_id,
                source=source,
                email_subject=email_subject,
                email_sender=email_sender,
            )
        except sqlite3.Error as e:
            # Retries already exhausted by the decorator (raise_last_exception
            # re-raises the last raw sqlite3.Error). Keep the caller-facing
            # conversion OUTSIDE the retried unit.
            conn.rollback()
            raise DocumentProcessingError(
                f"Database error while inserting/updating file record for "
                f"'{file_path}': {str(e)}"
            ) from e

    @with_retry(
        max_attempts=3, retry_exceptions=(sqlite3.Error,), raise_last_exception=True
    )
    def _insert_or_get_file_record_once(
        self,
        file_path: str,
        file_hash: str,
        conn: sqlite3.Connection,
        vault_id: int,
        source: str = "upload",
        email_subject: Optional[str] = None,
        email_sender: Optional[str] = None,
    ) -> int:
        """Single attempt; sqlite3.Error propagates so @with_retry can retry."""
        path = Path(file_path)
        file_name = path.name
        file_size = path.stat().st_size
        file_type = path.suffix.lower() if path.suffix else None
        now = datetime.now(UTC).isoformat()
        path_str = str(file_path)

        try:
            # Check if file record already exists by path
            cursor = conn.execute(
                "SELECT id FROM files WHERE file_path = ?", (path_str,)
            )
            existing = cursor.fetchone()

            if existing:
                # Validate existing row id
                existing_id = existing["id"]
                if existing_id is None:
                    raise DocumentProcessingError(
                        f"Existing file record for '{path_str}' has invalid NULL id"
                    )
                file_id = int(existing_id)

                # Update existing record
                conn.execute(
                    """UPDATE files
                       SET file_hash = ?, file_size = ?, file_type = ?, vault_id = ?,
                           source = ?, email_subject = ?, email_sender = ?,
                           status = 'pending', error_message = NULL,
                           enrichment_status = NULL, enrichment_error = NULL,
                           enrichment_updated_at = NULL,
                           modified_at = ?, processed_at = NULL
                       WHERE id = ?""",
                    (
                        file_hash,
                        file_size,
                        file_type,
                        vault_id,
                        source,
                        email_subject,
                        email_sender,
                        now,
                        file_id,
                    ),
                )
                # Draft Room evidence freshness (SPEC section 12.6, issue #516
                # DRAFT-001): an overwrite changes the file's content hash, so
                # evidence snapshotted against the old content must be
                # invalidated before this transaction commits. Best effort: the
                # hook cannot raise into the ingest path, and its hash guard
                # makes a same-content overwrite a no-op.
                from app.services.draft_evidence_freshness import on_document_changed

                on_document_changed(conn, file_id=file_id, new_content_sha256=file_hash)
            else:
                # Insert new record
                cursor = conn.execute(
                    """INSERT INTO files
                       (file_path, file_name, file_hash, file_size, file_type, vault_id,
                        source, email_subject, email_sender, status, created_at, modified_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                    (
                        path_str,
                        file_name,
                        file_hash,
                        file_size,
                        file_type,
                        vault_id,
                        source,
                        email_subject,
                        email_sender,
                        now,
                        now,
                    ),
                )
                lastrowid = cursor.lastrowid
                if lastrowid is None:
                    raise DocumentProcessingError(
                        f"Failed to insert file record for '{path_str}': lastrowid is None"
                    )
                file_id = int(lastrowid)

            # Commit within the context of this method
            conn.commit()
            return file_id

        except sqlite3.IntegrityError as e:
            conn.rollback()
            if "file_hash" in str(e).lower() or "unique" in str(e).lower():
                raise DuplicateFileError(
                    "A file with the same content already exists in this vault"
                ) from e
            raise DocumentProcessingError(f"Database integrity error: {e}") from e
        # No `except sqlite3.Error` here by design (issue #704, T1-25-KR-11):
        # re-typing the transient error inside the retried body hid it from
        # @with_retry, so "database is locked" failed on the first attempt
        # forever. Raw sqlite3.Error propagates to the decorator; the public
        # wrapper re-types whatever survives the retries.

    @with_retry(
        max_attempts=3, retry_exceptions=(sqlite3.Error,), raise_last_exception=True
    )
    def _update_status(
        self,
        file_id: int,
        status: str,
        conn: sqlite3.Connection,
        chunk_count: Optional[int] = None,
        error_message: Optional[str] = None,
        chunks_failed: int = 0,
        partial_embeddings: Optional[int] = None,
    ) -> None:
        """
        Update the processing status of a file.

        Args:
            file_id: The database ID of the file
            status: New status ('pending', 'processing', 'indexed', 'partial',
                'error') — success-family callers pass 'indexed' (both paths,
                full success) or 'partial' (upload path, partial success)
            conn: Database connection
            chunk_count: Number of chunks produced (optional)
            error_message: Error message if status is 'error' (optional)
            chunks_failed: Chunks dropped due to embedding failures (Issue #221)
            partial_embeddings: 1 when this success had chunks_failed > 0
                (partial content indexed), else 0 (issue #513 AC27). Only
                meaningful for success-family statuses; None leaves the column
                untouched.

        Note:
            This method does not commit - caller is responsible for transaction management.
        """
        now = datetime.now(UTC).isoformat()

        if status in ("indexed", "partial"):
            # Success-family transitions (issue #513 W9/W14/AC27): full
            # success lands in 'indexed'; a partial success (some chunks
            # failed embedding, retrievable content exists) is 'indexed' +
            # partial_embeddings=1 on the scan/sync path (frozen C6) or
            # 'partial' on the upload/reindex path (frozen C27). Both write
            # the truthful chunk accounting and the marker, and CLEAR the
            # attempt-scoped error_message so a historical failure never
            # persists on a successful status.
            marker = partial_embeddings if partial_embeddings is not None else 0
            conn.execute(
                """UPDATE files
                   SET status = ?, chunk_count = ?, chunks_failed = ?,
                       partial_embeddings = ?, error_message = NULL,
                       processed_at = ?, modified_at = ?
                   WHERE id = ?""",
                (status, chunk_count, chunks_failed, int(marker), now, now, file_id),
            )
        elif status == "error":
            # issue #704 review (PRR-002): guarded like the other terminal
            # writers — an accepted cancel (or an AC3 restore) that landed
            # between the caller's registry check and this commit must not
            # be buried under 'error'.
            conn.execute(
                """UPDATE files
                   SET status = ?, error_message = ?, modified_at = ?
                   WHERE id = ?
                   AND status IN ('pending', 'processing')""",
                (status, error_message, now, file_id),
            )
        else:
            conn.execute(
                """UPDATE files
                   SET status = ?, modified_at = ?
                   WHERE id = ?""",
                (status, now, file_id),
            )
        # Note: No commit here - caller manages transactions

    def _is_schema_file(self, file_path: str) -> bool:
        """
        Check if a file should be processed as a schema file.

        Args:
            file_path: Path to the file

        Returns:
            True if the file has a schema extension (.sql, .ddl)
        """
        return Path(file_path).suffix.lower() in self.SCHEMA_EXTENSIONS

    def _is_spreadsheet_file(self, file_path: str) -> bool:
        """
        Check if a file should be processed as a spreadsheet.

        Args:
            file_path: Path to the file.

        Returns:
            True if the file has a spreadsheet extension (.csv, .xls, .xlsx).
        """
        return Path(file_path).suffix.lower() in self.SPREADSHEET_EXTENSIONS

    def _is_image_file(self, file_path: str) -> bool:
        """
        Check if a file should be processed as an image.

        Args:
            file_path: Path to the file.

        Returns:
            True if the file has an image extension.
        """
        return Path(file_path).suffix.lower() in self.IMAGE_EXTENSIONS

    async def _process_spreadsheet_file(
        self,
        file_path: str,
        file_id: int,
        generation_hash: str = "",
        parser_fingerprint: str = "",
    ) -> Tuple[List[ProcessedChunk], str, ParsedDocument]:
        """
        Process a spreadsheet file using SpreadsheetParser.

        Runs the synchronous SpreadsheetParser.parse() in a thread pool to
        avoid blocking the event loop on large files.

        Args:
            file_path: Path to the .csv, .xls, or .xlsx file.
            file_id: Database ID of the file.
            generation_hash: Source generation fingerprint.
            parser_fingerprint: Parser provenance string.

        Returns:
            Tuple of (List of ProcessedChunk objects, joined text, ParsedDocument).

        Raises:
            DocumentParseError: If SpreadsheetParser.parse() raises.
            DocumentProcessingError: If the file parses but produces no chunks
                (e.g., all sheets are empty).
        """
        sheet_chunks = await _parse_with_deadline(
            file_path,
            lambda: self.spreadsheet_parser.parse(file_path),
            stage="Spreadsheet parsing",
        )

        if not sheet_chunks:
            raise DocumentProcessingError(
                f"Spreadsheet '{file_path}' contains no readable data. "
                "All sheets may be empty or all rows may be blank."
            )

        processed_chunks: List[ProcessedChunk] = []
        atoms: List[DocumentAtom] = []
        total = len(sheet_chunks)

        for idx, chunk_data in enumerate(sheet_chunks):
            # One atom per chunk -> exact, provable provenance.
            atom = DocumentAtom(
                atom_id=make_atom_id(file_id, generation_hash, idx),
                schema_version=ATOM_SCHEMA_VERSION,
                file_id=file_id,
                generation_hash=generation_hash,
                ordinal=idx,
                kind=AtomKind.TEXT,
                raw_text=chunk_data["text"],
                parser_fingerprint=parser_fingerprint,
            )
            atoms.append(atom)
            chunk = ProcessedChunk(
                text=chunk_data["text"],
                metadata={
                    **chunk_data["metadata"],
                    "chunk_index": idx,
                    "total_chunks": total,
                    "chunk_scale": "default",  # Spreadsheet files bypass multi-scale
                    "atom_id": atom.atom_id,
                    "atom_kind": atom.kind.value,
                    "generation_hash": generation_hash,
                },
                chunk_index=idx,
            )
            processed_chunks.append(chunk)

        document_text = " ".join(c.text for c in processed_chunks)
        parsed = ParsedDocument(
            atoms=tuple(atoms),
            raw_projection=document_text,
            normalized_projection=project_text(atoms),
            parser_fingerprint=parser_fingerprint,
        )
        return processed_chunks, document_text, parsed

    async def _process_schema_file(
        self,
        file_path: str,
        file_id: int,
        generation_hash: str = "",
        parser_fingerprint: str = "",
    ) -> Tuple[List[ProcessedChunk], str, ParsedDocument]:
        """
        Process a schema file using SchemaParser.

        Args:
            file_path: Path to the schema file
            file_id: Database ID of the file.
            generation_hash: Source generation fingerprint.
            parser_fingerprint: Parser provenance string.

        Returns:
            Tuple of (List of ProcessedChunk objects, joined text, ParsedDocument).
        """
        schema_chunks = await _parse_with_deadline(
            file_path,
            lambda: self.schema_parser.parse(file_path),
            stage="Schema parsing",
        )

        if not schema_chunks:
            raise DocumentProcessingError(
                f"Schema file '{Path(file_path).name}' contains no CREATE "
                "TABLE statements and no other SQL statements to index "
                "(only comments or whitespace)."
            )

        processed_chunks = []
        atoms: List[DocumentAtom] = []
        for idx, chunk_data in enumerate(schema_chunks):
            # One atom per chunk -> exact, provable provenance.
            atom = DocumentAtom(
                atom_id=make_atom_id(file_id, generation_hash, idx),
                schema_version=ATOM_SCHEMA_VERSION,
                file_id=file_id,
                generation_hash=generation_hash,
                ordinal=idx,
                kind=AtomKind.CODE,
                raw_text=chunk_data["text"],
                parser_fingerprint=parser_fingerprint,
            )
            atoms.append(atom)
            chunk = ProcessedChunk(
                text=chunk_data["text"],
                metadata={
                    **chunk_data["metadata"],
                    "chunk_index": idx,
                    "total_chunks": len(schema_chunks),
                    "chunk_scale": "default",  # Schema files don't use multi-scale
                    "atom_id": atom.atom_id,
                    "atom_kind": atom.kind.value,
                    "generation_hash": generation_hash,
                },
                chunk_index=idx,
            )
            processed_chunks.append(chunk)

        document_text = " ".join(c.text for c in processed_chunks)
        parsed = ParsedDocument(
            atoms=tuple(atoms),
            raw_projection=document_text,
            normalized_projection=project_text(atoms),
            parser_fingerprint=parser_fingerprint,
        )
        return processed_chunks, document_text, parsed

    def _publish_artifacts(
        self,
        file_id: int,
        vault_id: int,
        generation_hash: str,
        parsed: ParsedDocument,
        conn: Optional[sqlite3.Connection] = None,
        materialized: Optional[List[DocumentAsset]] = None,
    ) -> None:
        """Persist a generation's atoms/assets/stage rows (issue #460).

        Production callers pass ``conn`` from a ``_write_session`` so the
        publish runs under the shared write permit, and pre-materialize the
        asset bytes via ``_materialize_generation_assets`` BEFORE the session
        so synchronous disk I/O never runs under the permit (issue #704
        review, PRR-005). The ``conn=None`` path (tests and legacy sync
        callers only) checks out its own pooled connection — a sync function
        cannot acquire the asyncio permit — and releases it iff it created
        it; a passed-in connection is never released here (its session owns
        it). ``materialized=None`` materializes internally on the given
        connection's clock (permit-held for callers passing ``conn``), which
        keeps the test/legacy path byte-compatible.

        See ``_publish_artifacts_on`` for the transaction and failure
        contract.
        """
        own_conn = conn is None
        if own_conn:
            conn = self.pool.get_connection()
        try:
            if materialized is None:
                materialized_bucket: List[DocumentAsset] = []
                try:
                    self._materialize_generation_assets(
                        file_id,
                        vault_id,
                        generation_hash,
                        parsed,
                        bucket=materialized_bucket,
                    )
                except Exception:
                    # issue #704 review: a materialization failure must STILL
                    # tombstone the partial bytes written so far (issue #460
                    # property) and pair the failed file with its durable
                    # publish marker (plan change 6, publish_reached=False
                    # branch) before re-raising so the caller errors the file.
                    conn.rollback()
                    self._tombstone_materialized_assets_on(
                        conn,
                        materialized_bucket,
                        file_id,
                        vault_id,
                        generation_hash,
                    )
                    self._record_publish_failure(
                        conn, file_id, vault_id, generation_hash, parsed
                    )
                    raise
                materialized = materialized_bucket
            self._publish_artifacts_on(
                conn,
                file_id,
                vault_id,
                generation_hash,
                parsed,
                materialized=materialized,
            )
        finally:
            if own_conn:
                self.pool.release_connection(conn)

    def _materialize_generation_assets(
        self,
        file_id: int,
        vault_id: int,
        generation_hash: str,
        parsed: ParsedDocument,
        bucket: Optional[List[DocumentAsset]] = None,
    ) -> List[DocumentAsset]:
        """Materialize deferred asset bytes to disk (NO connection/permit).

        Split out of the publish transaction so synchronous disk I/O does not
        extend the shared write permit's hold (issue #704 review, PRR-005).
        A materialization failure here raises to the caller before any
        session is opened. ``bucket`` (optional) receives every asset as it
        materializes, so a mid-loop failure still exposes the partial bytes
        for tombstoning (issue #460 property).
        """
        materialized = bucket if bucket is not None else []
        for asset in list(parsed.assets):
            materialized.append(
                self._materialize_asset(
                    asset,
                    parsed.asset_payloads,
                    file_id,
                    vault_id,
                    generation_hash,
                )
            )
        return materialized

    def _publish_artifacts_on(
        self,
        conn: sqlite3.Connection,
        file_id: int,
        vault_id: int,
        generation_hash: str,
        parsed: ParsedDocument,
        materialized: Optional[List[DocumentAsset]] = None,
    ) -> None:
        """Publish one generation on the caller's connection (issue #460/#704).

        Called only after the new vectors are durable, so old-generation atom and
        asset rows are retired (and their filesystem bytes tombstoned) only once
        the replacement searchable state is in place — never before. The write
        and the old-generation retirement share one short transaction (no
        connection is held during parse/embed/vector work).

        A failure here is non-fatal to retrieval when it happens during the
        publish transaction itself: the transaction rolls back (no partial
        rows), any asset bytes already written for this generation are
        tombstoned for the sweep, and a durable ``stage='publish',
        status='failed_retryable'`` row in ``ingestion_stage_states`` records
        the stranded generation — the marker a later reprocess/reindex
        republish resolves and the one operators can query directly (SQL;
        no automated sweep consumes publish rows — issue #704, T1-25-KR-14,
        review PRR-013: the prior wording implied machinery that does not
        exist). A materialization (disk I/O) failure before
        any rows were published re-raises so the caller marks the file
        errored; the marker is recorded on that path too.
        """
        atoms = list(parsed.atoms)
        asset_specs = list(parsed.assets)
        if not atoms and not asset_specs:
            return
        stage_states = [
            {
                "stage": "parse",
                "status": "succeeded",
                "attempts": 1,
                "completed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            {
                "stage": "publish",
                "status": "succeeded",
                "attempts": 1,
                "completed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        ]
        if materialized is None:
            materialized = self._materialize_generation_assets(
                file_id, vault_id, generation_hash, parsed
            )
        publish_reached = False
        try:
            # Materialization may have happened before the session opened
            # (production callers, PRR-005); the in-session path above keeps
            # the issue #460 property that a partial I/O failure tombstones
            # exactly the bytes written so far.
            publish_reached = True
            artifact_store.publish_generation(
                conn,
                file_id=file_id,
                vault_id=vault_id,
                generation_hash=generation_hash,
                atoms=atoms,
                assets=materialized,
                stage_states=stage_states,
                parser_fingerprint=parsed.parser_fingerprint,
                implementation_version=str(ATOM_SCHEMA_VERSION),
            )
            conn.commit()
        except Exception as exc:  # noqa: BLE001 - publish failure is compensated
            logger.warning(
                "Failed to publish artifacts for file_id=%s gen=%s: %s",
                file_id,
                generation_hash,
                exc,
            )
            # Compensation: roll back the failed publish entirely (discarding any
            # partial rows and any old-generation retirement it performed
            # mid-way), then tombstone ONLY the bytes materialized for this
            # attempt — they have no committed row now, so the sweep must collect
            # them. A compensation failure is surfaced (not silently swallowed)
            # so orphaned bytes require operator reconciliation (issue #460,
            # final-critic findings 2/4).
            conn.rollback()
            self._tombstone_materialized_assets_on(
                conn, materialized, file_id, vault_id, generation_hash
            )
            # issue #704 (T1-25-KR-14): the rollback above discarded the publish
            # stage rows too, so record the durable failure marker now — this
            # generation's atoms/assets are stranded until a reprocess/reindex
            # republishes, and nothing else in the DB says so.
            self._record_publish_failure(
                conn, file_id, vault_id, generation_hash, parsed
            )
            if not publish_reached:
                # A materialization (disk I/O) failure occurred before any rows
                # were published. Its partial bytes were tombstoned above;
                # re-raise so the caller marks the file errored.
                raise
        finally:
            # Nothing to release here: the caller's session (or the wrapper's
            # own_conn finally) owns the connection.
            pass

    def _record_publish_failure(
        self,
        conn: sqlite3.Connection,
        file_id: int,
        vault_id: int,
        generation_hash: str,
        parsed: ParsedDocument,
    ) -> None:
        """Best-effort durable marker for a compensated publish failure.

        Never raises into the caller's compensation path; a marker failure is
        logged at ERROR so the stranded generation still surfaces to the
        operator (issue #460 reconciliation + issue #704 T1-25-KR-14).
        """
        try:
            # issue #704 review (concurrency-M11): a FAILED RE-publish of a
            # generation whose rows are already committed strands nothing
            # (the rollback restores them) — writing the marker would
            # overwrite the committed succeeded row. Only a generation with
            # no committed atoms gets the marker.
            existing = conn.execute(
                "SELECT COUNT(*) FROM document_atoms "
                "WHERE file_id = ? AND generation_hash = ?",
                (file_id, generation_hash),
            ).fetchone()[0]
            if existing:
                logger.info(
                    "Publish retry failed for file_id=%s gen=%s but the "
                    "generation's committed rows are intact; no marker "
                    "written",
                    file_id,
                    generation_hash,
                )
                return
            artifact_store.record_stage_failure(
                conn,
                file_id=file_id,
                vault_id=vault_id,
                generation_hash=generation_hash,
                parser_fingerprint=parsed.parser_fingerprint,
                implementation_version=str(ATOM_SCHEMA_VERSION),
                stage="publish",
                status="failed_retryable",
                error_code="PUBLISH_COMPENSATED",
            )
        except Exception as marker_exc:  # noqa: BLE001 - advisory marker only
            logger.error(
                "Failed to record publish-failure marker for file_id=%s "
                "gen=%s; the generation's artifacts are stranded and require "
                "operator reconciliation: %s",
                file_id,
                generation_hash,
                marker_exc,
            )

    def _tombstone_materialized_assets_on(
        self,
        conn: sqlite3.Connection,
        materialized: List[DocumentAsset],
        file_id: int,
        vault_id: int,
        generation_hash: str,
    ) -> None:
        """Tombstone on the caller's connection (issue #460 review, PRR-004/012).

        No-op when nothing was materialized. A compensation failure is surfaced
        at ERROR (not silently swallowed) so orphaned bytes require operator
        reconciliation.
        """
        if not materialized:
            return
        try:
            # issue #704 review (OOB-5, pre-existing): a failed RE-publish of
            # a generation whose asset rows are already committed must not
            # tombstone bytes those committed rows still reference — filter
            # the tombstone set against the file's committed rel_paths.
            committed = {
                row["rel_path"]
                for row in conn.execute(
                    "SELECT rel_path FROM document_assets WHERE file_id = ?",
                    (file_id,),
                ).fetchall()
            }
            rel_paths = [
                a.rel_path for a in materialized if a.rel_path not in committed
            ]
            if not rel_paths:
                logger.info(
                    "Publish compensation for file_id=%s gen=%s: all "
                    "materialized assets are still referenced by committed "
                    "rows; nothing tombstoned",
                    file_id,
                    generation_hash,
                )
                return
            artifact_store.enqueue_asset_cleanup(
                conn,
                file_id=file_id,
                vault_id=vault_id,
                rel_paths=rel_paths,
                generation_hash=generation_hash,
            )
            conn.commit()
        except Exception as exc2:  # noqa: BLE001
            conn.rollback()
            logger.error(
                "Artifact compensation failed for file_id=%s gen=%s; "
                "new-generation bytes may be orphaned and require operator "
                "reconciliation: %s",
                file_id,
                generation_hash,
                exc2,
            )

    def _materialize_asset(
        self,
        asset: DocumentAsset,
        payloads: Dict[str, bytes],
        file_id: int,
        vault_id: int,
        generation_hash: str,
    ) -> DocumentAsset:
        """Write a deferred asset's bytes to disk, returning the materialized asset.

        If no payload was planned for this asset (e.g. already materialized, or a
        best-effort plan produced no bytes), the asset is returned unchanged. The
        planned path is identical to the materialized path, so provenance and the
        tombstone path remain consistent.
        """
        data = payloads.get(asset.asset_id) if payloads else None
        if data is None:
            return asset
        return artifact_store.store_asset_bytes(
            file_id=file_id,
            vault_id=vault_id,
            generation_hash=generation_hash,
            data=data,
            mime_type=asset.mime_type,
            width=asset.width,
            height=asset.height,
            extra_metadata=asset.metadata,
        )

    async def _process_image_file(
        self,
        file_path: str,
        file_id: int,
        vault_id: int,
        generation_hash: str,
        parser_fingerprint: str,
    ) -> Tuple[List[ProcessedChunk], str, ParsedDocument]:
        """
        Process an image file using image_processor + image_search.

        Extracts OCR text and image metadata, stores the raster bytes as a
        content-addressed binary asset under the vault artifact root, builds a
        single ``IMAGE`` document atom with exact provenance, and produces a
        single chunk that flows through the standard embedding/indexing pipeline.

        Failure: if image processing fails (missing OCR stack, unreadable
        image), raises DocumentProcessingError carrying the
        PARSER_UNAVAILABLE ingest code so the file is failed with an
        accurate, distinguishable cause (issue #703).

        Args:
            file_path: Path to the image file.
            file_id: Database ID of the file.
            vault_id: Owning vault (asset root selection).
            generation_hash: Source generation fingerprint.
            parser_fingerprint: Parser provenance string.

        Returns:
            Tuple of (list of ProcessedChunk objects, searchable document text,
            ParsedDocument with atoms and assets).

        Raises:
            DocumentProcessingError: When image processing fails (with the
                PARSER_UNAVAILABLE ingest code) or the parse deadline fires.
        """
        # Route the SYNCHRONOUS image worker through the same deadline
        # wrapper as the other parsers (issue #703). The async public
        # entry (process_image) wraps this same sync helper in its own
        # to_thread; going through the wrapper's thread keeps the
        # in-flight slot held until the OCR worker thread itself exits —
        # wrapping the coroutine instead would release the slot at
        # coroutine cancellation while the abandoned OCR thread ran on.
        image_result: ImageProcessingResult = await _parse_with_deadline(
            file_path,
            lambda: _process_image_sync(file_path),
            stage="Image processing",
        )

        filename = Path(file_path).name
        searchable_text = (
            build_searchable_text(image_result, filename)
            if image_result.success
            else ""
        )

        # Parse-quality diagnostics (issue #514 PRODUCT-ENH-06): this IS the
        # OCR seam — the raster page is reported through the shared producer
        # (a synthetic single-page element carries the OCR text), so an
        # unreadable or text-less image reveals its zero-text page in the
        # same payload that reports chunk/index state. Computed before the
        # failure raise. ocr_used records whether OCR actually
        # contributed — a failed image must not present itself as
        # OCR-sourced (issue #703 review).
        try:
            from types import SimpleNamespace as _SimpleNamespace

            from app.services.document_extraction import (
                build_extraction_diagnostics,
            )

            await self._persist_extraction_diagnostics(
                file_id,
                build_extraction_diagnostics(
                    [
                        _SimpleNamespace(
                            category="Image",
                            text=searchable_text,
                            metadata=_SimpleNamespace(page_number=1),
                        )
                    ],
                    ocr_used=image_result.success,
                ),
            )
        except Exception as exc:  # noqa: BLE001 — advisory parse metadata only
            logger.warning(
                "Extraction diagnostics build failed for file_id=%s: %s",
                file_id,
                exc,
            )

        if not image_result.success:
            logger.warning(
                "Image processing failed for %s: %s. File not indexed "
                "(issue #703: an image whose OCR could not run is a "
                "degraded failure, not a clean success).",
                file_path,
                image_result.error,
            )
            error = DocumentProcessingError(
                "Image processing failed; OCR is unavailable or the "
                f"image could not be read ({image_result.error_code})."
            )
            # The issue's own named classification: a missing/broken OCR
            # stack is a parser-unavailability failure, distinguishable
            # from a content parse failure (issue #703 review).
            error.ingest_error_code = INGEST_ERROR_PARSER_UNAVAILABLE
            raise error

        if not searchable_text or not searchable_text.strip():
            # issue #704 (T1-25-KR-06): the caller raises DocumentProcessingError
            # on the empty chunk list ("No extractable content found"), so the
            # file ends 'error' — the old wording claimed it was recorded.
            logger.warning(
                "Image %s produced no searchable text; the ingest will fail "
                "with a no-extractable-content error.",
                file_path,
            )
            return (
                [],
                searchable_text,
                ParsedDocument(atoms=(), parser_fingerprint=parser_fingerprint),
            )

        # Plan the raster asset (opaque id + confined path) WITHOUT touching
        # disk. The raw bytes are stashed on the ParsedDocument and materialized
        # on disk only inside _publish_artifacts, immediately before the rows are
        # published — so bytes can never linger unowned if the generation is
        # never published (pre-publish failure or no vector store), and are
        # tombstoned by compensation on a publish failure (issue #460,
        # final-critic finding 3). Failure here is non-fatal: the atom and
        # searchable text still index, and provenance is simply omitted.
        asset: Optional[DocumentAsset] = None
        asset_payloads: Dict[str, bytes] = {}
        try:
            image_data = Path(file_path).read_bytes()
            fmt = str(image_result.metadata.get("format") or "").lower()
            asset_id = hashlib.sha256(image_data).hexdigest()
            asset = DocumentAsset(
                asset_id=asset_id,
                file_id=file_id,
                generation_hash=generation_hash,
                sha256=asset_id,
                rel_path=artifact_store.compute_asset_rel_path(
                    file_id, generation_hash, asset_id
                ),
                mime_type=f"image/{fmt}" if fmt else None,
                width=image_result.metadata.get("width"),
                height=image_result.metadata.get("height"),
                byte_size=len(image_data),
                metadata={"format": fmt, "mode": image_result.metadata.get("mode")},
            )
            asset_payloads[asset_id] = image_data
        except Exception as exc:  # noqa: BLE001 - asset planning is best-effort
            logger.warning("Failed to plan image asset for %s: %s", file_path, exc)

        atom = DocumentAtom(
            atom_id=make_atom_id(file_id, generation_hash, 0),
            schema_version=ATOM_SCHEMA_VERSION,
            file_id=file_id,
            generation_hash=generation_hash,
            ordinal=0,
            kind=AtomKind.IMAGE,
            raw_text=searchable_text,
            asset_id=asset.asset_id if asset else None,
            parser_fingerprint=parser_fingerprint,
            metadata={
                "width": image_result.metadata.get("width"),
                "height": image_result.metadata.get("height"),
                "format": image_result.metadata.get("format"),
                "mode": image_result.metadata.get("mode"),
            },
        )

        # Build chunk metadata mirroring the structure used by text/spreadsheet
        # paths, plus exact atom/asset provenance.
        chunk_metadata: Dict[str, Any] = {
            "source_type": "image",
            "file_path": str(file_path),
            "chunk_scale": "default",
            "image_width": image_result.metadata.get("width"),
            "image_height": image_result.metadata.get("height"),
            "image_format": image_result.metadata.get("format"),
            "image_mode": image_result.metadata.get("mode"),
            "atom_id": atom.atom_id,
            "atom_kind": atom.kind.value,
            "generation_hash": generation_hash,
        }
        if asset is not None:
            chunk_metadata["asset_id"] = asset.asset_id
        chunk_metadata["file_id"] = str(file_id)

        chunk_index = 0
        chunk_metadata["chunk_index"] = chunk_index
        chunk_metadata["total_chunks"] = 1

        chunk = ProcessedChunk(
            text=searchable_text,
            metadata=chunk_metadata,
            chunk_index=chunk_index,
        )

        parsed = ParsedDocument(
            atoms=(atom,),
            raw_projection=searchable_text,
            normalized_projection=searchable_text,
            parser_fingerprint=parser_fingerprint,
            assets=(asset,) if asset else (),
            asset_payloads=asset_payloads,
        )
        return [chunk], searchable_text, parsed

    async def _process_document_file(
        self,
        file_path: str,
        file_id: Optional[int] = None,
        stage_timings: Optional[dict[str, float]] = None,
        generation_hash: str = "",
        parser_fingerprint: str = "",
    ) -> Tuple[List[ProcessedChunk], str, ParsedDocument]:
        """
        Process a document file using DocumentParser and SemanticChunker.

        Args:
            file_path: Path to the document file
            file_id: Database ID of the file (required for multi-scale indexing)
            stage_timings: optional timing accumulator
            generation_hash: Source generation fingerprint.
            parser_fingerprint: Parser provenance string.

        Returns:
            Tuple of (List of ProcessedChunk objects, document text as string,
            ParsedDocument of typed atoms).
        """
        elements = await _parse_with_deadline(
            file_path,
            lambda: self.parser.parse(file_path),
            stage="Document parsing",
            stage_timings=stage_timings,
            timing_key="parse_ms",
        )
        # Parse-quality diagnostics (issue #514 PRODUCT-ENH-06): derived from
        # the elements this parse actually produced and persisted as JSON so
        # the status payload can reveal omissions (low-content pages, dropped
        # structures) even when every chunk embeds. ocr_used stays False on
        # this seam — the honest OCR fact is threaded from the image pipeline
        # seam (_process_image_file), not inferred here.
        try:
            from app.services.document_extraction import (
                build_extraction_diagnostics,
            )

            await self._persist_extraction_diagnostics(
                file_id, build_extraction_diagnostics(elements)
            )
        except Exception as exc:  # noqa: BLE001 — advisory parse metadata only
            logger.warning(
                "Extraction diagnostics build failed for file_id=%s: %s",
                file_id,
                exc,
            )
        # Join all element texts for use as context in contextual chunking.
        # Unchanged raw join: parent-window offsets and retrieval depend on it
        # byte-for-byte (issue #460 preserves existing text retrieval behavior).
        document_text = "\n".join([str(e) for e in elements])

        # Parser-neutral ordered atoms (issue #460). Document chunks generally
        # merge multiple elements, so per-chunk singular atom provenance is NOT
        # asserted here; the atoms themselves are durable and validation-scoped.
        atoms = parse_elements_to_atoms(
            elements,
            file_id=file_id or 0,
            generation_hash=generation_hash,
            parser_fingerprint=parser_fingerprint,
        )
        parsed = ParsedDocument(
            atoms=tuple(atoms),
            raw_projection=document_text,
            normalized_projection=project_text(atoms),
            parser_fingerprint=parser_fingerprint,
        )

        # Check if multi-scale indexing is enabled
        chunk_started_at = time.monotonic()
        if settings.multi_scale_indexing_enabled:
            # Parse chunk sizes from settings
            scale_strs = settings.multi_scale_chunk_sizes.split(",")
            scales = [int(s.strip()) for s in scale_strs if s.strip()]

            all_chunks = []
            for scale in scales:
                # Create chunker for this scale
                chunk_overlap = int(scale * settings.multi_scale_overlap_ratio)
                scale_chunker = SemanticChunker(
                    chunk_size_chars=scale, chunk_overlap_chars=chunk_overlap
                )

                # Chunk elements with this scale's chunker
                scale_chunks = await asyncio.to_thread(
                    scale_chunker.chunk_elements, elements
                )

                # Add chunk_scale metadata to each chunk
                for idx, chunk in enumerate(scale_chunks):
                    chunk.metadata["chunk_scale"] = str(scale)
                    # Use scale-aware index format for multi-scale
                    if file_id is not None:
                        chunk.metadata["chunk_index"] = f"{scale}_{idx}"

                all_chunks.extend(scale_chunks)

            total_chunks = len(all_chunks)
            scale_list = [str(s) for s in scales]
            logger.info(
                "Multi-scale chunking processed %d chunks across scales %s",
                total_chunks,
                scale_list,
            )

            if stage_timings is not None:
                _add_elapsed_ms(stage_timings, "chunk_ms", chunk_started_at)
            return all_chunks, document_text, parsed
        else:
            # Existing single-scale behavior — use the live-settings chunker so
            # chunk_size_chars / chunk_overlap_chars changes apply per-document.
            active_chunker = self._get_chunker()
            if isinstance(active_chunker, EmbeddingSemanticChunker):
                # Embedding-based chunking is async (it embeds sentence windows)
                # and operates on the joined document text rather than elements.
                chunks = await active_chunker.chunk_text(document_text)
            else:
                chunks = await asyncio.to_thread(
                    active_chunker.chunk_elements, elements
                )
            if stage_timings is not None:
                _add_elapsed_ms(stage_timings, "chunk_ms", chunk_started_at)
            return chunks, document_text, parsed

    async def _finalize_indexed_success(
        self,
        *,
        file_id: int,
        vault_id: int,
        chunks: List[ProcessedChunk],
        document_text: str,
        chunks_failed_count: int,
        embeddings: Optional[List[List[float]]] = None,
        partial_final_status: str = "indexed",
        vector_target: object | None = None,
    ) -> bool:
        """Shared success finalization for BOTH ingest entry points (W9).

        Returns False when the generation was voided by a concurrent settle
        (issue #783 review PRR-003) — the caller must return None so the
        transport treats the attempt as record-only instead of success.

        ``process_file`` and ``process_existing_file`` converge here with
        identical semantics (issue #513 C26): the final status write, the
        truthful partial marker ``partial_embeddings = 1`` when any chunk
        failed embedding this attempt else ``0`` (W14/AC27), parsed_text
        persistence + the gated wiki compile enqueue, the gated KMS compile
        enqueue, the advisory near-duplicate centroid recording (W26, strictly
        AFTER the final status commit), and progress cleanup. Success-family
        writes always CLEAR the attempt-scoped error_message (AC27 clause 2).

        The one intentional per-entry-point difference is the terminal status
        of a PARTIAL success (frozen-check reconciliation, issue #513 C6 vs
        C27): ``process_file`` (scan/sync path) pins ``'indexed'`` — its
        partial state is flagged via ``partial_embeddings`` (frozen C6
        asserts 'indexed' after a below-threshold embed failure). The default
        ``partial_final_status='indexed'`` encodes that contract;
        ``process_existing_file`` (upload/reindex path) passes ``'partial'``
        because frozen C27 requires the upload-path row to be distinguishable
        from full success by status alone. Full success is ``'indexed'`` on
        both paths (frozen C26 convergence).
        """
        # Finalize guard (issue #783): a cancel landing AFTER the pre-write
        # gate spans this whole vector-write + publish span. Detecting it
        # here must not merely skip this finalize — the already-written
        # vectors and atoms would orphan on a 'cancelled' row — so unwind the
        # generation with the same rollback and surface the cancellation to
        # the transport (raised outside the ingest paths' try blocks, so it
        # reaches the cancellation handlers directly).
        if self.is_cancel_requested(file_id):
            await self.rollback_cancelled_ingest(
                file_id, run_deletes=vector_target is None
            )
            raise IngestCancelledError(
                f"Ingest cancelled after vector write (file_id={file_id})",
                file_id=file_id,
            )
        final_status = partial_final_status if chunks_failed_count > 0 else "indexed"
        # issue #704 (T1-25-S2-10): parsed_text commits in the SAME
        # transaction as the terminal status. Previously it rode a second,
        # best-effort session whose failure was swallowed — a kill between
        # the two left an 'indexed' row carrying the previous generation's
        # parsed_text.
        _full_text = document_text or ""
        async with self._write_session() as conn:
            # Reviewer finding (Phase 4.5, Important): the route can accept a
            # cancel (guarded UPDATE rowcount 1, 200 returned) in the window
            # between the registry check above and this commit — an
            # unconditional success write would then bury the accepted
            # 'cancelled' under 'indexed'. The write is therefore conditioned
            # on the row still being pre-terminal, exactly like the route's
            # own flip; rowcount 0 means the cancel won the race and the
            # generation must unwind so the 200 survives.
            landed = (
                conn.execute(
                    "UPDATE files SET status = ?, chunk_count = ?, chunks_failed = ?, "
                    "partial_embeddings = ?, error_message = NULL, parsed_text = ?, "
                    "processed_at = ?, modified_at = ? "
                    "WHERE id = ? AND status IN ('pending', 'processing')",
                    (
                        final_status,
                        len(chunks),
                        chunks_failed_count,
                        1 if chunks_failed_count > 0 else 0,
                        _full_text,
                        datetime.now(UTC).isoformat(),
                        datetime.now(UTC).isoformat(),
                        file_id,
                    ),
                ).rowcount
                > 0
            )
            if landed:
                conn.commit()
            else:
                conn.rollback()
        if not landed:
            # Reviewer PRR-003: rowcount-0 also covers a concurrently-settled
            # or deleted row, not just a cancel. Only a row that is actually
            # 'cancelled' runs the destructive rollback; any other loser
            # (indexed/partial winner, vault delete) discards this generation
            # WITHOUT deleting the survivor's vectors/atoms.
            reread = None
            try:
                # issue #704 (T1-25-KR-09): read-only, but still a checkout in
                # a committing function — route it through the write permit
                # like every other checkout this file makes.
                async with self._write_session() as conn:
                    row = conn.execute(
                        "SELECT status FROM files WHERE id = ?", (file_id,)
                    ).fetchone()
                    reread = None if row is None else row["status"]
            except sqlite3.Error:
                reread = None
            if reread == "cancelled":
                await self.rollback_cancelled_ingest(
                    file_id, run_deletes=vector_target is None
                )
                raise IngestCancelledError(
                    f"Ingest cancelled before finalize committed (file_id={file_id})",
                    file_id=file_id,
                )
            logger.warning(
                "Finalize skipped for file_id=%d: row settled concurrently "
                "(status=%r); discarding generation without deletes",
                file_id,
                reread,
            )
            self.clear_cancel(file_id)
            # Void, not raise (PRR-003): a raised failure would retry toward
            # _mark_task_permanently_failed's unconditional 'error' write,
            # clobbering the concurrent winner. The callers translate the
            # False return into a quiet None result (the existing
            # record-only contract).
            return False

        # Enqueue wiki compile job (fire-and-forget; non-blocking). parsed_text
        # is stored on the files row (committed with the status above) so manual
        # recompile can use it without re-parsing the original file.
        try:
            from app.services.wiki_store import WikiStore as _WikiStore

            # issue #704 (T1-25-S2-10/T1-25-KR-09): everything in this block
            # runs inline on ONE session-held connection — the wiki_pending
            # set/clear previously took nested raw checkouts, which would
            # deadlock once every checkout routes through the shared write
            # permit.
            async with self._write_session() as conn:
                # Mark wiki_pending=1 and COMMIT before the wiki job is created
                # so the status route can report wiki_status="pending" during
                # the window before the wiki_compile_jobs row exists (issue
                # #704 review: set+clear inside ONE transaction made the flag
                # unobservable). A crash in the window strands wiki_pending=1,
                # exactly like base; the route falls back to the latest job
                # row and the next ingest clears the flag.
                conn.execute(
                    "UPDATE files SET wiki_pending = 1 WHERE id = ?", (file_id,)
                )
                conn.commit()
                if settings.wiki_enabled and settings.wiki_compile_on_ingest:
                    _WikiStore(conn).create_job(
                        vault_id=vault_id,
                        trigger_type="ingest",
                        trigger_id=f"file:{file_id}",
                        input_json={"file_id": file_id, "vault_id": vault_id},
                    )
                # Always clear the transient marker after the decision is made,
                # regardless of whether a job row was created.
                conn.execute(
                    "UPDATE files SET wiki_pending = 0 WHERE id = ?", (file_id,)
                )
                conn.commit()
        except Exception as _wiki_exc:
            logger.warning(
                "Failed to enqueue wiki ingest job for file_id=%d: %s",
                file_id,
                _wiki_exc,
            )
            # If wiki enqueue failed, don't leave wiki_pending=1 hanging.
            # (Outside every session here, so its own permit-held checkout is
            # safe.)
            await set_wiki_pending(self.pool, file_id, False)

        # Enqueue a KMS compile job so the document becomes a user-curatable,
        # full-text-searchable KMS entry. Independent of the wiki pipeline and
        # gated by the kms_enabled / kms_compile_on_ingest flags.
        try:
            from app.config import settings as _settings

            if _settings.kms_enabled and _settings.kms_compile_on_ingest:
                from app.services.kms_store import KMSStore as _KMSStore

                async with self._write_session() as conn:
                    _KMSStore(conn).create_job(
                        vault_id=vault_id,
                        trigger_type="ingest",
                        trigger_id=f"file:{file_id}",
                        input_json={"file_id": file_id, "vault_id": vault_id},
                    )
        except Exception as _kms_exc:
            logger.warning(
                "Failed to enqueue KMS ingest job for file_id=%d: %s",
                file_id,
                _kms_exc,
            )

        # [W26] Advisory near-duplicate centroid: computed from THIS
        # generation's chunk embeddings (already in memory — no re-embedding)
        # and recorded only after the final status commit succeeded, so a
        # failing ingest never leaves an orphan centroid row and re-ingest
        # replaces the row idempotently. Advisory and never fatal: skipped
        # silently when no embeddings are in scope (e.g. record-only ingests).
        # The file's full text is passed so pre-existing vault documents that
        # pre-date centroid recording can join the advisory group via the
        # deterministic text-fingerprint fallback (issue #513 C25).
        if embeddings:
            try:
                async with self._write_session() as conn:
                    # Off-loop (issue #697): the advisory scan re-tokenizes
                    # candidate texts and runs SQLite I/O — bounded, but far
                    # too slow for the event loop on large vaults. Pool
                    # connections are check_same_thread=False, so the pooled
                    # conn hands off to the worker thread safely.
                    await asyncio.to_thread(
                        near_duplicates.record_file_centroid,
                        conn,
                        vault_id,
                        file_id,
                        embeddings,
                        document_text=document_text or None,
                    )
            except Exception:  # noqa: BLE001 - advisory only, never fails ingest
                logger.warning(
                    "Near-duplicate centroid recording skipped for file_id=%d "
                    "(advisory only; ingest unaffected)",
                    file_id,
                    exc_info=True,
                )

        # Clear transient progress fields and pin phase=indexed so polls show
        # a clean "ready" snapshot rather than stale embedding counters.
        await clear_progress(self.pool, file_id)
        return True

    async def process_file(
        self,
        file_path: str,
        vault_id: int,
        source: str = "upload",
        email_subject: Optional[str] = None,
        email_sender: Optional[str] = None,
    ) -> ProcessedDocument:
        """
        Process a file with status tracking and deduplication.

        Args:
            file_path: Path to the file to process
            vault_id: The vault ID to associate the file with (required — no default)
            source: Source of the file ('upload', 'scan', 'email')
            email_subject: Subject line for email-sourced files
            email_sender: Sender address for email-sourced files

        Returns:
            ProcessedDocument containing file_id and chunks

        Raises:
            FileNotFoundError: If the file does not exist
            DuplicateFileError: If a file with the same hash is already indexed
            DocumentParseError: If parsing fails
        """
        # Validate file exists
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        if not path.is_file():
            raise FileNotFoundError(f"Path is not a file: {file_path}")

        # Compute file hash
        file_hash = compute_file_hash(file_path)
        stage_timings = _new_stage_timings()

        # Phase 1: Quick DB operations - get connection, do quick ops, release
        # issue #704 (T1-25-S2-04): remember the row's terminal status before
        # the insert resets it to 'pending' — a failure that predates any
        # vector write must restore it instead of demoting to 'error'.
        pre_ingest_status: Optional[str] = None
        pre_ingest_file_hash: Optional[str] = None
        prior_generation_disturbed = False
        async with self._write_session() as conn:
            # Check for duplicates
            duplicate = self._check_duplicate(file_hash, conn, vault_id)
            if duplicate:
                raise DuplicateFileError(
                    f"File with hash {file_hash} already indexed as '{duplicate['file_path']}'"
                )

            # issue #704 review (PRR-004b): capture the CONTENT hash too —
            # the insert below overwrites it with the new attempt's hash, and
            # a restore that left the new hash over the old generation's
            # vectors would make the duplicate check 409 the new content
            # forever.
            prior_row = conn.execute(
                "SELECT status, file_hash FROM files WHERE file_path = ?",
                (str(file_path),),
            ).fetchone()
            if prior_row is not None:
                pre_ingest_status = prior_row["status"]
                pre_ingest_file_hash = prior_row["file_hash"]

            # Insert or get file record (handles its own commit)
            file_id = self._insert_or_get_file_record(
                file_path,
                file_hash,
                conn,
                vault_id,
                source,
                email_subject,
                email_sender,
            )

            # Update status to processing. Guarded (issue #783 review
            # PRR-013): same cancel-reversion guard as the upload path.
            flipped = (
                conn.execute(
                    "UPDATE files SET status = 'processing', modified_at = ? "
                    "WHERE id = ? AND status != 'cancelled'",
                    (datetime.now(UTC).isoformat(), file_id),
                ).rowcount
                > 0
            )
            if not flipped:
                raise IngestCancelledError(
                    f"Ingest cancelled before processing started (file_id={file_id})",
                    file_id=file_id,
                )
            # Clear stale failed-chunk records from a prior partial-failure ingest
            # (Issue #396). Idempotent re-ingest must not accumulate stale rows.
            conn.execute("DELETE FROM failed_chunks WHERE file_id = ?", (file_id,))
            conn.commit()

        # Mark processing started + initial phase. Best-effort; ignored on failure.
        await set_phase(
            self.pool,
            file_id,
            phase=PHASE_PARSING,
            message="Parsing document",
            mark_processing_started=True,
        )

        # Phase 2: Long operations (NO connection held!)
        try:
            generation_hash, parser_fingerprint = _compile_generation(file_hash)
            parsed: ParsedDocument = ParsedDocument(
                atoms=(), parser_fingerprint=parser_fingerprint
            )
            # Process the file based on type
            if self._is_schema_file(file_path):
                stage_started_at = time.monotonic()
                chunks, document_text, parsed = await self._process_schema_file(
                    file_path, file_id, generation_hash, parser_fingerprint
                )
                _add_elapsed_ms(stage_timings, "parse_ms", stage_started_at)
            elif self._is_spreadsheet_file(file_path):
                await set_phase(
                    self.pool,
                    file_id,
                    phase=PHASE_PARSING,
                    message="Parsing spreadsheet (large spreadsheets can take several minutes)",
                )
                stage_started_at = time.monotonic()
                chunks, document_text, parsed = await self._process_spreadsheet_file(
                    file_path, file_id, generation_hash, parser_fingerprint
                )
                _add_elapsed_ms(stage_timings, "parse_ms", stage_started_at)
            elif self._is_image_file(file_path):
                await set_phase(
                    self.pool,
                    file_id,
                    phase=PHASE_PARSING,
                    message="Processing image",
                )
                stage_started_at = time.monotonic()
                chunks, document_text, parsed = await self._process_image_file(
                    file_path, file_id, vault_id, generation_hash, parser_fingerprint
                )
                _add_elapsed_ms(stage_timings, "parse_ms", stage_started_at)
            else:
                chunks, document_text, parsed = await self._process_document_file(
                    file_path,
                    file_id,
                    stage_timings,
                    generation_hash,
                    parser_fingerprint,
                )

            await set_phase(
                self.pool,
                file_id,
                phase=PHASE_EXTRACTING_TEXT,
                message="Text extracted",
            )

            # Define source_filename here so it is available to both contextual
            # chunking and chunk enrichment below, regardless of which branches run.
            source_filename = Path(file_path).name

            # Apply contextual chunking if enabled
            if settings.contextual_chunking_enabled and chunks and document_text:
                chunker = self._get_contextual_chunker()
                if chunker is not None:
                    logger.info(
                        "Contextual chunking: processing %d chunks for %s",
                        len(chunks),
                        source_filename,
                    )
                    stage_started_at = time.monotonic()
                    try:
                        await chunker.contextualize_chunks(
                            document_text=document_text,
                            chunks=chunks,
                            source_filename=source_filename,
                        )
                        logger.info(
                            "Contextual chunking: completed for %s (%d chunks contextualized)",
                            source_filename,
                            len(chunks),
                        )
                    except Exception as e:
                        logger.warning(
                            "Contextual chunking failed for %s: %s",
                            source_filename,
                            str(e),
                        )
                    finally:
                        _add_elapsed_ms(
                            stage_timings, "contextual_ms", stage_started_at
                        )
                # else: _get_contextual_chunker already logged a warning if needed

            # Compute parent window offsets for small-to-big retrieval (Issue #12)
            # Run after contextual chunking so raw_text is the pre-enrichment text.
            # Only meaningful when document_text is available (not spreadsheets/schemas).
            if not document_text and chunks and settings.parent_retrieval_enabled:
                logger.debug(
                    "parent_retrieval_enabled but document_text unavailable for %s "
                    "(schema/spreadsheet files do not support parent windows)",
                    Path(file_path).name,
                )
            if document_text and chunks:
                stage_started_at = time.monotonic()
                try:
                    compute_parent_windows(
                        chunks,
                        document_text,
                        window_chars=settings.parent_window_chars,
                    )
                except Exception as e:
                    logger.warning(
                        "compute_parent_windows failed for %s: %s — parent offsets will be None",
                        Path(file_path).name,
                        e,
                    )
                finally:
                    _add_elapsed_ms(stage_timings, "parent_window_ms", stage_started_at)

            if not chunks:
                raise DocumentProcessingError(
                    "No extractable content found in document. "
                    "The file may be empty, encrypted, or in an unsupported format."
                )

            await set_phase(
                self.pool,
                file_id,
                phase=PHASE_CHUNKING,
                message=f"Prepared {len(chunks)} chunks",
                total=len(chunks),
                processed=len(chunks),
                unit="chunks",
                percent=100.0,
            )

            # Chunks dropped due to partial embedding failures (Issue #221);
            # persisted to files.chunks_failed when the document is indexed.
            chunks_failed_count = 0
            # This generation's chunk embeddings, kept in scope for the
            # finalization helper's advisory centroid computation (W26).
            chunk_embeddings: List[List[float]] = []

            # Cancel gate A (issue #783): a cancel that landed during
            # parse/chunking skips the expensive embedding entirely.
            self._raise_if_cancelled(file_id)

            # Generate embeddings and store in vector store
            if self.embedding_service is not None and self.vector_store is not None:
                # Skip embedding/indexing if no chunks (status indexed with 0 chunks is acceptable)
                if chunks:
                    # Filter out empty/whitespace-only chunks before embedding
                    chunks = [c for c in chunks if c.text and c.text.strip()]
                    if not chunks:
                        raise DocumentProcessingError(
                            "All chunks were empty after filtering. "
                            "The document may contain only whitespace or unsupported content."
                        )

                    # Extract texts from chunks
                    texts = [c.text for c in chunks]

                    # Validate chunk sizes before embedding
                    self._validate_chunk_sizes(texts, source_filename)

                    # Phase: embedding. embed_batch is opaque per-batch internally,
                    # so we report total/processed at start and again on completion
                    # rather than streaming sub-batch progress (avoids reaching into
                    # the embedding service's batching boundary).
                    await set_phase(
                        self.pool,
                        file_id,
                        phase=PHASE_EMBEDDING,
                        message=f"Embedding {len(chunks)} chunks",
                        total=len(chunks),
                        processed=0,
                        unit="chunks",
                        percent=0.0,
                    )
                    # Generate dense embeddings (Harrier dense-only)
                    stage_started_at = time.monotonic()
                    embeddings_result = await self.embedding_service.embed_batch(
                        texts, fail_fast=False
                    )
                    _add_elapsed_ms(stage_timings, "embedding_ms", stage_started_at)
                    batch_embeddings, failed_batch_indices = embeddings_result

                    # Handle partial embedding failures. [W10] The failed TEXT
                    # positions are derived from the per-text None placeholders
                    # in batch_embeddings — a stable contract that identifies
                    # exactly the failed texts regardless of how the provider
                    # batched them (never from failed_batch_indices × a
                    # re-read batch-size setting, which desyncs when the
                    # setting changes mid-flight).
                    if failed_batch_indices or any(
                        emb is None for emb in batch_embeddings
                    ):
                        failed_chunk_indices = {
                            i for i, emb in enumerate(batch_embeddings) if emb is None
                        }

                        kept_chunks = []
                        kept_embeddings = []
                        for i, (chunk, emb) in enumerate(zip(chunks, batch_embeddings)):
                            if i not in failed_chunk_indices:
                                kept_chunks.append(chunk)
                                kept_embeddings.append(emb)

                        failure_pct = len(failed_chunk_indices) / len(chunks) * 100
                        logger.warning(
                            "Embedding partial failure: %d/%d chunks failed (%.0f%%). "
                            "Failed batch indices: %s",
                            len(failed_chunk_indices),
                            len(chunks),
                            failure_pct,
                            failed_batch_indices,
                        )

                        # Persist failed-chunk identity for chunk-scoped retry
                        # (Issue #396) BEFORE dropping them from the kept list.
                        # Uses the ORIGINAL chunks list (indices are into it).
                        try:
                            # issue #704 (T1-25-KR-09): permit-held session.
                            async with self._write_session() as _fc_conn:
                                self._persist_failed_chunks(
                                    file_id,
                                    chunks,
                                    failed_chunk_indices,
                                    document_text,
                                    _fc_conn,
                                )
                                _fc_conn.commit()
                        except sqlite3.Error:
                            logger.warning(
                                "Failed to persist failed-chunk identity for file %d "
                                "(retry-chunks endpoint will be unavailable for this ingest).",
                                file_id,
                                exc_info=True,
                            )

                        original_chunk_count = len(chunks)
                        chunks = kept_chunks
                        embeddings = kept_embeddings
                        chunks_failed_count = len(failed_chunk_indices)

                        if failure_pct > 50:
                            # The file is about to land in status='error', which
                            # the chunk-scoped retry endpoint rejects (409). The
                            # failed_chunks rows persisted above are therefore
                            # unreachable for chunk retry and would desync from
                            # files.chunks_failed (which stays 0 on the error
                            # path). Clear them so the only recovery is the
                            # whole-document retry, which re-ingests from scratch.
                            try:
                                # issue #704 (T1-25-KR-09): permit-held session.
                                async with self._write_session() as _abort_conn:
                                    _abort_conn.execute(
                                        "DELETE FROM failed_chunks WHERE file_id = ?",
                                        (file_id,),
                                    )
                                    _abort_conn.commit()
                            except sqlite3.Error:
                                logger.warning(
                                    "Could not clear failed_chunks rows before "
                                    ">50%% abort for file %d; rows will be cleared "
                                    "on next re-ingest.",
                                    file_id,
                                    exc_info=True,
                                )
                            _abort_error = DocumentProcessingError(
                                "Too many embedding failures: %d/%d chunks failed (%.0f%%). "
                                "Aborting document ingest."
                                % (
                                    len(failed_chunk_indices),
                                    original_chunk_count,
                                    failure_pct,
                                ),
                            )
                            # issue #704 (PRR-001): an embedder outage lands
                            # here because embed_batch(fail_fast=False)
                            # converts batch failures to None placeholders —
                            # without this code the outage persisted as
                            # PARSE_FAILED.
                            _abort_error.ingest_error_code = (
                                INGEST_ERROR_EMBEDDING_FAILED
                            )
                            raise _abort_error
                    else:
                        embeddings = batch_embeddings
                    chunk_embeddings = embeddings

                    sparse_embeddings = [None] * len(chunks)
                    await set_phase(
                        self.pool,
                        file_id,
                        phase=PHASE_EMBEDDING,
                        message="Embeddings ready",
                        total=len(chunks),
                        processed=len(chunks),
                        unit="chunks",
                        percent=100.0,
                    )

                    # Validate embeddings count matches chunks count
                    if len(embeddings) != len(chunks):
                        raise DocumentProcessingError(
                            f"Embedding count mismatch: expected {len(chunks)}, got {len(embeddings)}"
                        )
                    # Validate all embeddings are non-empty lists with consistent dimension
                    expected_dim = len(embeddings[0]) if embeddings[0] else 0
                    for i, emb in enumerate(embeddings):
                        if not emb or not isinstance(emb, list):
                            raise DocumentProcessingError(
                                f"Embedding {i} is empty or not a list"
                            )
                        if len(emb) != expected_dim:
                            raise DocumentProcessingError(
                                f"Embedding {i} has dimension {len(emb)}, expected {expected_dim}"
                            )
                    # Map chunks to records for vector store
                    records = []
                    for chunk, embedding, sparse_emb in zip(
                        chunks, embeddings, sparse_embeddings
                    ):
                        records.append(
                            self._build_vector_record(
                                file_id=file_id,
                                vault_id=vault_id,
                                file_hash=file_hash,
                                chunk=chunk,
                                embedding=embedding,
                                sparse_emb=sparse_emb,
                                document_text=document_text,
                            )
                        )

                    # Phase: writing vector index
                    await set_phase(
                        self.pool,
                        file_id,
                        phase=PHASE_WRITING_INDEX,
                        message="Writing vector index",
                        total=len(records),
                        processed=0,
                        unit="chunks",
                        percent=0.0,
                    )

                    # Last durable-write staleness gate (issue #692 /
                    # T1-21-S2-10): mirror the process_existing_file guard —
                    # a vault delete that committed during this worker's
                    # parse/embed span must not receive chunks for a row that
                    # no longer exists.
                    # Cancel gate B (issue #783): same seam — a cancel that
                    # landed during embedding writes no vectors and publishes
                    # no atoms.
                    self._raise_if_cancelled(file_id)
                    self._raise_if_file_row_missing(file_id)
                    # Initialize vector table with embedding dimension and add chunks
                    embedding_dim = len(embeddings[0])
                    stage_started_at = time.monotonic()
                    # [issue #691] Scan/email must refuse a dimension-changing
                    # ingest exactly like the upload path (same actionable
                    # error, before any write) instead of failing mid-write
                    # inside add_chunks against the old-dimension live table.
                    await self._ensure_live_dimension_compatible(embedding_dim)
                    # issue #704 (T1-25-S2-04): from here on the vector store is
                    # being mutated — a later failure may legitimately land
                    # 'error', because the prior generation may be gone.
                    prior_generation_disturbed = True
                    await self.vector_store.init_table(embedding_dim)
                    _add_elapsed_ms(stage_timings, "vector_write_ms", stage_started_at)

                    if settings.reupload_safe_order:
                        # Safe re-upload: insert new generation first, then delete old (Issue #13)
                        # Step 1+2: Insert new-generation chunks (hash-prefixed IDs);
                        # the generation prefix makes a same-hash reprocess a
                        # no-op instead of a duplicate append (issue #693).
                        vector_timings = await self.vector_store.add_chunks(
                            records,
                            generation_prefix=f"{file_id}_{file_hash[:8]}_",
                        )
                        _merge_vector_timings(stage_timings, vector_timings)
                        # Step 3: Delete old-generation chunks for this file
                        # (chunks whose IDs don't start with the new hash prefix)
                        stage_started_at = time.monotonic()
                        deleted = await self.vector_store.delete_old_generation_by_file(
                            str(file_id), file_hash[:8]
                        )
                        _add_elapsed_ms(
                            stage_timings, "vector_write_ms", stage_started_at
                        )
                        if deleted > 0:
                            logger.info(
                                "Safe re-upload: deleted %d old-generation chunks for file_id=%s",
                                deleted,
                                file_id,
                            )
                    else:
                        # Legacy: delete-then-insert (not crash-safe, kept for compat)
                        stage_started_at = time.monotonic()
                        await self.vector_store.delete_by_file(str(file_id))
                        _add_elapsed_ms(
                            stage_timings, "vector_write_ms", stage_started_at
                        )
                        vector_timings = await self.vector_store.add_chunks(records)
                        _merge_vector_timings(stage_timings, vector_timings)

                    await self._verify_vector_rows_visible(file_id)
                    await self._discard_vectors_if_row_gone(file_id)

                    # Publish the generation's atoms/assets/stage rows after the
                    # new vectors are durable, so old-generation atoms/assets are
                    # retired only once the replacement retrieval state is in place.
                    # issue #704 (T1-25-KR-09): the publish runs under the
                    # shared write permit like every other committing write.
                    # issue #704 review (PRR-005): asset bytes materialize
                    # BEFORE the session so synchronous disk I/O does not
                    # extend the permit's hold.
                    _materialized: List[DocumentAsset] = []
                    try:
                        # bucket= exposes partial bytes so a mid-loop disk
                        # failure still tombstones exactly what was written
                        # (issue #460 property; delta re-gate).
                        self._materialize_generation_assets(
                            file_id,
                            vault_id,
                            generation_hash,
                            parsed,
                            bucket=_materialized,
                        )
                    except Exception:
                        # issue #704 review: materialization failure still
                        # pairs the failed file with its durable marker
                        # (publish_reached=False branch semantics) and
                        # tombstones the partial bytes.
                        async with self._write_session() as _mk_conn:
                            self._tombstone_materialized_assets_on(
                                _mk_conn,
                                _materialized,
                                file_id,
                                vault_id,
                                generation_hash,
                            )
                            self._record_publish_failure(
                                _mk_conn,
                                file_id,
                                vault_id,
                                generation_hash,
                                parsed,
                            )
                        raise
                    async with self._write_session() as _pub_conn:
                        self._publish_artifacts(
                            file_id,
                            vault_id,
                            generation_hash,
                            parsed,
                            conn=_pub_conn,
                            materialized=_materialized,
                        )
        except IngestCancelledError:
            # issue #783: a user cancellation is not a failure — never write
            # status='error' over the terminal 'cancelled'; the transport's
            # cancellation handler unwinds the generation.
            raise
        except Exception as e:
            # The raw exception (server path + underlying parser error) stays
            # in the server log only; persisted fields are user-facing
            # (issue #562).
            logger.exception("Ingestion failed for file_id=%s", file_id)
            if self.is_cancel_requested(file_id):
                # issue #783: the failure raced a user cancel; the terminal
                # 'cancelled' must not be clobbered with 'error'.
                await self.rollback_cancelled_ingest(file_id)
                raise IngestCancelledError(
                    f"Ingest failed after cancel was requested (file_id={file_id})",
                    file_id=file_id,
                ) from e
            safe_error = redact_ingest_error(e)
            restored = False
            if not prior_generation_disturbed and pre_ingest_status in (
                "indexed",
                "partial",
            ):
                # issue #704 (T1-25-S2-04): the failure predated any write to
                # the vector store, so the prior generation's vectors are
                # intact — restore the prior terminal status instead of
                # demoting the row to 'error'. Guarded so a concurrent
                # cancel/settle still wins the race.
                async with self._write_session() as conn:
                    # issue #704 review (PRR-004): restore the CONTENT hash
                    # too — the failed attempt overwrote it, and leaving the
                    # new hash over the old generation's vectors would make
                    # the duplicate check 409 the new content forever. The
                    # aborted attempt's failed_chunks rows (parsed from the
                    # NEW content) are foreign to the restored generation —
                    # drop them; the pre-attempt counters are unrecoverable
                    # (the ingest start deleted the ledger), so the restored
                    # row reports zero remaining chunks.
                    restored = (
                        conn.execute(
                            "UPDATE files SET status = ?, file_hash = ?, "
                            "chunks_failed = 0, partial_embeddings = 0, "
                            "error_message = ?, modified_at = ? WHERE id = ? "
                            "AND status IN ('pending', 'processing')",
                            (
                                pre_ingest_status,
                                pre_ingest_file_hash,
                                safe_error,
                                datetime.now(UTC).isoformat(),
                                file_id,
                            ),
                        ).rowcount
                        > 0
                    )
                    if restored:
                        conn.execute(
                            "DELETE FROM failed_chunks WHERE file_id = ?",
                            (file_id,),
                        )
                    conn.commit()
                if restored:
                    logger.warning(
                        "Ingestion failed for file_id=%s before any vector "
                        "write; restored prior status %r (issue #704)",
                        file_id,
                        pre_ingest_status,
                    )
                    # clear_progress (not set_phase): a terminal row must not
                    # keep the aborted attempt's in-flight progress counters
                    # (PRR-016).
                    await clear_progress(
                        self.pool,
                        file_id,
                        phase=PHASE_INDEXED,
                        phase_message="Last ingest attempt failed before "
                        "replacing the index; the previous version remains "
                        "searchable",
                    )
                    raise
            # Phase 3: Update status to error on failure
            # Get connection again to update error status
            async with self._write_session() as conn:
                self._update_status(file_id, "error", conn, error_message=safe_error)
                conn.commit()
            # Surface error in the phase fields so the frontend can render it
            # without waiting for a status-route round-trip.
            await set_phase(
                self.pool,
                file_id,
                phase="error",
                message=safe_error,
                percent=None,
            )
            raise

        # Phase 3: Final DB operations - shared success finalization (status
        # write incl. the partial_embeddings marker, parsed_text + gated wiki
        # enqueue, gated KMS
        # enqueue, near-dup centroid, progress cleanup). Both ingest entry
        # points converge here with identical semantics (issue #513 W9/C26).
        stage_started_at = time.monotonic()
        finalized = await self._finalize_indexed_success(
            file_id=file_id,
            vault_id=vault_id,
            chunks=chunks,
            document_text=document_text,
            chunks_failed_count=chunks_failed_count,
            embeddings=chunk_embeddings,
        )
        if not finalized:
            logger.info(
                "Ingest generation voided for file_id=%d: row settled "
                "concurrently during finalize",
                file_id,
            )
            return None
        _add_elapsed_ms(stage_timings, "sqlite_finalize_ms", stage_started_at)

        logger.info(
            "Ingestion stage timings file_id=%s file_name=%s parse_ms=%.1f chunk_ms=%.1f "
            "contextual_ms=%.1f parent_window_ms=%.1f "
            "embedding_ms=%.1f vector_write_ms=%.1f optimize_ms=%.1f sqlite_finalize_ms=%.1f",
            file_id,
            Path(file_path).name,
            stage_timings["parse_ms"],
            stage_timings["chunk_ms"],
            stage_timings["contextual_ms"],
            stage_timings["parent_window_ms"],
            stage_timings["embedding_ms"],
            stage_timings["vector_write_ms"],
            stage_timings["optimize_ms"],
            stage_timings["sqlite_finalize_ms"],
        )

        return ProcessedDocument(
            file_id=file_id,
            chunks=chunks,
            document_text=document_text,
            file_hash=file_hash,
            file_path=file_path,
            vault_id=vault_id,
        )

    async def process_existing_file(
        self,
        file_id: int,
        file_path: str,
        vault_id: int,
        *,
        file_hash: Optional[str] = None,
        vector_target: object | None = None,
    ) -> ProcessedDocument:
        """Process a file whose `files` row already exists.

        Used by the async upload path: the route inserts the `files` row
        with status='pending' / phase='queued' and a duplicate-hash check
        already passed. The worker then calls this method instead of
        ``process_file`` to avoid re-running the duplicate check or
        creating a second row.

        Args:
            file_id: Existing ``files`` row id.
            file_path: Path of the file to process.
            vault_id: Owning vault.
            file_hash: Content hash computed by the caller (the upload route
                already hashes the bytes for dedup). When provided it is used
                as-is — the single content hash per upload feeds dedup,
                storage, and chunk identity (issue #513 W8/C30). When None
                (reindex / recovery callers) it is computed here.
            vector_target: Optional vector-store rebuild target (issue #513
                W13). When set, every vector-store call below (init_table,
                add_chunks, delete_*, visibility count) threads it as a
                trailing ``target=`` argument so a dimension-migrating reindex
                writes into the rebuild table instead of the live index.
                When None (bare call), a dimension mismatch against the live
                table is REFUSED with an actionable error before any write
                (issue #691): a single-file ingest must never open the staged
                rebuild itself — that swap would destroy every other indexed
                file's vectors. Dimension migration is the admin reindex
                job's business; it passes ``vector_target`` explicitly.

        Failure semantics match ``process_file``: status -> 'error',
        phase -> 'error', error_message populated. Wiki ingest job is
        enqueued at the end on success (same as ``process_file``).
        """
        path = Path(file_path)
        if not path.exists():
            # Surface as error on the existing row so the frontend can render
            # it. The persisted message is a stable code, not the server path
            # (issue #562); the path stays in the server log.
            logger.warning(
                "process_existing_file: file missing for file_id=%s: %s",
                file_id,
                file_path,
            )
            safe_error = format_ingest_error(INGEST_ERROR_FILE_MISSING)
            async with self._write_session() as conn:
                self._update_status(
                    file_id,
                    "error",
                    conn,
                    error_message=safe_error,
                )
                conn.commit()
            await set_phase(
                self.pool,
                file_id,
                phase="error",
                message=safe_error,
            )
            raise FileNotFoundError(f"File not found: {file_path}")
        if not path.is_file():
            logger.warning(
                "process_existing_file: path is not a file for file_id=%s: %s",
                file_id,
                file_path,
            )
            safe_error = format_ingest_error(INGEST_ERROR_FILE_MISSING)
            async with self._write_session() as conn:
                self._update_status(
                    file_id,
                    "error",
                    conn,
                    error_message=safe_error,
                )
                conn.commit()
            await set_phase(
                self.pool,
                file_id,
                phase="error",
                message=safe_error,
            )
            raise FileNotFoundError(f"Path is not a file: {file_path}")

        # Transition status: pending -> processing. Duplicate check intentionally
        # skipped — the route already ran it before inserting the row.
        # Guarded (issue #783 review PRR-013): a route-accepted cancel must
        # not be silently reverted to 'processing' for the whole parse span.
        # issue #704 (T1-25-S2-04): capture the row's pre-ingest terminal
        # status in the SAME session, atomically before the flip — a failure
        # that predates any vector write restores it instead of demoting.
        pre_ingest_status: Optional[str] = None
        pre_ingest_file_hash: Optional[str] = None
        prior_generation_disturbed = False
        async with self._write_session() as conn:
            status_row = conn.execute(
                "SELECT status, file_hash FROM files WHERE id = ?", (file_id,)
            ).fetchone()
            if status_row is not None:
                pre_ingest_status = status_row["status"]
                pre_ingest_file_hash = status_row["file_hash"]
            flipped = (
                conn.execute(
                    "UPDATE files SET status = 'processing', modified_at = ? "
                    "WHERE id = ? AND status != 'cancelled'",
                    (datetime.now(UTC).isoformat(), file_id),
                ).rowcount
                > 0
            )
            if not flipped:
                raise IngestCancelledError(
                    f"Ingest cancelled before processing started (file_id={file_id})",
                    file_id=file_id,
                )
            # Clear stale failed-chunk records from a prior partial-failure ingest
            # (Issue #396). Idempotent re-ingest must not accumulate stale rows.
            conn.execute("DELETE FROM failed_chunks WHERE file_id = ?", (file_id,))
            conn.commit()

        await set_phase(
            self.pool,
            file_id,
            phase=PHASE_PARSING,
            message="Parsing document",
            mark_processing_started=True,
        )

        # The remainder mirrors process_file Phase 2/3 exactly so behavior is
        # identical to the synchronous path. [W8] Reuse the caller-provided
        # content hash when available (single hash per upload); otherwise
        # re-derive it here for the safe-reupload chunk-id prefix logic.
        if file_hash is None:
            file_hash = compute_file_hash(file_path)
        stage_timings = _new_stage_timings()

        try:
            generation_hash, parser_fingerprint = _compile_generation(file_hash)
            parsed: ParsedDocument = ParsedDocument(
                atoms=(), parser_fingerprint=parser_fingerprint
            )
            if self._is_schema_file(file_path):
                stage_started_at = time.monotonic()
                chunks, document_text, parsed = await self._process_schema_file(
                    file_path, file_id, generation_hash, parser_fingerprint
                )
                _add_elapsed_ms(stage_timings, "parse_ms", stage_started_at)
            elif self._is_spreadsheet_file(file_path):
                await set_phase(
                    self.pool,
                    file_id,
                    phase=PHASE_PARSING,
                    message="Parsing spreadsheet (large spreadsheets can take several minutes)",
                )
                stage_started_at = time.monotonic()
                chunks, document_text, parsed = await self._process_spreadsheet_file(
                    file_path, file_id, generation_hash, parser_fingerprint
                )
                _add_elapsed_ms(stage_timings, "parse_ms", stage_started_at)
            elif self._is_image_file(file_path):
                await set_phase(
                    self.pool,
                    file_id,
                    phase=PHASE_PARSING,
                    message="Processing image",
                )
                stage_started_at = time.monotonic()
                chunks, document_text, parsed = await self._process_image_file(
                    file_path, file_id, vault_id, generation_hash, parser_fingerprint
                )
                _add_elapsed_ms(stage_timings, "parse_ms", stage_started_at)
            else:
                chunks, document_text, parsed = await self._process_document_file(
                    file_path,
                    file_id,
                    stage_timings,
                    generation_hash,
                    parser_fingerprint,
                )

            await set_phase(
                self.pool,
                file_id,
                phase=PHASE_EXTRACTING_TEXT,
                message="Text extracted",
            )

            source_filename = Path(file_path).name

            if settings.contextual_chunking_enabled and chunks and document_text:
                chunker = self._get_contextual_chunker()
                if chunker is not None:
                    stage_started_at = time.monotonic()
                    try:
                        await chunker.contextualize_chunks(
                            document_text=document_text,
                            chunks=chunks,
                            source_filename=source_filename,
                        )
                    except Exception as e:
                        logger.warning(
                            "Contextual chunking failed for %s: %s",
                            source_filename,
                            str(e),
                        )
                    finally:
                        _add_elapsed_ms(
                            stage_timings, "contextual_ms", stage_started_at
                        )

            if document_text and chunks:
                stage_started_at = time.monotonic()
                try:
                    compute_parent_windows(
                        chunks,
                        document_text,
                        window_chars=settings.parent_window_chars,
                    )
                except Exception as e:
                    logger.warning(
                        "compute_parent_windows failed for %s: %s",
                        Path(file_path).name,
                        e,
                    )
                finally:
                    _add_elapsed_ms(stage_timings, "parent_window_ms", stage_started_at)

            if not chunks:
                raise DocumentProcessingError(
                    "No extractable content found in document. "
                    "The file may be empty, encrypted, or in an unsupported format."
                )

            await set_phase(
                self.pool,
                file_id,
                phase=PHASE_CHUNKING,
                message=f"Prepared {len(chunks)} chunks",
                total=len(chunks),
                processed=len(chunks),
                unit="chunks",
                percent=100.0,
            )

            # Chunks dropped due to partial embedding failures (Issue #221);
            # persisted to files.chunks_failed when the document is indexed.
            chunks_failed_count = 0
            # This generation's chunk embeddings for the finalization helper's
            # advisory centroid computation (W26).
            chunk_embeddings: List[List[float]] = []

            # Cancel gate A (issue #783): a cancel that landed during
            # parse/chunking skips the expensive embedding entirely.
            self._raise_if_cancelled(file_id)

            if self.embedding_service is not None and self.vector_store is not None:
                if chunks:
                    chunks = [c for c in chunks if c.text and c.text.strip()]
                    if not chunks:
                        raise DocumentProcessingError(
                            "All chunks were empty after filtering. "
                            "The document may contain only whitespace or unsupported content."
                        )

                    texts = [c.text for c in chunks]
                    self._validate_chunk_sizes(texts, source_filename)

                    await set_phase(
                        self.pool,
                        file_id,
                        phase=PHASE_EMBEDDING,
                        message=f"Embedding {len(chunks)} chunks",
                        total=len(chunks),
                        processed=0,
                        unit="chunks",
                        percent=0.0,
                    )
                    stage_started_at = time.monotonic()
                    embeddings_result = await self.embedding_service.embed_batch(
                        texts, fail_fast=False
                    )
                    _add_elapsed_ms(stage_timings, "embedding_ms", stage_started_at)
                    batch_embeddings, failed_batch_indices = embeddings_result

                    # Handle partial embedding failures. [W10] Failed TEXT
                    # positions come from the per-text None placeholders —
                    # never from failed_batch_indices × a re-read batch-size
                    # setting (issue #513 C6).
                    if failed_batch_indices or any(
                        emb is None for emb in batch_embeddings
                    ):
                        failed_chunk_indices = {
                            i for i, emb in enumerate(batch_embeddings) if emb is None
                        }

                        kept_chunks = []
                        kept_embeddings = []
                        for i, (chunk, emb) in enumerate(zip(chunks, batch_embeddings)):
                            if i not in failed_chunk_indices:
                                kept_chunks.append(chunk)
                                kept_embeddings.append(emb)

                        failure_pct = len(failed_chunk_indices) / len(chunks) * 100
                        logger.warning(
                            "Embedding partial failure: %d/%d chunks failed (%.0f%%). "
                            "Failed batch indices: %s",
                            len(failed_chunk_indices),
                            len(chunks),
                            failure_pct,
                            failed_batch_indices,
                        )

                        # Persist failed-chunk identity for chunk-scoped retry
                        # (Issue #396) BEFORE dropping them from the kept list.
                        # Uses the ORIGINAL chunks list (indices are into it).
                        try:
                            # issue #704 (T1-25-KR-09): permit-held session.
                            async with self._write_session() as _fc_conn:
                                self._persist_failed_chunks(
                                    file_id,
                                    chunks,
                                    failed_chunk_indices,
                                    document_text,
                                    _fc_conn,
                                )
                                _fc_conn.commit()
                        except sqlite3.Error:
                            logger.warning(
                                "Failed to persist failed-chunk identity for file %d "
                                "(retry-chunks endpoint will be unavailable for this ingest).",
                                file_id,
                                exc_info=True,
                            )

                        original_chunk_count = len(chunks)
                        chunks = kept_chunks
                        embeddings = kept_embeddings
                        chunks_failed_count = len(failed_chunk_indices)

                        if failure_pct > 50:
                            # The file is about to land in status='error', which
                            # the chunk-scoped retry endpoint rejects (409). The
                            # failed_chunks rows persisted above are therefore
                            # unreachable for chunk retry and would desync from
                            # files.chunks_failed (which stays 0 on the error
                            # path). Clear them so the only recovery is the
                            # whole-document retry, which re-ingests from scratch.
                            try:
                                # issue #704 (T1-25-KR-09): permit-held session.
                                async with self._write_session() as _abort_conn:
                                    _abort_conn.execute(
                                        "DELETE FROM failed_chunks WHERE file_id = ?",
                                        (file_id,),
                                    )
                                    _abort_conn.commit()
                            except sqlite3.Error:
                                logger.warning(
                                    "Could not clear failed_chunks rows before "
                                    ">50%% abort for file %d; rows will be cleared "
                                    "on next re-ingest.",
                                    file_id,
                                    exc_info=True,
                                )
                            _abort_error = DocumentProcessingError(
                                "Too many embedding failures: %d/%d chunks failed (%.0f%%). "
                                "Aborting document ingest."
                                % (
                                    len(failed_chunk_indices),
                                    original_chunk_count,
                                    failure_pct,
                                ),
                            )
                            # issue #704 (PRR-001): an embedder outage lands
                            # here because embed_batch(fail_fast=False)
                            # converts batch failures to None placeholders —
                            # without this code the outage persisted as
                            # PARSE_FAILED.
                            _abort_error.ingest_error_code = (
                                INGEST_ERROR_EMBEDDING_FAILED
                            )
                            raise _abort_error
                    else:
                        embeddings = batch_embeddings
                    chunk_embeddings = embeddings

                    sparse_embeddings = [None] * len(chunks)
                    await set_phase(
                        self.pool,
                        file_id,
                        phase=PHASE_EMBEDDING,
                        message="Embeddings ready",
                        total=len(chunks),
                        processed=len(chunks),
                        unit="chunks",
                        percent=100.0,
                    )

                    if len(embeddings) != len(chunks):
                        raise DocumentProcessingError(
                            f"Embedding count mismatch: expected {len(chunks)}, got {len(embeddings)}"
                        )
                    expected_dim = len(embeddings[0]) if embeddings[0] else 0
                    for i, emb in enumerate(embeddings):
                        if not emb or not isinstance(emb, list):
                            raise DocumentProcessingError(
                                f"Embedding {i} is empty or not a list"
                            )
                        if len(emb) != expected_dim:
                            raise DocumentProcessingError(
                                f"Embedding {i} has dimension {len(emb)}, expected {expected_dim}"
                            )

                    records = []
                    for chunk, embedding, sparse_emb in zip(
                        chunks, embeddings, sparse_embeddings
                    ):
                        records.append(
                            self._build_vector_record(
                                file_id=file_id,
                                vault_id=vault_id,
                                file_hash=file_hash,
                                chunk=chunk,
                                embedding=embedding,
                                sparse_emb=sparse_emb,
                                document_text=document_text,
                            )
                        )

                    await set_phase(
                        self.pool,
                        file_id,
                        phase=PHASE_WRITING_INDEX,
                        message="Writing vector index",
                        total=len(records),
                        processed=0,
                        unit="chunks",
                        percent=0.0,
                    )

                    embedding_dim = len(embeddings[0])
                    stage_started_at = time.monotonic()
                    # Last durable-write staleness gate (issue #692 /
                    # T1-21-S2-10): a vault delete may have committed while
                    # this worker parsed/embedded. Discard the generation
                    # instead of writing chunks for a row that no longer
                    # exists. Covers bare and vector_target (staged rebuild)
                    # calls alike, and every parse branch funnels through here.
                    # Cancel gate B (issue #783): same seam — a cancel that
                    # landed during embedding writes no vectors and publishes
                    # no atoms.
                    self._raise_if_cancelled(file_id)
                    self._raise_if_file_row_missing(file_id)
                    # [W8/W13 contract] Thread the optional rebuild target into
                    # every vector-store call below as a trailing ``target=``
                    # argument (dimension-migrating reindex); omitted entirely
                    # when None so stores/doubles without the parameter behave
                    # exactly as before.
                    # [issue #691] A bare call (no ``vector_target``) must
                    # never auto-migrate the index dimension: the staged
                    # rebuild would hold only this file's rows, so the swap
                    # would wipe every other indexed file's vectors while
                    # their rows still claim ``status='indexed'``. Fail
                    # closed with an actionable error instead; the admin
                    # reindex job owns dimension migration (it re-embeds the
                    # whole corpus into its staged rebuild before its own
                    # validated swap).
                    if vector_target is None:
                        await self._ensure_live_dimension_compatible(embedding_dim)
                    _target_kwargs = (
                        {"target": vector_target} if vector_target is not None else {}
                    )
                    # issue #704 (T1-25-S2-04): the vector store is being
                    # mutated from here — a later failure may legitimately
                    # land 'error' because the prior generation may be gone.
                    prior_generation_disturbed = True
                    await self.vector_store.init_table(embedding_dim, **_target_kwargs)
                    _add_elapsed_ms(stage_timings, "vector_write_ms", stage_started_at)

                    if settings.reupload_safe_order:
                        # The generation prefix makes a same-hash reprocess a
                        # no-op instead of a duplicate append (issue #693);
                        # with a rebuild target the guard runs against the
                        # rebuild temp table (threaded through _target_kwargs'
                        # target), never the live table.
                        vector_timings = await self.vector_store.add_chunks(
                            records,
                            generation_prefix=f"{file_id}_{file_hash[:8]}_",
                            **_target_kwargs,
                        )
                        _merge_vector_timings(stage_timings, vector_timings)
                        stage_started_at = time.monotonic()
                        deleted = await self.vector_store.delete_old_generation_by_file(
                            str(file_id), file_hash[:8], **_target_kwargs
                        )
                        _add_elapsed_ms(
                            stage_timings, "vector_write_ms", stage_started_at
                        )
                        if deleted > 0:
                            logger.info(
                                "Safe re-upload: deleted %d old-generation chunks for file_id=%s",
                                deleted,
                                file_id,
                            )
                    else:
                        stage_started_at = time.monotonic()
                        await self.vector_store.delete_by_file(
                            str(file_id), **_target_kwargs
                        )
                        _add_elapsed_ms(
                            stage_timings, "vector_write_ms", stage_started_at
                        )
                        vector_timings = await self.vector_store.add_chunks(
                            records, **_target_kwargs
                        )
                        _merge_vector_timings(stage_timings, vector_timings)

                    await self._verify_vector_rows_visible(file_id, vector_target)
                    await self._discard_vectors_if_row_gone(
                        file_id, target_kwargs=_target_kwargs
                    )

                    # Publish the generation's atoms/assets/stage rows after the
                    # new vectors are durable (issue #460).
                    # issue #704 (T1-25-KR-09): the publish runs under the
                    # shared write permit like every other committing write.
                    # issue #704 review (PRR-005): asset bytes materialize
                    # BEFORE the session so synchronous disk I/O does not
                    # extend the permit's hold.
                    _materialized: List[DocumentAsset] = []
                    try:
                        # bucket= exposes partial bytes so a mid-loop disk
                        # failure still tombstones exactly what was written
                        # (issue #460 property; delta re-gate).
                        self._materialize_generation_assets(
                            file_id,
                            vault_id,
                            generation_hash,
                            parsed,
                            bucket=_materialized,
                        )
                    except Exception:
                        # issue #704 review: materialization failure still
                        # pairs the failed file with its durable marker
                        # (publish_reached=False branch semantics) and
                        # tombstones the partial bytes.
                        async with self._write_session() as _mk_conn:
                            self._tombstone_materialized_assets_on(
                                _mk_conn,
                                _materialized,
                                file_id,
                                vault_id,
                                generation_hash,
                            )
                            self._record_publish_failure(
                                _mk_conn,
                                file_id,
                                vault_id,
                                generation_hash,
                                parsed,
                            )
                        raise
                    async with self._write_session() as _pub_conn:
                        self._publish_artifacts(
                            file_id,
                            vault_id,
                            generation_hash,
                            parsed,
                            conn=_pub_conn,
                            materialized=_materialized,
                        )
        except IngestCancelledError:
            # issue #783: a user cancellation is not a failure — never write
            # status='error' over the terminal 'cancelled'; the transport's
            # cancellation handler unwinds the generation.
            raise
        except Exception as e:
            # Raw exception stays in the server log; persisted fields are
            # user-facing (issue #562).
            logger.exception("Existing-file ingestion failed for file_id=%s", file_id)
            if self.is_cancel_requested(file_id):
                # issue #783: the failure raced a user cancel; the terminal
                # 'cancelled' must not be clobbered with 'error'.
                await self.rollback_cancelled_ingest(
                    file_id, run_deletes=vector_target is None
                )
                raise IngestCancelledError(
                    f"Ingest failed after cancel was requested (file_id={file_id})",
                    file_id=file_id,
                ) from e
            safe_error = redact_ingest_error(e)
            restored = False
            if not prior_generation_disturbed and pre_ingest_status in (
                "indexed",
                "partial",
            ):
                # issue #704 (T1-25-S2-04): the failure predated any write to
                # the vector store, so the prior generation's vectors are
                # intact — restore the prior terminal status instead of
                # demoting the row to 'error'. Guarded so a concurrent
                # cancel/settle still wins the race.
                async with self._write_session() as conn:
                    # issue #704 review (PRR-004): restore the CONTENT hash
                    # too and drop the aborted attempt's foreign ledger —
                    # same rationale as the process_file restore block.
                    restored = (
                        conn.execute(
                            "UPDATE files SET status = ?, file_hash = ?, "
                            "chunks_failed = 0, partial_embeddings = 0, "
                            "error_message = ?, modified_at = ? WHERE id = ? "
                            "AND status IN ('pending', 'processing')",
                            (
                                pre_ingest_status,
                                pre_ingest_file_hash,
                                safe_error,
                                datetime.now(UTC).isoformat(),
                                file_id,
                            ),
                        ).rowcount
                        > 0
                    )
                    if restored:
                        conn.execute(
                            "DELETE FROM failed_chunks WHERE file_id = ?",
                            (file_id,),
                        )
                    conn.commit()
                if restored:
                    logger.warning(
                        "Existing-file ingestion failed for file_id=%s "
                        "before any vector write; restored prior status %r "
                        "(issue #704)",
                        file_id,
                        pre_ingest_status,
                    )
                    await clear_progress(
                        self.pool,
                        file_id,
                        phase=PHASE_INDEXED,
                        phase_message="Last re-ingest attempt failed before "
                        "replacing the index; the previous version remains "
                        "searchable",
                    )
                    raise
            async with self._write_session() as conn:
                self._update_status(file_id, "error", conn, error_message=safe_error)
                conn.commit()
            await set_phase(
                self.pool,
                file_id,
                phase="error",
                message=safe_error,
                percent=None,
            )
            raise

        # Mark indexed (or 'partial' — the upload path's truthful partial
        # status, see the helper docstring) via the shared finalization
        # helper — identical semantics to process_file's tail otherwise
        # (issue #513 W9/C26): status write, parsed_text + gated wiki
        # enqueue, gated KMS enqueue, near-dup centroid, progress cleanup.
        stage_started_at = time.monotonic()
        finalized = await self._finalize_indexed_success(
            file_id=file_id,
            vault_id=vault_id,
            chunks=chunks,
            document_text=document_text,
            chunks_failed_count=chunks_failed_count,
            embeddings=chunk_embeddings,
            partial_final_status="partial",
            vector_target=vector_target,
        )
        if not finalized:
            logger.info(
                "Ingest generation voided for file_id=%d: row settled "
                "concurrently during finalize",
                file_id,
            )
            return None
        _add_elapsed_ms(stage_timings, "sqlite_finalize_ms", stage_started_at)

        logger.info(
            "Ingestion stage timings file_id=%s file_name=%s parse_ms=%.1f chunk_ms=%.1f "
            "contextual_ms=%.1f parent_window_ms=%.1f "
            "embedding_ms=%.1f vector_write_ms=%.1f optimize_ms=%.1f sqlite_finalize_ms=%.1f",
            file_id,
            Path(file_path).name,
            stage_timings["parse_ms"],
            stage_timings["chunk_ms"],
            stage_timings["contextual_ms"],
            stage_timings["parent_window_ms"],
            stage_timings["embedding_ms"],
            stage_timings["vector_write_ms"],
            stage_timings["optimize_ms"],
            stage_timings["sqlite_finalize_ms"],
        )

        return ProcessedDocument(
            file_id=file_id,
            chunks=chunks,
            document_text=document_text,
            file_hash=file_hash,
            file_path=file_path,
            vault_id=vault_id,
        )
