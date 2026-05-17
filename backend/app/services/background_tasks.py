"""
Background task processor for document ingestion.

Provides BackgroundProcessor class that manages an asyncio queue for processing
documents with retry logic and graceful shutdown.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import List, Optional

from ..models.database import SQLiteConnectionPool
from .document_processor import DocumentProcessingError, DocumentProcessor
from .embeddings import EmbeddingService
from .llm_client import LLMClient
from .maintenance import MaintenanceService
from .vector_store import VectorStore

from app.config import settings

logger = logging.getLogger(__name__)

# Timeout for processing rows during startup recovery sweep.
# If a row has been in status='processing' for longer than this,
# it will be reset to 'pending' for re-processing.
STRANDED_PROCESSING_TIMEOUT_MINUTES = 30

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


class BackgroundProcessor:
    """
    Background task processor using asyncio.Queue for document ingestion.

    Manages a worker loop that processes files using DocumentProcessor with
    retry logic (max 3 attempts). Failed tasks are requeued with exponential
    backoff delay. Provides graceful shutdown via asyncio.Event.

    Attributes:
        max_retries: Maximum number of retry attempts per task
        retry_delay: Base delay in seconds between retries (doubles each attempt)
        queue: asyncio.Queue holding TaskItem objects
        shutdown_event: asyncio.Event for graceful shutdown
        processor: DocumentProcessor instance for file processing
        _worker_tasks: List of worker task references
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
        self.queue: asyncio.Queue[TaskItem] = asyncio.Queue()
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
        self._running = False
        self.maintenance_service = maintenance_service
        self._write_semaphore: Optional[asyncio.Semaphore] = None

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
        """
        if self._running:
            logger.warning("Background processor is already running")
            return

        self._running = True
        self.shutdown_event.clear()
        await self._recover_stranded_pending_rows()
        # Spawn N workers based on configuration
        self._worker_tasks = []
        for i in range(settings.ingestion_worker_count):
            task = asyncio.create_task(self._worker_loop(), name=f"worker-{i}")
            self._worker_tasks.append(task)
        logger.info(f"Background processor started with {settings.ingestion_worker_count} worker(s)")

        # Create write semaphore for SQLite contention when running multiple workers
        if settings.ingestion_worker_count > 1:
            self._write_semaphore = asyncio.Semaphore(settings.ingestion_worker_count)
            self.processor._write_semaphore = self._write_semaphore
        else:
            self._write_semaphore = None

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

                # NEW: recover processing rows stuck for >30 minutes
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
            logger.warning("Stranded-row recovery sweep failed at SELECT: %s", e)
            return

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
                    source="upload",
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
        self.shutdown_event.set()

        # Phase 1: Wait for in-flight tasks to complete (with timeout)
        try:
            await asyncio.wait_for(self.queue.join(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("Queue did not drain within timeout, force-cancelling workers...")

        # Phase 2: Cancel remaining workers
        if self._worker_tasks:
            for task in self._worker_tasks:
                task.cancel()
            await asyncio.gather(*self._worker_tasks, return_exceptions=True)

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
                await self.processor.process_existing_file(
                    file_id=task.file_id,
                    file_path=task.file_path,
                    vault_id=task.vault_id,
                )
            else:
                # Legacy path (scan/email): processor handles dup check + insert.
                await self.processor.process_file(
                    task.file_path,
                    source=task.source,
                    email_subject=task.email_subject,
                    email_sender=task.email_sender,
                    vault_id=task.vault_id,
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

        Requeues the task with incremented attempt count if retries remain,
        otherwise logs the permanent failure.
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
