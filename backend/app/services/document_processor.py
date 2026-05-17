"""
Document processing service with orchestration, status tracking, and deduplication.

Provides DocumentProcessor class that coordinates parsing, chunking, and schema extraction
while tracking processing status in SQLite and handling file deduplication.
"""

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, List, Optional, Tuple

import pandas as pd

from ..config import settings
from ..models.database import SQLiteConnectionPool, get_pool
from ..utils.file_utils import compute_file_hash
from ..utils.retry import with_retry
from .chunk_enrichment import ChunkEnrichmentService
from .chunking import ProcessedChunk, SemanticChunker, compute_parent_windows
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
from .llm_client import LLMClient
from .schema_parser import SchemaParser
from .vector_store import VectorStore

logger = logging.getLogger(__name__)


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

    def _get_chunker(self) -> SemanticChunker:
        """Return a chunker configured with the live settings values.

        Reading ``settings.chunk_size_chars`` / ``settings.chunk_overlap_chars``
        at call time lets admins change chunking via the Settings UI and have
        the next ingested document use the new values without restarting.
        Falls back to the values passed to ``__init__`` when settings are unset.
        """
        size = settings.chunk_size_chars or self._chunk_size_fallback
        overlap = settings.chunk_overlap_chars or self._chunk_overlap_fallback
        # Reuse the cached chunker if it already matches; rebuild when settings change.
        existing = getattr(self, "chunker", None)
        if (
            existing is not None
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

    def _get_chunk_enrichment_service(self) -> Optional[ChunkEnrichmentService]:
        """Lazily create a ChunkEnrichmentService when needed."""
        if not settings.chunk_enrichment_enabled:
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
    ) -> None:
        """
        Update the processing status of a file.

        Args:
            file_id: The database ID of the file
            status: New status ('pending', 'processing', 'indexed', 'error')
            conn: Database connection
            chunk_count: Number of chunks produced (optional)
            error_message: Error message if status is 'error' (optional)

        Note:
            This method does not commit - caller is responsible for transaction management.
        """
        now = datetime.now(UTC).isoformat()

        if status == "indexed":
            conn.execute(
                """UPDATE files
                   SET status = ?, chunk_count = ?, processed_at = ?, modified_at = ?
                   WHERE id = ?""",
                (status, chunk_count, now, now, file_id),
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

    async def _process_document_file(
        self, file_path: str, file_id: Optional[int] = None
    ) -> Tuple[List[ProcessedChunk], str]:
        """
        Process a document file using DocumentParser and SemanticChunker.

        Args:
            file_path: Path to the document file
            file_id: Database ID of the file (required for multi-scale indexing)

        Returns:
            Tuple of (List of ProcessedChunk objects, document text as string)
        """
        try:
            elements = await asyncio.wait_for(
                asyncio.to_thread(self.parser.parse, file_path),
                timeout=settings.document_parse_timeout,
            )
        except asyncio.TimeoutError:
            raise DocumentProcessingError(
                f"Document parsing timed out after {settings.document_parse_timeout}s: {file_path}"
            )
        # Join all element texts for use as context in contextual chunking
        document_text = "\n".join([str(e) for e in elements])

        # Check if multi-scale indexing is enabled
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

            return all_chunks, document_text
        else:
            # Existing single-scale behavior — use the live-settings chunker so
            # chunk_size_chars / chunk_overlap_chars changes apply per-document.
            active_chunker = self._get_chunker()
            chunks = await asyncio.to_thread(active_chunker.chunk_elements, elements)
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
                chunks = await self._process_schema_file(file_path)
                document_text = ""
            elif self._is_spreadsheet_file(file_path):
                set_phase(
                    self.pool,
                    file_id,
                    phase=PHASE_PARSING,
                    message="Parsing spreadsheet (large spreadsheets can take several minutes)",
                )
                chunks = await self._process_spreadsheet_file(file_path)
                document_text = " ".join(c.text for c in chunks)
            else:
                chunks, document_text = await self._process_document_file(
                    file_path, file_id
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

            # Chunk enrichment: generate auxiliary metadata (summary, questions, entities)
            enrichment_service = self._get_chunk_enrichment_service()
            if enrichment_service is not None:
                try:
                    chunk_dicts = [
                        {
                            "chunk_uid": self._build_chunk_uid(file_id, c),
                            "text": c.text,
                            "metadata": c.metadata,
                        }
                        for c in chunks
                    ]
                    enrichments = await enrichment_service.enrich_chunks(
                        chunk_dicts, document_title=source_filename
                    )
                    for chunk, enrichment in zip(chunks, enrichments):
                        chunk.metadata["enrichment"] = enrichment.to_dict()
                    logger.info(
                        "Chunk enrichment completed for %s: %d chunks enriched",
                        source_filename,
                        len(enrichments),
                    )
                except Exception as e:
                    logger.warning(
                        "Chunk enrichment failed for %s: %s",
                        source_filename,
                        str(e),
                    )

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
                    embeddings_result = await self.embedding_service.embed_batch(
                        texts, fail_fast=False
                    )
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

                        original_chunk_count = len(chunks)
                        chunks = kept_chunks
                        embeddings = kept_embeddings

                        if failure_pct > 50:
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
                        # Determine chunk_scale from metadata (set during chunking)
                        chunk_scale = chunk.metadata.get("chunk_scale", "default")

                        # Create chunk_uid for windowing support
                        chunk_uid = self._build_chunk_uid(file_id, chunk)

                        # Add chunk_uid to metadata for adjacent chunk lookups
                        chunk_metadata = chunk.metadata.copy()
                        chunk_metadata["chunk_uid"] = chunk_uid
                        chunk_metadata["file_id"] = str(file_id)
                        chunk_metadata["chunk_count"] = chunk.metadata.get(
                            "total_chunks", len(chunks)
                        )
                        # Ensure chunk_scale is included in metadata
                        chunk_metadata["chunk_scale"] = chunk_scale
                        # Preserve raw text when contextual chunking modified the text
                        if hasattr(chunk, "raw_text") and chunk.raw_text:
                            chunk_metadata["raw_text"] = chunk.raw_text

                        # Store parent window offsets + text in metadata (Issue #12)
                        if chunk.parent_window_start is not None:
                            chunk_metadata["parent_window_start"] = chunk.parent_window_start
                        if chunk.parent_window_end is not None:
                            chunk_metadata["parent_window_end"] = chunk.parent_window_end
                        if chunk.chunk_position is not None:
                            chunk_metadata["chunk_position"] = chunk.chunk_position
                        # Store parent window text for fast retrieval-time expansion
                        # (avoids re-parsing the document at query time)
                        if (
                            chunk.parent_window_start is not None
                            and chunk.parent_window_end is not None
                            and document_text
                        ):
                            chunk_metadata["parent_window_text"] = document_text[
                                chunk.parent_window_start : chunk.parent_window_end
                            ]

                        # For safe re-upload, prefix chunk IDs with the new file hash
                        # so old-generation chunks have distinguishable IDs (Issue #13)
                        if settings.reupload_safe_order:
                            record_id = f"{file_id}_{file_hash[:8]}_{chunk_scale}_{chunk.chunk_index}"
                        else:
                            record_id = chunk_uid

                        record = {
                            "id": record_id,
                            "text": chunk.text,
                            "file_id": str(file_id),
                            "chunk_index": chunk.chunk_index,
                            "vault_id": str(vault_id),
                            "chunk_scale": chunk_scale,
                            # Parent-document retrieval fields (Issue #12)
                            "parent_doc_id": str(file_id),
                            "parent_window_start": chunk.parent_window_start,
                            "parent_window_end": chunk.parent_window_end,
                            "chunk_position": chunk.chunk_position,
                            "metadata": json.dumps(chunk_metadata),
                            "embedding": embedding,
                        }
                        # Add sparse embedding if available (tri-vector mode)
                        if sparse_emb is not None:
                            try:
                                record["sparse_embedding"] = json.dumps(sparse_emb)
                            except (TypeError, ValueError) as e:
                                logger.warning(
                                    f"Failed to serialize sparse embedding: {e}"
                                )
                                record["sparse_embedding"] = None
                        records.append(record)

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
                    await self.vector_store.init_table(embedding_dim)

                    if settings.reupload_safe_order:
                        # Safe re-upload: insert new generation first, then delete old (Issue #13)
                        # Step 1+2: Insert new-generation chunks (hash-prefixed IDs)
                        await self.vector_store.add_chunks(records)
                        # Step 3: Delete old-generation chunks for this file
                        # (chunks whose IDs don't start with the new hash prefix)
                        deleted = await self.vector_store.delete_old_generation_by_file(
                            str(file_id), file_hash[:8]
                        )
                        if deleted > 0:
                            logger.info(
                                "Safe re-upload: deleted %d old-generation chunks for file_id=%s",
                                deleted,
                                file_id,
                            )
                    else:
                        # Legacy: delete-then-insert (not crash-safe, kept for compat)
                        await self.vector_store.delete_by_file(str(file_id))
                        await self.vector_store.add_chunks(records)
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
        if self._write_semaphore:
            await self._write_semaphore.acquire()
        conn = self.pool.get_connection()
        try:
            self._update_status(file_id, "indexed", conn, chunk_count=len(chunks))
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
                _WikiStore(conn).create_job(
                    vault_id=vault_id,
                    trigger_type="ingest",
                    trigger_id=f"file:{file_id}",
                    input_json={"file_id": file_id, "vault_id": vault_id},
                )
                # Job row now exists; the status route will derive wiki_status
                # from it directly. Clear the flag so the row doesn't carry a
                # permanently-stale wiki_pending=1 marker.
                set_wiki_pending(self.pool, file_id, False)
            finally:
                self.pool.release_connection(conn)
                if self._write_semaphore:
                    self._write_semaphore.release()
        except Exception as _wiki_exc:
            logger.warning("Failed to enqueue wiki ingest job for file_id=%d: %s", file_id, _wiki_exc)
            # If wiki enqueue failed, don't leave wiki_pending=1 hanging.
            set_wiki_pending(self.pool, file_id, False)

        # Clear transient progress fields and pin phase=indexed so polls show
        # a clean "ready" snapshot rather than stale embedding counters.
        clear_progress(self.pool, file_id)

        return ProcessedDocument(file_id=file_id, chunks=chunks)

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

        try:
            if self._is_schema_file(file_path):
                chunks = await self._process_schema_file(file_path)
                document_text = ""
            elif self._is_spreadsheet_file(file_path):
                set_phase(
                    self.pool,
                    file_id,
                    phase=PHASE_PARSING,
                    message="Parsing spreadsheet (large spreadsheets can take several minutes)",
                )
                chunks = await self._process_spreadsheet_file(file_path)
                document_text = " ".join(c.text for c in chunks)
            else:
                chunks, document_text = await self._process_document_file(
                    file_path, file_id
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

            if document_text and chunks:
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

            enrichment_service = self._get_chunk_enrichment_service()
            if enrichment_service is not None:
                try:
                    chunk_dicts = [
                        {
                            "chunk_uid": self._build_chunk_uid(file_id, c),
                            "text": c.text,
                            "metadata": c.metadata,
                        }
                        for c in chunks
                    ]
                    enrichments = await enrichment_service.enrich_chunks(
                        chunk_dicts, document_title=source_filename
                    )
                    for chunk, enrichment in zip(chunks, enrichments):
                        chunk.metadata["enrichment"] = enrichment.to_dict()
                except Exception as e:
                    logger.warning(
                        "Chunk enrichment failed for %s: %s",
                        source_filename,
                        str(e),
                    )

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
                    embeddings_result = await self.embedding_service.embed_batch(
                        texts, fail_fast=False
                    )
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

                        original_chunk_count = len(chunks)
                        chunks = kept_chunks
                        embeddings = kept_embeddings

                        if failure_pct > 50:
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
                        chunk_scale = chunk.metadata.get("chunk_scale", "default")
                        chunk_uid = self._build_chunk_uid(file_id, chunk)
                        chunk_metadata = chunk.metadata.copy()
                        chunk_metadata["chunk_uid"] = chunk_uid
                        chunk_metadata["file_id"] = str(file_id)
                        chunk_metadata["chunk_count"] = chunk.metadata.get(
                            "total_chunks", len(chunks)
                        )
                        chunk_metadata["chunk_scale"] = chunk_scale
                        if hasattr(chunk, "raw_text") and chunk.raw_text:
                            chunk_metadata["raw_text"] = chunk.raw_text

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
                            "text": chunk.text,
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
                                logger.warning(
                                    f"Failed to serialize sparse embedding: {e}"
                                )
                                record["sparse_embedding"] = None
                        records.append(record)

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
                    await self.vector_store.init_table(embedding_dim)

                    if settings.reupload_safe_order:
                        await self.vector_store.add_chunks(records)
                        deleted = await self.vector_store.delete_old_generation_by_file(
                            str(file_id), file_hash[:8]
                        )
                        if deleted > 0:
                            logger.info(
                                "Safe re-upload: deleted %d old-generation chunks for file_id=%s",
                                deleted,
                                file_id,
                            )
                    else:
                        await self.vector_store.delete_by_file(str(file_id))
                        await self.vector_store.add_chunks(records)
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
        if self._write_semaphore:
            await self._write_semaphore.acquire()
        conn = self.pool.get_connection()
        try:
            self._update_status(file_id, "indexed", conn, chunk_count=len(chunks))
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
                _WikiStore(conn).create_job(
                    vault_id=vault_id,
                    trigger_type="ingest",
                    trigger_id=f"file:{file_id}",
                    input_json={"file_id": file_id, "vault_id": vault_id},
                )
                # Clear flag now that the job row exists — status route
                # derives wiki_status from the job row directly.
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

        return ProcessedDocument(file_id=file_id, chunks=chunks)
