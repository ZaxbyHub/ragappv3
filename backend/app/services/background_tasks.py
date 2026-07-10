"""
Background task processor for document ingestion.

Provides BackgroundProcessor class that manages an asyncio queue for processing
documents with retry logic and graceful shutdown.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import List, Optional

from app.config import settings

from ..models.database import SQLiteConnectionPool
from .document_processor import DocumentProcessingError, DocumentProcessor
from .embeddings import EmbeddingService
from .llm_client import LLMClient
from .maintenance import MaintenanceService
from .vector_store import VectorStore

logger = logging.getLogger(__name__)

# Timeout for processing rows during startup recovery sweep.
# If a row has been in status='processing' for longer than this,
# it will be reset to 'pending' for re-processing.
STRANDED_PROCESSING_TIMEOUT_MINUTES = 30

# Retry cap for pending vector-store deletes (Issue #219). Rows that exceed
# this are left in place and logged for operator visibility — we never delete
# a pending record without confirming the chunks are actually gone.
MAX_VECTOR_DELETE_ATTEMPTS = 10

# Singleton instance
_processor_instance: Optional["BackgroundProcessor"] = None


def get_background_processor(
    max_retries: int = 3,
    retry_delay: float = 1.0,
    chunk_size_chars: int = 2000,
    chunk_overlap_chars: int = 200,
    vector_store: Optional[VectorStore] = None,
    embedding_service: Optional[EmbeddingService] = None,
    maintenance_service: Optional[MaintenanceService] = None,
    pool: Optional["SQLiteConnectionPool"] = None,
    llm_client: Optional[LLMClient] = None,
) -> "BackgroundProcessor":
    """
    Get or create the singleton BackgroundProcessor instance.

    This factory function ensures that only one BackgroundProcessor exists
    across the entire application lifecycle, preventing the issue where
    local instances are created and destroyed in request handlers.

    Args:
        max_retries: Maximum retry attempts for failed tasks (default: 3)
        retry_delay: Base delay in seconds between retries (default: 1.0)
        chunk_size_chars: Target chunk size in characters for DocumentProcessor
        chunk_overlap_chars: Overlap between chunks in characters for DocumentProcessor
        vector_store: VectorStore instance for document storage
        embedding_service: EmbeddingService instance for generating embeddings
        maintenance_service: MaintenanceService instance for maintenance mode checks
        pool: SQLiteConnectionPool instance for database connections
        llm_client: LLMClient instance for contextual chunking

    Returns:
        The singleton BackgroundProcessor instance
    """
    global _processor_instance
    if _processor_instance is None:
        _processor_instance = BackgroundProcessor(
            max_retries=max_retries,
            retry_delay=retry_delay,
            chunk_size_chars=chunk_size_chars,
            chunk_overlap_chars=chunk_overlap_chars,
            vector_store=vector_store,
            embedding_service=embedding_service,
            maintenance_service=maintenance_service,
            pool=pool,
            llm_client=llm_client,
        )
        logger.info("Created singleton BackgroundProcessor instance")
    return _processor_instance


def reset_background_processor() -> None:
    """Reset the singleton instance (for testing purposes)."""
    global _processor_instance
    if _processor_instance is not None and _processor_instance.is_running:
        import asyncio
        asyncio.create_task(_processor_instance.stop())
    _processor_instance = None


@dataclass
class TaskItem:
    """
    Represents a task item in the background queue.

    Attributes:
        file_path: Path to the file to process
        vault_id: Vault to associate the file with
        attempt: Current attempt count (starts at 1)
        source: Source of the file ('upload', 'scan', 'email')
        email_subject: Subject line for email-sourced files
        email_sender: Sender address for email-sourced files
    """
    file_path: str
    vault_id: int
    attempt: int = 1
    source: str = 'upload'
    email_subject: Optional[str] = None
    email_sender: Optional[str] = None
    # When set, the worker calls DocumentProcessor.process_existing_file
    # against this row id instead of process_file. The async upload route
    # populates this so the worker does NOT re-run duplicate detection or
    # create a duplicate `files` row. Scan/email paths leave this None so
    # legacy behavior (process_file) is preserved.
    file_id: Optional[int] = None


@dataclass
class EnrichmentTaskItem:
    """Post-index enrichment task for an already indexed file.

    Attributes:
        file_id: Database ID of the file.
        file_path: Path to the indexed file.
        vault_id: Vault the file belongs to.
        file_hash: SHA-256 hash of the file content.
        chunks: List of chunk dictionaries produced during indexing.
        document_text: Full text of the document for enrichment.
        attempt: Current retry attempt count (0 = first attempt).
            Incremented on each retry; compared against ``max_retries``.
    """

    file_id: int
    file_path: str
    vault_id: int
    file_hash: str
    chunks: list
    document_text: str
    attempt: int = 0


@dataclass
class ReindexTaskItem:
    """Reindex job task item.

    Attributes:
        job_id: Database ID of the reindex job.
    """

    job_id: int


class BackgroundProcessor:
    """
    Background task processor using asyncio.Queue for document ingestion.

    Manages a worker loop that processes files using DocumentProcessor with
    retry logic (max 3 attempts). Failed tasks are requeued with exponential
    backoff delay. A separate enrichment worker retries failed enrichment jobs
    up to max_retries times with the same exponential backoff, and skips
    requeue when shutdown is in progress. Provides graceful shutdown via
    asyncio.Event.

    Attributes:
        max_retries: Maximum number of retry attempts per task
        retry_delay: Base delay in seconds between retries (doubles each attempt)
        queue: asyncio.Queue holding TaskItem objects
        enrichment_queue: asyncio.Queue holding EnrichmentTaskItem objects
        shutdown_event: asyncio.Event for graceful shutdown
        processor: DocumentProcessor instance for file processing
        _worker_tasks: List of worker task references
        _enrichment_worker_task: Enrichment worker task reference
        _running: Boolean indicating if processor is active
    """

    def __init__(
        self,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        chunk_size_chars: int = 2000,
        chunk_overlap_chars: int = 200,
        vector_store: Optional[VectorStore] = None,
        embedding_service: Optional[EmbeddingService] = None,
        maintenance_service: Optional[MaintenanceService] = None,
        pool: Optional["SQLiteConnectionPool"] = None,
        llm_client: Optional[LLMClient] = None,
    ):
        """
        Initialize the background processor.

        Args:
            max_retries: Maximum retry attempts for failed tasks (default: 3)
            retry_delay: Base delay in seconds between retries (default: 1.0)
            chunk_size_chars: Target chunk size in characters for DocumentProcessor
            chunk_overlap_chars: Overlap between chunks in characters for DocumentProcessor
            vector_store: VectorStore instance for document storage
            embedding_service: EmbeddingService instance for generating embeddings
            maintenance_service: MaintenanceService instance for maintenance mode
            pool: SQLiteConnectionPool instance for database connections
            llm_client: LLMClient instance for contextual chunking
        """
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.queue: asyncio.Queue[TaskItem] = asyncio.Queue(maxsize=settings.ingestion_queue_max_size)
        self.enrichment_queue: asyncio.Queue[EnrichmentTaskItem] = asyncio.Queue(maxsize=settings.ingestion_queue_max_size)
        self.reindex_queue: asyncio.Queue[ReindexTaskItem] = asyncio.Queue(maxsize=settings.ingestion_queue_max_size)
        self.shutdown_event = asyncio.Event()
        self.processor = DocumentProcessor(
            chunk_size_chars=chunk_size_chars,
            chunk_overlap_chars=chunk_overlap_chars,
            vector_store=vector_store,
            embedding_service=embedding_service,
            pool=pool,
            llm_client=llm_client,
        )
        self._worker_tasks: List[asyncio.Task] = []
        self._enrichment_worker_task: Optional[asyncio.Task] = None
        self._vector_delete_sweep_task: Optional[asyncio.Task] = None
        self._reindex_worker_task: Optional[asyncio.Task] = None
        self._running = False
        self.maintenance_service = maintenance_service
        self._write_semaphore: Optional[asyncio.Semaphore] = None

    def set_llm_client(self, llm_client: Optional[LLMClient]) -> None:
        """Rebind the owned DocumentProcessor to a different ingestion LLM client."""
        self.processor.set_llm_client(llm_client)

    async def start(self) -> None:
        """
        Start the background processor worker loop.

        Creates and starts the worker task that processes items from the queue.
        Safe to call multiple times - will not create duplicate workers.

        Also runs a startup recovery sweep: under the async upload route the
        request inserts a `files` row with status='pending' / phase='queued'
        and only then enqueues. If the process crashes between the insert and
        the worker pickup, the row would be stranded forever and the
        in-flight duplicate check would 409 every retry of the same hash.
        The sweep re-enqueues stranded rows so they are processed normally.

        CRITICAL ORDERING: workers MUST be spawned before _recover_stranded_pending_rows()
        runs. The recovery sweep enqueues into the bounded queue
        (maxsize=settings.ingestion_queue_max_size, default 1000). If more
        stranded rows exist than the queue can hold, put() blocks indefinitely
        waiting for a consumer. Spawning workers first ensures the consumers
        exist before any enqueue happens.
        """
        if self._running:
            logger.warning("Background processor is already running")
            return

        self._running = True
        self.shutdown_event.clear()

        # Step 1: configure write semaphore (before any worker can consume)
        worker_count = settings.ingestion_worker_count
        if worker_count > 1:
            self._write_semaphore = asyncio.Semaphore(1)
            self.processor._write_semaphore = self._write_semaphore
        else:
            self._write_semaphore = None
            self.processor._write_semaphore = None

        # Step 2: spawn workers BEFORE recovery so consumers exist when
        # the recovery sweep enqueues stranded rows. Workers will be idle
        # but available to consume recovered items.
        self._worker_tasks = []
        for i in range(worker_count):
            task = asyncio.create_task(self._worker_loop(), name=f"worker-{i}")
            self._worker_tasks.append(task)
        self._enrichment_worker_task = asyncio.create_task(
            self._enrichment_worker_loop(), name="enrichment-worker"
        )
        self._reindex_worker_task = asyncio.create_task(self._reindex_worker_loop(), name="reindex-worker")

        # Step 3: NOW run recovery. Workers are ready to consume.
        await self._recover_stranded_pending_rows()
        await self._recover_stranded_enrichment_rows()
        await self.retry_pending_vector_deletes()
        # Re-run the vector-delete reconciliation hourly so orphaned chunks
        # from a failed delete are cleaned without waiting for a restart.
        self._vector_delete_sweep_task = asyncio.create_task(
            self._vector_delete_sweep_loop(), name="vector-delete-sweep"
        )

        logger.info(f"Background processor started with {worker_count} worker(s)")

    async def _vector_delete_sweep_loop(self, interval_seconds: float = 3600.0) -> None:
        """Hourly retry loop for pending vector deletes (startup pass runs first)."""
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                await self.retry_pending_vector_deletes()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — sweep must never kill the loop
                logger.exception("Periodic vector-delete sweep failed")

    async def _recover_stranded_pending_rows(self) -> None:
        """Re-enqueue any `files` rows left at status='pending' from a prior process.

        Detection: status='pending' AND phase='queued'. The async upload
        route is the only writer of this exact combination; legacy scan/
        email paths leave phase NULL. We deliberately do NOT touch rows
        in any other phase (parsing/embedding/...) — those imply a worker
        was actively in the middle of processing them and the operator
        should investigate manually.

        Best-effort: pool absence (e.g. tests) is silently skipped.
        """
        if self.processor is None or self.processor.pool is None:
            return

        # SELECT 1: Pending rows
        try:
            with self.processor.pool.connection() as conn:
                cursor = conn.execute(
                    """
                    SELECT id, file_path, vault_id, source
                    FROM files
                    WHERE status = 'pending' AND phase = 'queued'
                    """,
                )
                stranded = cursor.fetchall()
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Stranded-row recovery sweep failed at SELECT: %s", e)
            stranded = []

        # SELECT 2: Processing rows
        processing_stranded = []
        try:
            with self.processor.pool.connection() as conn:
                processing_cursor = conn.execute(
                    """
                    SELECT id, file_path, vault_id, source
                    FROM files
                    WHERE status = 'processing'
                      AND (phase_started_at IS NOT NULL
                           AND phase_started_at < datetime('now', ?))
                    """,
                    (f"-{STRANDED_PROCESSING_TIMEOUT_MINUTES} minutes",),
                )
                processing_stranded = processing_cursor.fetchall()
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Processing-row recovery sweep failed at SELECT: %s", e)

        # Skip if no stranded rows to recover
        if not stranded and not processing_stranded:
            return

        logger.info(
            "Recovering %d stranded async-upload row(s) left at status=pending/phase=queued",
            len(stranded),
        )
        for row in stranded:
            try:
                row_id = row["id"] if hasattr(row, "keys") else row[0]
                file_path = row["file_path"] if hasattr(row, "keys") else row[1]
                vault_id = row["vault_id"] if hasattr(row, "keys") else row[2]
                source = (
                    (row["source"] if hasattr(row, "keys") else row[3]) or "upload"
                )
                # If the saved file no longer exists on disk, mark error
                # rather than re-enqueueing — the worker would just fail.
                from pathlib import Path as _Path

                if not _Path(file_path).exists():
                    try:
                        with self.processor.pool.connection() as conn:
                            conn.execute(
                                "UPDATE files SET status='error', "
                                "error_message='Upload file missing after process restart', "
                                "phase='error' WHERE id = ?",
                                (row_id,),
                            )
                            conn.commit()
                    except Exception:  # pragma: no cover - defensive
                        pass
                    continue
                await self.enqueue(
                    file_path=file_path,
                    source=source,
                    vault_id=int(vault_id),
                    file_id=int(row_id),
                )
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("Failed to re-enqueue stranded row id=%s: %s", row, e)

        # Recover stuck processing rows
        for row in processing_stranded:
            try:
                row_id = row["id"] if hasattr(row, "keys") else row[0]
                file_path = row["file_path"] if hasattr(row, "keys") else row[1]
                vault_id = row["vault_id"] if hasattr(row, "keys") else row[2]
                source = (
                    (row["source"] if hasattr(row, "keys") else row[3]) or "upload"
                )

                from pathlib import Path as _Path
                if not _Path(file_path).exists():
                    with self.processor.pool.connection() as conn:
                        conn.execute(
                            "UPDATE files SET status='error', "
                            "error_message='File missing after process restart', "
                            "phase='error' WHERE id = ?",
                            (row_id,),
                        )
                        conn.commit()
                    continue

                with self.processor.pool.connection() as conn:
                    conn.execute(
                        "UPDATE files SET status='pending', phase='queued', "
                        "error_message=NULL WHERE id = ?",
                        (row_id,),
                    )
                    conn.commit()

                logger.info(
                    "Recovered stuck processing row id=%s: status=pending, phase=queued",
                    row_id,
                )
                # Re-enqueue for processing
                await self.enqueue(
                    file_path=file_path,
                    source=source,
                    vault_id=int(vault_id),
                    file_id=int(row_id),
                )
            except Exception as e:
                logger.warning("Failed to recover processing row %s: %s", row, e)

        if processing_stranded:
            logger.info(
                "Recovered %d stuck processing row(s) older than %d minutes",
                len(processing_stranded),
                STRANDED_PROCESSING_TIMEOUT_MINUTES,
            )

    async def retry_pending_vector_deletes(self) -> None:
        """Retry vector-store chunk deletes recorded by failed document deletes.

        Document deletes record a `vector_delete_pending` row when removing the
        file's LanceDB chunks fails (Issue #219) — without this sweep those
        orphaned chunks would stay searchable forever. On success the pending
        row is removed; on failure `attempts` is incremented and the row is
        left for the next sweep. Rows past MAX_VECTOR_DELETE_ATTEMPTS are kept
        and logged for operator visibility.

        Best-effort: pool/vector-store absence (e.g. tests) is silently skipped.
        VectorStore.delete_by_file serializes writes via its own write lock.
        """
        if self.processor is None or self.processor.pool is None:
            return
        vector_store = self.processor.vector_store
        if vector_store is None:
            return

        try:
            with self.processor.pool.connection() as conn:
                cursor = conn.execute(
                    "SELECT id, file_id, attempts FROM vector_delete_pending"
                )
                pending = cursor.fetchall()
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Pending vector-delete sweep failed at SELECT: %s", e)
            return

        if not pending:
            return

        logger.info("Retrying %d pending vector delete(s)", len(pending))
        for row in pending:
            row_id = row["id"] if hasattr(row, "keys") else row[0]
            file_id = row["file_id"] if hasattr(row, "keys") else row[1]
            attempts = row["attempts"] if hasattr(row, "keys") else row[2]

            if attempts >= MAX_VECTOR_DELETE_ATTEMPTS:
                logger.error(
                    "Pending vector delete for file_id=%s exceeded %d attempts; "
                    "leaving row for operator review",
                    file_id,
                    MAX_VECTOR_DELETE_ATTEMPTS,
                )
                continue

            try:
                await vector_store.delete_by_file(str(file_id))
            except Exception as e:
                logger.warning(
                    "Retry of vector delete for file_id=%s failed: %s", file_id, e
                )
                try:
                    with self.processor.pool.connection() as conn:
                        conn.execute(
                            "UPDATE vector_delete_pending "
                            "SET attempts = attempts + 1 WHERE id = ?",
                            (row_id,),
                        )
                        conn.commit()
                except Exception:  # pragma: no cover - defensive
                    pass
                continue

            try:
                with self.processor.pool.connection() as conn:
                    conn.execute(
                        "DELETE FROM vector_delete_pending WHERE id = ?", (row_id,)
                    )
                    conn.commit()
                logger.info(
                    "Completed deferred vector delete for file_id=%s", file_id
                )
            except Exception as e:  # pragma: no cover - defensive
                logger.warning(
                    "Failed to clear pending vector delete row id=%s: %s", row_id, e
                )

    async def _recover_stranded_enrichment_rows(self) -> None:
        """Mark interrupted post-index enrichment as failed without touching indexed files."""
        if self.processor is None or self.processor.pool is None:
            return
        try:
            with self.processor.pool.connection() as conn:
                cursor = conn.execute(
                    """
                    UPDATE files
                    SET enrichment_status = 'error',
                        enrichment_error = 'Enrichment interrupted or queued before completion; base index remains available',
                        enrichment_updated_at = ?
                    WHERE status = 'indexed'
                      AND enrichment_status IN ('pending', 'processing')
                    """,
                    (datetime.now(UTC).isoformat(),),
                )
                recovered = cursor.rowcount
                conn.commit()
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Stranded enrichment recovery sweep failed: %s", e)
            return

        if recovered:
            logger.info("Recovered %d stranded enrichment row(s)", recovered)

    async def stop(self, timeout: float = 60.0) -> None:
        """
        Stop the background processor gracefully.

        Signals the worker to shut down and waits for it to complete.
        Pending queue items ARE processed before shutdown (up to timeout).

        Args:
            timeout: Maximum time to wait for graceful shutdown (default: 60 seconds)
        """
        if not self._running:
            logger.warning("Background processor is not running")
            return

        logger.info("Stopping background processor...")

        # Phase 1: let ingestion workers finish first. They may enqueue
        # post-index enrichment, so do not signal the enrichment worker to exit
        # until the ingestion queue has drained.
        queue_drained = True
        try:
            await asyncio.wait_for(self.queue.join(), timeout=timeout)
        except asyncio.TimeoutError:
            queue_drained = False
            logger.warning("Queue did not drain within timeout, force-cancelling workers...")

        if queue_drained:
            try:
                await asyncio.wait_for(self.enrichment_queue.join(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning("Enrichment queue did not drain within timeout")
        self.shutdown_event.set()

        # Phase 2: Cancel remaining workers
        if self._worker_tasks:
            for task in self._worker_tasks:
                task.cancel()
            await asyncio.gather(*self._worker_tasks, return_exceptions=True)
        if self._enrichment_worker_task:
            self._enrichment_worker_task.cancel()
            await asyncio.gather(self._enrichment_worker_task, return_exceptions=True)
        if self._vector_delete_sweep_task:
            self._vector_delete_sweep_task.cancel()
            await asyncio.gather(self._vector_delete_sweep_task, return_exceptions=True)
        if self._reindex_worker_task:
            self._reindex_worker_task.cancel()
            await asyncio.gather(self._reindex_worker_task, return_exceptions=True)

        # Phase 3: Flush optimize on VectorStore if available
        if hasattr(self.processor, 'vector_store') and self.processor.vector_store is not None:
            try:
                from app.config import settings as _settings
                if _settings.optimize_on_shutdown:
                    await self.processor.vector_store.flush_optimize()
            except Exception as e:
                logger.warning("Failed to flush vector store on shutdown: %s", e)

        self._running = False
        logger.info("Background processor stopped")

    async def enqueue(
        self,
        file_path: str,
        vault_id: int,
        source: str = 'upload',
        email_subject: Optional[str] = None,
        email_sender: Optional[str] = None,
        file_id: Optional[int] = None,
    ) -> None:
        """
        Add a file to the processing queue.

        Args:
            file_path: Path to the file to process
            vault_id: Vault to associate the file with
            source: Source of the file ('upload', 'scan', 'email')
            email_subject: Subject line for email-sourced files
            email_sender: Sender address for email-sourced files
            file_id: When provided, the worker calls
                ``DocumentProcessor.process_existing_file`` against this row
                instead of ``process_file``. Used by the async upload route
                so duplicate detection and row insertion do not run twice.

        Note:
            If the processor is not running, the item will still be queued
            and processed when start() is called.
        """
        if self.maintenance_service:
            flag = self.maintenance_service.get_flag()
            if flag and flag.enabled:
                raise DocumentProcessingError("Maintenance mode prevents enqueueing")
        task = TaskItem(
            file_path=file_path,
            attempt=1,
            source=source,
            email_subject=email_subject,
            email_sender=email_sender,
            vault_id=vault_id,
            file_id=file_id,
        )
        await self.queue.put(task)
        logger.debug(f"Enqueued file: {file_path} (file_id={file_id})")

    async def enqueue_enrichment(self, item: EnrichmentTaskItem) -> None:
        """Add a post-index enrichment job to the enrichment queue."""
        await self.enrichment_queue.put(item)
        logger.debug("Enqueued enrichment for file_id=%s", item.file_id)

    async def enqueue_reindex(self, job_id: int) -> None:
        """Add a reindex job to the reindex queue."""
        await self.reindex_queue.put(ReindexTaskItem(job_id=job_id))
        logger.debug("Enqueued reindex job: %s", job_id)

    async def _worker_loop(self) -> None:
        """
        Main worker loop that processes items from the queue.

        Continuously processes tasks until shutdown_event is set AND queue is empty.
        Ensures all pending tasks are processed before shutdown.
        Handles retries with exponential backoff on failure.
        """
        while True:
            # Check if we should shutdown: shutdown_event is set AND queue is empty
            if self.shutdown_event.is_set() and self.queue.empty():
                break

            try:
                # Wait for task with timeout to check shutdown periodically
                task = await asyncio.wait_for(
                    self.queue.get(),
                    timeout=0.5
                )
            except asyncio.TimeoutError:
                continue

            if task is None:
                continue

            await self._process_task_wrapper(task)

    async def _enrichment_worker_loop(self) -> None:
        """Process optional enrichment after base indexing completes."""
        while True:
            if self.shutdown_event.is_set() and self.enrichment_queue.empty():
                break
            try:
                item = await asyncio.wait_for(self.enrichment_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            try:
                await self.processor.run_enrichment_job(
                    file_id=item.file_id,
                    file_path=item.file_path,
                    vault_id=item.vault_id,
                    file_hash=item.file_hash,
                    chunks=item.chunks,
                    document_text=item.document_text,
                )
            except Exception:
                logger.exception("Enrichment job failed for file_id=%s", item.file_id)
                if self.shutdown_event.is_set():
                    logger.warning(
                        "Enrichment failed for file_id=%s during shutdown, not requeuing",
                        item.file_id,
                    )
                    continue
                if item.attempt < self.max_retries:
                    delay = self.retry_delay * (2 ** item.attempt)
                    logger.warning(
                        "Enrichment failed for file_id=%s, retrying in %ss "
                        "(attempt %s/%s)",
                        item.file_id,
                        delay,
                        item.attempt + 1,
                        self.max_retries,
                    )
                    await asyncio.sleep(delay)
                    # Re-check shutdown after backoff; stop() may have been
                    # called while we were sleeping.
                    if self.shutdown_event.is_set():
                        logger.warning(
                            "Enrichment retry for file_id=%s skipped after backoff "
                            "because shutdown is in progress",
                            item.file_id,
                        )
                        continue
                    new_item = EnrichmentTaskItem(
                        file_id=item.file_id,
                        file_path=item.file_path,
                        vault_id=item.vault_id,
                        file_hash=item.file_hash,
                        chunks=item.chunks,
                        document_text=item.document_text,
                        attempt=item.attempt + 1,
                    )
                    await self.enrichment_queue.put(new_item)
                else:
                    logger.error(
                        "Enrichment permanently failed for file_id=%s after %s attempts",
                        item.file_id,
                        self.max_retries,
                    )
            finally:
                self.enrichment_queue.task_done()

    async def _reindex_worker_loop(self) -> None:
        """Process reindex jobs from the reindex queue."""
        while True:
            if self.shutdown_event.is_set() and self.reindex_queue.empty():
                break
            try:
                item = await asyncio.wait_for(self.reindex_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            try:
                await self._process_reindex_job(item.job_id)
            finally:
                self.reindex_queue.task_done()

    async def _process_reindex_job(self, job_id: int) -> None:
        """Process a reindex job: re-embed all stored documents with the current model.

        Steps:
        1. Mark job as 'running'.
        2. Read vault_id / input_json from the job row.
        3. Select files with status IN ('indexed', 'error'), optionally filtered by vault_id.
        4. Group by vault_id and iterate in sorted order.
        5. For each file call process_existing_file (current model), counting successes/failures.
        6. On any file failure: continue to next file (partial-failure strategy).
        7. Mark job 'completed' on full success, 'failed' if any file failed.
        8. Leave settings_kv and vector_store._ready unchanged (Task 1.7).
        """
        logger.info("Starting reindex job %d", job_id)
        if self.processor is None or self.processor.pool is None:
            logger.warning("Processor or pool unavailable for reindex job %d; skipping.", job_id)
            return

        try:
            # Step 1: Mark as running (only pending jobs can transition)
            with self.processor.pool.connection() as conn:
                cursor = conn.execute(
                    """
                    UPDATE document_reindex_jobs
                    SET status = 'running', started_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND status = 'pending'
                    """,
                    (job_id,),
                )
                conn.commit()
                if cursor.rowcount == 0:
                    logger.warning("Reindex job %d not found or not in pending state; skipping.", job_id)
                    return
            logger.info("Reindex job %d status updated to running.", job_id)

            # Step 2: Read vault_id and input_json from the job row
            with self.processor.pool.connection() as conn:
                row = conn.execute(
                    "SELECT vault_id, input_json FROM document_reindex_jobs WHERE id = ? AND status = 'running'",
                    (job_id,),
                ).fetchone()
                if not row:
                    logger.warning("Reindex job %d not found or not running; skipping.", job_id)
                    return
                vault_id = row["vault_id"] if hasattr(row, "keys") else row[0]

            # Step 3: Select files to reindex
            with self.processor.pool.connection() as conn:
                if vault_id is not None:
                    rows = conn.execute(
                        "SELECT id, file_path, vault_id FROM files WHERE vault_id = ? AND status IN ('indexed', 'error')",
                        (vault_id,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT id, file_path, vault_id FROM files WHERE status IN ('indexed', 'error')",
                    ).fetchall()

            # Step 4: Group by vault_id
            vaults_files: dict[int, list[tuple[int, str, int]]] = {}
            for row in rows:
                fid = row["id"] if hasattr(row, "keys") else row[0]
                fpath = row["file_path"] if hasattr(row, "keys") else row[1]
                vid = row["vault_id"] if hasattr(row, "keys") else row[2]
                vaults_files.setdefault(vid, []).append((fid, fpath, vid))

            # Step 5: Initialize counters
            total_files = 0
            processed_files = 0
            failed_files = 0
            failed_details: list[str] = []

            for vault_id_sorted in sorted(vaults_files.keys()):
                file_list = vaults_files[vault_id_sorted]
                for file_id, file_path, vault_id_file in file_list:
                    total_files += 1
                    logger.info("Re-embedding file_id=%d in vault_id=%d", file_id, vault_id_file)
                    try:
                        await self.processor.process_existing_file(file_id, file_path, vault_id_file)
                        processed_files += 1
                    except (
                        DocumentProcessingError,
                        FileNotFoundError,
                        OSError,
                        RuntimeError,
                        Exception,
                    ) as exc:
                        logger.exception(
                            "Re-embed failed for file_id=%d in vault_id=%d: %s",
                            file_id,
                            vault_id_file,
                            exc,
                        )
                        failed_files += 1
                        failed_details.append(f"file_id={file_id}: {exc}")
                        continue

            # Step 6: Determine final job status and result
            if total_files == 0:
                result = {"processed": 0, "failed": 0}
                with self.processor.pool.connection() as conn:
                    conn.execute(
                        "UPDATE document_reindex_jobs SET status = 'completed', result_json = ?, completed_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (json.dumps(result), job_id),
                    )
                    conn.commit()
                logger.info("Reindex job %d completed (no files to reindex).", job_id)
            elif failed_files > 0:
                result = {"processed": processed_files, "failed": failed_files, "details": failed_details[:10]}
                with self.processor.pool.connection() as conn:
                    conn.execute(
                        "UPDATE document_reindex_jobs SET status = 'failed', error = ?, completed_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (json.dumps(result), job_id),
                    )
                    conn.commit()
                logger.error(
                    "Reindex job %d failed for %d/%d files; stored model identity left unchanged.",
                    job_id,
                    failed_files,
                    total_files,
                )
            else:
                # Step 7a: Update stored model identity and readiness BEFORE marking completed.
                # If this fails, mark the job as failed so the app is not left in a mismatched
                # state on restart (metadata not persisted but job reported as completed).
                try:
                    vector_store = self.processor.vector_store
                    if vector_store is not None:
                        await vector_store.record_embedding_metadata(settings.embedding_dim, raise_on_error=True)
                        await vector_store.mark_ready(True)
                        logger.info(
                            "Vector store model identity updated and marked ready after reindex job %d.",
                            job_id,
                        )
                    else:
                        logger.warning("Vector store unavailable; cannot update model identity after reindex job %d.", job_id)
                except Exception as exc:
                    logger.exception("Failed to update vector store model identity after reindex job %d", job_id)
                    try:
                        with self.processor.pool.connection() as conn:
                            conn.execute(
                                "UPDATE document_reindex_jobs SET status = 'failed', completed_at = ?, error = ? WHERE id = ?",
                                (datetime.now(UTC).isoformat(), str(exc), job_id),
                            )
                            conn.commit()
                    except Exception:
                        logger.warning("Failed to update reindex job %d status to failed", job_id)
                    return

                # Step 7b: Only mark completed after metadata and readiness updates succeeded.
                result = {"processed": processed_files, "failed": 0}
                with self.processor.pool.connection() as conn:
                    conn.execute(
                        "UPDATE document_reindex_jobs SET status = 'completed', result_json = ?, completed_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (json.dumps(result), job_id),
                    )
                    conn.commit()
                logger.info(
                    "Reindex job %d completed successfully for %d files.",
                    job_id,
                    processed_files,
                )

        except Exception as exc:
            logger.exception("Error processing reindex job %d", job_id)
            try:
                with self.processor.pool.connection() as conn:
                    conn.execute(
                        "UPDATE document_reindex_jobs SET status = 'failed', completed_at = ?, error = ? WHERE id = ?",
                        (datetime.now(UTC).isoformat(), str(exc), job_id),
                    )
                    conn.commit()
            except Exception:
                logger.warning("Failed to update reindex job %d status to failed", job_id)

    async def _process_task_wrapper(self, task: TaskItem) -> None:
        """
        Wrapper for _process_task that ensures task_done() is always called.

        This wrapper guarantees queue.task_done() is called even if _process_task
        raises an exception or continues early.
        """
        try:
            await self._process_task(task)
        finally:
            self.queue.task_done()

    async def _process_task(self, task: TaskItem) -> None:
        """
        Process a single task with retry logic.

        Args:
            task: TaskItem containing file path, attempt count, and optional email metadata

        On failure, requeues the task with incremented attempt count
        and exponential backoff delay if retries remain.
        """
        logger.info(
            f"Processing file: {task.file_path} (attempt {task.attempt}, file_id={task.file_id})"
        )

        try:
            if task.file_id is not None:
                # Async upload path: the row already exists with status='pending'
                # / phase='queued' and the duplicate check has already passed.
                result = await self.processor.process_existing_file(
                    file_id=task.file_id,
                    file_path=task.file_path,
                    vault_id=task.vault_id,
                )
            else:
                # Legacy path (scan/email): processor handles dup check + insert.
                result = await self.processor.process_file(
                    task.file_path,
                    source=task.source,
                    email_subject=task.email_subject,
                    email_sender=task.email_sender,
                    vault_id=task.vault_id,
                )
            if self.processor.should_enqueue_enrichment(result.chunks, result.vault_id, result.file_id):
                self.processor.set_enrichment_status(result.file_id, "pending")
                await self.enqueue_enrichment(
                    EnrichmentTaskItem(
                        file_id=result.file_id,
                        file_path=result.file_path,
                        vault_id=result.vault_id,
                        file_hash=result.file_hash,
                        chunks=result.chunks,
                        document_text=result.document_text,
                        attempt=0,
                    )
                )
            logger.info(f"Successfully processed: {task.file_path}")

        except DocumentProcessingError as e:
            logger.error(f"Processing error for {task.file_path}: {e}")
            await self._handle_failure(task, str(e))

        except Exception as e:
            logger.error(f"Unexpected error processing {task.file_path}: {e}")
            await self._handle_failure(task, str(e))

    async def _handle_failure(self, task: TaskItem, error_message: str) -> None:
        """
        Handle task failure with retry logic.

        Args:
            task: The failed task
            error_message: Error message from the failure

        Requeues the task with incremented attempt count if retries remain.
        When retries are exhausted (permanent failure) and ``task.file_id`` is set,
        writes ``status='error'``, ``error_message``, and ``phase='error'`` to the
        corresponding ``files`` row so the file is not left stuck in 'processing'.
        """
        # Don't requeue if shutdown is in progress
        if self.shutdown_event.is_set():
            logger.warning(
                f"Task failed for {task.file_path} during shutdown, not requeuing"
            )
            return

        if task.attempt < self.max_retries:
            # Calculate exponential backoff delay
            delay = self.retry_delay * (2 ** (task.attempt - 1))
            logger.warning(
                f"Task failed for {task.file_path}, "
                f"retrying in {delay}s (attempt {task.attempt + 1}/{self.max_retries})"
            )

            # Wait before requeuing
            await asyncio.sleep(delay)

            # Requeue with incremented attempt count, preserving metadata
            new_task = TaskItem(
                file_path=task.file_path,
                attempt=task.attempt + 1,
                source=task.source,
                email_subject=task.email_subject,
                email_sender=task.email_sender,
                vault_id=task.vault_id,
                file_id=task.file_id,
            )
            await self.queue.put(new_task)
        else:
            # Mark file as error in database so it doesn't stay in 'processing'
            if task.file_id is not None and self.processor.pool is not None:
                try:
                    with self.processor.pool.connection() as conn:
                        conn.execute(
                            "UPDATE files SET status='error', "
                            "error_message=?, phase='error' WHERE id = ?",
                            (error_message[:500], task.file_id),
                        )
                        conn.commit()
                except Exception:
                    logger.warning(
                        "Failed to update file status to 'error' "
                        "for file_id=%s", task.file_id,
                    )
            logger.error(
                f"Task permanently failed for {task.file_path} "
                f"after {self.max_retries} attempts: {error_message}"
            )

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def queue_size(self) -> int:
        return self.queue.qsize()
