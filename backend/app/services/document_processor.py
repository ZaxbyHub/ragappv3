"""
Document processing service with orchestration, status tracking, and deduplication.

Provides DocumentProcessor class that coordinates parsing, chunking, and schema extraction
while tracking processing status in SQLite and handling file deduplication.
"""

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from ..config import settings
from ..models.database import SQLiteConnectionPool, get_pool
from ..utils.file_utils import compute_file_hash
from ..utils.retry import with_retry
from .chunk_enrichment import ChunkEnrichmentService
from .chunking import (
    EmbeddingSemanticChunker,
    ProcessedChunk,
    SemanticChunker,
    compute_parent_windows,
)
from .contextual_chunking import ContextualChunker
from .document_progress import (
    PHASE_CHUNKING,
    PHASE_EMBEDDING,
    PHASE_EXTRACTING_TEXT,
    PHASE_PARSING,
    PHASE_WRITING_INDEX,
    clear_progress,
    set_phase,
    set_wiki_pending,
)
from .embeddings import EmbeddingService
from .image_processor import ImageProcessingResult, process_image
from .image_search import build_searchable_text
from .llm_client import LLMClient
from .schema_parser import SchemaParser
from .vector_store import VectorStore

logger = logging.getLogger(__name__)

_STAGE_TIMING_FIELDS = (
    "parse_ms",
    "chunk_ms",
    "contextual_ms",
    "parent_window_ms",
    "enrichment_ms",
    "embedding_ms",
    "vector_write_ms",
    "optimize_ms",
    "sqlite_finalize_ms",
)


def _new_stage_timings() -> dict[str, float]:
    return {field: 0.0 for field in _STAGE_TIMING_FIELDS}


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
        # DB error — conservatively fall back to global
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
        vault_override = row["vault_override"] if isinstance(row, sqlite3.Row) else row[1]

        # File-level override takes precedence
        if file_override is not None:
            return bool(file_override)

        # No file override: fall through to vault resolution
        # Build a synthetic vault row for is_enrichment_enabled_for_vault logic
        if vault_override is None:
            return settings.chunk_enrichment_enabled
        return bool(vault_override)
    except Exception:
        # DB error — conservatively fall back to vault resolution
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


class DocumentParseError(Exception):
    """Exception raised when document parsing fails."""

    pass


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
                f"{col}: {val}"
                for col, val in zip(headers, row)
                if str(val).strip()
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
        self, col_val_pairs: List[tuple], sheet_name: str, row_idx: int, total_rows: int, total_col_count: int
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
                    truncated_val = val[:max(0, available)]
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
                    single_col_text = self._build_column_group_text([(col, val)], sheet_name)
                    if len(single_col_text) > self.MAX_CHUNK_CHARS:
                        # Single column exceeds limit; truncate it
                        header_section = f"Sheet: {sheet_name}\nColumns: {col}\n\n{col}: "
                        available = self.MAX_CHUNK_CHARS - len(header_section)
                        truncated_val = val[:max(0, available)]
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

    def _build_column_group_text(self, col_val_pairs: List[tuple], sheet_name: str) -> str:
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
                # Single-sheet: wrap in dict to unify the loop below
                df = pd.read_csv(file_path, dtype=str).fillna("")
                sheets: dict = {"Sheet1": df}
            elif ext in {".xls", ".xlsx"}:
                xf = pd.ExcelFile(file_path)
                sheets = {
                    name: xf.parse(name, dtype=str).fillna("")
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
                logger.debug(
                    "Skipping empty sheet '%s' in '%s'", sheet_name, file_path
                )
                continue

            headers = list(df.columns)
            header_str = " | ".join(str(h) for h in headers)
            total_rows = len(df)

            # Calculate adaptive row count for this sheet based on column width
            rows_per_chunk = self._calculate_adaptive_rows_per_chunk(
                df.iloc[0:min(5, total_rows)], headers, sheet_name
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
                    f"Columns: {header_str}\n\n"
                    + "\n".join(rows_text_lines)
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

    # File extensions that should use image processing (OCR + metadata)
    IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp"}

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

    def set_llm_client(self, llm_client: Optional[LLMClient]) -> None:
        """Rebind optional ingestion LLM work to a different live client."""
        if self._llm_client is llm_client:
            return
        self._llm_client = llm_client
        self._contextual_chunker = None
        self._chunk_enrichment_service = None

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
        size = settings.chunk_size_chars or self._chunk_size_fallback
        overlap = settings.chunk_overlap_chars or self._chunk_overlap_fallback
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
                "embed_batch() will raise EmbeddingError for these chunks.",
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

    async def _verify_vector_rows_visible(self, file_id: int) -> None:
        """Ensure a vector-enabled ingest has visible LanceDB rows before indexing."""
        if self.vector_store is None:
            return

        visible_rows = await self.vector_store.count_by_file(str(file_id))
        if visible_rows <= 0:
            raise DocumentProcessingError(
                f"Vector store visibility check failed: file_id={file_id} has zero LanceDB rows"
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
    def _search_text_with_enrichment(chunk: ProcessedChunk, enrichment: Any = None) -> str:
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
            (chunk, self._build_chunk_uid(file_id, chunk))
            for chunk in selected_chunks
        ]

    def _select_chunks_for_enrichment(
        self, chunks: List[ProcessedChunk]
    ) -> List[ProcessedChunk]:
        """Choose chunks to enrich without requiring a file-specific chunk UID."""
        if not chunks:
            return []

        default_chunks = [
            chunk for chunk in chunks if chunk.metadata.get("chunk_scale", "default") == "default"
        ]
        source_chunks = default_chunks or chunks
        selected: List[ProcessedChunk] = []
        seen: set[tuple[Any, ...]] = set()

        for chunk in source_chunks:
            if chunk.parent_window_start is not None and chunk.parent_window_end is not None:
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

    def set_enrichment_status(
        self, file_id: int, status: str, error_message: Optional[str] = None
    ) -> Optional[str]:
        """Update enrichment state without changing files.status."""
        if self.pool is None:
            return None
        updated_at = datetime.now(UTC).isoformat()
        try:
            with self.pool.connection() as conn:
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
            logger.warning("Failed to update enrichment status for file_id=%s: %s", file_id, e)
            return None

    def _set_enrichment_status(
        self, file_id: int, status: str, error_message: Optional[str] = None
    ) -> Optional[str]:
        """Backward-compatible wrapper for tests and older call sites."""
        return self.set_enrichment_status(file_id, status, error_message)

    def _mark_enrichment_stale_if_current_job(
        self, file_id: int, processing_started_at: Optional[str]
    ) -> None:
        """Finish the same enrichment attempt if it becomes stale mid-flight."""
        if self.pool is None or processing_started_at is None:
            return
        try:
            with self.pool.connection() as conn:
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
            logger.warning("Failed to mark stale enrichment for file_id=%s: %s", file_id, e)

    def _is_enrichment_job_current(self, file_id: int, file_hash: str) -> bool:
        """Return True only if this queued enrichment still matches the live file row."""
        if self.pool is None:
            return False
        try:
            with self.pool.connection() as conn:
                row = conn.execute(
                    "SELECT file_hash, status FROM files WHERE id = ?",
                    (file_id,),
                ).fetchone()
        except sqlite3.Error as e:
            logger.warning("Failed to validate enrichment job for file_id=%s: %s", file_id, e)
            return False

        if row is None:
            logger.info("Skipping stale enrichment for deleted file_id=%s", file_id)
            return False
        current_hash = row["file_hash"] if isinstance(row, sqlite3.Row) else row[0]
        current_status = row["status"] if isinstance(row, sqlite3.Row) else row[1]
        if current_hash != file_hash or current_status != "indexed":
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
        conn.execute(
            "DELETE FROM failed_chunks WHERE file_id = ?", (file_id,)
        )
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
            record_id = (
                f"{file_id}_{file_hash[:8]}_{chunk_scale}_{chunk.chunk_index}"
            )
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
        retryable state (status != 'indexed') so the caller can map to 409.
        [C1.2, C1.3, C1.4, N1, N3]
        """
        if self.embedding_service is None or self.vector_store is None:
            raise RuntimeError("Embedding service or vector store unavailable")

        conn = self.pool.get_connection()
        try:
            row = conn.execute(
                "SELECT id, vault_id, file_hash, status, chunks_failed FROM files WHERE id = ?",
                (file_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"File {file_id} not found")
            if row["status"] != "indexed":
                raise ValueError(
                    f"File {file_id} status is '{row['status']}', must be 'indexed'"
                )
            vault_id = int(row["vault_id"])
            file_hash = str(row["file_hash"] or "")

            stored_rows = conn.execute(
                "SELECT id, chunk_index, chunk_text, chunk_metadata, attempts "
                "FROM failed_chunks WHERE file_id = ? ORDER BY chunk_index",
                (file_id,),
            ).fetchall()
        finally:
            self.pool.release_connection(conn)

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

        # [N3] LanceDB write FIRST (not transactional with SQLite), then SQLite
        # cleanup in its own try/except. On SQLite failure the next retry's
        # pre-check reconciles already-indexed chunks.
        if records:
            await self.vector_store.add_chunks(records)

        conn = self.pool.get_connection()
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
            conn.execute(
                "UPDATE files SET chunks_failed = "
                "(SELECT COUNT(*) FROM failed_chunks WHERE file_id = ?), "
                "chunk_count = chunk_count + ? "
                "WHERE id = ?",
                (file_id, len(records), file_id),
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
        finally:
            self.pool.release_connection(conn)

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
        self, chunks: List[ProcessedChunk], vault_id: Optional[int] = None, file_id: Optional[int] = None
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
            self.set_enrichment_status(file_id, "complete")
            return

        candidates = self._candidate_chunks_for_enrichment(file_id, chunks)
        if not candidates:
            self.set_enrichment_status(file_id, "complete")
            return

        source_filename = Path(file_path).name
        processing_started_at = self.set_enrichment_status(file_id, "processing")
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
                self.set_enrichment_status(file_id, "complete")
                return

            enrichment_texts = [
                self._search_text_with_enrichment(
                    chunk,
                    enrichment_by_uid.get(self._build_chunk_uid(file_id, chunk)),
                )
                for chunk in chunks
            ]
            self._validate_chunk_sizes(enrichment_texts, source_filename)
            embeddings_result = await self.embedding_service.embed_batch(
                enrichment_texts, fail_fast=False
            )
            embeddings, failed_batch_indices = embeddings_result
            if failed_batch_indices:
                raise DocumentProcessingError(
                    f"Embedding failed for enriched chunks: batches {failed_batch_indices}"
                )
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
                self._mark_enrichment_stale_if_current_job(file_id, processing_started_at)
                return
            await self._swap_in_enriched_vector_records(records, old_ids)
            await self._verify_vector_rows_visible(file_id)
            self.set_enrichment_status(file_id, "complete")
        except asyncio.CancelledError:
            self.set_enrichment_status(
                file_id,
                "error",
                "Enrichment job cancelled before completion",
            )
            raise
        except Exception as e:
            logger.warning("Post-index enrichment failed for file_id=%s: %s", file_id, e)
            self.set_enrichment_status(file_id, "error", str(e)[:500])

    @with_retry(
        max_attempts=3, retry_exceptions=(sqlite3.Error,), raise_last_exception=True
    )
    def _check_duplicate(
        self, file_hash: str, conn: sqlite3.Connection, vault_id: int
    ) -> Optional[sqlite3.Row]:
        """
        Check if a file with the given hash already exists and is indexed.

        Args:
            file_hash: The hash of the file to check
            conn: Database connection
            vault_id: The vault ID to check for duplicates in (defaults to 1)

        Returns:
            The existing file row if found and indexed, None otherwise
        """
        cursor = conn.execute(
            "SELECT * FROM files WHERE file_hash = ? AND vault_id = ? AND status = 'indexed'",
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
            The existing row when one of {pending, processing, indexed} matches,
            else None. Rows in 'error' state are intentionally NOT matched —
            re-uploading a previously-failed file should be allowed.
        """
        cursor = conn.execute(
            """
            SELECT * FROM files
            WHERE file_hash = ?
              AND vault_id = ?
              AND status IN ('pending', 'processing', 'indexed')
            ORDER BY id DESC
            LIMIT 1
            """,
            (file_hash, vault_id),
        )
        return cursor.fetchone()

    @with_retry(
        max_attempts=3, retry_exceptions=(sqlite3.Error,), raise_last_exception=True
    )
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
        except sqlite3.Error as e:
            # Rollback on error and wrap in DocumentProcessingError
            conn.rollback()
            raise DocumentProcessingError(
                f"Database error while inserting/updating file record for '{path_str}': {str(e)}"
            ) from e

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
    ) -> None:
        """
        Update the processing status of a file.

        Args:
            file_id: The database ID of the file
            status: New status ('pending', 'processing', 'indexed', 'error')
            conn: Database connection
            chunk_count: Number of chunks produced (optional)
            error_message: Error message if status is 'error' (optional)
            chunks_failed: Chunks dropped due to embedding failures (Issue #221)

        Note:
            This method does not commit - caller is responsible for transaction management.
        """
        now = datetime.now(UTC).isoformat()

        if status == "indexed":
            conn.execute(
                """UPDATE files
                   SET status = ?, chunk_count = ?, chunks_failed = ?,
                       processed_at = ?, modified_at = ?
                   WHERE id = ?""",
                (status, chunk_count, chunks_failed, now, now, file_id),
            )
        elif status == "error":
            conn.execute(
                """UPDATE files
                   SET status = ?, error_message = ?, modified_at = ?
                   WHERE id = ?""",
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

    async def _process_spreadsheet_file(self, file_path: str) -> List[ProcessedChunk]:
        """
        Process a spreadsheet file using SpreadsheetParser.

        Runs the synchronous SpreadsheetParser.parse() in a thread pool to
        avoid blocking the event loop on large files.

        Args:
            file_path: Path to the .csv, .xls, or .xlsx file.

        Returns:
            List of ProcessedChunk objects, one per row-group per sheet.

        Raises:
            DocumentParseError: If SpreadsheetParser.parse() raises.
            DocumentProcessingError: If the file parses but produces no chunks
                (e.g., all sheets are empty).
        """
        sheet_chunks = await asyncio.to_thread(
            self.spreadsheet_parser.parse, file_path
        )

        if not sheet_chunks:
            raise DocumentProcessingError(
                f"Spreadsheet '{file_path}' contains no readable data. "
                "All sheets may be empty or all rows may be blank."
            )

        processed_chunks: List[ProcessedChunk] = []
        total = len(sheet_chunks)

        for idx, chunk_data in enumerate(sheet_chunks):
            chunk = ProcessedChunk(
                text=chunk_data["text"],
                metadata={
                    **chunk_data["metadata"],
                    "chunk_index": idx,
                    "total_chunks": total,
                    "chunk_scale": "default",  # Spreadsheet files bypass multi-scale
                },
                chunk_index=idx,
            )
            processed_chunks.append(chunk)

        return processed_chunks

    async def _process_schema_file(self, file_path: str) -> List[ProcessedChunk]:
        """
        Process a schema file using SchemaParser.

        Args:
            file_path: Path to the schema file

        Returns:
            List of ProcessedChunk objects
        """
        schema_chunks = await asyncio.to_thread(self.schema_parser.parse, file_path)

        processed_chunks = []
        for idx, chunk_data in enumerate(schema_chunks):
            chunk = ProcessedChunk(
                text=chunk_data["text"],
                metadata={
                    **chunk_data["metadata"],
                    "chunk_index": idx,
                    "total_chunks": len(schema_chunks),
                    "chunk_scale": "default",  # Schema files don't use multi-scale
                },
                chunk_index=idx,
            )
            processed_chunks.append(chunk)

        return processed_chunks

    async def _process_image_file(
        self,
        file_path: str,
        file_id: Optional[int] = None,
    ) -> Tuple[List[ProcessedChunk], str]:
        """
        Process an image file using image_processor + image_search.

        Extracts OCR text and image metadata, builds a searchable text
        representation, and produces a single chunk that flows through the
        standard embedding/indexing pipeline.

        Graceful degradation: if image libraries are missing or the image
        is corrupt, logs a warning and returns an empty chunk list so the
        file is recorded but not searchable.

        Args:
            file_path: Path to the image file.
            file_id: Database ID of the file (used for chunk UID construction).

        Returns:
            Tuple of (list of ProcessedChunk objects, searchable document text).
            Returns ([], "") when processing fails.
        """
        image_result: ImageProcessingResult = await asyncio.to_thread(
            process_image, file_path
        )

        if not image_result.success:
            logger.warning(
                "Image processing failed for %s: %s. "
                "File recorded but not searchable.",
                file_path,
                image_result.error,
            )
            return [], ""

        filename = Path(file_path).name
        searchable_text = build_searchable_text(image_result, filename)

        if not searchable_text or not searchable_text.strip():
            logger.warning(
                "Image %s produced no searchable text. "
                "File recorded but not searchable.",
                file_path,
            )
            return [], searchable_text

        # Build chunk metadata mirroring the structure used by text/spreadsheet paths
        chunk_metadata: Dict[str, Any] = {
            "source_type": "image",
            "file_path": str(file_path),
            "chunk_scale": "default",
            "image_width": image_result.metadata.get("width"),
            "image_height": image_result.metadata.get("height"),
            "image_format": image_result.metadata.get("format"),
            "image_mode": image_result.metadata.get("mode"),
        }
        if file_id is not None:
            chunk_metadata["file_id"] = str(file_id)

        chunk_index = 0
        chunk_metadata["chunk_index"] = chunk_index
        chunk_metadata["total_chunks"] = 1

        chunk = ProcessedChunk(
            text=searchable_text,
            metadata=chunk_metadata,
            chunk_index=chunk_index,
        )

        return [chunk], searchable_text

    async def _process_document_file(
        self,
        file_path: str,
        file_id: Optional[int] = None,
        stage_timings: Optional[dict[str, float]] = None,
    ) -> Tuple[List[ProcessedChunk], str]:
        """
        Process a document file using DocumentParser and SemanticChunker.

        Args:
            file_path: Path to the document file
            file_id: Database ID of the file (required for multi-scale indexing)

        Returns:
            Tuple of (List of ProcessedChunk objects, document text as string)
        """
        parse_started_at = time.monotonic()
        try:
            elements = await asyncio.wait_for(
                asyncio.to_thread(self.parser.parse, file_path),
                timeout=settings.document_parse_timeout,
            )
        except asyncio.TimeoutError:
            raise DocumentProcessingError(
                f"Document parsing timed out after {settings.document_parse_timeout}s: {file_path}"
            )
        finally:
            if stage_timings is not None:
                _add_elapsed_ms(stage_timings, "parse_ms", parse_started_at)
        # Join all element texts for use as context in contextual chunking
        document_text = "\n".join([str(e) for e in elements])

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
            return all_chunks, document_text
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
            return chunks, document_text

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
        if self._write_semaphore:
            await self._write_semaphore.acquire()
        conn = self.pool.get_connection()
        try:
            # Check for duplicates
            duplicate = self._check_duplicate(file_hash, conn, vault_id)
            if duplicate:
                raise DuplicateFileError(
                    f"File with hash {file_hash} already indexed as '{duplicate['file_path']}'"
                )

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

            # Update status to processing
            self._update_status(file_id, "processing", conn)
            # Clear stale failed-chunk records from a prior partial-failure ingest
            # (Issue #396). Idempotent re-ingest must not accumulate stale rows.
            conn.execute("DELETE FROM failed_chunks WHERE file_id = ?", (file_id,))
            conn.commit()
        finally:
            # Release connection before long-running operations
            self.pool.release_connection(conn)
            if self._write_semaphore:
                self._write_semaphore.release()

        # Mark processing started + initial phase. Best-effort; ignored on failure.
        set_phase(
            self.pool,
            file_id,
            phase=PHASE_PARSING,
            message="Parsing document",
            mark_processing_started=True,
        )

        # Phase 2: Long operations (NO connection held!)
        try:
            # Process the file based on type
            if self._is_schema_file(file_path):
                stage_started_at = time.monotonic()
                chunks = await self._process_schema_file(file_path)
                _add_elapsed_ms(stage_timings, "parse_ms", stage_started_at)
                document_text = ""
            elif self._is_spreadsheet_file(file_path):
                set_phase(
                    self.pool,
                    file_id,
                    phase=PHASE_PARSING,
                    message="Parsing spreadsheet (large spreadsheets can take several minutes)",
                )
                stage_started_at = time.monotonic()
                chunks = await self._process_spreadsheet_file(file_path)
                _add_elapsed_ms(stage_timings, "parse_ms", stage_started_at)
                document_text = " ".join(c.text for c in chunks)
            elif self._is_image_file(file_path):
                set_phase(
                    self.pool,
                    file_id,
                    phase=PHASE_PARSING,
                    message="Processing image",
                )
                stage_started_at = time.monotonic()
                chunks, document_text = await self._process_image_file(file_path, file_id)
                _add_elapsed_ms(stage_timings, "parse_ms", stage_started_at)
            else:
                chunks, document_text = await self._process_document_file(
                    file_path, file_id, stage_timings
                )

            set_phase(
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
                        _add_elapsed_ms(stage_timings, "contextual_ms", stage_started_at)
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

            set_phase(
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
                    set_phase(
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

                    # Handle partial embedding failures
                    if failed_batch_indices:
                        batch_size_val = settings.embedding_batch_size
                        failed_chunk_indices = set()
                        for batch_idx in failed_batch_indices:
                            start = batch_idx * batch_size_val
                            end = min(start + batch_size_val, len(chunks))
                            for idx in range(start, end):
                                failed_chunk_indices.add(idx)

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
                            len(failed_chunk_indices), len(chunks), failure_pct,
                            failed_batch_indices,
                        )

                        # Persist failed-chunk identity for chunk-scoped retry
                        # (Issue #396) BEFORE dropping them from the kept list.
                        # Uses the ORIGINAL chunks list (indices are into it).
                        try:
                            _fc_conn = self.pool.get_connection()
                            try:
                                self._persist_failed_chunks(
                                    file_id,
                                    chunks,
                                    failed_chunk_indices,
                                    document_text,
                                    _fc_conn,
                                )
                                _fc_conn.commit()
                            finally:
                                self.pool.release_connection(_fc_conn)
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
                                _abort_conn = self.pool.get_connection()
                                try:
                                    _abort_conn.execute(
                                        "DELETE FROM failed_chunks WHERE file_id = ?",
                                        (file_id,),
                                    )
                                    _abort_conn.commit()
                                finally:
                                    self.pool.release_connection(_abort_conn)
                            except sqlite3.Error:
                                logger.warning(
                                    "Could not clear failed_chunks rows before "
                                    ">50%% abort for file %d; rows will be cleared "
                                    "on next re-ingest.",
                                    file_id,
                                    exc_info=True,
                                )
                            raise DocumentProcessingError(
                                "Too many embedding failures: %d/%d chunks failed (%.0f%%). "
                                "Aborting document ingest." % (len(failed_chunk_indices), original_chunk_count, failure_pct),
                            )
                    else:
                        embeddings = batch_embeddings

                    sparse_embeddings = [None] * len(chunks)
                    set_phase(
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
                    set_phase(
                        self.pool,
                        file_id,
                        phase=PHASE_WRITING_INDEX,
                        message="Writing vector index",
                        total=len(records),
                        processed=0,
                        unit="chunks",
                        percent=0.0,
                    )

                    # Initialize vector table with embedding dimension and add chunks
                    embedding_dim = len(embeddings[0])
                    stage_started_at = time.monotonic()
                    await self.vector_store.init_table(embedding_dim)
                    _add_elapsed_ms(stage_timings, "vector_write_ms", stage_started_at)

                    if settings.reupload_safe_order:
                        # Safe re-upload: insert new generation first, then delete old (Issue #13)
                        # Step 1+2: Insert new-generation chunks (hash-prefixed IDs)
                        vector_timings = await self.vector_store.add_chunks(records)
                        _merge_vector_timings(stage_timings, vector_timings)
                        # Step 3: Delete old-generation chunks for this file
                        # (chunks whose IDs don't start with the new hash prefix)
                        stage_started_at = time.monotonic()
                        deleted = await self.vector_store.delete_old_generation_by_file(
                            str(file_id), file_hash[:8]
                        )
                        _add_elapsed_ms(stage_timings, "vector_write_ms", stage_started_at)
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
                        _add_elapsed_ms(stage_timings, "vector_write_ms", stage_started_at)
                        vector_timings = await self.vector_store.add_chunks(records)
                        _merge_vector_timings(stage_timings, vector_timings)

                    await self._verify_vector_rows_visible(file_id)
        except Exception as e:
            # Phase 3: Update status to error on failure
            # Get connection again to update error status
            if self._write_semaphore:
                await self._write_semaphore.acquire()
            conn = self.pool.get_connection()
            try:
                self._update_status(file_id, "error", conn, error_message=str(e))
                conn.commit()
            finally:
                self.pool.release_connection(conn)
                if self._write_semaphore:
                    self._write_semaphore.release()
            # Surface error in the phase fields so the frontend can render it
            # without waiting for a status-route round-trip.
            set_phase(
                self.pool,
                file_id,
                phase="error",
                message=str(e)[:500],
                percent=None,
            )
            raise

        # Phase 3: Final DB operations - update status to indexed
        stage_started_at = time.monotonic()
        if self._write_semaphore:
            await self._write_semaphore.acquire()
        conn = self.pool.get_connection()
        try:
            self._update_status(
                file_id,
                "indexed",
                conn,
                chunk_count=len(chunks),
                chunks_failed=chunks_failed_count,
            )
            conn.commit()
        finally:
            self.pool.release_connection(conn)
            if self._write_semaphore:
                self._write_semaphore.release()

        # Save full parsed text and enqueue wiki compile job (fire-and-forget; non-blocking).
        # parsed_text is stored on the files row so manual recompile can use it without
        # re-parsing the original file.
        try:
            from app.services.wiki_store import WikiStore as _WikiStore

            _full_text = document_text or ""
            if self._write_semaphore:
                await self._write_semaphore.acquire()
            conn = self.pool.get_connection()
            try:
                if _full_text:
                    conn.execute(
                        "UPDATE files SET parsed_text = ? WHERE id = ?",
                        (_full_text, file_id),
                    )
                    conn.commit()
                # Mark wiki_pending=1 synchronously BEFORE the wiki job is created
                # so the status route can report wiki_status="pending" during the
                # brief window before the wiki_compile_jobs row exists.
                set_wiki_pending(self.pool, file_id, True)
                if settings.wiki_enabled and settings.wiki_compile_on_ingest:
                    _WikiStore(conn).create_job(
                        vault_id=vault_id,
                        trigger_type="ingest",
                        trigger_id=f"file:{file_id}",
                        input_json={"file_id": file_id, "vault_id": vault_id},
                    )
                # Always clear the transient marker after the decision is made,
                # regardless of whether a job row was created.
                set_wiki_pending(self.pool, file_id, False)
            finally:
                self.pool.release_connection(conn)
                if self._write_semaphore:
                    self._write_semaphore.release()
        except Exception as _wiki_exc:
            logger.warning("Failed to enqueue wiki ingest job for file_id=%d: %s", file_id, _wiki_exc)
            # If wiki enqueue failed, don't leave wiki_pending=1 hanging.
            set_wiki_pending(self.pool, file_id, False)

        # Enqueue a KMS compile job so the document becomes a user-curatable,
        # full-text-searchable KMS entry. Independent of the wiki pipeline and
        # gated by the kms_enabled / kms_compile_on_ingest flags.
        try:
            from app.config import settings as _settings

            if _settings.kms_enabled and _settings.kms_compile_on_ingest:
                from app.services.kms_store import KMSStore as _KMSStore

                if self._write_semaphore:
                    await self._write_semaphore.acquire()
                conn = self.pool.get_connection()
                try:
                    _KMSStore(conn).create_job(
                        vault_id=vault_id,
                        trigger_type="ingest",
                        trigger_id=f"file:{file_id}",
                        input_json={"file_id": file_id, "vault_id": vault_id},
                    )
                finally:
                    self.pool.release_connection(conn)
                    if self._write_semaphore:
                        self._write_semaphore.release()
        except Exception as _kms_exc:
            logger.warning("Failed to enqueue KMS ingest job for file_id=%d: %s", file_id, _kms_exc)

        # Clear transient progress fields and pin phase=indexed so polls show
        # a clean "ready" snapshot rather than stale embedding counters.
        clear_progress(self.pool, file_id)
        _add_elapsed_ms(stage_timings, "sqlite_finalize_ms", stage_started_at)

        logger.info(
            "Ingestion stage timings file_id=%s file_name=%s parse_ms=%.1f chunk_ms=%.1f "
            "contextual_ms=%.1f parent_window_ms=%.1f enrichment_ms=%.1f "
            "embedding_ms=%.1f vector_write_ms=%.1f optimize_ms=%.1f sqlite_finalize_ms=%.1f",
            file_id,
            Path(file_path).name,
            stage_timings["parse_ms"],
            stage_timings["chunk_ms"],
            stage_timings["contextual_ms"],
            stage_timings["parent_window_ms"],
            stage_timings["enrichment_ms"],
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
    ) -> ProcessedDocument:
        """Process a file whose `files` row already exists.

        Used by the async upload path: the route inserts the `files` row
        with status='pending' / phase='queued' and a duplicate-hash check
        already passed. The worker then calls this method instead of
        ``process_file`` to avoid re-running the duplicate check or
        creating a second row.

        Failure semantics match ``process_file``: status -> 'error',
        phase -> 'error', error_message populated. Wiki ingest job is
        enqueued at the end on success (same as ``process_file``).
        """
        path = Path(file_path)
        if not path.exists():
            # Surface as error on the existing row so the frontend can render it.
            if self._write_semaphore:
                await self._write_semaphore.acquire()
            conn = self.pool.get_connection()
            try:
                self._update_status(
                    file_id,
                    "error",
                    conn,
                    error_message=f"File not found: {file_path}",
                )
                conn.commit()
            finally:
                self.pool.release_connection(conn)
                if self._write_semaphore:
                    self._write_semaphore.release()
            set_phase(
                self.pool,
                file_id,
                phase="error",
                message=f"File not found: {file_path}",
            )
            raise FileNotFoundError(f"File not found: {file_path}")
        if not path.is_file():
            if self._write_semaphore:
                await self._write_semaphore.acquire()
            conn = self.pool.get_connection()
            try:
                self._update_status(
                    file_id,
                    "error",
                    conn,
                    error_message=f"Path is not a file: {file_path}",
                )
                conn.commit()
            finally:
                self.pool.release_connection(conn)
                if self._write_semaphore:
                    self._write_semaphore.release()
            set_phase(
                self.pool,
                file_id,
                phase="error",
                message=f"Path is not a file: {file_path}",
            )
            raise FileNotFoundError(f"Path is not a file: {file_path}")

        # Transition status: pending -> processing. Duplicate check intentionally
        # skipped — the route already ran it before inserting the row.
        if self._write_semaphore:
            await self._write_semaphore.acquire()
        conn = self.pool.get_connection()
        try:
            self._update_status(file_id, "processing", conn)
            # Clear stale failed-chunk records from a prior partial-failure ingest
            # (Issue #396). Idempotent re-ingest must not accumulate stale rows.
            conn.execute("DELETE FROM failed_chunks WHERE file_id = ?", (file_id,))
            conn.commit()
        finally:
            self.pool.release_connection(conn)
            if self._write_semaphore:
                self._write_semaphore.release()

        set_phase(
            self.pool,
            file_id,
            phase=PHASE_PARSING,
            message="Parsing document",
            mark_processing_started=True,
        )

        # The remainder mirrors process_file Phase 2/3 exactly so behavior is
        # identical to the synchronous path. We re-derive file_hash here for
        # the safe-reupload chunk-id prefix logic; this is cheap I/O.
        file_hash = compute_file_hash(file_path)
        stage_timings = _new_stage_timings()

        try:
            if self._is_schema_file(file_path):
                stage_started_at = time.monotonic()
                chunks = await self._process_schema_file(file_path)
                _add_elapsed_ms(stage_timings, "parse_ms", stage_started_at)
                document_text = ""
            elif self._is_spreadsheet_file(file_path):
                set_phase(
                    self.pool,
                    file_id,
                    phase=PHASE_PARSING,
                    message="Parsing spreadsheet (large spreadsheets can take several minutes)",
                )
                stage_started_at = time.monotonic()
                chunks = await self._process_spreadsheet_file(file_path)
                _add_elapsed_ms(stage_timings, "parse_ms", stage_started_at)
                document_text = " ".join(c.text for c in chunks)
            elif self._is_image_file(file_path):
                set_phase(
                    self.pool,
                    file_id,
                    phase=PHASE_PARSING,
                    message="Processing image",
                )
                stage_started_at = time.monotonic()
                chunks, document_text = await self._process_image_file(file_path, file_id)
                _add_elapsed_ms(stage_timings, "parse_ms", stage_started_at)
            else:
                chunks, document_text = await self._process_document_file(
                    file_path, file_id, stage_timings
                )

            set_phase(
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
                        _add_elapsed_ms(stage_timings, "contextual_ms", stage_started_at)

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

            set_phase(
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

                    set_phase(
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

                    # Handle partial embedding failures
                    if failed_batch_indices:
                        batch_size_val = settings.embedding_batch_size
                        failed_chunk_indices = set()
                        for batch_idx in failed_batch_indices:
                            start = batch_idx * batch_size_val
                            end = min(start + batch_size_val, len(chunks))
                            for idx in range(start, end):
                                failed_chunk_indices.add(idx)

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
                            len(failed_chunk_indices), len(chunks), failure_pct,
                            failed_batch_indices,
                        )

                        # Persist failed-chunk identity for chunk-scoped retry
                        # (Issue #396) BEFORE dropping them from the kept list.
                        # Uses the ORIGINAL chunks list (indices are into it).
                        try:
                            _fc_conn = self.pool.get_connection()
                            try:
                                self._persist_failed_chunks(
                                    file_id,
                                    chunks,
                                    failed_chunk_indices,
                                    document_text,
                                    _fc_conn,
                                )
                                _fc_conn.commit()
                            finally:
                                self.pool.release_connection(_fc_conn)
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
                                _abort_conn = self.pool.get_connection()
                                try:
                                    _abort_conn.execute(
                                        "DELETE FROM failed_chunks WHERE file_id = ?",
                                        (file_id,),
                                    )
                                    _abort_conn.commit()
                                finally:
                                    self.pool.release_connection(_abort_conn)
                            except sqlite3.Error:
                                logger.warning(
                                    "Could not clear failed_chunks rows before "
                                    ">50%% abort for file %d; rows will be cleared "
                                    "on next re-ingest.",
                                    file_id,
                                    exc_info=True,
                                )
                            raise DocumentProcessingError(
                                "Too many embedding failures: %d/%d chunks failed (%.0f%%). "
                                "Aborting document ingest." % (len(failed_chunk_indices), original_chunk_count, failure_pct),
                            )
                    else:
                        embeddings = batch_embeddings

                    sparse_embeddings = [None] * len(chunks)
                    set_phase(
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

                    set_phase(
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
                    await self.vector_store.init_table(embedding_dim)
                    _add_elapsed_ms(stage_timings, "vector_write_ms", stage_started_at)

                    if settings.reupload_safe_order:
                        vector_timings = await self.vector_store.add_chunks(records)
                        _merge_vector_timings(stage_timings, vector_timings)
                        stage_started_at = time.monotonic()
                        deleted = await self.vector_store.delete_old_generation_by_file(
                            str(file_id), file_hash[:8]
                        )
                        _add_elapsed_ms(stage_timings, "vector_write_ms", stage_started_at)
                        if deleted > 0:
                            logger.info(
                                "Safe re-upload: deleted %d old-generation chunks for file_id=%s",
                                deleted,
                                file_id,
                            )
                    else:
                        stage_started_at = time.monotonic()
                        await self.vector_store.delete_by_file(str(file_id))
                        _add_elapsed_ms(stage_timings, "vector_write_ms", stage_started_at)
                        vector_timings = await self.vector_store.add_chunks(records)
                        _merge_vector_timings(stage_timings, vector_timings)

                    await self._verify_vector_rows_visible(file_id)
        except Exception as e:
            if self._write_semaphore:
                await self._write_semaphore.acquire()
            conn = self.pool.get_connection()
            try:
                self._update_status(file_id, "error", conn, error_message=str(e))
                conn.commit()
            finally:
                self.pool.release_connection(conn)
                if self._write_semaphore:
                    self._write_semaphore.release()
            set_phase(
                self.pool,
                file_id,
                phase="error",
                message=str(e)[:500],
                percent=None,
            )
            raise

        # Mark indexed
        stage_started_at = time.monotonic()
        if self._write_semaphore:
            await self._write_semaphore.acquire()
        conn = self.pool.get_connection()
        try:
            self._update_status(
                file_id,
                "indexed",
                conn,
                chunk_count=len(chunks),
                chunks_failed=chunks_failed_count,
            )
            conn.commit()
        finally:
            self.pool.release_connection(conn)
            if self._write_semaphore:
                self._write_semaphore.release()

        # Save parsed text + enqueue wiki ingest job (best-effort, non-blocking).
        try:
            from app.services.wiki_store import WikiStore as _WikiStore

            _full_text = document_text or ""
            if self._write_semaphore:
                await self._write_semaphore.acquire()
            conn = self.pool.get_connection()
            try:
                if _full_text:
                    conn.execute(
                        "UPDATE files SET parsed_text = ? WHERE id = ?",
                        (_full_text, file_id),
                    )
                    conn.commit()
                set_wiki_pending(self.pool, file_id, True)
                if settings.wiki_enabled and settings.wiki_compile_on_ingest:
                    _WikiStore(conn).create_job(
                        vault_id=vault_id,
                        trigger_type="ingest",
                        trigger_id=f"file:{file_id}",
                        input_json={"file_id": file_id, "vault_id": vault_id},
                    )
                # Always clear the transient marker after the decision is made,
                # regardless of whether a job row was created.
                set_wiki_pending(self.pool, file_id, False)
            finally:
                self.pool.release_connection(conn)
                if self._write_semaphore:
                    self._write_semaphore.release()
        except Exception as _wiki_exc:
            logger.warning(
                "Failed to enqueue wiki ingest job for file_id=%d: %s",
                file_id,
                _wiki_exc,
            )
            set_wiki_pending(self.pool, file_id, False)

        clear_progress(self.pool, file_id)
        _add_elapsed_ms(stage_timings, "sqlite_finalize_ms", stage_started_at)

        logger.info(
            "Ingestion stage timings file_id=%s file_name=%s parse_ms=%.1f chunk_ms=%.1f "
            "contextual_ms=%.1f parent_window_ms=%.1f enrichment_ms=%.1f "
            "embedding_ms=%.1f vector_write_ms=%.1f optimize_ms=%.1f sqlite_finalize_ms=%.1f",
            file_id,
            Path(file_path).name,
            stage_timings["parse_ms"],
            stage_timings["chunk_ms"],
            stage_timings["contextual_ms"],
            stage_timings["parent_window_ms"],
            stage_timings["enrichment_ms"],
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
