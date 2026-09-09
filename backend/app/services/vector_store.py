"""
LanceDB vector store service for semantic search.
"""

import asyncio
import hashlib
import json
import logging
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, cast

import lancedb
import numpy as np
import pyarrow as pa
from lancedb.expr import col, lit
from lancedb.index import FTS, IvfPq

from app.config import settings
from app.models import migration_journal
from app.utils.fusion import rrf_fuse

logger = logging.getLogger(__name__)

# Thread lock for FTS exceptions counter
_fts_lock = threading.Lock()

# Minimum rows before creating vector index (deferred creation)
VECTOR_INDEX_MIN_ROWS = 256


def _get_search_semaphore_timeout() -> float:
    """Return the configured search semaphore timeout, falling back to the default when settings is mocked."""
    value = settings.search_semaphore_timeout_seconds
    return value if isinstance(value, (int, float)) else 30.0


def _get_vector_search_concurrency() -> int:
    """Return the configured search semaphore concurrency, falling back to the default when settings is mocked."""
    value = settings.vector_search_concurrency
    return value if isinstance(value, int) else 32


def _lance_escape(value) -> str:
    """Escape a value for use in LanceDB SQL-like where clauses.

    Uses SQL-standard doubled single-quote escaping consistently.

    Security note — vault_id injection surface:
        LanceDB's ``.where()`` method accepts only a string expression, not
        parameter-bound values. Vault IDs are interpolated into filter strings
        like ``f"vault_id = '{safe_vault_id}'"`` at multiple call sites in this
        module (search, dense search, delete). Without escaping, a crafted
        vault_id value containing a single quote could break out of the string
        literal and inject arbitrary filter predicates.

        This function mitigates that risk by doubling all single quotes,
        following the SQL-standard escaping convention. While LanceDB exposes
        Expr-based filtering via ``col()``/``lit()``, the ``.where()`` method used
        throughout this module accepts only string expressions, so string-level
        escaping is the required defense for this call pattern.

        FTS search paths have been migrated to use Expr API (``col("vault_id").eq(lit(vault_id))``)
        at ``_search_single_scale()`` (~line 897) and ``search()`` (~line 1160). Dense arms
        retain string interpolation as a fallback since ``.where()`` requires a single string
        predicate and cannot mix Expr objects with user-provided filter strings.

    See also: ``_search_single_scale()`` (dense arm ~line 857), ``search()`` (dense arm ~line 1131),
    ``delete_by_vault()`` (line ~1385) for remaining call sites.
    """
    return str(value).replace("'", "''")


class VectorStoreError(Exception):
    """Custom exception for vector store errors."""

    pass


class VectorStoreConnectionError(VectorStoreError):
    """Exception raised when connection to LanceDB fails."""

    pass


class VectorStoreValidationError(VectorStoreError):
    """Exception raised when record validation fails."""

    pass


class VectorIndexCreationError(VectorStoreError):
    """Exception raised when vector index creation fails."""

    pass


class SearchSemaphoreTimeoutError(VectorStoreError):
    """Exception raised when search semaphore acquisition times out."""

    pass


def publish_index_generation(
    *,
    store: str = "vector_store",
    table_name: str = "chunks",
    detail: str,
) -> int:
    """Publish the authoritative index generation to the recovery journal.

    Index-generation publication interface (issue #512 AC10): opens its own
    sqlite3 connection to ``settings.sqlite_path`` and appends one generation
    row via ``app.models.migration_journal.publish_index_generation``; the
    returned monotonic journal id is the authoritative generation for that
    (store, table). May raise on journal failure — callers on success paths
    guard the call so a journal problem never undoes a completed swap.
    """
    conn = sqlite3.connect(str(settings.sqlite_path))
    try:
        generation = migration_journal.publish_index_generation(
            conn, store=store, table_name=table_name, detail=detail
        )
        conn.commit()
        return generation
    finally:
        conn.close()


# Staging table used by the non-destructive chunks rewrite swaps (issue #512
# VECTOR-001b). Shared by every migrate_add_* rewrite: written BEFORE the
# canonical table is dropped and deleted only after the replacement validates
# (see VectorStore._swap_chunks_table).
CHUNKS_STAGING_TABLE = "chunks_rebuild"

# Temp table used by the dimension-migrating reindex (issue #513 W13 / RC-8).
# Distinct from CHUNKS_STAGING_TABLE so a dimension rebuild never collides
# with the migration machinery's own staging reconciliation.
DIMENSION_REBUILD_TABLE = "chunks_dim_rebuild"


class DimensionRebuildHandle:
    """Opaque handle to an in-progress dimension-migrating table rebuild.

    Created by :meth:`VectorStore.begin_dimension_rebuild`. While open, all
    vector writes routed through ``target=handle`` land in the temp table; the
    live ``chunks`` table is untouched until ``commit_dimension_rebuild``
    performs the validated swap. ``abort_dimension_rebuild`` drops the temp
    table and leaves the old index fully intact.
    """

    __slots__ = ("table_name", "dim", "table")

    def __init__(self, table_name: str, dim: int, table: Any) -> None:
        self.table_name = table_name
        self.dim = int(dim)
        self.table = table


def _chunks_table_schema(embedding_dim: int) -> pa.Schema:
    """Build the canonical ``chunks`` table schema for an embedding dimension.

    Single source of schema truth shared by ``_init_table_unlocked`` (live
    table creation) and ``begin_dimension_rebuild`` (rebuild temp table), so a
    dimension-migrated table is schema-identical apart from the vector width.
    """
    import hashlib as _hashlib

    _creation_model_id = str(settings.embedding_model or "")
    _creation_prefix_hash = _hashlib.sha256(
        _creation_model_id.encode("utf-8")
    ).hexdigest()[:16]
    _schema_metadata = {
        b"embedding_model_id": _creation_model_id.encode("utf-8"),
        b"embedding_dim": str(embedding_dim).encode("utf-8"),
        b"embedding_prefix_hash": _creation_prefix_hash.encode("utf-8"),
    }
    return pa.schema(
        [
            ("id", pa.string()),
            ("text", pa.string()),
            ("file_id", pa.string()),
            ("vault_id", pa.string()),  # Vault isolation
            ("chunk_index", pa.int32()),
            (
                "chunk_scale",
                pa.string(),
            ),  # Scale label like "512", "1024", "default"
            (
                "sparse_embedding",
                pa.string(),
            ),  # JSON string for sparse vectors — retained for schema compat (unused post-Harrier migration)
            ("metadata", pa.string()),  # JSON string for flexibility
            # Parent-document retrieval columns (Issue #12) — nullable, backfilled by migration
            pa.field("parent_doc_id", pa.string(), nullable=True),
            pa.field("parent_window_start", pa.int32(), nullable=True),
            pa.field("parent_window_end", pa.int32(), nullable=True),
            pa.field("chunk_position", pa.int32(), nullable=True),
            ("embedding", pa.list_(pa.float32(), embedding_dim)),
        ],
        metadata=_schema_metadata,
    )


class VectorStore:
    """LanceDB-based vector store for document chunk embeddings."""

    def __init__(self, db_path: Optional[Path] = None):
        """
        Initialize the vector store.

        Args:
            db_path: Path to LanceDB database. Defaults to settings.lancedb_path.
        """
        self.db_path = db_path or settings.lancedb_path
        self.db: Optional[lancedb.AsyncConnection] = None
        self.table: Optional[lancedb.table.AsyncTable] = None
        self._embedding_dim: Optional[int] = None
        self._fts_exceptions: int = 0
        self._ready: bool = True
        # Track the row count at last IVF_PQ build to detect post-delete churn (Issue #13)
        self._last_index_build_row_count: int = 0
        self._index_mutation_generation: int = 0
        self._last_index_build_generation: int = 0
        # Track chunk count between optimize calls when mode is 'periodic'
        self._optimize_counter: int = 0
        # Serialize LanceDB table mutations, including deferred index creation that
        # may run before search, when callers share one VectorStore instance.
        self._write_lock = asyncio.Lock()
        # Shared semaphore limiting concurrent LanceDB search operations across all callers.
        # Lazily initialised on first use to avoid event-loop binding issues.
        self._search_semaphore: Optional[asyncio.Semaphore] = None

    def _get_search_semaphore(self) -> asyncio.Semaphore:
        """Return the shared search semaphore, creating it on first call."""
        if self._search_semaphore is None:
            self._search_semaphore = asyncio.Semaphore(_get_vector_search_concurrency())
        return self._search_semaphore

    def _compute_embedding_prefix_hash(self) -> str:
        """Compute a 16-char hex hash from the concatenation of doc and query prefix strings.

        Returns:
            First 16 characters of the SHA-256 hex digest of
            ``settings.embedding_doc_prefix + '|' + settings.embedding_query_prefix``.
            Falsy values (including ``None``) are treated as empty strings, and
            truthy non-string values are coerced via ``str()`` so the function is
            safe under partially-mocked settings.
        """
        doc_prefix = str(settings.embedding_doc_prefix or "")
        query_prefix = str(settings.embedding_query_prefix or "")
        combined = (doc_prefix + "|" + query_prefix).encode()
        return hashlib.sha256(combined).hexdigest()[:16]

    async def record_embedding_metadata(
        self, embedding_dim: int, raise_on_error: bool = False
    ) -> None:
        """Persist embedding model metadata to the SQLite settings_kv table.

        Stores three keys: embedding_model_id, embedding_dim, embedding_prefix_hash.
        Uses INSERT OR REPLACE so repeated calls are idempotent.

        Args:
            embedding_dim: The embedding vector dimension.
            raise_on_error: When True, re-raise sqlite3.Error instead of logging
                it as non-fatal. When False (default), errors are logged and
                swallowed so startup/validate_schema paths are unaffected.
        """
        def _write() -> None:
            conn = sqlite3.connect(str(settings.sqlite_path))
            try:
                cursor = conn.cursor()
                keys_values = [
                    ("embedding_model_id", settings.embedding_model or ""),
                    ("embedding_dim", str(embedding_dim)),
                    ("embedding_prefix_hash", self._compute_embedding_prefix_hash()),
                ]
                for key, value in keys_values:
                    cursor.execute(
                        "INSERT OR REPLACE INTO settings_kv (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                        (key, value),
                    )
                conn.commit()
            finally:
                conn.close()

        try:
            await asyncio.to_thread(_write)
            logger.info(
                "Embedding metadata persisted: model=%s dim=%d hash=%s",
                settings.embedding_model,
                embedding_dim,
                self._compute_embedding_prefix_hash(),
            )
        except sqlite3.Error as e:
            if raise_on_error:
                raise
            logger.warning("Failed to persist embedding metadata to settings_kv (non-fatal): %s", e)

    async def get_embedding_metadata(self) -> dict:
        """Retrieve embedding model metadata from the SQLite settings_kv table.

        Returns:
            Dict with keys: embedding_model_id (str | None), embedding_dim (int | None),
            embedding_prefix_hash (str | None).
        """
        def _read() -> list:
            conn = sqlite3.connect(str(settings.sqlite_path))
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT key, value FROM settings_kv WHERE key IN (?, ?, ?)",
                    ("embedding_model_id", "embedding_dim", "embedding_prefix_hash"),
                )
                return cursor.fetchall()
            finally:
                conn.close()

        try:
            rows = await asyncio.to_thread(_read)
        except sqlite3.Error as e:
            logger.warning("Failed to read embedding metadata from settings_kv (non-fatal): %s", e)
            return {
                "embedding_model_id": None,
                "embedding_dim": None,
                "embedding_prefix_hash": None,
            }

        result: dict = {
            "embedding_model_id": None,
            "embedding_dim": None,
            "embedding_prefix_hash": None,
        }
        for key, value in rows:
            if key == "embedding_model_id":
                result["embedding_model_id"] = value
            elif key == "embedding_dim":
                try:
                    result["embedding_dim"] = int(value)
                except (ValueError, TypeError):
                    result["embedding_dim"] = None
            elif key == "embedding_prefix_hash":
                result["embedding_prefix_hash"] = value
        return result

    async def mark_ready(self, ready: bool = True) -> None:
        """Set the vector store ready flag.

        Args:
            ready: True if the vector store is ready for queries, False otherwise.
        """
        self._ready = ready
        logger.debug("VectorStore readiness set to %s", ready)

    @asynccontextmanager
    async def _acquire_write_lock(self):
        """Acquire the write lock with a configurable timeout.

        Yields:
            None

        Raises:
            VectorStoreError: If the lock acquisition times out.
        """
        try:
            await asyncio.wait_for(
                self._write_lock.acquire(),
                timeout=settings.write_lock_timeout_seconds,
            )
        except asyncio.TimeoutError:
            raise VectorStoreError(
                f"Write lock acquisition timed out after {settings.write_lock_timeout_seconds}s"
            )
        try:
            yield
        finally:
            self._write_lock.release()

    @asynccontextmanager
    async def _acquire_search_semaphore(self):
        """Acquire the search semaphore with a configurable timeout.

        Yields:
            None

        Raises:
            VectorStoreError: If the semaphore acquisition times out.
        """
        semaphore = self._get_search_semaphore()
        try:
            await asyncio.wait_for(
                semaphore.acquire(),
                timeout=_get_search_semaphore_timeout(),
            )
        except asyncio.TimeoutError:
            raise SearchSemaphoreTimeoutError(
                f"Search semaphore acquisition timed out after {_get_search_semaphore_timeout()}s; "
                f"concurrency={getattr(semaphore, '_value', '?')}"
            )
        try:
            yield
        finally:
            semaphore.release()

    async def connect(self) -> "VectorStore":
        """Connect to LanceDB.

        Raises:
            VectorStoreConnectionError: If connection to LanceDB fails.
        """
        try:
            self.db = await lancedb.connect_async(str(self.db_path))
        except (OSError, RuntimeError, ValueError) as e:
            raise VectorStoreConnectionError(
                f"Failed to connect to LanceDB at {self.db_path}: {e}"
            ) from e
        return self

    async def init_table(
        self, embedding_dim: int, target: Optional[DimensionRebuildHandle] = None
    ) -> "VectorStore":
        """Initialize or open the live (or rebuild-target) 'chunks' table.

        Args:
            embedding_dim: Dimension of embedding vectors.
            target: When a dimension-rebuild handle is supplied, ensure the
                rebuild temp table is ready instead of touching the live table
                (the temp table is created by ``begin_dimension_rebuild``).
                ``None`` keeps the live-table behavior exactly as before.

        Returns:
            Self for method chaining.
        """
        if target is not None:
            if self.db is None:
                await self.connect()
            if target.table is None:
                raise VectorStoreConnectionError(
                    "Dimension rebuild target table is not available."
                )
            if embedding_dim != target.dim:
                raise VectorStoreValidationError(
                    f"Embedding dimension mismatch: rebuild target expects "
                    f"{target.dim}, got {embedding_dim}."
                )
            return self
        async with self._acquire_write_lock():
            return await self._init_table_unlocked(embedding_dim)

    async def _init_table_unlocked(self, embedding_dim: int) -> "VectorStore":
        """
        Initialize or open the 'chunks' table.

        Args:
            embedding_dim: Dimension of embedding vectors.

        Returns:
            Self for method chaining.

        Raises:
            VectorStoreConnectionError: If connection or table operations fail.
        """
        if self.db is None:
            await self.connect()

        if self.db is None:
            raise VectorStoreConnectionError("Database connection is not available.")

        self._embedding_dim = embedding_dim
        table_just_created = False

        # Persist the embedding model identity into the table schema metadata at
        # creation so validate_schema() can detect a same-dimension-but-different-
        # model reindex need (F2-2). LanceDB cannot update metadata on an existing
        # table, so this only applies to freshly-created tables — but it gives the
        # validate_schema model-id comparison a source of truth for new tables.
        # The schema itself is shared with the dimension-rebuild temp table
        # (issue #513 W13) via _chunks_table_schema.
        schema = _chunks_table_schema(embedding_dim)

        # Create or open table with error handling
        try:
            table_names = await self.db.table_names()
            if "chunks" in table_names:
                try:
                    self.table = await self.db.open_table("chunks")
                except (OSError, RuntimeError, ValueError) as e:
                    # Non-destructive init contract (issue #512 VECTOR-001a):
                    # an OSError/RuntimeError/ValueError from open_table is a
                    # TRANSIENT failure (I/O blip, lock, version skew), not
                    # evidence the authoritative table is stale. Never drop
                    # and recreate over it — report the failure so startup
                    # can retry or fail fast. The table is preserved on disk.
                    raise VectorStoreConnectionError(
                        f"Failed to open existing 'chunks' table: {e}. "
                        f"The existing table was preserved (no drop/recreate attempted)."
                    ) from e
                # Seed churn baseline so post-delete rebuild fires on existing indexes.
                # Without this, _last_index_build_row_count stays 0 after restart
                # and the churn-based rebuild path never triggers.
                try:
                    existing_indices = await self.table.list_indices()
                    # Detect the existing ANN index by NAME, consistent with
                    # every other index check in this class. LanceDB names a
                    # vector index on the "embedding" column "embedding_idx"
                    # by default. The previous check matched the literal
                    # "IVF_PQ" against idx.index_type, but LanceDB reports the
                    # type as "IvfPq" (mixed-case, no underscore) — so the
                    # check was ALWAYS False, leaving _last_index_build_row_count
                    # at 0 and forcing a full IVF_PQ rebuild on the first search
                    # after every startup (a multi-second to multi-minute stall
                    # on large tables).
                    has_ivfpq = any(
                        getattr(idx, "name", None) == "embedding_idx"
                        for idx in existing_indices
                    )
                    if has_ivfpq:
                        self._last_index_build_row_count = await self.table.count_rows()
                        self._last_index_build_generation = (
                            self._index_mutation_generation
                        )
                        logger.debug(
                            "Seeded _last_index_build_row_count=%d from existing IVF_PQ index",
                            self._last_index_build_row_count,
                        )
                except Exception as _seed_exc:
                    logger.debug("Could not seed index row count baseline: %s", _seed_exc)
            else:
                self.table = await self.db.create_table("chunks", schema=schema)
                table_just_created = True
        except (OSError, RuntimeError, ValueError) as e:
            raise VectorStoreConnectionError(
                f"Failed to initialize 'chunks' table: {e}"
            ) from e

        # Persist embedding metadata to settings_kv on new table creation
        if table_just_created:
            await self.record_embedding_metadata(embedding_dim)

        # Defer vector index creation until sufficient rows (FR-013)
        if table_just_created:
            logger.info(
                "Table created; vector index deferred until ≥%d rows",
                VECTOR_INDEX_MIN_ROWS,
            )

        # Create FTS index only if missing (FR-014)
        fts_index_exists = False
        try:
            indices = await self.table.list_indices()
            fts_index_exists = any(idx.name == "fts_text" for idx in indices)
        except (OSError, RuntimeError, ValueError):
            pass

        if not fts_index_exists:
            try:
                await self.table.create_index(
                    column="text",
                    config=FTS(),
                    replace=False,
                )
                logger.info("Full-text search index created on 'text' column")
            except (OSError, RuntimeError, ValueError) as e:
                logger.warning(
                    f"FTS index creation failed (hybrid search will be unavailable): {e}"
                )
        else:
            logger.debug("FTS index already exists, skipping creation")

        return self

    def get_fts_exceptions(self) -> int:
        """Return the number of FTS exceptions since last reset and reset counter."""
        with _fts_lock:
            count = self._fts_exceptions
            self._fts_exceptions = 0
            return count

    async def has_parent_window_text_sample(self) -> bool:
        """Return True if at least one indexed chunk's metadata contains a
        non-empty ``parent_window_text``.

        Used at startup to decide whether parent-window retrieval will
        actually deliver wider context or silently fall back to small-chunk
        rendering. Best-effort: returns False on any error so the lifespan
        check does not block startup.
        """
        try:
            if self.table is None:
                return False
            # LanceDB table.search().limit(N) is an iterator of dicts; we
            # only need the first matching record. Filter on a sentinel
            # JSON substring to avoid pulling rows that lack parent windows.
            try:
                cursor = (
                    await self.table.search()
                    .where(
                        "metadata LIKE '%\"parent_window_text\"%'",
                        prefilter=True,
                    )
                    .limit(1)
                )
                # ``to_list()`` returns a list (possibly empty).
                if hasattr(cursor, "to_list"):
                    rows = await cursor.to_list()
                else:
                    rows = list(cursor)
            except Exception:
                # Older LanceDB API shapes — fall back to a row scan.
                rows = list(await self.table.head(50))
                for row in rows:
                    md = row.get("metadata") if isinstance(row, dict) else None
                    if md and "parent_window_text" in str(md):
                        return True
                return False
            return bool(rows)
        except Exception:
            return False

    async def _get_expected_embedding_dim(self) -> Optional[int]:
        """Get the expected embedding dimension from the table schema."""
        if self.table is None:
            return self._embedding_dim

        try:
            schema = await self.table.schema()
            embedding_field = schema.field("embedding")
            if hasattr(embedding_field.type, "list_size"):
                return embedding_field.type.list_size
        except (AttributeError, KeyError, IndexError):
            # Schema access or field lookup failed
            pass
        return self._embedding_dim

    async def _vector_index_needs_creation(self) -> bool:
        """Read-only fast check used to avoid taking the write lock on search.

        Returns False ONLY when we are confident the ANN index is fresh, so the
        hot search path can skip acquiring the write lock (and thus avoid
        blocking behind in-flight ingestion writes that hold the same lock).
        On any uncertainty or error, returns True so the authoritative,
        write-locked ``_maybe_create_vector_index`` makes the real decision.

        This performs only read-only LanceDB metadata reads and plain attribute
        comparisons; it never mutates the table.
        """
        if self.table is None:
            return False
        try:
            row_count = await self.table.count_rows()
            if row_count < VECTOR_INDEX_MIN_ROWS:
                return False
            indices = await self.table.list_indices()
            has_embedding_idx = any(idx.name == "embedding_idx" for idx in indices)
        except (OSError, RuntimeError, ValueError) as e:
            logger.debug("Vector index freshness probe failed (%s); will take lock", e)
            return True  # uncertain → let the locked path decide
        if (
            has_embedding_idx
            and self._last_index_build_row_count == row_count
            and self._last_index_build_generation == self._index_mutation_generation
        ):
            return False  # confidently fresh — safe to skip the write lock
        return True

    async def _maybe_create_vector_index(self) -> None:
        """Conditionally create vector ANN index if conditions are met.

        Checks:
        1. Skip if row count < VECTOR_INDEX_MIN_ROWS (256)
        2. Skip only when embedding_idx exists and was built for the current row count
        3. Create index with num_partitions=256, num_sub_vectors=embedding_dim//8

        This method re-validates freshness under the write lock, so it is the
        authoritative decision point even when a lock-free probe
        (``_vector_index_needs_creation``) reported that creation may be needed.
        """
        if self.table is None:
            return

        # Check row count
        try:
            row_count = await self.table.count_rows()
        except (OSError, RuntimeError, ValueError) as e:
            logger.debug("Could not get row count: %s", e)
            return

        if row_count < VECTOR_INDEX_MIN_ROWS:
            logger.debug(
                "Vector index deferred: %d rows < %d threshold",
                row_count,
                VECTOR_INDEX_MIN_ROWS,
            )
            return

        has_embedding_idx = False
        try:
            indices = await self.table.list_indices()
            has_embedding_idx = any(idx.name == "embedding_idx" for idx in indices)
        except (OSError, RuntimeError, ValueError) as e:
            logger.debug("Could not check existing indices: %s", e)

        if (
            has_embedding_idx
            and self._last_index_build_row_count == row_count
            and self._last_index_build_generation == self._index_mutation_generation
        ):
            logger.debug(
                "Vector index already fresh for %d rows at generation %d, skipping creation",
                row_count,
                self._index_mutation_generation,
            )
            return

        # Create the index
        t0 = time.monotonic()
        try:
            num_sub_vectors = settings.embedding_dim // 8
            await self.table.create_index(
                column="embedding",
                config=IvfPq(
                    distance_type=cast(
                        "Literal['l2', 'cosine', 'dot']", settings.vector_metric
                    ),
                    num_partitions=256,
                    num_sub_vectors=num_sub_vectors,
                ),
                replace=True,
            )
            self._last_index_build_row_count = row_count  # Track for post-delete churn check
            self._last_index_build_generation = self._index_mutation_generation
            logger.info(
                "Vector index creation completed in %.2fs", time.monotonic() - t0
            )
            logger.info(
                "Vector index created with metric=%s (%d rows)",
                settings.vector_metric,
                row_count,
            )
        except (OSError, RuntimeError, ValueError) as e:
            logger.warning("Vector index creation failed: %s", e)

    async def _maybe_rebuild_or_drop_vector_index(self, deleted_count: int) -> None:
        """Post-delete ANN index lifecycle management (Issue #13).

        After bulk deletes the IVF_PQ index may be stale because it was trained
        on rows that no longer exist, or we may have crossed the 256-row threshold
        downward and should fall back to brute-force search.

        Rules:
        - If current row count drops below VECTOR_INDEX_MIN_ROWS and an IVF_PQ
          index exists, drop the index so LanceDB falls back to brute-force.
        - If churn (deleted_count / last_build_row_count) >= INDEX_REBUILD_DELTA,
          rebuild the index on the remaining rows.
        """
        if self.table is None or deleted_count == 0:
            return

        try:
            current_rows = await self.table.count_rows()
        except (OSError, RuntimeError, ValueError) as e:
            logger.debug("Could not count rows for post-delete index check: %s", e)
            return

        # Check if index exists
        has_ivfpq = False
        try:
            indices = await self.table.list_indices()
            has_ivfpq = any(idx.name == "embedding_idx" for idx in indices)
        except (OSError, RuntimeError, ValueError):
            pass

        if not has_ivfpq:
            return  # No index to manage

        # Case 1: Dropped below brute-force threshold — drop the IVF_PQ index
        if current_rows < VECTOR_INDEX_MIN_ROWS:
            try:
                await self.table.drop_index("embedding_idx")
                self._last_index_build_row_count = 0
                self._last_index_build_generation = self._index_mutation_generation
                logger.info(
                    "Dropped IVF_PQ index: row count (%d) fell below %d threshold; "
                    "falling back to brute-force search.",
                    current_rows,
                    VECTOR_INDEX_MIN_ROWS,
                )
            except (OSError, RuntimeError, ValueError) as e:
                logger.warning("Failed to drop IVF_PQ index after threshold crossed: %s", e)
            return

        # Case 2: Churn threshold exceeded — rebuild the index
        if self._last_index_build_row_count > 0:
            churn = deleted_count / self._last_index_build_row_count
            if churn >= settings.index_rebuild_delta:
                t0 = time.monotonic()
                try:
                    num_sub_vectors = settings.embedding_dim // 8
                    await self.table.create_index(
                        column="embedding",
                        config=IvfPq(
                            distance_type=cast(
                                "Literal['l2', 'cosine', 'dot']", settings.vector_metric
                            ),
                            num_partitions=256,
                            num_sub_vectors=num_sub_vectors,
                        ),
                        replace=True,
                    )
                    self._last_index_build_row_count = current_rows
                    self._last_index_build_generation = (
                        self._index_mutation_generation
                    )
                    logger.info(
                        "IVF_PQ index rebuilt after %.0f%% row churn (%d deleted, %d remaining) "
                        "in %.2fs",
                        churn * 100,
                        deleted_count,
                        current_rows,
                        time.monotonic() - t0,
                    )
                    return
                except (OSError, RuntimeError, ValueError) as e:
                    logger.warning("IVF_PQ index rebuild after delete churn failed: %s", e)
                    return

            # Below-threshold churn: keep the existing IVF_PQ index by design.
            # Record that decision so the next search() does not force a rebuild
            # solely because the mutation generation changed.
            self._last_index_build_row_count = current_rows
            self._last_index_build_generation = self._index_mutation_generation
            logger.debug(
                "Kept existing IVF_PQ index after %.2f%% row churn (%d deleted, %d remaining)",
                churn * 100,
                deleted_count,
                current_rows,
            )

    async def add_chunks(
        self,
        records: List[Dict[str, Any]],
        target: Optional[DimensionRebuildHandle] = None,
    ) -> Dict[str, float]:
        """Serialize chunk writes and related LanceDB maintenance.

        When ``target`` is a dimension-rebuild handle the records are written
        into the rebuild temp table (expected dim taken from the handle) and
        the live table's index bookkeeping is left untouched; the validated
        swap happens only at ``commit_dimension_rebuild``.
        """
        async with self._acquire_write_lock():
            if target is not None:
                return await self._add_chunks_unlocked(records, target=target)
            return await self._add_chunks_unlocked(records)

    async def add_chunks_then_delete_ids(
        self, records: List[Dict[str, Any]], old_ids: List[str]
    ) -> int:
        """Add replacement chunks before deleting exact old row IDs.

        Used by optional post-index enrichment so a failed enriched write cannot
        remove the already-searchable base index. If cleanup fails after the add,
        callers may temporarily see duplicate rows, but not zero rows.
        """
        async with self._acquire_write_lock():
            await self._add_chunks_unlocked(records)
            return await self._delete_ids_unlocked(old_ids)

    async def _delete_ids_unlocked(self, ids: List[str]) -> int:
        """Delete exact chunk row IDs. Caller must hold _write_lock."""
        if not ids:
            return 0
        if self.db is None:
            await self.connect()
        if self.table is None:
            if self.db is None:
                return 0
            try:
                table_names = await self.db.table_names()
                if "chunks" not in table_names:
                    return 0
                self.table = await self.db.open_table("chunks")
            except (OSError, RuntimeError, ValueError):
                return 0
        if self.table is None:
            return 0

        clauses = [f"id = '{_lance_escape(chunk_id)}'" for chunk_id in ids]
        where = "(" + " OR ".join(clauses) + ")"
        try:
            count_before = await self.table.count_rows(where)
        except (OSError, RuntimeError, ValueError):
            count_before = 0
        await self.table.delete(where)
        if count_before > 0:
            self._index_mutation_generation += 1
            await self._maybe_rebuild_or_drop_vector_index(count_before)
        return count_before

    async def _add_chunks_unlocked(
        self,
        records: List[Dict[str, Any]],
        target: Optional[DimensionRebuildHandle] = None,
    ) -> Dict[str, float]:
        """
        Add chunk records to the vector store.

        Args:
            records: List of records with keys: id, text, file_id, chunk_index,
                     metadata, embedding, vault_id (required, caller must provide).
            target: Optional dimension-rebuild handle; writes go to the rebuild
                temp table instead of the live table.

        Raises:
            RuntimeError: If table is not initialized.
            VectorStoreValidationError: If records validation fails.
        """
        if target is not None:
            table = target.table
            if table is None:
                raise RuntimeError(
                    "Dimension rebuild target table unavailable. Call "
                    "begin_dimension_rebuild() first."
                )
            expected_dim = target.dim
        else:
            if self.table is None:
                raise RuntimeError("Table not initialized. Call init_table() first.")
            table = self.table
            # Get expected embedding dimension from table schema
            expected_dim = await self._get_expected_embedding_dim()

        # Handle empty records
        timings = {"vector_write_ms": 0.0, "optimize_ms": 0.0}
        if not records:
            return timings

        # Required fields for validation
        required_fields = ["id", "text", "file_id", "chunk_index", "embedding"]

        # Convert records to arrow-compatible format
        processed_records = []
        for record in records:
            # Validate required fields
            missing_fields = [field for field in required_fields if field not in record]
            if missing_fields:
                raise VectorStoreValidationError(
                    f"Record missing required fields: {', '.join(missing_fields)}"
                )

            # Ensure embedding is a list (convert from numpy if needed)
            embedding = record["embedding"]
            if isinstance(embedding, np.ndarray):
                embedding = embedding.tolist()
            elif not isinstance(embedding, list):
                raise VectorStoreValidationError(
                    f"Embedding must be a list or numpy array, got {type(embedding).__name__}"
                )

            # Validate embedding dimension matches table schema
            actual_dim = len(embedding)
            if expected_dim is not None and actual_dim != expected_dim:
                raise VectorStoreValidationError(
                    f"Embedding dimension mismatch: expected {expected_dim} dimensions, "
                    f"got {actual_dim}. The table was created with a different embedding model. "
                    f"Delete the lancedb directory at {self.db_path} and restart to use the new model."
                )

            processed_record = {
                "id": record["id"],
                "text": record["text"],
                "file_id": record["file_id"],
                "vault_id": record["vault_id"],  # Caller must provide vault_id
                "chunk_index": record["chunk_index"],
                "chunk_scale": record.get("chunk_scale", "default"),  # Scale label
                "sparse_embedding": record.get(
                    "sparse_embedding"
                ),  # Retained for schema compat — unused post-Harrier migration
                "metadata": record.get("metadata", "{}"),
                # Parent-document retrieval fields (Issue #12) — None for legacy chunks
                "parent_doc_id": record.get("parent_doc_id"),
                "parent_window_start": record.get("parent_window_start"),
                "parent_window_end": record.get("parent_window_end"),
                "chunk_position": record.get("chunk_position"),
                "embedding": embedding,
            }

            # Validate sparse_embedding JSON format if provided
            sparse_emb = processed_record.get("sparse_embedding")
            if sparse_emb is not None:
                try:
                    json.loads(sparse_emb)  # Validate it's valid JSON
                except json.JSONDecodeError:
                    raise VectorStoreValidationError(
                        "sparse_embedding must be valid JSON"
                    )
            processed_records.append(processed_record)

        t0 = time.monotonic()
        await table.add(processed_records)
        timings["vector_write_ms"] += (time.monotonic() - t0) * 1000

        if target is not None:
            # Rebuild temp-table writes: no live index bookkeeping and no
            # optimize — the temp table is swapped in (with fresh indices) at
            # commit_dimension_rebuild and dropped on abort.
            return timings

        self._index_mutation_generation += 1

        # Compact the table per configured optimize_mode
        optimize_mode = settings.optimize_mode
        if optimize_mode == "after_every_write":
            try:
                t0 = time.monotonic()
                await self.table.optimize()
                timings["optimize_ms"] += (time.monotonic() - t0) * 1000
            except Exception as e:
                logger.warning("table.optimize() after add_chunks failed (non-fatal): %s", e)
        elif optimize_mode == "periodic":
            self._optimize_counter += len(processed_records)
            if self._optimize_counter >= settings.optimize_interval_chunks:
                try:
                    t0 = time.monotonic()
                    await self.table.optimize()
                    timings["optimize_ms"] += (time.monotonic() - t0) * 1000
                    self._optimize_counter = 0
                except Exception as e:
                    logger.warning(
                        "Periodic table.optimize() failed (non-fatal, counter reset): %s", e
                    )
                    self._optimize_counter = 0
        # optimize_mode == "manual": never optimize during ingestion

        # Check if we should create the vector index after adding chunks
        await self._maybe_create_vector_index()
        return timings

    async def flush_optimize(self) -> None:
        """Serialize an explicit LanceDB optimize call."""
        async with self._acquire_write_lock():
            return await self._flush_optimize_unlocked()

    async def _flush_optimize_unlocked(self) -> None:
        """
        Force an immediate table.optimize() for final compaction.

        Called by BackgroundProcessor.stop() when optimize_on_shutdown is True.
        Ensures all pending chunk writes are compacted before shutdown.
        """
        if self.table is None:
            return
        try:
            await self.table.optimize()
            self._optimize_counter = 0
            logger.info("flush_optimize: table compaction completed")
        except Exception as e:
            logger.warning("flush_optimize failed (non-fatal): %s", e)

    async def count_by_file(
        self, file_id: str, target: Optional[DimensionRebuildHandle] = None
    ) -> int:
        """Return the number of visible LanceDB rows for a file_id.

        With a ``target`` rebuild handle the count is taken against the rebuild
        temp table instead of the live table (issue #513 W13).
        """
        if target is not None:
            if target.table is None:
                return 0
            safe_file_id = _lance_escape(file_id)
            return await target.table.count_rows(f"file_id = '{safe_file_id}'")

        if self.db is None:
            await self.connect()

        if self.db is None:
            raise VectorStoreConnectionError("Database connection is not available.")

        if self.table is None:
            try:
                table_names = await self.db.table_names()
            except (OSError, RuntimeError, ValueError) as e:
                raise VectorStoreConnectionError(
                    f"Failed to list table names: {e}"
                ) from e

            if "chunks" not in table_names:
                return 0

            try:
                self.table = await self.db.open_table("chunks")
            except (OSError, RuntimeError, ValueError) as e:
                raise VectorStoreConnectionError(
                    f"Failed to open 'chunks' table: {e}"
                ) from e

        if self.table is None:
            return 0

        safe_file_id = _lance_escape(file_id)
        return await self.table.count_rows(f"file_id = '{safe_file_id}'")

    async def _search_single_scale(
        self,
        embedding: List[float],
        scale: str,
        fetch_k: int,
        filter_expr: Optional[str] = None,
        vault_id: Optional[str] = None,
        query_text: str = "",
        hybrid: bool = True,
        hybrid_alpha: float = 0.5,
        bypass_vector_index: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Search within a single chunk scale.

        Args:
            embedding: Query embedding vector.
            scale: The chunk_scale value to filter by (e.g., "512", "1024", "default").
            fetch_k: Number of results to fetch per search type.
            filter_expr: Optional additional filter expression.
            vault_id: Optional vault ID to filter results.
            query_text: Raw query text for BM25 FTS search.
            hybrid: If True, combine dense vector search with BM25 FTS using RRF.

        Returns:
            List of matching records for this scale with RRF scores.
        """
        if self.table is None:
            return []

        # Build scale filter
        safe_scale = _lance_escape(scale)
        scale_filter = f"chunk_scale = '{safe_scale}'"

        # Combine with vault filter if present
        # Dense arm fallback: LanceDB's .where() requires a single string predicate;
        # there is no parameter-binding API for the dense path. The vault_id is
        # already a string column so integer behaviour is unaffected.
        if vault_id is not None:
            safe_vault_id = _lance_escape(vault_id)
            vault_filter = f"vault_id = '{safe_vault_id}'"
            if filter_expr:
                combined_filter = (
                    f"({filter_expr}) AND ({scale_filter}) AND ({vault_filter})"
                )
            else:
                combined_filter = f"{scale_filter} AND ({vault_filter})"
        elif filter_expr:
            combined_filter = f"({filter_expr}) AND ({scale_filter})"
        else:
            combined_filter = scale_filter

        # Dense vector search with scale filter
        # NOTE: Using query_type="vector" to bypass LanceDB's buggy auto-detection
        # which can cause UnboundLocalError when embedding_conf is None
        embedding_np = np.array(embedding, dtype=np.float32)

        async def _run_dense() -> List[Dict[str, Any]]:
            query = await self.table.search(embedding_np, query_type="vector")
            # Explicit distance type on EVERY dense query (issue #510
            # VECTOR-004): flat/brute-force scans otherwise default to L2 even
            # when vector_metric is cosine, so cosine-calibrated thresholds
            # discard valid matches. Must agree with ANN index creation.
            if hasattr(query, "distance_type"):
                query = query.distance_type(settings.vector_metric)
            if bypass_vector_index and hasattr(query, "bypass_vector_index"):
                query = query.bypass_vector_index()
            if combined_filter:
                query = query.where(combined_filter)
            return await query.limit(fetch_k).to_list()

        # If hybrid disabled, return dense results only (skip the FTS round-trip).
        if not hybrid or not query_text:
            return await _run_dense()

        async def _run_fts():
            # Returns (results, fts_status). Self-contained try/except so a
            # concurrent gather degrades to dense-only on FTS failure and the
            # _fts_exceptions counter is still incremented (preserving the
            # original sequential semantics) rather than aborting the search.
            try:
                fts_query = await self.table.search(query_text, query_type="fts")
                fts_filter_parts = [scale_filter]
                if vault_id:
                    # Use Expr API for type-safe vault_id filtering instead of
                    # string interpolation. lit() handles type conversion safely.
                    # .to_sql() is required before joining with string parts below.
                    fts_filter_parts.append(col("vault_id").eq(lit(vault_id)).to_sql())
                if filter_expr:
                    fts_filter_parts.append(f"({filter_expr})")
                fts_combined_filter = " AND ".join(f"({f})" for f in fts_filter_parts)
                fts_query = fts_query.where(fts_combined_filter)
                results = await fts_query.limit(fetch_k).to_list()
                if results:
                    logger.info(
                        f"Hybrid search (BM25 FTS) succeeded for scale {scale}: {len(results)} FTS results (alpha={hybrid_alpha})"
                    )
                    return results, 'ok'
                return results, 'empty'
            except Exception as e:
                logger.warning(
                    f"FTS search failed for scale {scale} (falling back to dense-only): {type(e).__name__}: {e}"
                )
                with _fts_lock:
                    self._fts_exceptions += 1
                return [], 'failed'

        # Run the dense (ANN) and BM25 FTS arms concurrently — independent
        # LanceDB queries feeding an order-insensitive RRF fusion. Use
        # return_exceptions=True (consistent with the multi-scale path) so a
        # transient dense failure degrades to FTS-only results instead of
        # aborting the whole hybrid search; _run_fts already self-handles errors.
        dense_raw, fts_pair = await asyncio.gather(
            _run_dense(), _run_fts(), return_exceptions=True
        )
        if isinstance(dense_raw, BaseException):
            logger.warning(
                f"Dense search failed for scale {scale} (using FTS-only): "
                f"{type(dense_raw).__name__}: {dense_raw}"
            )
            dense_results = []
        else:
            dense_results = dense_raw
        if isinstance(fts_pair, BaseException):
            # _run_fts swallows its own errors; this is defense-in-depth only.
            fts_results, fts_status = [], 'failed'
        else:
            fts_results, fts_status = fts_pair

        # RRF Fusion for this scale
        k_rrf = 60 if settings.rrf_legacy_mode else settings.hybrid_rrf_k
        clamped_alpha = max(0.0, min(1.0, hybrid_alpha))
        result_list = rrf_fuse(
            [dense_results, fts_results],
            k=k_rrf,
            weights=[clamped_alpha, 1.0 - clamped_alpha],
        )
        # Attach FTS status to each result
        for record in result_list:
            record["_fts_status"] = fts_status
        return result_list

    async def search(
        self,
        embedding: List[float],
        limit: int = 10,
        filter_expr: Optional[str] = None,
        vault_id: Optional[str] = None,
        query_text: str = "",
        hybrid: bool = True,
        hybrid_alpha: float = 0.5,
        bypass_vector_index: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Search for similar chunks by embedding.

        Args:
            embedding: Query embedding vector.
            limit: Maximum number of results.
            filter_expr: Optional filter expression (LanceDB syntax).
            vault_id: Optional vault ID to filter results. If provided, only returns
                      chunks from the specified vault.
            query_text: Raw query text for BM25 FTS search (used in hybrid search).
            hybrid: If True, combine dense vector search with BM25 FTS using RRF.
            hybrid_alpha: Weight for dense vs BM25 scores in RRF (0.0 = pure BM25, 1.0 = pure dense).
            bypass_vector_index: If True and the LanceDB query API supports it,
                run the dense search as a flat scan instead of using ANN.

        Returns:
            List of matching records with similarity scores. Each record includes:
            - All original fields (id, text, file_id, chunk_index, metadata, etc.)
            - _distance: Cosine distance from query embedding (lower = more similar)
            - _rrf_score: Reciprocal Rank Fusion score (when hybrid=True)
            Empty list if no table exists.

        Note:
            For cosine distance metric:
            - Distance of 0 = identical vectors (perfect match)
            - Distance of 1 = orthogonal vectors
            - Distance of 2 = opposite vectors (perfect mismatch)
            The _distance field is provided by LanceDB's vector search.
        """
        # Ensure DB connection exists
        if self.db is None:
            await self.connect()

        # Try to open existing table if not already loaded
        if self.table is None:
            try:
                table_names = await self.db.table_names()
            except (OSError, RuntimeError, ValueError) as e:
                raise VectorStoreConnectionError(
                    f"Failed to list table names: {e}"
                ) from e

            if "chunks" not in table_names:
                # No table exists yet - graceful no-docs behavior
                return []

            # Table exists, try to open it
            try:
                self.table = await self.db.open_table("chunks")
            except (OSError, RuntimeError, ValueError) as e:
                raise VectorStoreConnectionError(
                    f"Failed to open 'chunks' table: {e}"
                ) from e

            # Set embedding_dim from table schema if available
            if self._embedding_dim is None:
                try:
                    schema = await self.table.schema()
                    embedding_field = schema.field("embedding")
                    # Extract dimension from fixed size list type
                    if hasattr(embedding_field.type, "list_size"):
                        self._embedding_dim = embedding_field.type.list_size
                except (AttributeError, KeyError, IndexError, TypeError):
                    # If we can't determine embedding_dim, leave it as None
                    pass

        # Deferred index creation can mutate LanceDB even on this read path, so
        # it must be serialized with ingestion/delete via the write lock. To
        # avoid blocking EVERY search behind that lock (and behind in-flight
        # ingestion writes that hold it for up to write_lock_timeout_seconds),
        # first do a lock-free freshness probe and only acquire the write lock
        # when creation may actually be needed. _maybe_create_vector_index
        # re-validates freshness under the lock (double-checked locking), so a
        # concurrent builder cannot cause a duplicate rebuild.
        if await self._vector_index_needs_creation():
            async with self._acquire_write_lock():
                await self._maybe_create_vector_index()

        # Check for multi-scale search
        fetch_k = limit * 2

        if settings.multi_scale_indexing_enabled and settings.multi_scale_chunk_sizes:
            # Parse scale sizes
            scale_strs = [
                s.strip()
                for s in settings.multi_scale_chunk_sizes.split(",")
                if s.strip()
            ]

            if len(scale_strs) > 1:
                # Multi-scale search: query each scale with limited concurrency
                # and perform cross-scale RRF

                async def _sem_search_single_scale(scale: str) -> List[Dict[str, Any]]:
                    """Semaphore-guarded wrapper for _search_single_scale."""
                    async with self._acquire_search_semaphore():
                        return await self._search_single_scale(
                            embedding=embedding,
                            scale=scale,
                            fetch_k=fetch_k,
                            filter_expr=filter_expr,
                            vault_id=vault_id,
                            query_text=query_text,
                            hybrid=hybrid,
                            hybrid_alpha=hybrid_alpha,
                            bypass_vector_index=bypass_vector_index,
                        )

                # Create tasks for parallel execution (concurrency limited by semaphore)
                search_tasks = [_sem_search_single_scale(scale) for scale in scale_strs]

                # Execute all scale searches with limited concurrency
                scale_results_list = await asyncio.gather(
                    *search_tasks, return_exceptions=True
                )

                # Collect results from each successful scale into its own
                # list so we can run true cross-scale Reciprocal Rank
                # Fusion. Previously this code summed per-scale RRF scores
                # directly, ignoring rank position across scales and
                # making ``multi_scale_rrf_k`` a no-op.
                per_scale_lists: List[List[Dict[str, Any]]] = []
                for i, scale_results in enumerate(scale_results_list):
                    if isinstance(scale_results, list):
                        per_scale_lists.append(scale_results)
                    else:
                        logger.warning(
                            f"Search failed for scale {scale_strs[i]}: {scale_results}"
                        )
                        per_scale_lists.append([])

                # Defer to the shared rrf_fuse helper so the math is
                # consistent with the multi-query and memory paths. The
                # ``k`` parameter (``settings.multi_scale_rrf_k``) now
                # actually shapes cross-scale ranking.
                fused = rrf_fuse(
                    per_scale_lists,
                    k=settings.multi_scale_rrf_k,
                    limit=limit,
                )

                logger.info(
                    f"Multi-scale search: queried {len(scale_strs)} scales, "
                    f"returning {len(fused)} results (multi_scale_rrf_k={settings.multi_scale_rrf_k})"
                )
                return fused

        # Single-scale or multi-scale disabled: use existing behavior

        if self.table is None:
            return []

        async with self._acquire_search_semaphore():
            # Dense vector search
            # NOTE: Using query_type="vector" to bypass LanceDB's buggy auto-detection
            # which can cause UnboundLocalError when embedding_conf is None
            embedding_np = np.array(embedding, dtype=np.float32)

            # Apply vault filter if specified (pure string building — cheap, done
            # once and shared by the dense arm).
            # Dense arm fallback: LanceDB's .where() requires a single string predicate;
            # parameter binding is not available for the dense path. The vault_id is
            # already a string column so integer behaviour is unaffected.
            _filter_expr = filter_expr
            if vault_id is not None:
                safe_vault_id = _lance_escape(vault_id)
                vault_filter = f"vault_id = '{safe_vault_id}'"
                if _filter_expr:
                    _filter_expr = f"({_filter_expr}) AND ({vault_filter})"
                else:
                    _filter_expr = vault_filter

            async def _run_dense() -> List[Dict[str, Any]]:
                query = await self.table.search(embedding_np, query_type="vector")
                # Explicit distance type on EVERY dense query (issue #510
                # VECTOR-004) — see the comment on the single-scale path.
                if hasattr(query, "distance_type"):
                    query = query.distance_type(settings.vector_metric)
                if bypass_vector_index and hasattr(query, "bypass_vector_index"):
                    query = query.bypass_vector_index()
                if _filter_expr:
                    query = query.where(_filter_expr)
                return await query.limit(fetch_k).to_list()

            # If hybrid disabled, return dense results only (skip FTS round-trip).
            if not hybrid or not query_text:
                logger.debug(
                    "Dense-only search (hybrid disabled or no query text)"
                )
                return await _run_dense()

            async def _run_fts():
                # Returns (results, fts_status). Self-contained try/except so a
                # concurrent gather degrades to dense-only on FTS failure and the
                # _fts_exceptions counter is still incremented (preserving the
                # original sequential semantics) rather than aborting the search.
                try:
                    fts_query = await self.table.search(query_text, query_type="fts")
                    if vault_id:
                        # Use Expr API for type-safe vault_id filtering instead of
                        # string interpolation. lit() handles type conversion safely.
                        # Fallback to string interpolation when filter_expr is also
                        # present (requires string concatenation).
                        if filter_expr:
                            vault_id_filter = col("vault_id").eq(lit(vault_id)).to_sql()
                            fts_query = fts_query.where(
                                f"({vault_id_filter}) AND ({filter_expr})"
                            )
                        else:
                            fts_query = fts_query.where(col("vault_id").eq(lit(vault_id)))
                    elif filter_expr:
                        fts_query = fts_query.where(filter_expr)
                    results = await fts_query.limit(fetch_k).to_list()
                    if results:
                        logger.info(
                            f"Hybrid search (BM25 FTS) succeeded: {len(results)} FTS results (alpha={hybrid_alpha})"
                        )
                        return results, 'ok'
                    return results, 'empty'
                except Exception as e:
                    logger.warning(f"FTS search failed (falling back to dense-only): {type(e).__name__}: {e}")
                    with _fts_lock:
                        self._fts_exceptions += 1
                    return [], 'failed'

            # Run the dense (ANN) and BM25 FTS arms concurrently — independent
            # LanceDB queries feeding an order-insensitive RRF fusion. Use
            # return_exceptions=True (consistent with the multi-scale path) so a
            # transient dense failure degrades to FTS-only results instead of
            # aborting the whole hybrid search; _run_fts already self-handles errors.
            dense_raw, fts_pair = await asyncio.gather(
                _run_dense(), _run_fts(), return_exceptions=True
            )
            if isinstance(dense_raw, BaseException):
                logger.warning(
                    f"Dense search failed (using FTS-only): "
                    f"{type(dense_raw).__name__}: {dense_raw}"
                )
                dense_results = []
            else:
                dense_results = dense_raw
            if isinstance(fts_pair, BaseException):
                # _run_fts swallows its own errors; this is defense-in-depth only.
                fts_results, fts_status = [], 'failed'
            else:
                fts_results, fts_status = fts_pair

            # RRF Fusion using shared utility
            k_rrf = 60 if settings.rrf_legacy_mode else settings.hybrid_rrf_k
            clamped_alpha = max(0.0, min(1.0, hybrid_alpha))
            fused = rrf_fuse(
                [dense_results, fts_results],
                k=k_rrf,
                limit=limit,
                weights=[clamped_alpha, 1.0 - clamped_alpha],
            )
            # Attach FTS status to each result
            for record in fused:
                record["_fts_status"] = fts_status
            return fused

    async def vector_search_bypass_index_diagnostic(
        self,
        embedding: List[float],
        limit: int = 10,
        filter_expr: Optional[str] = None,
        vault_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Compare normal dense search with a flat-scan bypass when supported.

        LanceDB exposes ``bypass_vector_index()`` on vector queries in versions
        that support flat-scan ANN recall diagnostics. Older installed versions
        may not have that method, so this helper reports support explicitly.
        """
        normal = await self.search(
            embedding=embedding,
            limit=limit,
            filter_expr=filter_expr,
            vault_id=vault_id,
            hybrid=False,
            bypass_vector_index=False,
        )
        bypass = await self.search(
            embedding=embedding,
            limit=limit,
            filter_expr=filter_expr,
            vault_id=vault_id,
            hybrid=False,
            bypass_vector_index=True,
        )

        def _ids(rows: List[Dict[str, Any]]) -> List[str]:
            return [str(row.get("id", "")) for row in rows]

        normal_ids = _ids(normal)
        bypass_ids = _ids(bypass)
        overlap = len(set(normal_ids) & set(bypass_ids))

        return {
            "supported": await self._supports_bypass_vector_index(),
            "normal_results": normal,
            "bypass_results": bypass,
            "normal_ids": normal_ids,
            "bypass_ids": bypass_ids,
            "overlap_count": overlap,
            "limit": limit,
        }

    async def _supports_bypass_vector_index(self) -> bool:
        """Best-effort check for LanceDB flat-scan bypass support."""
        if self.table is None:
            return False
        try:
            expected_dim = await self._get_expected_embedding_dim()
            if expected_dim is None:
                return False
            probe = np.zeros(expected_dim, dtype=np.float32)
            query = await self.table.search(probe, query_type="vector")
            return hasattr(query, "bypass_vector_index")
        except Exception:
            return False

    async def delete_by_file(
        self, file_id: str, target: Optional[DimensionRebuildHandle] = None
    ) -> int:
        """Serialize deletion of all chunks for a file.

        With a ``target`` rebuild handle the delete runs against the rebuild
        temp table; the live table is untouched (issue #513 W13).
        """
        async with self._acquire_write_lock():
            if target is not None:
                return await self._delete_by_file_unlocked(file_id, target=target)
            return await self._delete_by_file_unlocked(file_id)

    async def _delete_by_file_unlocked(
        self, file_id: str, target: Optional[DimensionRebuildHandle] = None
    ) -> int:
        """
        Delete all chunks for a given file_id.

        Args:
            file_id: The file ID to delete chunks for.
            target: Optional dimension-rebuild handle to delete from the
                rebuild temp table instead of the live table.

        Returns:
            Number of records deleted.
        """
        if target is not None:
            if target.table is None:
                return 0
            safe_file_id = _lance_escape(file_id)
            try:
                count_before = await target.table.count_rows(
                    f"file_id = '{safe_file_id}'"
                )
            except (OSError, RuntimeError, ValueError):
                count_before = 0
            await target.table.delete(f"file_id = '{safe_file_id}'")
            return count_before
        # Ensure DB connection exists
        if self.db is None:
            await self.connect()

        # Try to open existing table if not already loaded
        if self.table is None:
            try:
                table_names = await self.db.table_names()
            except (OSError, RuntimeError, ValueError) as e:
                raise VectorStoreConnectionError(
                    f"Failed to list table names: {e}"
                ) from e

            if "chunks" not in table_names:
                # No table exists yet - nothing to delete
                return 0

            # Table exists, try to open it
            try:
                self.table = await self.db.open_table("chunks")
            except (OSError, RuntimeError, ValueError) as e:
                raise VectorStoreConnectionError(
                    f"Failed to open 'chunks' table: {e}"
                ) from e

            # Set embedding_dim from table schema if available
            if self._embedding_dim is None:
                try:
                    schema = await self.table.schema()
                    embedding_field = schema.field("embedding")
                    # Extract dimension from fixed size list type
                    if hasattr(embedding_field.type, "list_size"):
                        self._embedding_dim = embedding_field.type.list_size
                except (AttributeError, KeyError, IndexError, TypeError):
                    # If we can't determine embedding_dim, leave it as None
                    pass

        if self.table is None:
            return 0

        # Query count before delete to return accurate deletion count
        safe_file_id = _lance_escape(file_id)
        try:
            count_before = await self.table.count_rows(f"file_id = '{safe_file_id}'")
        except (OSError, RuntimeError, ValueError):
            # If count_rows fails, safely default to 0
            count_before = 0

        # LanceDB delete using filter expression
        await self.table.delete(f"file_id = '{safe_file_id}'")
        if count_before > 0:
            self._index_mutation_generation += 1

        # Manage ANN index lifecycle after delete (Issue #13)
        await self._maybe_rebuild_or_drop_vector_index(count_before)

        return count_before

    async def delete_by_vault(self, vault_id: str) -> int:
        """Serialize deletion of all chunks for a vault."""
        async with self._acquire_write_lock():
            return await self._delete_by_vault_unlocked(vault_id)

    async def _delete_by_vault_unlocked(self, vault_id: str) -> int:
        """
        Delete all chunks for a given vault_id.

        Args:
            vault_id: The vault ID to delete all chunks for.

        Returns:
            Number of records deleted.
        """
        # Ensure DB connection exists
        if self.db is None:
            await self.connect()

        # Try to open existing table if not already loaded
        if self.table is None:
            try:
                table_names = await self.db.table_names()
            except (OSError, RuntimeError, ValueError) as e:
                raise VectorStoreConnectionError(
                    f"Failed to list table names: {e}"
                ) from e

            if "chunks" not in table_names:
                return 0

            try:
                self.table = await self.db.open_table("chunks")
            except (OSError, RuntimeError, ValueError) as e:
                raise VectorStoreConnectionError(
                    f"Failed to open 'chunks' table: {e}"
                ) from e

        if self.table is None:
            return 0

        safe_vault_id = _lance_escape(vault_id)
        try:
            count_before = await self.table.count_rows(f"vault_id = '{safe_vault_id}'")
        except (OSError, RuntimeError, ValueError):
            count_before = 0

        await self.table.delete(f"vault_id = '{safe_vault_id}'")
        if count_before > 0:
            self._index_mutation_generation += 1

        # Manage ANN index lifecycle after delete (Issue #13)
        await self._maybe_rebuild_or_drop_vector_index(count_before)

        return count_before

    async def delete_old_generation_by_file(
        self,
        file_id: str,
        new_hash_short: str,
        target: Optional[DimensionRebuildHandle] = None,
    ) -> int:
        """Serialize stale-generation cleanup for a safe re-upload.

        With a ``target`` rebuild handle the cleanup runs against the rebuild
        temp table; the live table is untouched (issue #513 W13).
        """
        async with self._acquire_write_lock():
            if target is not None:
                return await self._delete_old_generation_by_file_unlocked(
                    file_id, new_hash_short, target=target
                )
            return await self._delete_old_generation_by_file_unlocked(
                file_id, new_hash_short
            )

    async def _delete_old_generation_by_file_unlocked(
        self,
        file_id: str,
        new_hash_short: str,
        target: Optional[DimensionRebuildHandle] = None,
    ) -> int:
        """Delete stale-generation chunks for a file after a safe re-upload (Issue #13).

        During safe re-upload the new chunks are inserted with IDs of the form
        ``{file_id}_{new_hash_short}_…``.  This method deletes every chunk for
        *file_id* whose ``id`` does NOT start with ``{file_id}_{new_hash_short}_``,
        i.e. chunks from the previous generation.

        Called only when ``REUPLOAD_SAFE_ORDER=True`` and after the new-generation
        chunks are already visible in the index.

        Args:
            file_id: The file ID whose old-generation chunks should be removed.
            new_hash_short: First 8 characters of the new file hash used as
                generation prefix for the newly inserted chunks.
            target: Optional dimension-rebuild handle to delete from the
                rebuild temp table instead of the live table.

        Returns:
            Number of stale-generation chunks deleted.
        """
        if target is not None:
            if target.table is None:
                return 0
            table = target.table
            safe_file_id = _lance_escape(file_id)
            safe_hash = _lance_escape(new_hash_short)
            new_prefix = f"{safe_file_id}_{safe_hash}_"
            try:
                count_before = await table.count_rows(
                    f"file_id = '{safe_file_id}'"
                )
                count_new = await table.count_rows(
                    f"file_id = '{safe_file_id}' AND id LIKE '{new_prefix}%'"
                )
                old_count = count_before - count_new
                if old_count <= 0:
                    return 0
                await table.delete(
                    f"file_id = '{safe_file_id}' AND NOT (id LIKE '{new_prefix}%')"
                )
                return old_count
            except (OSError, RuntimeError, ValueError) as e:
                logger.warning(
                    "delete_old_generation_by_file (rebuild target) failed: %s", e
                )
                return 0
        if self.db is None:
            await self.connect()

        if self.table is None:
            try:
                if self.db is not None:
                    table_names = await self.db.table_names()
                    if "chunks" not in table_names:
                        return 0
                    self.table = await self.db.open_table("chunks")
            except (OSError, RuntimeError, ValueError):
                return 0

        if self.table is None:
            return 0

        safe_file_id = _lance_escape(file_id)
        safe_hash = _lance_escape(new_hash_short)
        # Keep only chunks whose id starts with "{file_id}_{hash_short}_"
        new_prefix = f"{safe_file_id}_{safe_hash}_"
        try:
            count_before = await self.table.count_rows(
                f"file_id = '{safe_file_id}'"
            )
            # Count how many we're keeping (new generation)
            count_new = await self.table.count_rows(
                f"file_id = '{safe_file_id}' AND id LIKE '{new_prefix}%'"
            )
            old_count = count_before - count_new
            if old_count <= 0:
                return 0
            # Delete the old ones: file_id matches but id does NOT have new prefix
            await self.table.delete(
                f"file_id = '{safe_file_id}' AND NOT (id LIKE '{new_prefix}%')"
            )
            self._index_mutation_generation += 1
            # Manage ANN index lifecycle after delete (Issue #13)
            await self._maybe_rebuild_or_drop_vector_index(old_count)
            return old_count
        except (OSError, RuntimeError, ValueError) as e:
            logger.warning("delete_old_generation_by_file failed: %s", e)
            return 0

    async def _safe_table_to_pandas(self, table, operation_name: str):
        """Load a LanceDB table into pandas with OOM protection.

        Checks row count first and warns for large tables. For very large
        tables (>500k rows), raises an error instead of risking OOM.
        For tables up to 500k rows, proceeds with the load but logs a
        warning above 100k rows.
        """
        try:
            row_count = await table.count_rows()
        except Exception:
            row_count = -1  # Unknown — proceed cautiously

        if row_count > 500_000:
            raise VectorStoreError(
                f"{operation_name}: table has {row_count} rows. "
                f"Loading into memory would risk OOM. "
                f"Please run this migration with a dedicated script that processes in batches."
            )
        if row_count > 100_000:
            logger.warning(
                "%s: loading %d rows into memory. This may use significant RAM.",
                operation_name,
                row_count,
            )
        elif row_count > 0:
            logger.info(
                "%s: loading %d rows for migration.",
                operation_name,
                row_count,
            )

        return await table.to_pandas()

    async def _restore_default_indices(self, *, migration_name: str) -> None:
        """Recreate FTS (always) and ANN (rows >= threshold) on ``self.table``.

        Mirrors the staging swap's index step: a table recovered from the
        staging copy must not stay index-degraded until the next write or
        search happens to self-heal it (PRR-001, PR #526 review). Raises on
        failure so the caller preserves the staging table for another
        recovery attempt instead of dropping it.
        """
        await self.table.create_index(column="text", config=FTS(), replace=True)
        row_count = await self.table.count_rows()
        if row_count >= VECTOR_INDEX_MIN_ROWS:
            num_sub_vectors = (self._embedding_dim or settings.embedding_dim) // 8
            await self.table.create_index(
                column="embedding",
                config=IvfPq(
                    distance_type=cast(
                        "Literal['l2', 'cosine', 'dot']", settings.vector_metric
                    ),
                    num_partitions=256,
                    num_sub_vectors=num_sub_vectors,
                ),
                replace=True,
            )

    async def _reconcile_chunks_rebuild_state(self, *, migration_name: str) -> None:
        """Recovery-first reconciliation of a crashed ``chunks`` table swap.

        A prior failed run of any ``migrate_add_*`` rewrite may have left the
        staging table (``chunks_rebuild``) on disk. Re-entry NEVER assumes a
        clean state; every migration calls this before probing the canonical
        table (issue #512 VECTOR-001b):

        - staging absent → nothing to do;
        - canonical present → validate the canonical table holds at least the
          staging row count (the swap validates the final create BEFORE
          deleting staging, so a surviving canonical table is complete), then
          drop the stale staging table;
        - canonical absent → the staging table holds the last durable copy of
          the data: rebuild the canonical table from its data, validate row
          parity, then drop staging.

        Any failure logs CRITICAL naming the recoverable table and raises —
        this helper never reports success on failure.
        """
        if self.db is None:
            return
        table_names = await self.db.table_names()
        if CHUNKS_STAGING_TABLE not in table_names:
            return

        if "chunks" in table_names:
            canonical = await self.db.open_table("chunks")
            staging = await self.db.open_table(CHUNKS_STAGING_TABLE)
            canonical_rows = await canonical.count_rows()
            staging_rows = await staging.count_rows()
            if canonical_rows >= staging_rows:
                await self.db.drop_table(CHUNKS_STAGING_TABLE)
                logger.info(
                    "%s: dropped stale staging table '%s' (canonical 'chunks' "
                    "has %d rows >= staging %d)",
                    migration_name,
                    CHUNKS_STAGING_TABLE,
                    canonical_rows,
                    staging_rows,
                )
                return
            # Canonical incomplete — the staging copy is authoritative.
            staging_df = await self._safe_table_to_pandas(
                staging, f"{migration_name} (staging recovery)"
            )
            # Rebind outside _write_lock: recovery runs during lifespan
            # startup before the app serves requests; acquiring the lock here
            # would risk re-entrant deadlock if a future caller holds it.
            await self.db.drop_table("chunks")
            self.table = await self.db.create_table("chunks", data=staging_df)
            recovered_rows = await self.table.count_rows()
            if recovered_rows != len(staging_df):
                logger.critical(
                    "%s: parity check failed rebuilding 'chunks' from '%s' "
                    "(%d -> %d); staging table preserved for manual recovery.",
                    migration_name,
                    CHUNKS_STAGING_TABLE,
                    len(staging_df),
                    recovered_rows,
                )
                raise VectorStoreError(
                    f"{migration_name}: canonical 'chunks' rebuild from "
                    f"'{CHUNKS_STAGING_TABLE}' failed row parity "
                    f"({len(staging_df)} -> {recovered_rows})."
                )
            # No index restore here (NF-A, PR #526 feedback): when the
            # calling migration's swap follows, the swap recreates the
            # indices itself; in the worst case where every migration then
            # early-returns, init_table heals FTS later in the same startup
            # and ANN self-heals on first search. The canonical-absent path
            # below is where the restore is load-bearing (no swap follows).
            await self.db.drop_table(CHUNKS_STAGING_TABLE)
            logger.warning(
                "%s: restored incomplete 'chunks' from staging table '%s'",
                migration_name,
                CHUNKS_STAGING_TABLE,
            )
            return

        # Canonical absent: the staging table is the only durable copy.
        staging = await self.db.open_table(CHUNKS_STAGING_TABLE)
        staging_df = await self._safe_table_to_pandas(
            staging, f"{migration_name} (staging recovery)"
        )
        # Rebind outside _write_lock — see the sibling recovery path above
        # (startup-only; lock acquisition would risk re-entrant deadlock).
        self.table = await self.db.create_table("chunks", data=staging_df)
        recovered_rows = await self.table.count_rows()
        if recovered_rows != len(staging_df):
            logger.critical(
                "%s: parity check failed rebuilding 'chunks' from '%s' "
                "(%d -> %d); staging table preserved for manual recovery.",
                migration_name,
                CHUNKS_STAGING_TABLE,
                len(staging_df),
                recovered_rows,
            )
            raise VectorStoreError(
                f"{migration_name}: canonical 'chunks' rebuild from "
                f"'{CHUNKS_STAGING_TABLE}' failed row parity "
                f"({len(staging_df)} -> {recovered_rows})."
            )
        # Restore FTS/ANN before releasing the staging copy (PRR-001).
        await self._restore_default_indices(migration_name=migration_name)
        await self.db.drop_table(CHUNKS_STAGING_TABLE)
        logger.warning(
            "%s: recovered canonical 'chunks' (%d rows) from staging table '%s'",
            migration_name,
            recovered_rows,
            CHUNKS_STAGING_TABLE,
        )

    async def _swap_chunks_table(self, df, *, migration_name: str) -> None:
        """Non-destructive rewrite of the ``chunks`` table (issue #512 VECTOR-001b).

        State machine — every entry begins with reconciliation, never assumes
        an already-migrated or clean state:

        (a) recovery-first: reconcile any leftover ``chunks_rebuild`` from a
            prior failed run (see ``_reconcile_chunks_rebuild_state``);
        (b) write the replacement to the staging table ``chunks_rebuild``
            FIRST — BEFORE any drop — and validate its row count equals
            ``len(df)`` (this is the load-bearing non-destructive invariant);
        (c) validated swap: drop ``chunks`` → create ``chunks`` from the same
            in-memory df → validate row parity → restore the FTS (always) and
            ANN (≥ VECTOR_INDEX_MIN_ROWS) indices → drop staging → publish the
            new index generation to the recovery journal;
        (d) any failure at any point logs CRITICAL naming the recoverable
            table — the original before the drop, or the staging table after
            it — and RAISES. Failures are never reported as success-as-zero.

        API surface is strictly the db-level ``table_names / open_table /
        drop_table / create_table(schema=|data=)`` and table-level
        ``schema / count_rows / to_pandas`` calls (via
        ``_safe_table_to_pandas``): no rename_table, which the recovery
        contract does not provide.
        """
        if self.db is None:
            raise VectorStoreConnectionError("Database connection is not available.")

        # (a) recovery-first.
        await self._reconcile_chunks_rebuild_state(migration_name=migration_name)

        # (b) staging FIRST — before any drop.
        try:
            staging = await self.db.create_table(CHUNKS_STAGING_TABLE, data=df)
            staging_rows = await staging.count_rows()
            if staging_rows != len(df):
                raise VectorStoreError(
                    f"{migration_name}: staging table row count {staging_rows} "
                    f"!= expected {len(df)} — aborting before any drop."
                )
        except Exception as exc:
            logger.critical(
                "%s: staging table '%s' create/validate failed BEFORE any "
                "drop — the original 'chunks' table is intact (%s).",
                migration_name,
                CHUNKS_STAGING_TABLE,
                exc,
            )
            raise

        # (c) validated swap. From here on, a failure leaves the replacement
        # data durably recoverable in the staging table.
        try:
            await self.db.drop_table("chunks")
            # Rebind outside _write_lock: swaps run during lifespan startup
            # before the app serves requests (see the recovery paths above).
            self.table = await self.db.create_table("chunks", data=df)
            final_rows = await self.table.count_rows()
            if final_rows != len(df):
                raise VectorStoreError(
                    f"{migration_name}: final 'chunks' row count {final_rows} "
                    f"!= expected {len(df)}."
                )
            await self.table.create_index(column="text", config=FTS(), replace=True)
            num_sub_vectors = (self._embedding_dim or settings.embedding_dim) // 8
            if final_rows >= VECTOR_INDEX_MIN_ROWS:
                await self.table.create_index(
                    column="embedding",
                    config=IvfPq(
                        distance_type=cast(
                            "Literal['l2', 'cosine', 'dot']", settings.vector_metric
                        ),
                        num_partitions=256,
                        num_sub_vectors=num_sub_vectors,
                    ),
                    replace=True,
                )
                self._last_index_build_row_count = final_rows
                self._last_index_build_generation = self._index_mutation_generation
        except Exception as exc:
            logger.critical(
                "%s: 'chunks' swap failed after the original table was dropped "
                "(%s). The replacement data is durably preserved in the "
                "recoverable staging table '%s' — re-running the migration "
                "restores 'chunks' from it.",
                migration_name,
                exc,
                CHUNKS_STAGING_TABLE,
            )
            raise

        await self.db.drop_table(CHUNKS_STAGING_TABLE)

        # Publish the authoritative generation; a journal failure must never
        # break an already-completed swap.
        try:
            publish_index_generation(
                detail=(
                    f"{migration_name}: 'chunks' swapped via staging table "
                    f"'{CHUNKS_STAGING_TABLE}' ({len(df)} rows)"
                )
            )
        except Exception as exc:
            logger.warning(
                "%s: index-generation publication failed (swap already "
                "complete): %s",
                migration_name,
                exc,
            )

    async def get_live_embedding_dim(self) -> Optional[int]:
        """Public read of the live ``chunks`` table's embedding dimension.

        Returns None when no live table exists. Used by the reindex job (and
        the ingest path) to decide whether a re-embed requires a
        dimension-migrating rebuild (issue #513 W13 / RC-8).
        """
        if self.db is None:
            await self.connect()
        if self.db is None:
            return None
        if self.table is None:
            try:
                table_names = await self.db.table_names()
            except (OSError, RuntimeError, ValueError):
                return None
            if "chunks" not in table_names:
                return None
            try:
                self.table = await self.db.open_table("chunks")
            except (OSError, RuntimeError, ValueError):
                return None
        return await self._get_expected_embedding_dim()

    async def begin_dimension_rebuild(self, new_dim: int) -> DimensionRebuildHandle:
        """Open a dimension-migrating rebuild of the ``chunks`` table (W13).

        Creates a temp table (``chunks_dim_rebuild``) with the FULL canonical
        schema at ``new_dim``. While the handle is open, callers route vector
        writes through ``target=handle``; the live table is untouched. A stale
        temp table from a previously aborted/crashed run is dropped first — it
        was never committed, so the live table remains the only authority.

        Raises VectorStoreConnectionError when the connection or table
        creation fails (no state is left behind on failure).
        """
        async with self._acquire_write_lock():
            if self.db is None:
                await self.connect()
            if self.db is None:
                raise VectorStoreConnectionError(
                    "Database connection is not available."
                )
            try:
                table_names = await self.db.table_names()
                if DIMENSION_REBUILD_TABLE in table_names:
                    await self.db.drop_table(DIMENSION_REBUILD_TABLE)
                    logger.warning(
                        "Dropped stale dimension-rebuild table '%s' from a "
                        "prior aborted run (live 'chunks' untouched)",
                        DIMENSION_REBUILD_TABLE,
                    )
                table = await self.db.create_table(
                    DIMENSION_REBUILD_TABLE,
                    schema=_chunks_table_schema(new_dim),
                )
            except (OSError, RuntimeError, ValueError) as e:
                raise VectorStoreConnectionError(
                    f"Failed to create dimension-rebuild table: {e}"
                ) from e
            # FTS on the temp table so the post-swap index is immediately
            # hybrid-searchable; a failure here is non-fatal (commit recreates
            # indices on the swapped-in table anyway).
            try:
                await table.create_index(column="text", config=FTS(), replace=False)
            except (OSError, RuntimeError, ValueError) as e:
                logger.warning(
                    "FTS index creation on dimension-rebuild table failed "
                    "(non-fatal; recreated at commit): %s",
                    e,
                )
            logger.info(
                "Opened dimension rebuild into '%s' at dim=%d (live 'chunks' untouched)",
                DIMENSION_REBUILD_TABLE,
                new_dim,
            )
            return DimensionRebuildHandle(DIMENSION_REBUILD_TABLE, new_dim, table)

    async def commit_dimension_rebuild(self, handle: DimensionRebuildHandle) -> None:
        """Atomically swap the rebuild temp table in as the live ``chunks``.

        Mirrors the validated staging-swap guarantees of
        ``_swap_chunks_table``: the temp table holds the complete replacement
        data durably; the old ``chunks`` table is dropped only then, the new
        table is created from the temp data with the EXPLICIT new-dim schema
        (fixed-size embedding list), row parity is validated, and FTS/ANN
        indices are restored — all before the temp table is dropped. Any
        failure logs CRITICAL naming the still-recoverable temp table and
        RAISES; it is never reported as success.
        """
        async with self._acquire_write_lock():
            if self.db is None:
                raise VectorStoreConnectionError(
                    "Database connection is not available."
                )
            if handle is None or handle.table is None:
                raise VectorStoreError(
                    "Dimension rebuild handle has no temp table to commit."
                )
            df = await self._safe_table_to_pandas(
                handle.table, "dimension rebuild commit"
            )
            expected_rows = len(df)
            try:
                await self.db.drop_table("chunks")
                # Explicit schema keeps the fixed-size embedding list type at
                # the NEW dimension (create-from-data alone would infer a
                # variable-width list and lose the schema dim).
                self.table = await self.db.create_table(
                    "chunks", data=df, schema=_chunks_table_schema(handle.dim)
                )
                final_rows = await self.table.count_rows()
                if final_rows != expected_rows:
                    raise VectorStoreError(
                        f"dimension rebuild: final 'chunks' row count "
                        f"{final_rows} != expected {expected_rows}."
                    )
                self._embedding_dim = handle.dim
                await self.table.create_index(
                    column="text", config=FTS(), replace=True
                )
                if final_rows >= VECTOR_INDEX_MIN_ROWS:
                    await self.table.create_index(
                        column="embedding",
                        config=IvfPq(
                            distance_type=cast(
                                "Literal['l2', 'cosine', 'dot']", settings.vector_metric
                            ),
                            num_partitions=256,
                            num_sub_vectors=handle.dim // 8,
                        ),
                        replace=True,
                    )
                    self._last_index_build_row_count = final_rows
                    self._last_index_build_generation = self._index_mutation_generation
            except Exception as exc:
                logger.critical(
                    "dimension rebuild: 'chunks' swap failed after the old table "
                    "was dropped (%s). The replacement data is durably preserved "
                    "in the recoverable temp table '%s'.",
                    exc,
                    handle.table_name,
                )
                raise
            await self.db.drop_table(handle.table_name)
            self._index_mutation_generation += 1
            self._optimize_counter = 0
            try:
                publish_index_generation(
                    detail=(
                        f"dimension rebuild: 'chunks' swapped via temp table "
                        f"'{handle.table_name}' at dim={handle.dim} "
                        f"({expected_rows} rows)"
                    )
                )
            except Exception as exc:
                logger.warning(
                    "dimension rebuild: index-generation publication failed "
                    "(swap already complete): %s",
                    exc,
                )
            logger.info(
                "Committed dimension rebuild: 'chunks' now dim=%d (%d rows)",
                handle.dim,
                expected_rows,
            )

    async def abort_dimension_rebuild(self, handle: DimensionRebuildHandle) -> None:
        """Drop the rebuild temp table; the old index is left fully intact."""
        if handle is None:
            return
        async with self._acquire_write_lock():
            if self.db is None:
                return
            try:
                table_names = await self.db.table_names()
                if handle.table_name in table_names:
                    await self.db.drop_table(handle.table_name)
                    logger.info(
                        "Aborted dimension rebuild: dropped temp table '%s' "
                        "(live 'chunks' untouched)",
                        handle.table_name,
                    )
            except (OSError, RuntimeError, ValueError) as e:
                logger.warning(
                    "Failed to drop dimension-rebuild temp table '%s': %s "
                    "(left for the next begin_dimension_rebuild to reclaim)",
                    handle.table_name,
                    e,
                )
            finally:
                handle.table = None

    async def migrate_add_vault_id(self) -> int:
        """
        Migration: Assign vault_id to legacy chunks that lack it.

        LanceDB doesn't support ALTER TABLE or UPDATE, so this reads all data,
        adds the vault_id field, and rewrites the table through the validated
        staging swap (``_swap_chunks_table`` — non-destructive, restart-safe;
        issue #512 VECTOR-001b). Idempotent — safe to call multiple times
        (no-op if all records already have vault_id). Failures RAISE; only
        genuine no-ops (no connection, no table, nothing to backfill) return 0.

        Returns:
            Number of records migrated. 0 if no migration was needed.
        """
        if self.db is None:
            await self.connect()

        if self.db is None:
            logger.info("LanceDB vault_id migration: no connection available")
            return 0

        # Recovery-first: reconcile any crashed swap from a prior run before
        # probing the canonical table (issue #512).
        await self._reconcile_chunks_rebuild_state(migration_name="LanceDB vault_id migration")

        try:
            table_names = await self.db.table_names()
        except (OSError, RuntimeError, ValueError) as e:
            logger.warning(f"LanceDB vault_id migration failed: {e}")
            raise

        if "chunks" not in table_names:
            logger.info("LanceDB vault_id migration: no table exists")
            return 0

        try:
            table = await self.db.open_table("chunks")
        except (OSError, RuntimeError, ValueError) as e:
            logger.warning(f"LanceDB vault_id migration failed: {e}")
            raise

        # Check if vault_id column exists in schema
        schema = await table.schema()
        field_names = [schema.field(i).name for i in range(len(schema))]

        if "vault_id" in field_names:
            # Column exists — check if any rows have null vault_id
            df = await self._safe_table_to_pandas(table, "vault_id migration")
            null_count = df["vault_id"].isna().sum()
            if null_count == 0:
                logger.info("LanceDB vault_id migration: no migration needed")
                return 0  # All records already have vault_id

            # Backfill null vault_ids with empty string (truly unassigned)
            df["vault_id"] = df["vault_id"].fillna("")
            count = int(null_count)

            await self._swap_chunks_table(df, migration_name="LanceDB vault_id migration")
            logger.info(f"LanceDB vault_id migration: backfilled {count} records")
            return count

        # Column doesn't exist — add it to all records
        df = await self._safe_table_to_pandas(
            table, "vault_id migration (add column)"
        )
        if len(df) == 0:
            # Empty table — just drop and recreate with new schema
            # (zero rows: no data at risk). Try to get embedding_dim from
            # the existing schema before dropping.
            if self._embedding_dim is None:
                try:
                    schema = await table.schema()
                    embedding_field = schema.field("embedding")
                    if hasattr(embedding_field.type, "list_size"):
                        self._embedding_dim = embedding_field.type.list_size
                except (AttributeError, KeyError, IndexError, TypeError):
                    # If we can't determine embedding_dim, leave it as None
                    pass

            await self.db.drop_table("chunks")
            if self._embedding_dim:
                await self.init_table(self._embedding_dim)
            logger.info(
                "LanceDB vault_id migration: empty table, recreated with new schema"
            )
            return 0

        # Add vault_id column — legacy chunks are unassigned (vault unknown)
        df["vault_id"] = ""
        migrated_count = len(df)

        await self._swap_chunks_table(df, migration_name="LanceDB vault_id migration")
        logger.info(
            f"LanceDB vault_id migration: backfilled {migrated_count} records"
        )
        return migrated_count

    async def migrate_add_chunk_scale(self) -> int:
        """
        Migration: Backfill chunk_scale='default' on existing chunks that lack it.

        LanceDB doesn't support ALTER TABLE or UPDATE, so this reads all data,
        adds the chunk_scale field, and rewrites the table through the validated
        staging swap (``_swap_chunks_table`` — non-destructive, restart-safe;
        issue #512 VECTOR-001b). Idempotent — safe to call multiple times
        (no-op if all records already have chunk_scale). Failures RAISE; only
        genuine no-ops return 0.

        Returns:
            Number of records migrated. 0 if no migration was needed.
        """
        if self.db is None:
            await self.connect()

        if self.db is None:
            logger.info("LanceDB chunk_scale migration: no connection available")
            return 0

        # Recovery-first: reconcile any crashed swap from a prior run.
        await self._reconcile_chunks_rebuild_state(migration_name="LanceDB chunk_scale migration")

        try:
            table_names = await self.db.table_names()
        except (OSError, RuntimeError, ValueError) as e:
            logger.warning(f"LanceDB chunk_scale migration failed: {e}")
            raise

        if "chunks" not in table_names:
            logger.info("LanceDB chunk_scale migration: no table exists")
            return 0

        try:
            table = await self.db.open_table("chunks")
        except (OSError, RuntimeError, ValueError) as e:
            logger.warning(f"LanceDB chunk_scale migration failed: {e}")
            raise

        # Check if chunk_scale column exists in schema
        schema = await table.schema()
        field_names = [schema.field(i).name for i in range(len(schema))]

        if "chunk_scale" in field_names:
            # Column exists — check if any rows have null chunk_scale
            df = await self._safe_table_to_pandas(table, "chunk_scale migration")
            null_count = df["chunk_scale"].isna().sum()
            if null_count == 0:
                logger.info("LanceDB chunk_scale migration: no migration needed")
                return 0  # All records already have chunk_scale

            # Backfill null chunk_scales with "default"
            df["chunk_scale"] = df["chunk_scale"].fillna("default")
            count = int(null_count)

            await self._swap_chunks_table(df, migration_name="LanceDB chunk_scale migration")
            logger.info(
                f"LanceDB chunk_scale migration: backfilled {count} records"
            )
            return count

        # Column doesn't exist — add it to all records
        df = await self._safe_table_to_pandas(
            table, "chunk_scale migration (add column)"
        )
        if len(df) == 0:
            # Empty table — just drop and recreate with new schema
            # (zero rows: no data at risk). Try to get embedding_dim from
            # the existing schema before dropping.
            if self._embedding_dim is None:
                try:
                    schema = await table.schema()
                    embedding_field = schema.field("embedding")
                    if hasattr(embedding_field.type, "list_size"):
                        self._embedding_dim = embedding_field.type.list_size
                except (AttributeError, KeyError, IndexError, TypeError):
                    # If we can't determine embedding_dim, leave it as None
                    pass

            await self.db.drop_table("chunks")
            if self._embedding_dim:
                await self.init_table(self._embedding_dim)
            logger.info(
                "LanceDB chunk_scale migration: empty table, recreated with new schema"
            )
            return 0

        # Add chunk_scale column with default "default"
        df["chunk_scale"] = "default"
        migrated_count = len(df)

        await self._swap_chunks_table(df, migration_name="LanceDB chunk_scale migration")
        logger.info(
            f"LanceDB chunk_scale migration: backfilled {migrated_count} records"
        )
        return migrated_count

    async def migrate_add_sparse_embedding(self) -> int:
        """
        Migration: Add sparse_embedding column to existing chunks table.

        LanceDB doesn't support ALTER TABLE, so this reads all data,
        adds the sparse_embedding field (default null), and rewrites the table
        through the validated staging swap (``_swap_chunks_table`` —
        non-destructive, restart-safe; issue #512 VECTOR-001b). Idempotent —
        safe to call multiple times. Failures RAISE; only genuine no-ops
        return 0.

        Returns:
            Number of records migrated. 0 if no migration was needed.
        """
        if self.db is None:
            await self.connect()

        if self.db is None:
            logger.info("LanceDB sparse_embedding migration: no connection available")
            return 0

        # Recovery-first: reconcile any crashed swap from a prior run.
        await self._reconcile_chunks_rebuild_state(migration_name="LanceDB sparse_embedding migration")

        try:
            table_names = await self.db.table_names()
        except (OSError, RuntimeError, ValueError) as e:
            logger.warning(f"LanceDB sparse_embedding migration failed: {e}")
            raise

        if "chunks" not in table_names:
            logger.info("LanceDB sparse_embedding migration: no table exists")
            return 0

        try:
            table = await self.db.open_table("chunks")
        except (OSError, RuntimeError, ValueError) as e:
            logger.warning(f"LanceDB sparse_embedding migration failed: {e}")
            raise

        # Check if sparse_embedding column exists in schema
        schema = await table.schema()
        field_names = [schema.field(i).name for i in range(len(schema))]

        if "sparse_embedding" in field_names:
            logger.info("LanceDB sparse_embedding migration: column already exists")
            return 0

        # Column doesn't exist — add it to all records (default None/null)
        df = await self._safe_table_to_pandas(table, "sparse_embedding migration")
        if len(df) == 0:
            # Empty table — just drop and recreate with new schema
            if self._embedding_dim is None:
                try:
                    schema = await table.schema()
                    embedding_field = schema.field("embedding")
                    if hasattr(embedding_field.type, "list_size"):
                        self._embedding_dim = embedding_field.type.list_size
                except (AttributeError, KeyError, IndexError, TypeError):
                    # If we can't determine embedding_dim, leave it as None
                    pass

            # Explicit error if embedding_dim cannot be determined
            if self._embedding_dim is None:
                raise VectorStoreError(
                    "Cannot determine embedding dimension for migration"
                )

            await self.db.drop_table("chunks")
            if self._embedding_dim:
                await self.init_table(self._embedding_dim)
            logger.info(
                "LanceDB sparse_embedding migration: empty table, recreated with new schema"
            )
            return 0

        # Add sparse_embedding column with default None (null)
        df["sparse_embedding"] = None
        migrated_count = len(df)

        # Validated staging swap: recreate the table AND restore the FTS/ANN
        # indices (ANN only at ≥ VECTOR_INDEX_MIN_ROWS, matching the deferred
        # index-creation policy FR-013).
        await self._swap_chunks_table(df, migration_name="LanceDB sparse_embedding migration")
        logger.info(
            f"LanceDB sparse_embedding migration: added column to {migrated_count} records"
        )
        return migrated_count

    async def migrate_add_parent_window(self, dry_run: bool = False) -> int:
        """Migration: Add parent_doc_id, parent_window_start, parent_window_end, and
        chunk_position columns to existing chunks that lack them (Issue #12).

        Idempotent — safe to run multiple times. Existing rows receive:
        - parent_doc_id = file_id (denormalized for query-time access)
        - parent_window_start = None (backfilled to 0 for first chunk of each file)
        - parent_window_end = None
        - chunk_position = chunk_index (sequential index already stored)

        The rewrite goes through the validated staging swap
        (``_swap_chunks_table`` — non-destructive, restart-safe; issue #512
        VECTOR-001b). An EMPTY legacy table gets the current schema applied
        immediately via ``init_table`` instead of being skipped (issue #512
        VECTOR-005 — "applied on first ingest" left the legacy schema in
        place and the next new-format write failed). Failures RAISE; only
        genuine no-ops return 0.

        Args:
            dry_run: If True, report rows that would be updated but make no changes.

        Returns:
            Number of rows that were (or would be, in dry-run) updated.
        """
        if self.db is None:
            await self.connect()

        if self.db is None:
            logger.info("LanceDB parent_window migration: no connection available")
            return 0

        # Recovery-first: reconcile any crashed swap from a prior run.
        await self._reconcile_chunks_rebuild_state(migration_name="LanceDB parent_window migration")

        try:
            table_names = await self.db.table_names()
        except (OSError, RuntimeError, ValueError) as e:
            logger.warning(f"LanceDB parent_window migration failed: {e}")
            raise

        if "chunks" not in table_names:
            logger.info("LanceDB parent_window migration: no table exists — nothing to migrate")
            return 0

        try:
            table = await self.db.open_table("chunks")
        except (OSError, RuntimeError, ValueError) as e:
            logger.warning(f"LanceDB parent_window migration failed to open table: {e}")
            raise

        schema = await table.schema()
        field_names = [schema.field(i).name for i in range(len(schema))]
        new_cols = {"parent_doc_id", "parent_window_start", "parent_window_end", "chunk_position"}
        missing_cols = new_cols - set(field_names)

        if not missing_cols:
            # All columns present — check if any rows still have null parent_doc_id
            df = await self._safe_table_to_pandas(table, "parent_window migration (check)")
            null_count = int(df["parent_doc_id"].isna().sum())
            if null_count == 0:
                logger.info("LanceDB parent_window migration: all rows already backfilled")
                return 0
            if dry_run:
                logger.info(
                    "[DRY RUN] parent_window migration: %d rows would be backfilled "
                    "(parent_doc_id is null)", null_count
                )
                return null_count
            # Backfill rows where parent_doc_id is null
            mask = df["parent_doc_id"].isna()
            df.loc[mask, "parent_doc_id"] = df.loc[mask, "file_id"]
            df.loc[mask, "chunk_position"] = df.loc[mask, "chunk_index"]
            # parent_window_start/end remain null — populated on next re-ingest
            await self._swap_chunks_table(df, migration_name="LanceDB parent_window migration")
            logger.info(
                "LanceDB parent_window migration: backfilled parent_doc_id on %d rows",
                null_count,
            )
            return null_count

        # Some columns are missing — add them all
        df = await self._safe_table_to_pandas(table, "parent_window migration (add columns)")
        if len(df) == 0:
            # Empty table: apply the CURRENT schema now instead of deferring to
            # first ingest (issue #512 VECTOR-005). Zero rows means no data at
            # risk. Read the embedding dim from the legacy schema first and
            # recreate via init_table, mirroring the sibling empty-branches in
            # migrate_add_vault_id / chunk_scale / sparse_embedding.
            if self._embedding_dim is None:
                try:
                    schema = await table.schema()
                    embedding_field = schema.field("embedding")
                    if hasattr(embedding_field.type, "list_size"):
                        self._embedding_dim = embedding_field.type.list_size
                except (AttributeError, KeyError, IndexError, TypeError):
                    # If we can't determine embedding_dim, fall back to the
                    # configured dim below (existing behavior).
                    pass

            await self.db.drop_table("chunks")
            if self._embedding_dim:
                await self.init_table(self._embedding_dim)
            elif settings.embedding_dim:
                await self.init_table(settings.embedding_dim)
            logger.info(
                "LanceDB parent_window migration: empty table — recreated with "
                "the current schema (parent-window columns applied)"
            )
            return 0

        migrated_count = len(df)
        if dry_run:
            logger.info(
                "[DRY RUN] parent_window migration: %d rows would receive new columns: %s",
                migrated_count,
                ", ".join(sorted(missing_cols)),
            )
            return migrated_count

        # Add missing columns with sensible defaults
        if "parent_doc_id" in missing_cols:
            df["parent_doc_id"] = df["file_id"]
        if "chunk_position" in missing_cols:
            df["chunk_position"] = df["chunk_index"]
        # parent_window_start / end remain null — populated on next re-ingest
        for _pw_col in ("parent_window_start", "parent_window_end"):
            if _pw_col in missing_cols:
                df[_pw_col] = None

        await self._swap_chunks_table(df, migration_name="LanceDB parent_window migration")
        logger.info(
            "LanceDB parent_window migration: added columns to %d rows", migrated_count
        )
        return migrated_count

    async def get_chunks_by_uid(self, chunk_uids: List[str]) -> List[Dict[str, Any]]:
        """
        Fetch chunks by their unique IDs.

        Args:
            chunk_uids: List of chunk UIDs in format "{file_id}_{chunk_index}"

        Returns:
            List of matching chunk records from LanceDB.
        """
        if self.table is None:
            return []

        if not chunk_uids:
            return []

        try:
            # Build IN clause for chunk_uids
            # Each uid is in format "{file_id}_{chunk_index}"
            # Escape single quotes in uids for SQL-like syntax
            escaped_uids = [_lance_escape(uid) for uid in chunk_uids]
            quoted_uids = [f"'{uid}'" for uid in escaped_uids]
            uid_list = ", ".join(quoted_uids)

            # Query chunks where id is in the list of chunk_uids
            query = f"id IN ({uid_list})"
            results = await self.table.query().where(query).to_list()

            return results
        except (OSError, RuntimeError, ValueError) as e:
            logger.warning(f"Failed to fetch chunks by UID: {e}")
            return []

    async def get_chunks_by_file_range(
        self,
        file_id: str,
        chunk_index: int,
        before: int,
        after: int,
    ) -> List[Dict[str, Any]]:
        """Fetch neighbor chunks of a target chunk within the same file (Issue #396).

        Used by the configurable context-window endpoint to build structured
        before/match/after context. ``file_id`` is the LanceDB string column
        (escaped via ``_lance_escape``); ``chunk_index`` selects the center; the
        range ``[chunk_index - before, chunk_index + after]`` is fetched and
        returned sorted by ``chunk_index`` (client-side — LanceDB ``orderby`` is
        not relied on elsewhere in this codebase). Non-contiguous indices (gaps
        from failed-chunk retries) are handled naturally: only existing rows
        return.
        """
        if self.table is None:
            return []
        if before <= 0 and after <= 0:
            return []
        lo = int(chunk_index) - max(0, before)
        hi = int(chunk_index) + max(0, after)
        esc_file = _lance_escape(str(file_id))
        query = (
            f"file_id = '{esc_file}' "
            f"AND chunk_index BETWEEN {int(lo)} AND {int(hi)}"
        )
        try:
            rows = await self.table.query().where(query).to_list()
        except (OSError, RuntimeError, ValueError) as e:
            logger.warning(f"Failed to fetch neighbor chunks: {e}")
            return []
        rows.sort(key=lambda r: r.get("chunk_index", 0) if isinstance(r, dict) else 0)
        return rows

    async def get_stats(self) -> Dict[str, Any]:
        """
        Get statistics about the vector store.

        Returns:
            Dictionary with stats like total chunks, embedding dimension.
        """
        if self.table is None:
            return {"total_chunks": 0, "embedding_dim": self._embedding_dim}

        return {
            "total_chunks": await self.table.count_rows(),
            "embedding_dim": self._embedding_dim,
        }

    def close(self) -> None:
        """Close the database connection."""
        # LanceDB connections are typically stateless
        self.db = None
        self.table = None

    async def get_stored_metadata(self) -> Optional[Dict[str, Any]]:
        """
        Get stored metadata from the table's metadata.

        Returns:
            Dictionary with stored metadata (embedding_model_id, embedding_dim, embedding_prefix_hash)
            or None if table doesn't exist or no metadata is stored.
        """
        if self.table is None:
            return None

        try:
            # Try to get table metadata
            schema = await self.table.schema()
            table_metadata = schema.metadata
            if table_metadata:
                # Convert bytes keys/values to strings if needed
                metadata = {}
                for key, value in table_metadata.items():
                    if isinstance(key, bytes):
                        key = key.decode("utf-8")
                    if isinstance(value, bytes):
                        value = value.decode("utf-8")
                    metadata[key] = value

                # Extract our stored fields
                result = {}
                if (
                    b"embedding_model_id" in table_metadata
                    or "embedding_model_id" in metadata
                ):
                    result["embedding_model_id"] = metadata.get("embedding_model_id")
                if b"embedding_dim" in table_metadata or "embedding_dim" in metadata:
                    result["embedding_dim"] = int(metadata.get("embedding_dim", 0))
                if (
                    b"embedding_prefix_hash" in table_metadata
                    or "embedding_prefix_hash" in metadata
                ):
                    result["embedding_prefix_hash"] = metadata.get(
                        "embedding_prefix_hash"
                    )

                if result:
                    return result
        except (AttributeError, KeyError, IndexError, TypeError, ValueError) as e:
            logger.debug(f"Failed to read table metadata: {e}")

        return None

    async def validate_schema(
        self, embedding_model_id: str, embedding_dim: int
    ) -> Dict[str, Any]:
        """
        Validate that the table schema matches the current embedding configuration.

        Compares the stored embedding model identity from the SQLite settings_kv sidecar
        against the configured model and sets vector_store._ready = False on mismatch.

        Args:
            embedding_model_id: The embedding model identifier
            embedding_dim: The expected embedding dimension

        Returns:
            Dictionary with validation results including ready flag, stored/expected metadata,
            mismatch flag, mismatch details, and actual dimension.
        """
        # Generate a probe embedding for "dimension_probe" text (kept for diagnostic purposes)
        probe_text = "dimension_probe"
        try:
            probe_embedding = self._generate_probe_embedding(probe_text, embedding_dim)
        except (ValueError, TypeError, RuntimeError) as e:
            logger.warning(f"Failed to generate probe embedding: {e}")
            probe_embedding = None

        # Check if table exists
        if self.db is None:
            self._ready = True
            logger.debug("validate_schema: db is None, marking ready=True")
            return {
                "table_exists": False,
                "ready": True,
                "stored_metadata": None,
                "expected_metadata": None,
                "mismatch": False,
                "mismatch_details": [],
                "actual_dim": None,
                "probe_embedding_generated": probe_embedding is not None,
            }

        try:
            table_names = await self.db.table_names()
            table_exists = "chunks" in table_names
        except (OSError, RuntimeError, ValueError) as e:
            logger.warning(f"Failed to check table existence: {e}")
            self._ready = True
            return {
                "table_exists": False,
                "ready": True,
                "stored_metadata": None,
                "expected_metadata": None,
                "mismatch": False,
                "mismatch_details": [],
                "actual_dim": None,
                "probe_embedding_generated": probe_embedding is not None,
            }

        if not table_exists:
            self._ready = True
            logger.debug("validate_schema: chunks table does not exist, marking ready=True")
            return {
                "table_exists": False,
                "ready": True,
                "stored_metadata": None,
                "expected_metadata": None,
                "mismatch": False,
                "mismatch_details": [],
                "actual_dim": None,
                "probe_embedding_generated": probe_embedding is not None,
            }

        # Table exists - open it and read schema
        if self.table is None:
            self.table = await self.db.open_table("chunks")

        # Get schema and extract actual embedding dimension
        actual_dim: Optional[int] = None
        try:
            schema = await self.table.schema()
            embedding_field = schema.field("embedding")
            if hasattr(embedding_field.type, "list_size"):
                actual_dim = embedding_field.type.list_size
        except (AttributeError, KeyError, IndexError, TypeError, ValueError) as e:
            logger.warning(f"Failed to read embedding dimension from table schema: {e}")

        # Read stored model identity from sidecar
        stored_metadata = await self.get_embedding_metadata()

        # Build expected metadata
        expected_metadata = {
            "embedding_model_id": embedding_model_id,
            "embedding_dim": embedding_dim,
            "embedding_prefix_hash": self._compute_embedding_prefix_hash(),
        }

        # Check if sidecar has a stored identifier
        has_stored_identifier = (
            stored_metadata.get("embedding_model_id") is not None
            and stored_metadata.get("embedding_model_id") != ""
        )

        if not has_stored_identifier:
            self._ready = False
            logger.warning(
                "No stored embedding model identifier found in settings_kv; "
                "vector store marked not ready. Reindex required."
            )
            return {
                "table_exists": True,
                "ready": False,
                "stored_metadata": stored_metadata,
                "expected_metadata": expected_metadata,
                "mismatch": True,
                "mismatch_details": ["no_stored_identifier"],
                "actual_dim": actual_dim,
                "probe_embedding_generated": probe_embedding is not None,
            }

        # Compare all three components
        mismatch_details: List[str] = []

        # Compare embedding_model_id
        stored_model_id = stored_metadata.get("embedding_model_id")
        if stored_model_id != embedding_model_id:
            mismatch_details.append("embedding_model_id")

        # Compare embedding_dim (treat None stored_dim as mismatch; compare as strings)
        stored_dim = stored_metadata.get("embedding_dim")
        if stored_dim is None:
            mismatch_details.append("embedding_dim")
        elif str(stored_dim) != str(embedding_dim):
            mismatch_details.append("embedding_dim")

        # Compare embedding_prefix_hash
        stored_hash = stored_metadata.get("embedding_prefix_hash")
        expected_hash = expected_metadata["embedding_prefix_hash"]
        if stored_hash != expected_hash:
            mismatch_details.append("embedding_prefix_hash")

        if mismatch_details:
            self._ready = False
            logger.warning(
                "Embedding model identity mismatch detected; vector store marked not ready. "
                "Mismatched fields: %s. Stored: %s, Expected: %s",
                mismatch_details,
                stored_metadata,
                expected_metadata,
            )
            return {
                "table_exists": True,
                "ready": False,
                "stored_metadata": stored_metadata,
                "expected_metadata": expected_metadata,
                "mismatch": True,
                "mismatch_details": mismatch_details,
                "actual_dim": actual_dim,
                "probe_embedding_generated": probe_embedding is not None,
            }

        # Everything matches
        self._ready = True
        logger.info(
            "Vector store embedding model identity matches configured model (model=%s dim=%d).",
            embedding_model_id,
            embedding_dim,
        )
        return {
            "table_exists": True,
            "ready": True,
            "stored_metadata": stored_metadata,
            "expected_metadata": expected_metadata,
            "mismatch": False,
            "mismatch_details": [],
            "actual_dim": actual_dim,
            "probe_embedding_generated": probe_embedding is not None,
        }

    def _generate_probe_embedding(self, text: str, dim: int) -> List[float]:
        """
        Generate a probe embedding for dimension validation.

        Args:
            text: The text to generate embedding for
            dim: Expected dimension

        Returns:
            Generated embedding vector
        """
        # Use a deterministic hash-based approach for probe embedding
        import hashlib
        import random

        # Create a deterministic seed from the text
        seed_value = int(hashlib.md5(text.encode("utf-8")).hexdigest(), 16) % (2**32)
        random.seed(seed_value)

        # Generate a random vector of expected dimension
        # This simulates what a real embedding would look like
        probe = [random.gauss(0, 1) for _ in range(dim)]

        # Normalize the vector (typical for embeddings)
        magnitude = sum(x * x for x in probe) ** 0.5
        if magnitude > 0:
            probe = [x / magnitude for x in probe]

        return probe
