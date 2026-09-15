"""
Background task processor for document ingestion.

Provides BackgroundProcessor class that manages an asyncio queue for processing
documents with retry logic and graceful shutdown.
"""

import asyncio
import inspect
import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import List, Optional

from app.config import settings
from app.services.admission import (
    AdmissionClass,
    AdmissionRejected,
    get_admission_controller,
)

from ..models.database import SQLiteConnectionPool
from .document_processor import DocumentProcessingError, DocumentProcessor
from .embeddings import EmbeddingService
from .job_lease import JobLease, ensure_jobs_schema
from .llm_client import LLMClient
from .maintenance import MaintenanceService
from .multimodal_enrichment import ArtifactEnrichmentService
from .vector_store import VectorStore, VectorStoreError

logger = logging.getLogger(__name__)

# Queue name for the shared jobs-lease table's ingestion rows (issue #559).
INGESTION_QUEUE = "ingestion"

# Timeout for processing rows during the PERIODIC stranded-row rescan.
# If a row has been in status='processing' for longer than this, the periodic
# sweep (issue #513 W25) resets it to 'pending' for re-processing. The startup
# sweep deliberately bypasses this timeout for rows that already reached a
# post-parse stage (at startup, single-process, such rows are orphans by
# definition — see _recover_stranded_pending_rows); rows still in a parse
# stage remain age-gated by this constant even at startup so a long legitimate
# parse is never stolen by restart alone.
STRANDED_PROCESSING_TIMEOUT_MINUTES = 30

# Per-row cap on the startup stranded-row re-enqueue (PRR-011). Recovery is
# best-effort: if the bounded queue is saturated, bow out with a warning and
# leave the row for a later sweep instead of blocking startup on the backlog.
STRANDED_REENQUEUE_TIMEOUT_SECONDS = 30.0

# Keep a large missing-file backlog from running a synchronous recovery loop
# without returning control to the event loop (issue #591 C4).
RECOVERY_COOPERATIVE_YIELD_EVERY = 32

# Retry cap for pending vector-store deletes (Issue #219). Rows that exceed
# this are left in place and logged for operator visibility — we never delete
# a pending record without confirming the chunks are actually gone.
MAX_VECTOR_DELETE_ATTEMPTS = 10

# Bound on the deferred-retry backlog (issue #513 W11 / RC-6). Workers hand
# retryable failures to a dedicated scheduler instead of sleeping inline and
# re-entering the bounded queue they alone drain; the backlog itself must stay
# bounded so a failure storm cannot grow unbounded memory. When full, the
# failure escalates to the existing permanent-error path.
RETRY_BACKLOG_MAX_SIZE = 1000

# File pipeline phases that are still inside the parse stage. A 'processing'
# row in one of these phases may belong to a legitimately long parse, so the
# startup sweep (like the periodic rescan) only recovers them by age; every
# later phase (embedding/writing_index/...) has durable stage checkpoints and
# is unconditionally recovered at startup (issue #513 W25 / AC28).
PARSE_STAGE_PHASES = ("parsing", "extracting_text", "chunking")

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
    multimodal_service: Optional[ArtifactEnrichmentService] = None,
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
        multimodal_service: Optional atom-scoped multimodal enrichment service.

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
            multimodal_service=multimodal_service,
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
        file_id: When set, the worker calls process_existing_file on this row
        file_hash: Content hash computed by the enqueueing route (issue #513
            W8 — single content hash). When None the processor computes it.
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
    # Pre-computed content hash from the upload route (issue #513 W8 / RC-21d:
    # the route already hashed the bytes; re-hashing in the worker duplicated
    # the work and could diverge from what the route persisted).
    file_hash: Optional[str] = None
    # Set by BackgroundProcessor.cancel_pending_jobs on queued, not-yet-started
    # items whose attributes match the caller's cancellation match (e.g.
    # file_id). The ingestion worker checks this flag at the top of item
    # handling and skips cancelled items entirely (issue #516 / DRAFT-023) —
    # the enqueueing side has already compensated by deleting the files row
    # and the bytes, so processing would only fail against missing state. A
    # fresh enqueue of the same file creates a new TaskItem with the flag
    # unset, so cancellation never leaks into later legitimate work.
    cancelled: bool = False
    # A recovery enqueue owns a process-local reservation until its task (and
    # any deferred retry) settles. Normal route tasks that were queued during
    # the recovery SELECT are skipped while this claim is active.
    recovery_claim: bool = False
    # When set, this item is backed by a jobs-lease row (issue #559): the
    # durable claim ledger lives in the DB, the heartbeat renews it while the
    # worker runs, and the worker settles the row (complete/requeue/fail)
    # when processing ends. None = legacy in-memory-only item.
    job_id: Optional[int] = None
    # Worker identity that claimed the backed row (fencing half; the row's
    # current worker_id is the other half).
    worker_id: Optional[str] = None


@dataclass
class _RetryTicket:
    """One deferred retry handed to the retry scheduler (issue #513 W11 / RC-6).

    Workers never re-enter their own bounded work queue from inside the
    consumer coroutine; they park the due time plus the target queue here and
    the dedicated scheduler task delivers the item once the backoff elapsed.
    """

    due_at: float
    queue: "asyncio.Queue"
    item: object


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


@dataclass
class AtomEnrichmentTaskItem:
    """Atom-scoped multimodal enrichment task for an already indexed file.

    Attributes:
        file_id: Database ID of the file.
        vault_id: Vault the file belongs to.
        generation_hash: The source generation whose atoms to enrich.
        file_hash: SHA-256 of the file content.
        document_title: Title used as top-level prompt context.
        attempt: Current retry attempt (0 = first).
    """

    file_id: int
    vault_id: int
    generation_hash: str
    file_hash: str
    document_title: str
    attempt: int = 0


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
        multimodal_service: Optional[ArtifactEnrichmentService] = None,
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
            multimodal_service: Optional atom-scoped multimodal enrichment service.
        """
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.queue: asyncio.Queue[TaskItem] = asyncio.Queue(maxsize=settings.ingestion_queue_max_size)
        self.enrichment_queue: asyncio.Queue[EnrichmentTaskItem] = asyncio.Queue(maxsize=settings.ingestion_queue_max_size)
        self.atom_enrichment_queue: asyncio.Queue[AtomEnrichmentTaskItem] = asyncio.Queue(maxsize=settings.ingestion_queue_max_size)
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
        self.multimodal_service = multimodal_service
        self._worker_tasks: List[asyncio.Task] = []
        self._enrichment_worker_task: Optional[asyncio.Task] = None
        self._atom_enrichment_worker_task: Optional[asyncio.Task] = None
        self._vector_delete_sweep_task: Optional[asyncio.Task] = None
        self._artifact_delete_sweep_task: Optional[asyncio.Task] = None
        # ``asyncio.to_thread`` work cannot be cancelled once the worker
        # thread has started. Keep strong ownership of artifact sweep workers
        # so a cancelled outer recovery task cannot lose track of them. Real
        # SQLite pools use a standalone connection in that worker, so pool
        # shutdown never races a still-running sweep.
        self._artifact_sweep_tasks: set[asyncio.Task] = set()
        self._reindex_worker_task: Optional[asyncio.Task] = None
        # Deferred-retry scheduler (issue #513 W11 / RC-6): workers hand
        # retryable items to this bounded backlog; the dedicated scheduler
        # task re-inserts them into their work queue once the backoff elapses.
        # A producer blocking on a full queue is safe there — the consumer
        # keeps draining — so retry storms can no longer self-deadlock the
        # sole worker of a bounded queue.
        self._retry_backlog: asyncio.Queue[_RetryTicket] = asyncio.Queue(
            maxsize=RETRY_BACKLOG_MAX_SIZE
        )
        self._retry_scheduler_task: Optional[asyncio.Task] = None
        # Periodic stranded-row rescan (issue #513 W25 / FU-007): recovers
        # orphans that appear AFTER startup, honoring the live-job lease below.
        self._orphan_rescan_task: Optional[asyncio.Task] = None
        # Live-job lease: file ids currently held by an ingestion worker. The
        # periodic rescan never steals these rows, so a legitimately long parse
        # is not recovered by age alone while it is still running.
        self._active_file_ids: set = set()
        self._active_file_ids_lock = asyncio.Lock()
        # Recovery reservations close the SELECT -> action window: a route
        # enqueue either wins before recovery claims a row, or is suppressed
        # while the recovery-owned task is queued/running.
        self._recovery_file_ids: set = set()
        # Count queued tasks instead of storing only membership. The upload
        # route and deferred retry path can publish two TaskItems for one file;
        # a set loses ownership when the first item is dequeued.
        self._queued_file_ids: dict[int, int] = {}
        self._reindex_job_ids: set[int] = set()
        self._running = False
        self._starting = False
        self._startup_recovery_task: Optional[asyncio.Task] = None
        # New reindex work waits until startup has classified pre-existing
        # running rows, so detached recovery cannot interrupt a live request.
        self._reindex_start_gate = asyncio.Event()
        self._reindex_start_gate.set()
        self._startup_recovery_cutoff: Optional[str] = None
        self.maintenance_service = maintenance_service
        self._write_semaphore: Optional[asyncio.Semaphore] = None
        # DB-claimed ingestion lease (issue #559 stage 1). Enabled only when
        # the env-only switch is on AND a real pool exists; without a pool the
        # processor falls back to the legacy in-memory transport (tests).
        self._ingest_lease_enabled = (
            bool(getattr(settings, "ingestion_job_lease_enabled", False))
            and pool is not None
        )
        # Migration barrier (Round-2 R2-3): workers and the janitor wait on
        # this before their FIRST claim/reclaim, so the new claim path never
        # serves a row the in-flight migration has not yet created.
        self._jobs_barrier = asyncio.Event()
        self._jobs_migration_task: Optional[asyncio.Task] = None
        self._ingest_janitor_task: Optional[asyncio.Task] = None

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
        if self._running or self._starting:
            logger.warning("Background processor is already running or starting")
            return

        self._starting = True
        self.shutdown_event.clear()
        self._reindex_start_gate.clear()
        # Detached recovery only owns rows that predate this start.
        self._startup_recovery_cutoff = datetime.now(UTC).isoformat()

        created_tasks: List[asyncio.Task] = []

        def create_owned_task(coro, *, name: str) -> asyncio.Task:
            """Create a task and close its coroutine if publication fails."""
            try:
                task = asyncio.create_task(coro, name=name)
            except BaseException:
                coro.close()
                raise
            created_tasks.append(task)
            return task

        try:
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
                task = create_owned_task(self._worker_loop(), name=f"worker-{i}")
                self._worker_tasks.append(task)
            self._enrichment_worker_task = create_owned_task(
                self._enrichment_worker_loop(), name="enrichment-worker"
            )
            if self.multimodal_service is not None:
                self._atom_enrichment_worker_task = create_owned_task(
                    self._atom_enrichment_worker_loop(), name="atom-enrichment-worker"
                )
            self._reindex_worker_task = create_owned_task(
                self._reindex_worker_loop(), name="reindex-worker"
            )
            # Deferred-retry scheduler (issue #513 W11): deliver retry tickets into
            # the bounded work queues once their backoff elapses.
            self._retry_scheduler_task = create_owned_task(
                self._retry_scheduler_loop(), name="retry-scheduler"
            )

            # Publish all periodic tasks before detached startup recovery. Each
            # loop sleeps before its first tick, so publication does not create
            # a startup double-run window.
            self._vector_delete_sweep_task = create_owned_task(
                self._vector_delete_sweep_loop(), name="vector-delete-sweep"
            )
            self._artifact_delete_sweep_task = create_owned_task(
                self._artifact_delete_sweep_loop(), name="artifact-delete-sweep"
            )
            if not getattr(self, "_ingest_lease_enabled", False):
                # Legacy mode only: the hourly rescan re-enqueues stranded
                # rows. In lease mode the janitor owns that settlement
                # (issue #559), so the rescan would double-enqueue.
                self._orphan_rescan_task = create_owned_task(
                    self._orphan_rescan_loop(), name="orphan-rescan"
                )
                self._jobs_migration_task = None
                self._ingest_janitor_task = None
            else:
                # Detached in-flight migration, then the lease janitor. Both
                # gate their first pass on _jobs_barrier (R2-3).
                self._jobs_migration_task = create_owned_task(
                    self._run_jobs_migration(), name="jobs-migration"
                )
                self._ingest_janitor_task = create_owned_task(
                    self._ingest_janitor_loop(), name="ingest-janitor"
                )
            self._startup_recovery_task = create_owned_task(
                self._run_startup_recovery(),
                name="startup-recovery",
            )

            # No await occurs between the guard and successful publication.
            self._running = True
            self._starting = False
            logger.info(f"Background processor started with {worker_count} worker(s)")
        except BaseException:
            for task in created_tasks:
                task.cancel()
            if created_tasks:
                await asyncio.gather(*created_tasks, return_exceptions=True)
            self._worker_tasks = []
            self._enrichment_worker_task = None
            self._atom_enrichment_worker_task = None
            self._reindex_worker_task = None
            self._retry_scheduler_task = None
            self._vector_delete_sweep_task = None
            self._artifact_delete_sweep_task = None
            self._orphan_rescan_task = None
            self._startup_recovery_task = None
            self._jobs_migration_task = None
            self._ingest_janitor_task = None
            self._write_semaphore = None
            self.processor._write_semaphore = None
            self._running = False
            self._starting = False
            raise

    async def _run_startup_recovery(self) -> None:
        """Run startup recovery phases in order without coupling readiness."""
        phases = (
            (
                # Lease mode (issue #559): stranded files rows are synced into
                # jobs rows (no live counterpart -> a claimable row); legacy
                # mode keeps the re-enqueue sweep.
                "stranded pending rows"
                if not getattr(self, "_ingest_lease_enabled", False)
                else "missing ingest job rows",
                (
                    lambda: self._recover_stranded_pending_rows(
                        require_older_than_minutes=None
                    )
                    if not getattr(self, "_ingest_lease_enabled", False)
                    else self._sync_missing_ingest_job_rows_gated()
                ),
            ),
            (
                "interrupted reindex jobs",
                self._recover_interrupted_reindex_jobs,
            ),
            (
                "stranded enrichment rows",
                lambda: self._recover_stranded_enrichment_rows(
                    startup_cutoff=self._startup_recovery_cutoff
                ),
            ),
            (
                "stranded atom enrichment rows",
                lambda: self._recover_stranded_atom_enrichment_rows(
                    startup_cutoff=self._startup_recovery_cutoff
                ),
            ),
            ("pending atom enrichment", self._resume_pending_atom_enrichment),
            ("pending vector deletes", self.retry_pending_vector_deletes),
            ("pending artifact deletes", self.sweep_pending_artifact_deletes),
        )
        try:
            for phase_name, phase in phases:
                try:
                    await phase()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — continue independent recovery phases
                    logger.exception("Startup recovery phase failed: %s", phase_name)
                finally:
                    if phase_name == "interrupted reindex jobs":
                        self._reindex_start_gate.set()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive supervisor guard
            logger.exception("Startup recovery supervisor failed unexpectedly")
            raise
        finally:
            # If cancellation or an unexpected BaseException interrupts an
            # earlier phase, do not leave the reindex worker waiting forever.
            # Ordinary phase failures still follow the named-phase ordering
            # above, so the gate opens early only for an aborted recovery.
            self._reindex_start_gate.set()

    async def _orphan_rescan_loop(self) -> None:
        """Periodically re-run the stranded-row recovery (issue #513 W25).

        Each tick re-enqueues ``pending``/``queued`` rows and
        ``processing`` rows older than STRANDED_PROCESSING_TIMEOUT_MINUTES,
        skipping file ids held by a live worker (the active lease) so a long
        legitimate parse is never stolen. Rows whose file vanished are marked
        error, exactly like the startup sweep.
        """
        while True:
            interval = settings.orphan_rescan_interval_seconds
            # Tolerate a fully-mocked settings object (tests patch the module
            # attribute wholesale); same defensive coercion pattern as the
            # vector-store semaphore helpers.
            if not isinstance(interval, (int, float)):
                interval = 3600.0
            await asyncio.sleep(interval)
            if self.shutdown_event.is_set():
                break
            try:
                await self._recover_stranded_pending_rows()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — rescan must never kill the loop
                logger.exception("Periodic stranded-row rescan failed")

    async def _run_jobs_migration(self) -> None:
        """Detached boot migration for the ingestion lease (issue #559 R2-3).

        Copies every files row that awaits ingestion work but has no live
        (pending/running) jobs counterpart into the shared ``jobs`` table, and
        resets stranded ``processing`` rows to ``pending``/``queued`` so the
        janitor — not the restart — owns their settlement. Idempotent and
        resumable: a boot that dies mid-migration simply re-runs it. Workers
        and the janitor hold on ``_jobs_barrier`` until this completes.
        """
        try:
            if self.processor is None or self.processor.pool is None:
                return
            migrated = await asyncio.to_thread(self._sync_missing_ingest_job_rows)
            if migrated:
                logger.info(
                    "Jobs migration: created %d ingestion job row(s) from "
                    "stranded files rows",
                    migrated,
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — migration must not block startup
            logger.exception("Jobs migration failed; legacy recovery remains")
        finally:
            self._jobs_barrier.set()

    async def _sync_missing_ingest_job_rows_gated(self) -> None:
        """Recovery-phase wrapper: wait out the boot migration barrier, then
        re-run the sync, so it can never race the migration task's INSERT
        (both target the same files rows)."""
        await self._jobs_barrier.wait()
        await asyncio.to_thread(self._sync_missing_ingest_job_rows)

    def _sync_missing_ingest_job_rows(self) -> int:
        """Synchronous core of the boot migration. Returns rows created.

        Phase semantics preserved from the pre-lease recovery: a ``pending``/
        ``queued`` row or a stuck ``processing`` row (single-process: an
        orphan by definition once no live jobs row covers it) becomes a
        claimable ``jobs`` row; the processing row is reset to
        ``pending``/``queued`` so file state matches.
        """
        with self.processor.pool.connection() as conn:
            ensure_jobs_schema(conn)
            # Single explicit transaction: the INSERT and the files-row reset
            # commit together, and a concurrent caller (migration task vs
            # recovery phase) serializes behind BEGIN IMMEDIATE instead of
            # racing two autocommitted statements.
            owned = not conn.in_transaction
            if owned:
                conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                INSERT INTO jobs (queue, payload_json)
                SELECT 'ingestion', json_object(
                    'file_path', f.file_path,
                    'vault_id', f.vault_id,
                    'source', COALESCE(f.source, 'upload'),
                    'file_id', f.id,
                    'file_hash', NULL)
                FROM files f
                WHERE f.status IN ('pending', 'processing')
                  AND (f.phase IS NULL OR f.phase != 'error')
                  AND f.id NOT IN (
                    SELECT CAST(json_extract(j.payload_json, '$.file_id') AS INTEGER)
                    FROM jobs j
                    WHERE j.queue = 'ingestion'
                      AND j.status IN ('pending', 'running')
                      AND json_extract(j.payload_json, '$.file_id') IS NOT NULL
                  )
                """
            )
            created = cursor.rowcount
            # Files rows that are still 'processing' but now own a fresh
            # pending jobs row: reset them so the visible state matches.
            conn.execute(
                """
                UPDATE files SET status = 'pending', phase = 'queued',
                    error_message = NULL
                WHERE status = 'processing'
                  AND id IN (
                    SELECT CAST(json_extract(j.payload_json, '$.file_id') AS INTEGER)
                    FROM jobs j
                    WHERE j.queue = 'ingestion'
                      AND j.status = 'pending'
                      AND json_extract(j.payload_json, '$.file_id') IS NOT NULL
                  )
                """
            )
            if owned:
                conn.commit()
        return created if created and created > 0 else 0

    async def _ingest_janitor_loop(self) -> None:
        """Reclaim expired ingestion leases while the process lives (issue #559).

        The first pass runs as soon as the migration barrier opens — at boot,
        every 'running' lease left by the dead process is expired by
        definition, so settlement starts immediately instead of waiting out
        the first interval. A reclaimed job's files row is reset to
        ``pending``/``queued`` (or failed at the attempts cap) right here,
        which is what removes the boot-recovery dependence C05 exploited.
        """
        await self._jobs_barrier.wait()
        interval = max(5.0, float(settings.jobs_heartbeat_interval_seconds))
        while True:
            if self.shutdown_event.is_set():
                break
            try:
                await self._janitor_sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — janitor must never kill the loop
                logger.exception("Ingestion lease janitor sweep failed")
            await asyncio.sleep(interval)

    async def _janitor_sweep_once(self) -> None:
        """One janitor pass: reclaim expired leases, then re-sync files rows."""
        if self.processor is None or self.processor.pool is None:
            return

        def run_reclaim() -> int:
            with self.processor.pool.connection() as conn:
                lease = self._make_ingest_lease(conn)
                return lease.reclaim_expired(queue=INGESTION_QUEUE)

        settled = await asyncio.to_thread(run_reclaim)
        if not settled:
            return
        logger.info(
            "Ingestion janitor: reclaimed %d expired lease(s)", settled
        )

        def resync_files_rows() -> tuple[int, int]:
            requeued = 0
            cap_failed = 0
            with self.processor.pool.connection() as conn:
                ensure_jobs_schema(conn)
                # Reclaimed-to-pending jobs whose files row is stuck in
                # 'processing': reset it so the worker's next attempt starts
                # from the same visible state the route created.
                reset_cursor = conn.execute(
                    """
                    UPDATE files SET status = 'pending', phase = 'queued',
                        error_message = NULL
                    WHERE status = 'processing'
                      AND id IN (
                        SELECT CAST(json_extract(payload_json, '$.file_id') AS INTEGER)
                        FROM jobs
                        WHERE queue = ? AND status = 'pending'
                          AND json_extract(payload_json, '$.file_id') IS NOT NULL
                      )
                    """,
                    (INGESTION_QUEUE,),
                )
                requeued = reset_cursor.rowcount
                # Jobs the janitor settled terminally at the attempts cap:
                # fail their files rows too (only cap failures need this —
                # worker-driven failures already wrote the files row).
                cap_rows = conn.execute(
                    """
                    SELECT CAST(json_extract(payload_json, '$.file_id') AS INTEGER)
                        AS fid
                    FROM jobs
                    WHERE queue = ? AND status = 'failed'
                      AND error = 'lease_attempt_cap_exceeded'
                      AND json_extract(payload_json, '$.file_id') IS NOT NULL
                    """,
                    (INGESTION_QUEUE,),
                ).fetchall()
                cap_ids = [
                    int(row["fid"]) for row in cap_rows if row["fid"] is not None
                ]
                if cap_ids:
                    cap_cursor = conn.executemany(
                        "UPDATE files SET status = 'error', phase = 'error', "
                        "error_message = 'lease_attempt_cap_exceeded' "
                        "WHERE id = ? AND status IN ('processing', 'pending')",
                        [(fid,) for fid in cap_ids],
                    )
                    cap_failed = cap_cursor.rowcount
                conn.commit()
            return requeued, cap_failed

        requeued, cap_failed = await asyncio.to_thread(resync_files_rows)
        if requeued:
            logger.info(
                "Ingestion janitor: reset %d processing files row(s) for "
                "requeued jobs",
                requeued,
            )
        if cap_failed:
            logger.warning(
                "Ingestion janitor: failed %d files row(s) at the attempt cap",
                cap_failed,
            )

    def _make_ingest_lease(self, conn):
        """Build the ingestion JobLease on ``conn``, ensuring the schema.

        ``ensure_jobs_schema`` is idempotent IF-NOT-EXISTS DDL, so lease paths
        work against any database — including the minimal temp DBs some tests
        construct without running the full migration set.
        """
        ensure_jobs_schema(conn)
        return JobLease(
            conn,
            reclaim_timeout_seconds=settings.jobs_lease_reclaim_timeout_seconds,
            max_attempts=settings.jobs_max_attempts,
        )

    async def _claim_next_ingest_job(self, worker_id: str):
        """Claim one ingestion job row off the lease (off the event loop)."""
        if self.processor is None or self.processor.pool is None:
            return None

        def _claim():
            with self.processor.pool.connection() as conn:
                lease = self._make_ingest_lease(conn)
                return lease.claim(INGESTION_QUEUE, worker_id)

        try:
            return await asyncio.to_thread(_claim)
        except Exception:  # noqa: BLE001 — a failed claim is retried next poll
            logger.exception("Ingestion job claim failed")
            return None

    async def _settle_ingest_job(
        self,
        job_id: int,
        worker_id: str,
        outcome: str,
        error: Optional[str] = None,
        delay: Optional[float] = None,
    ) -> bool:
        """Settle a claimed ingestion job (complete/requeue/fail/release)."""

        def _settle() -> bool:
            with self.processor.pool.connection() as conn:
                lease = self._make_ingest_lease(conn)
                if outcome == "complete":
                    return bool(lease.complete(job_id, worker_id))
                if outcome == "fail":
                    return bool(
                        lease.fail(job_id, worker_id, (error or "failed")[:500])
                    )
                if outcome == "release":
                    return bool(lease.release(job_id, worker_id))
                return bool(
                    lease.requeue(
                        job_id,
                        worker_id,
                        delay_seconds=delay,
                        error=error,
                    )
                )

        try:
            settled = await asyncio.to_thread(_settle)
        except Exception:  # noqa: BLE001 — settlement failures are janitor food
            logger.exception(
                "Failed to settle ingestion job id=%s (%s)", job_id, outcome
            )
            return False
        if not settled:
            # Fenced off: the lease was reclaimed mid-run. The new claimant (or
            # the janitor) owns the row now; this worker's result is void.
            logger.warning(
                "Ingestion job id=%s settlement (%s) fenced off — lease lost",
                job_id,
                outcome,
            )
        return settled

    async def _heartbeat_loop(self, job_id: int, worker_id: str) -> None:
        """Renew one job's lease every heartbeat interval until it settles.

        Exits silently when the lease is lost (janitor reclaimed it) — the
        worker's own settlement write will be fenced off in that case.
        """
        interval = max(1.0, float(settings.jobs_heartbeat_interval_seconds))

        def _renew() -> bool:
            with self.processor.pool.connection() as conn:
                lease = self._make_ingest_lease(conn)
                return bool(lease.heartbeat(job_id, worker_id))

        while True:
            await asyncio.sleep(interval)
            if self.shutdown_event.is_set():
                return
            try:
                renewed = await asyncio.to_thread(_renew)
            except Exception:  # noqa: BLE001 — a dropped beat is recoverable
                logger.debug(
                    "Heartbeat for ingestion job id=%s failed", job_id,
                    exc_info=True,
                )
                continue
            if not renewed:
                return

    async def _process_ingest_job_row(self, job, worker_id: str) -> None:
        """Process one DB-claimed ingestion job end to end (issue #559).

        Rebuilds the TaskItem from the row payload, renews the lease heartbeat
        while the job runs, then settles the row: complete on success,
        requeue-with-backoff on retryable failure, terminal fail at the
        attempts cap, and release on shutdown.
        """
        job_id = int(job["id"])
        payload = json.loads(job["payload_json"] or "{}")
        attempt = int(job["attempts"] or 1)
        task = TaskItem(
            file_path=payload.get("file_path") or "",
            vault_id=int(payload.get("vault_id") or 0),
            attempt=attempt,
            source=payload.get("source") or "upload",
            email_subject=payload.get("email_subject"),
            email_sender=payload.get("email_sender"),
            file_id=payload.get("file_id"),
            file_hash=payload.get("file_hash"),
            job_id=job_id,
            worker_id=worker_id,
        )
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(job_id, worker_id),
            name=f"ingest-heartbeat-{job_id}",
        )
        outcome_error: Optional[str] = None
        try:
            try:
                async with get_admission_controller().admit(
                    AdmissionClass.BACKGROUND, foreground=False
                ):
                    # The RAISING core, not _process_task: the lease
                    # transport owns retry/settlement for its durable row.
                    await self._run_task_processing(task)
            except AdmissionRejected as exc:
                outcome_error = f"admission rejected: {exc.reason}"
            except Exception as exc:  # noqa: BLE001 — outcome drives settle
                outcome_error = str(exc)

            if outcome_error is None:
                await self._settle_ingest_job(job_id, worker_id, "complete")
                return
            max_attempts = int(
                getattr(settings, "jobs_max_attempts", 0) or self.max_retries
            )
            if self.shutdown_event.is_set():
                await self._settle_ingest_job(
                    job_id, worker_id, "release", error=outcome_error
                )
            elif task.attempt < max_attempts:
                delay = self.retry_delay * (2 ** (task.attempt - 1))
                await self._settle_ingest_job(
                    job_id,
                    worker_id,
                    "requeue",
                    error=outcome_error,
                    delay=delay,
                )
            else:
                # Terminal at the durable attempt cap: carry the plan's
                # operator-visible literal (issue #559 R2-2), preserving the
                # last processing error as the detail.
                await self._settle_ingest_job(
                    job_id,
                    worker_id,
                    "fail",
                    error=f"attempt_cap_exceeded: {outcome_error}"[:500],
                )
                self._mark_task_permanently_failed(task, outcome_error)
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)

    async def _wait_ingest_jobs_settled(self) -> None:
        """Poll until no ingestion job is pending or running (graceful stop)."""

        def _count() -> int:
            with self.processor.pool.connection() as conn:
                ensure_jobs_schema(conn)
                # Deferred (run_after in the future) rows are NOT outstanding
                # work: no worker can claim them yet and no worker holds them,
                # so waiting for them would burn the whole shutdown timeout.
                # They survive as pending and resume on the next boot.
                row = conn.execute(
                    "SELECT COUNT(*) FROM jobs WHERE queue = ? "
                    "AND status IN ('pending', 'running') "
                    "AND (status = 'running' OR run_after IS NULL "
                    "OR run_after <= CURRENT_TIMESTAMP)",
                    (INGESTION_QUEUE,),
                ).fetchone()
                return int(row[0])

        while True:
            outstanding = await asyncio.to_thread(_count)
            if outstanding == 0:
                return
            await asyncio.sleep(0.2)

    def _release_all_ingest_leases(self) -> int:
        """Give back every still-running ingestion lease (shutdown path)."""
        if self.processor is None or self.processor.pool is None:
            return 0
        with self.processor.pool.connection() as conn:
            ensure_jobs_schema(conn)
            cursor = conn.execute(
                "UPDATE jobs SET status = 'pending', worker_id = NULL, "
                "lease_generation = lease_generation + 1, heartbeat_at = NULL "
                "WHERE queue = ? AND status = 'running'",
                (INGESTION_QUEUE,),
            )
            conn.commit()
            return cursor.rowcount

    def _ensure_retry_scheduler(self) -> None:
        """Start the retry-scheduler task if it is not already running."""
        if self.shutdown_event.is_set():
            return
        task = self._retry_scheduler_task
        if task is not None and not task.done():
            return
        self._retry_scheduler_task = asyncio.create_task(
            self._retry_scheduler_loop(), name="retry-scheduler"
        )

    def _schedule_retry(self, *, queue: "asyncio.Queue", item: object, delay: float) -> bool:
        """Park a deferred retry with the scheduler instead of blocking the worker.

        Non-blocking: when the bounded retry backlog is full the ticket is
        refused (returns False) so the caller escalates to its existing
        permanent-error path — bounded resources are preserved (issue #513
        W11 / C7 / C31).
        """
        if self.shutdown_event.is_set():
            return False
        try:
            self._retry_backlog.put_nowait(
                _RetryTicket(
                    due_at=asyncio.get_running_loop().time() + max(0.0, delay),
                    queue=queue,
                    item=item,
                )
            )
        except asyncio.QueueFull:
            return False
        self._ensure_retry_scheduler()
        return True

    async def _retry_scheduler_loop(self) -> None:
        """Deliver deferred retry tickets into their work queues at due time.

        The scheduler is a PRODUCER: ``await queue.put(...)`` may block on a
        full bounded queue without deadlocking, because the worker consumers
        keep draining. On shutdown, pending tickets are discarded (never
        delivered, never hung on).
        """
        while True:
            if self.shutdown_event.is_set() and self._retry_backlog.empty():
                break
            try:
                ticket = await asyncio.wait_for(
                    self._retry_backlog.get(), timeout=0.5
                )
            except asyncio.TimeoutError:
                continue
            try:
                delay = ticket.due_at - asyncio.get_running_loop().time()
                if delay > 0:
                    await asyncio.sleep(delay)
                if self.shutdown_event.is_set():
                    logger.warning(
                        "Discarding pending retry during shutdown (queue=%s)",
                        getattr(ticket.queue, "__class__.__name__", "?"),
                    )
                    continue
                # Producer-side blocking put is safe here (see docstring).
                await ticket.queue.put(ticket.item)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — one bad ticket must not kill the loop
                logger.exception("Retry scheduler failed to deliver a ticket")
            finally:
                self._retry_backlog.task_done()

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

    async def sweep_pending_artifact_deletes(self) -> None:
        """Best-effort confined deletion of every pending binary-asset tombstone.

        Called at startup and hourly (issue #460) so a crash after a DB commit
        cannot leak asset bytes permanently. Best-effort: skipped when no pool.
        """
        if self.processor is None or self.processor.pool is None:
            return
        try:
            from .artifact_store import sweep_pending_asset_deletes

            def run_sweep() -> tuple[int, int]:
                def invoke(conn) -> tuple[int, int]:
                    try:
                        return sweep_pending_asset_deletes(conn)
                    except StopIteration as exc:
                        # ``asyncio.Future`` cannot transport StopIteration
                        # from an executor; normalize it so the outer
                        # best-effort guard can log and continue. Narrow test
                        # seams and unusual cursor adapters can raise it.
                        raise RuntimeError(
                            "Artifact cleanup sweep stopped unexpectedly"
                        ) from exc

                # Cancelling ``to_thread`` cannot stop the underlying
                # synchronous sweep.  A standalone SQLite connection keeps a
                # worker that outlives a cancelled startup task independent of
                # the application pool, which may be closed immediately during
                # lifespan shutdown. Narrow test doubles without ``sqlite_path``
                # retain the pooled connection seam.
                sqlite_path = getattr(self.processor.pool, "sqlite_path", None)
                if sqlite_path is not None:
                    from ..models.database import get_db_connection

                    conn = get_db_connection(str(sqlite_path))
                    try:
                        return invoke(conn)
                    finally:
                        conn.close()

                with self.processor.pool.connection() as conn:
                    return invoke(conn)

            sweep_task = asyncio.create_task(
                asyncio.to_thread(run_sweep), name="artifact-delete-sweep-worker"
            )
            self._artifact_sweep_tasks.add(sweep_task)
            sweep_task.add_done_callback(self._artifact_sweep_tasks.discard)
            removed, remaining = await asyncio.shield(sweep_task)
            if remaining:
                logger.warning(
                    "Artifact cleanup: %d removed, %d pending (will retry)",
                    removed,
                    remaining,
                )
            elif removed:
                logger.info("Artifact cleanup: removed %d orphaned asset(s)", removed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Artifact cleanup sweep failed: %s", exc)

    async def _artifact_delete_sweep_loop(self, interval_seconds: float = 3600.0) -> None:
        """Hourly retry loop for pending binary-asset deletes."""
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                await self.sweep_pending_artifact_deletes()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — sweep must never kill the loop
                logger.exception("Periodic artifact-delete sweep failed")

    async def _claim_recovery_file(self, file_id: int) -> bool:
        """Reserve a row for recovery across the SELECT -> action window."""
        async with self._active_file_ids_lock:
            if (
                file_id in self._active_file_ids
                or file_id in self._queued_file_ids
                or file_id in self._recovery_file_ids
            ):
                return False
            self._recovery_file_ids.add(file_id)
            return True

    async def _release_recovery_file(self, file_id: int) -> None:
        async with self._active_file_ids_lock:
            self._recovery_file_ids.discard(file_id)

    def _mark_file_queued_locked(self, file_id: int) -> None:
        """Record one queued task while ``_active_file_ids_lock`` is held."""
        self._queued_file_ids[file_id] = self._queued_file_ids.get(file_id, 0) + 1

    def _unmark_file_queued_locked(self, file_id: int) -> None:
        """Release one queued-task ownership while the lock is held."""
        count = self._queued_file_ids.get(file_id, 0)
        if count <= 1:
            self._queued_file_ids.pop(file_id, None)
        else:
            self._queued_file_ids[file_id] = count - 1

    async def _enqueue_recovery(
        self,
        *,
        file_path: str,
        source: str,
        vault_id: int,
        file_id: int,
    ) -> object:
        """Enqueue a recovery-owned item while preserving test seams.

        A few focused tests replace ``enqueue`` with a narrow coroutine seam.
        Passing the private ownership marker only when the active callable
        accepts it keeps those seams valid while production retains the marker
        needed to hold the reservation through worker processing.
        """
        kwargs = {
            "file_path": file_path,
            "source": source,
            "vault_id": vault_id,
            "file_id": file_id,
            "_maintenance_checked": True,
        }
        try:
            enqueue_parameters = inspect.signature(self.enqueue).parameters
        except (TypeError, ValueError):  # pragma: no cover - defensive
            enqueue_parameters = {}
        if "_recovery_claim" in enqueue_parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in enqueue_parameters.values()
        ):
            kwargs["_recovery_claim"] = True
        return await self.enqueue(**kwargs)

    async def _recover_stranded_pending_rows(
        self,
        *,
        ignore_active: Optional[set] = None,
        require_older_than_minutes: Optional[int] = STRANDED_PROCESSING_TIMEOUT_MINUTES,
    ) -> None:
        """Re-enqueue stranded `files` rows (startup sweep AND periodic rescan).

        Detection for pending rows: status='pending' AND phase='queued'. The
        async upload route is the only writer of this exact combination; legacy
        scan/email paths leave phase NULL.

        Processing rows follow one of two age rules:

        * ``require_older_than_minutes`` set (the periodic-rescan default, and
          the pre-#513 startup behavior): only rows with a phase_started_at
          older than the given minutes are recovered.
        * ``require_older_than_minutes=None`` (the startup sweep since issue
          #513 W25 / AC28): rows that already reached a POST-PARSE phase are
          recovered regardless of age — at startup, single-process SQLite,
          every such row is an orphan by definition. Rows still inside a parse
          phase ('parsing'/'extracting_text'/'chunking') remain age-gated by
          STRANDED_PROCESSING_TIMEOUT_MINUTES so a long legitimate parse is
          never stolen by a restart alone (AC24 lease semantics).

        ``ignore_active`` is the live-job lease: file ids in the set are never
        touched. When None, the processor's current active lease
        (``_active_file_ids``) is used.

        Best-effort: pool absence (e.g. tests) is silently skipped.
        """
        if self.processor is None or self.processor.pool is None:
            return

        if ignore_active is None:
            async with self._active_file_ids_lock:
                ignore_active = set(self._active_file_ids)

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

        # SELECT 2: Processing rows (age rule per the docstring).
        processing_stranded = []
        try:
            with self.processor.pool.connection() as conn:
                if require_older_than_minutes is not None:
                    processing_cursor = conn.execute(
                        """
                        SELECT id, file_path, vault_id, source
                        FROM files
                        WHERE status = 'processing'
                          AND (phase_started_at IS NOT NULL
                               AND phase_started_at < datetime('now', ?))
                        """,
                        (f"-{int(require_older_than_minutes)} minutes",),
                    )
                else:
                    # Startup semantics: post-parse stages are unconditional;
                    # parse stages stay age-gated (live-parse lease, AC24).
                    processing_cursor = conn.execute(
                        """
                        SELECT id, file_path, vault_id, source
                        FROM files
                        WHERE status = 'processing'
                          AND (
                            (phase IS NULL OR phase NOT IN
                              ('parsing', 'extracting_text', 'chunking'))
                            OR (phase_started_at IS NOT NULL
                                AND phase_started_at < datetime('now', ?))
                          )
                        """,
                        (f"-{STRANDED_PROCESSING_TIMEOUT_MINUTES} minutes",),
                    )
                processing_stranded = processing_cursor.fetchall()
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Processing-row recovery sweep failed at SELECT: %s", e)

        # Apply the live-job lease to both row sets (never steal a row a
        # worker currently holds, even between its dequeue and its first
        # status write).
        stranded = [
            row
            for row in stranded
            if (row["id"] if hasattr(row, "keys") else row[0]) not in ignore_active
        ]
        processing_stranded = [
            row
            for row in processing_stranded
            if (row["id"] if hasattr(row, "keys") else row[0]) not in ignore_active
        ]

        # Skip if no stranded rows to recover
        if not stranded and not processing_stranded:
            return

        # Recovery uses one maintenance snapshot per sweep. Normal enqueue
        # callers still read the live flag for every item; only these recovery
        # calls reuse the already-completed check.
        if self.maintenance_service:
            maintenance_flag = await asyncio.to_thread(
                self.maintenance_service.get_flag
            )
            if maintenance_flag and maintenance_flag.enabled:
                logger.info("Skipping stranded-row recovery during maintenance mode")
                return

        logger.info(
            "Recovering %d stranded async-upload row(s) left at status=pending/phase=queued",
            len(stranded),
        )
        for row_index, row in enumerate(stranded, start=1):
            if row_index > 1 and row_index % RECOVERY_COOPERATIVE_YIELD_EVERY == 0:
                await asyncio.sleep(0)
            try:
                row_id = row["id"] if hasattr(row, "keys") else row[0]
                file_path = row["file_path"] if hasattr(row, "keys") else row[1]
                vault_id = row["vault_id"] if hasattr(row, "keys") else row[2]
                source = (
                    (row["source"] if hasattr(row, "keys") else row[3]) or "upload"
                )
                if not await self._claim_recovery_file(int(row_id)):
                    continue
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
                        logger.debug(
                            "Failed to mark missing stranded file row id=%s as error",
                            row_id,
                            exc_info=True,
                        )
                    await self._release_recovery_file(int(row_id))
                    continue
                try:
                    await asyncio.wait_for(
                        self._enqueue_recovery(
                            file_path=file_path,
                            source=source,
                            vault_id=int(vault_id),
                            file_id=int(row_id),
                        ),
                        timeout=STRANDED_REENQUEUE_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    # Queue saturated: leave the row for the next recovery sweep
                    # rather than blocking startup on a stranded backlog (PRR-011).
                    logger.warning(
                        "Stranded re-enqueue timed out for row id=%s; left for "
                        "the next recovery sweep",
                        row_id,
                    )
                    await self._release_recovery_file(int(row_id))
                    continue
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("Failed to re-enqueue stranded row id=%s: %s", row, e)
                row_id = row["id"] if hasattr(row, "keys") else row[0]
                await self._release_recovery_file(int(row_id))

        # Recover stuck processing rows
        for row_index, row in enumerate(processing_stranded, start=1):
            if row_index > 1 and row_index % RECOVERY_COOPERATIVE_YIELD_EVERY == 0:
                await asyncio.sleep(0)
            try:
                row_id = row["id"] if hasattr(row, "keys") else row[0]
                file_path = row["file_path"] if hasattr(row, "keys") else row[1]
                vault_id = row["vault_id"] if hasattr(row, "keys") else row[2]
                source = (
                    (row["source"] if hasattr(row, "keys") else row[3]) or "upload"
                )
                if not await self._claim_recovery_file(int(row_id)):
                    continue

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
                    await self._release_recovery_file(int(row_id))
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
                # Re-enqueue for processing, bounded like the pending loop so a
                # saturated queue cannot stall startup (PRR-011).
                try:
                    await asyncio.wait_for(
                        self._enqueue_recovery(
                            file_path=file_path,
                            source=source,
                            vault_id=int(vault_id),
                            file_id=int(row_id),
                        ),
                        timeout=STRANDED_REENQUEUE_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    # The row was already flipped to pending/queued above, so the
                    # first recovery loop naturally re-queues it on the next start.
                    logger.warning(
                        "Processing-row re-enqueue timed out for row id=%s; left "
                        "for the next recovery sweep",
                        row_id,
                    )
                    await self._release_recovery_file(int(row_id))
                    continue
            except Exception as e:
                logger.warning("Failed to recover processing row %s: %s", row, e)
                row_id = row["id"] if hasattr(row, "keys") else row[0]
                await self._release_recovery_file(int(row_id))

        if processing_stranded:
            logger.info(
                "Recovered %d stuck processing row(s)",
                len(processing_stranded),
            )

    async def _recover_interrupted_reindex_jobs(self) -> None:
        """Take ownership of `document_reindex_jobs` rows at startup (issue #513 W12).

        A reindex job row must never survive a restart as 'running': the
        process that owned it is gone (single-process SQLite), so the row is
        marked 'interrupted' — an explicit terminal status the operator can
        see — with job identity and progress columns preserved. Pending jobs
        are re-enqueued (bounded by queue capacity, matching the stranded-row
        recovery semantics) so queued-but-never-started work resumes.

        Best-effort: pool absence (e.g. tests) is silently skipped.
        """
        if self.processor is None or self.processor.pool is None:
            return
        try:
            with self.processor.pool.connection() as conn:
                cursor = conn.execute(
                    """
                    UPDATE document_reindex_jobs
                    SET status = 'interrupted',
                        error = 'Interrupted by process restart',
                        completed_at = CURRENT_TIMESTAMP
                    WHERE status = 'running'
                    """,
                )
                interrupted = cursor.rowcount
                conn.commit()
                pending = conn.execute(
                    """
                    SELECT id FROM document_reindex_jobs
                    WHERE status = 'pending'
                    ORDER BY id
                    """
                ).fetchall()
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Reindex-job recovery sweep failed: %s", e)
            return

        if interrupted:
            logger.info(
                "Marked %d reindex job(s) interrupted by restart (no auto-resume)",
                interrupted,
            )

        requeued = 0
        for row in pending:
            job_id = row["id"] if hasattr(row, "keys") else row[0]
            job_id = int(job_id)
            if job_id in self._reindex_job_ids:
                continue
            self._reindex_job_ids.add(job_id)
            try:
                self.reindex_queue.put_nowait(ReindexTaskItem(job_id=job_id))
                requeued += 1
            except asyncio.QueueFull:
                self._reindex_job_ids.discard(job_id)
                logger.warning(
                    "Reindex queue full during startup recovery; %d pending "
                    "job(s) left for the next restart or manual trigger",
                    len(pending) - requeued,
                )
                break
        if requeued:
            logger.info("Re-enqueued %d pending reindex job(s) on startup", requeued)

    def _mark_running_reindex_jobs_interrupted(self, reason: str) -> None:
        """Shutdown half of the reindex lifecycle (issue #513 W12): after the
        reindex worker is cancelled, any 'running' row is marked 'interrupted'
        so a stopped job is never abandoned as 'running'."""
        if self.processor is None or self.processor.pool is None:
            return
        try:
            with self.processor.pool.connection() as conn:
                cursor = conn.execute(
                    """
                    UPDATE document_reindex_jobs
                    SET status = 'interrupted',
                        error = ?,
                        completed_at = CURRENT_TIMESTAMP
                    WHERE status = 'running'
                    """,
                    (reason,),
                )
                conn.commit()
            if cursor.rowcount:
                logger.info(
                    "Marked %d running reindex job(s) interrupted at shutdown",
                    cursor.rowcount,
                )
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Failed to mark reindex jobs interrupted: %s", e)

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
                    logger.debug(
                        "Failed to record vector-delete retry attempt for row id=%s",
                        row_id,
                        exc_info=True,
                    )
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

    async def _recover_stranded_enrichment_rows(
        self, *, startup_cutoff: Optional[str] = None
    ) -> None:
        """Mark interrupted post-index enrichment as failed without touching indexed files."""
        if self.processor is None or self.processor.pool is None:
            return
        cutoff = startup_cutoff or getattr(self, "_startup_recovery_cutoff", None)
        try:
            with self.processor.pool.connection() as conn:
                if cutoff is not None:
                    # ``enrichment_updated_at`` is written when the route or
                    # worker enters pending/processing.  Comparing its SQLite
                    # timestamp value (with a created_at fallback for legacy
                    # rows) prevents startup recovery from touching work
                    # submitted after this start began.
                    cursor = conn.execute(
                        """
                        UPDATE files
                        SET enrichment_status = 'error',
                            enrichment_error = 'Enrichment interrupted or queued before completion; base index remains available',
                            enrichment_updated_at = ?
                        WHERE status = 'indexed'
                          AND enrichment_status IN ('pending', 'processing')
                          AND julianday(COALESCE(enrichment_updated_at, created_at)) < julianday(?)
                        """,
                        (datetime.now(UTC).isoformat(), cutoff),
                    )
                else:
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

    async def _recover_stranded_atom_enrichment_rows(
        self, *, startup_cutoff: Optional[str] = None
    ) -> None:
        """Reclaim stranded atom-scoped 'enrich' stage rows (running -> pending).

        This is distinct from the file-level chunk-enrichment recovery above: it
        operates on the #460 ``ingestion_stage_states`` atom rows, resuming atom-
        scoped work instead of marking it as a file-level error. Safe to run on a
        worker that owns no multimodal service (no-op).
        """
        if self.processor is None or self.processor.pool is None:
            return
        try:
            from . import enrichment_state as est

            with self.processor.pool.connection() as conn:
                cutoff = startup_cutoff or getattr(
                    self, "_startup_recovery_cutoff", None
                )
                recovered = est.recover_stranded_atom_stages(
                    conn, startup_cutoff=cutoff
                )
                conn.commit()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Stranded atom enrichment recovery sweep failed: %s", exc)
            return
        if recovered:
            logger.info("Recovered %d stranded atom enrichment stage(s)", recovered)

    async def _resume_pending_atom_enrichment(self) -> None:
        """Re-enqueue files with actionable atom stages after a restart.

        Recovering stranded ``running`` rows to ``pending`` in itself only marks
        them actionable; this step finds every file with an actionable atom stage
        and enqueues it (still gated by the global/vault/allowlist authorization
        check) so interrupted multimodal work actually resumes on startup.
        """
        if self.multimodal_service is None or self.processor is None:
            return
        if self.processor.pool is None:
            return
        try:
            from . import enrichment_state as est

            enqueued = 0
            with self.processor.pool.connection() as conn:
                # (1) Files with actionable stage rows (interrupted work).
                rows = conn.execute(
                    "SELECT DISTINCT s.file_id, f.vault_id, f.file_hash "
                    "FROM ingestion_stage_states s "
                    "JOIN files f ON f.id = s.file_id "
                    "WHERE s.stage = ? "
                    "AND s.status IN (?, ?, ?)",
                    (
                        est.ENRICH_STAGE,
                        est.PENDING,
                        est.FAILED_RETRYABLE,
                        est.SKIPPED_NOT_APPLICABLE,
                    ),
                ).fetchall()
                # (1b) SUCCEEDED atoms whose durable proxy never landed
                # (issue #513 W17 / RC-10): the stage was committed before the
                # LanceDB proxy write failed, leaving the derived row with
                # proxy_vector_id NULL. Mirrors the actionable logic of
                # est.list_enrichable_atoms so recovery re-enqueues them.
                proxy_missing_rows = conn.execute(
                    "SELECT DISTINCT s.file_id, f.vault_id, f.file_hash "
                    "FROM ingestion_stage_states s "
                    "JOIN document_atoms a ON a.id = s.atom_id "
                    "AND a.file_id = s.file_id AND a.generation_hash = s.generation_hash "
                    "JOIN document_atom_enrichments d ON d.file_id = a.file_id "
                    "AND d.generation_hash = a.generation_hash AND d.atom_id = a.atom_id "
                    "JOIN files f ON f.id = s.file_id "
                    "WHERE s.stage = ? AND s.status = ? "
                    "AND a.kind IN ('image','chart','table','equation') "
                    "AND d.proxy_vector_id IS NULL",
                    (est.ENRICH_STAGE, est.SUCCEEDED),
                ).fetchall()
                rows = rows + proxy_missing_rows
                # (2) Backfill: files whose active generation has eligible-kind
                # atoms but NO enrichment stage row at all. Enabling multimodal
                # after ingestion creates no stage rows (F-6a), so these files
                # would otherwise never be enriched without a re-ingest.
                # Restricted to 'indexed' files so error/deleted/processing-state
                # rows are not (re)enqueued for enrichment.
                backfill_rows = conn.execute(
                    "SELECT DISTINCT a.file_id, f.vault_id, f.file_hash "
                    "FROM document_atoms a "
                    "JOIN files f ON f.id = a.file_id "
                    "WHERE f.status = 'indexed' "
                    "AND a.generation_hash = f.active_generation_hash "
                    "AND a.kind IN ('image','chart','table','equation') "
                    "AND NOT EXISTS ("
                    "  SELECT 1 FROM ingestion_stage_states s "
                    "  WHERE s.file_id = a.file_id "
                    "  AND s.generation_hash = a.generation_hash "
                    "  AND s.stage = ? "
                    "  AND s.atom_id = a.id"
                    ")",
                    (est.ENRICH_STAGE,),
                ).fetchall()
            seen: set[tuple[int, int]] = set()
            for row_index, row in enumerate(rows + backfill_rows, start=1):
                if row_index > 1 and row_index % RECOVERY_COOPERATIVE_YIELD_EVERY == 0:
                    await asyncio.sleep(0)
                if self.shutdown_event.is_set():
                    break
                key = (row["file_id"], row["vault_id"])
                if key in seen:
                    continue
                seen.add(key)
                file_id = row["file_id"]
                vault_id = row["vault_id"]
                if not self._should_enqueue_atom_enrichment(file_id, vault_id):
                    continue
                gen_row = None
                with self.processor.pool.connection() as conn:
                    gen_row = conn.execute(
                        "SELECT active_generation_hash FROM files WHERE id = ?",
                        (file_id,),
                    ).fetchone()
                generation_hash = gen_row["active_generation_hash"] if gen_row else None
                if not generation_hash:
                    continue
                try:
                    self.atom_enrichment_queue.put_nowait(
                        AtomEnrichmentTaskItem(
                            file_id=file_id,
                            vault_id=vault_id,
                            generation_hash=generation_hash,
                            file_hash=row["file_hash"] or "",
                            document_title="",
                            attempt=0,
                        )
                    )
                    enqueued += 1
                except asyncio.QueueFull:
                    break
            if enqueued:
                logger.info("Resumed atom enrichment for %d file(s) on startup", enqueued)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Atom enrichment resume sweep failed: %s", exc)

    def _should_enqueue_atom_enrichment(self, file_id: int, vault_id: int) -> bool:
        """Return True when atom enrichment should be enqueued (authorization gate).

        Checks global + vault opt-in + non-empty allowlist + presence of eligible
        typed atoms. Policy is rechecked immediately before every provider call.
        """
        if self.multimodal_service is None:
            return False
        if not settings.multimodal_enrichment_enabled:
            return False

        with self.processor.pool.connection() as conn:
            vault_row = conn.execute(
                "SELECT multimodal_provider_enabled FROM vaults WHERE id = ?",
                (vault_id,),
            ).fetchone()
            if vault_row is None:
                return False
            raw = vault_row["multimodal_provider_enabled"]
            # Multimodal enrichment sends vault artifacts to an external
            # provider, so it requires an EXPLICIT per-vault opt-in. The column
            # is SQLite INTEGER (1/0) — normalize via bool() so a stored 1 is
            # accepted and NULL (inherit) and 0 (off) are fail-closed.
            if not bool(raw):
                return False
            gen_row = conn.execute(
                "SELECT active_generation_hash FROM files WHERE id = ?", (file_id,)
            ).fetchone()
            generation_hash = gen_row["active_generation_hash"] if gen_row else None
            if not generation_hash:
                return False
            eligible = conn.execute(
                "SELECT 1 FROM document_atoms WHERE file_id = ? AND generation_hash = ? "
                "AND kind IN ('image','chart','table','equation') LIMIT 1",
                (file_id, generation_hash),
            ).fetchone()
            if eligible is None:
                return False
        allow = list(settings.multimodal_allowed_model_origins or [])
        if not (allow and settings.multimodal_chat_url):
            return False
        # Run the real exact-origin + SSRF policy check here, not just the
        # allowlist-non-empty test. Without it, a misconfigured origin (trailing
        # slash, scheme/port mismatch, non-allowlisted host) would pass enqueue and
        # then stamp every atom SKIPPED_POLICY permanently at the worker, where the
        # policy-only status is terminal (F-6). Catching it at the gate means the
        # item is simply not enqueued and no irrecoverable stage is written.
        try:
            self.multimodal_service.client._assert_policy()
        except Exception:  # noqa: BLE001 — policy denial is the point
            return False
        return True

    def enqueue_atom_enrichment(
        self,
        *,
        file_id: int,
        vault_id: int,
        file_hash: str,
        document_title: str,
    ) -> bool:
        """Enqueue atom enrichment (best-effort). Returns True if enqueued."""
        if not self._should_enqueue_atom_enrichment(file_id, vault_id):
            return False
        with self.processor.pool.connection() as conn:
            gen_row = conn.execute(
                "SELECT active_generation_hash FROM files WHERE id = ?", (file_id,)
            ).fetchone()
            generation_hash = gen_row["active_generation_hash"] if gen_row else None
        if not generation_hash:
            return False
        try:
            self.atom_enrichment_queue.put_nowait(
                AtomEnrichmentTaskItem(
                    file_id=file_id,
                    vault_id=vault_id,
                    generation_hash=generation_hash,
                    file_hash=file_hash,
                    document_title=document_title or "",
                    attempt=0,
                )
            )
            return True
        except asyncio.QueueFull:
            logger.warning("Atom enrichment queue full for file_id=%s", file_id)
            return False

    async def _atom_enrichment_worker_loop(self) -> None:
        """Process queued atom-scoped multimodal enrichment with bounded retry.

        Runs only when a multimodal service is configured. On each completed job,
        derived proxies are embedded and written through the existing LanceDB path
        with add-then-delete so a failed re-embed leaves the base/raw proxy intact.

        Retries (job-level exceptions AND per-atom retryable provider outcomes,
        issue #513 W17 / AC10) go through the deferred-retry scheduler, bounded
        by settings.multimodal_max_attempts total attempts per file.
        """
        while True:
            if self.shutdown_event.is_set() and self.atom_enrichment_queue.empty():
                break
            try:
                item = await asyncio.wait_for(
                    self.atom_enrichment_queue.get(), timeout=0.5
                )
            except asyncio.TimeoutError:
                continue
            retryable_atoms = 0
            try:
                outcome = await self.multimodal_service.enrich_atoms(
                    vault_id=item.vault_id,
                    file_id=item.file_id,
                    generation_hash=item.generation_hash,
                    document_title=item.document_title,
                )
                proxy_records = outcome.get("proxy_records", [])
                retryable_atoms = int(outcome.get("retryable", 0) or 0)
                if proxy_records:
                    await self._write_atom_proxies(
                        proxy_records,
                        file_id=item.file_id,
                        vault_id=item.vault_id,
                        generation_hash=item.generation_hash,
                    )
                # Per-file aggregate status reflects partial/failed atoms.
                self._sync_file_enrichment_status(item)
                # In-run retry for transient per-atom outcomes (AC10): without
                # this only a restart's resume sweep would ever re-reach the
                # atom. Bounded by multimodal_max_attempts total attempts.
                if retryable_atoms and not self.shutdown_event.is_set():
                    self._schedule_atom_enrichment_retry(item)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Atom enrichment job failed for file_id=%s", item.file_id
                )
                if not self.shutdown_event.is_set():
                    self._schedule_atom_enrichment_retry(item)
            finally:
                self.atom_enrichment_queue.task_done()

    def _schedule_atom_enrichment_retry(self, item: AtomEnrichmentTaskItem) -> None:
        """Schedule the next bounded atom-enrichment attempt (W17 / W11).

        Attempts are capped at settings.multimodal_max_attempts TOTAL runs per
        file (attempt is 0-based). Scheduling is non-blocking; when the retry
        backlog is full or the cap is exhausted the failure is terminal and
        the atom stages keep their retryable status for the next startup
        resume sweep.
        """
        max_atom_attempts = max(1, int(settings.multimodal_max_attempts or 1))
        if item.attempt + 1 >= max_atom_attempts:
            logger.error(
                "Atom enrichment permanently failed for file_id=%s after %d "
                "attempt(s) (multimodal_max_attempts=%d)",
                item.file_id,
                item.attempt + 1,
                max_atom_attempts,
            )
            return
        delay = self.retry_delay * (2 ** item.attempt)
        new_item = AtomEnrichmentTaskItem(
            file_id=item.file_id,
            vault_id=item.vault_id,
            generation_hash=item.generation_hash,
            file_hash=item.file_hash,
            document_title=item.document_title,
            attempt=item.attempt + 1,
        )
        if self._schedule_retry(
            queue=self.atom_enrichment_queue, item=new_item, delay=delay
        ):
            logger.warning(
                "Atom enrichment retry for file_id=%s scheduled in %.2fs "
                "(attempt %d/%d, retryable atoms in batch)",
                item.file_id,
                delay,
                item.attempt + 2,
                max_atom_attempts,
            )
        else:
            logger.error(
                "Atom enrichment retry for file_id=%s could not be scheduled "
                "(backlog full or shutdown); leaving atom stages retryable",
                item.file_id,
            )

    def _sync_file_enrichment_status(self, item: AtomEnrichmentTaskItem) -> None:
        """Map atom-stage aggregates onto files.enrichment_status (distinct from base)."""
        try:
            from . import enrichment_state as est

            with self.processor.pool.connection() as conn:
                counts = est.aggregate_stage_status(
                    conn, file_id=item.file_id, stage=est.ENRICH_STAGE
                )
                total = sum(counts.values())
                if total == 0:
                    status = "complete"
                elif counts[est.FAILED_PERMANENT] == total:
                    status = "error"
                elif counts[est.SUCCEEDED] == total:
                    status = "complete"
                else:
                    status = "partial"
                conn.execute(
                    "UPDATE files SET enrichment_status = ?, enrichment_error = NULL, "
                    "enrichment_updated_at = ? WHERE id = ?",
                    (status, datetime.now(UTC).isoformat(), item.file_id),
                )
                conn.commit()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Could not sync atom enrichment status: %s", exc)

    async def _write_atom_proxies(
        self,
        proxy_records: list[dict],
        *,
        file_id: int,
        vault_id: int,
        generation_hash: str,
    ) -> None:
        """Embed proxy texts and write them via the LanceDB add-then-delete path.

        New proxy rows are added first; only after they are durable are the prior
        proxy rows (tracked by id in the derived table) deleted. A failed re-embed
        therefore never removes the base/raw proxy.

        Durability and staleness rules (issue #513 W17/W18):

        * A per-text None embedding (failed proxy embed) is NOT a silent skip:
          the atom's stage is reverted to FAILED_RETRYABLE (bounded by
          multimodal_max_attempts via est.mark_proxy_missing_retryable), so the
          startup resume sweep and list_enrichable_atoms both re-reach it and
          the file is never reported complete without its durable proxy.
        * Immediately before each vector insert the atom's stage fingerprint is
          re-verified (est.is_atom_stage_current): a newer generation may have
          claimed the atom during the embedding await, and an obsolete proxy
          vector must not be published.
        """
        from . import enrichment_state as est

        emb_service = self.processor.embedding_service
        vec_store = self.processor.vector_store
        if emb_service is None or vec_store is None:
            return
        gen_short = generation_hash[:12]
        new_records: list[dict] = []
        new_ids: list[str] = []
        texts = [pr["proxy_text"] for pr in proxy_records]
        # Resolve the atoms' row PKs once (stage/proxy helpers key on the
        # document_atoms rowid, proxy records carry the opaque atom id).
        with self.processor.pool.connection() as conn:
            atom_pks: dict[str, Optional[int]] = {
                pr.get("atom_id") or "": est.resolve_atom_pk(
                    conn,
                    file_id=file_id,
                    generation_hash=generation_hash,
                    atom_id=pr.get("atom_id") or "",
                )
                for pr in proxy_records
            }
        embeddings = await emb_service.embed_batch(texts, fail_fast=False)
        emb_list, _failed = embeddings
        max_attempts = max(1, int(settings.multimodal_max_attempts or 1))
        for i, pr in enumerate(proxy_records):
            atom_pk = atom_pks.get(pr.get("atom_id") or "")
            if emb_list[i] is None:
                # Failed proxy embedding: revert the atom's stage so recovery
                # re-reaches it (W17 / AC16) instead of silently skipping.
                if atom_pk is not None:
                    try:
                        with self.processor.pool.connection() as conn:
                            new_status = est.mark_proxy_missing_retryable(
                                conn,
                                file_id=file_id,
                                generation_hash=generation_hash,
                                atom_pk=atom_pk,
                                max_attempts=max_attempts,
                            )
                            conn.commit()
                        if new_status:
                            logger.warning(
                                "Proxy embedding failed for atom=%s (file_id=%s): "
                                "stage reverted to '%s' for bounded retry",
                                pr.get("atom_id"),
                                file_id,
                                new_status,
                            )
                    except Exception as exc:  # noqa: BLE001 — never abort the batch
                        logger.warning(
                            "Could not revert atom=%s stage after proxy embed "
                            "failure: %s",
                            pr.get("atom_id"),
                            exc,
                        )
                continue
            # Pre-insert staleness re-verify (W18 / AC17): the embedding await
            # above may have straddled a newer generation claiming the atom.
            if atom_pk is not None:
                try:
                    with self.processor.pool.connection() as conn:
                        stage_current = est.is_atom_stage_current(
                            conn,
                            file_id=file_id,
                            generation_hash=generation_hash,
                            atom_pk=atom_pk,
                            input_fingerprint=pr.get("fingerprint") or "",
                        )
                except Exception:  # noqa: BLE001 — on read failure, write as before
                    stage_current = True
                if not stage_current:
                    logger.info(
                        "Skipping proxy insert for superseded atom=%s "
                        "(file_id=%s): stage fingerprint moved on",
                        pr.get("atom_id"),
                        file_id,
                    )
                    continue
            pid = f"{file_id}_{pr['atom_id']}_{gen_short}_{(pr.get('fingerprint') or '')[:8]}"
            new_ids.append(pid)
            metadata = {
                "proxy": True,
                "atom_id": pr.get("atom_id"),
                "atom_kind": pr.get("atom_kind"),
                "asset_id": pr.get("asset_id"),
                "generation_hash": generation_hash,
                "enrichment_status": pr.get("status", "succeeded"),
                "fingerprint": pr.get("fingerprint"),
                "description": pr.get("description", ""),
                "raw_text": pr.get("proxy_text", ""),
                # Persistent artifact presentation metadata (issue #462). bbox was
                # already bounded/validated at enrichment time; never raw paths.
                "page_number": pr.get("page_number"),
                "bbox": pr.get("bbox"),
            }
            new_records.append(
                {
                    "id": pid,
                    "text": pr["proxy_text"],
                    "file_id": str(file_id),
                    "vault_id": str(vault_id),
                    "chunk_index": 0,
                    "metadata": json.dumps(metadata),
                    "embedding": emb_list[i],
                }
            )
        if not new_records:
            return
        # Determine which atoms were (re-)written in THIS batch so stale-id deletion
        # is scoped to them only. Using the whole-file prior_proxy_ids here would
        # delete sibling atoms' durable proxies that a partial re-enrichment did not
        # touch, leaving dangling proxy_vector_id references in SQL (F-2 fix).
        batch_atom_ids = [pr.get("atom_id") for pr in proxy_records]
        with self.processor.pool.connection() as conn:
            prior_ids = est.prior_proxy_ids_for_atoms(
                conn, file_id=file_id, generation_hash=generation_hash,
                atom_ids=[aid for aid in batch_atom_ids if aid],
            )
        stale_ids = [pid for pid in prior_ids if pid not in new_ids]
        await vec_store.add_chunks_then_delete_ids(new_records, stale_ids)
        # Record the new proxy vector ids in the derived table. Records are only
        # persisted when the atom's current derived-record fingerprint still
        # matches (set_proxy_vector_id is fingerprint-guarded and returns 0 on a
        # stale row), so a concurrent re-enrichment can never pin a vector id to
        # the wrong/outdated derived record.
        with self.processor.pool.connection() as conn:
            for rec in new_records:
                meta = json.loads(rec["metadata"])
                est.set_proxy_vector_id(
                    conn,
                    file_id=file_id,
                    generation_hash=generation_hash,
                    atom_id=meta.get("atom_id") or "",
                    input_fingerprint=meta.get("fingerprint"),
                    proxy_vector_id=rec["id"],
                )
            # A batch atom may have had a durable prior proxy superseded (stale) but
            # produced no replacement row this run (e.g. its embed failed and was
            # skipped). Its vector was just deleted from LanceDB (it is in stale_ids),
            # so clear the dangling proxy_vector_id reference rather than let SQL keep
            # claiming a proxy that no longer exists. Atoms that DID get a replacement
            # this batch keep their freshly-recorded vector id.
            #
            # We derive the atom ids from the batch records directly rather than by
            # splitting the composite vector id, which is not a safe reversible
            # encoding (atom ids may contain underscores).
            written_atom_ids = {
                (json.loads(r["metadata"]).get("atom_id") or "") for r in new_records
            }
            for pr in proxy_records:
                clear_atom_id = pr.get("atom_id") or ""
                if clear_atom_id and clear_atom_id not in written_atom_ids:
                    est.clear_proxy_vector_id(
                        conn,
                        file_id=file_id,
                        generation_hash=generation_hash,
                        atom_id=clear_atom_id,
                    )
            conn.commit()

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

        # Detached startup recovery owns its enqueue side effects. Cancel and
        # await it before queue draining so shutdown cannot race a new enqueue.
        startup_recovery_task = getattr(self, "_startup_recovery_task", None)
        if startup_recovery_task is not None:
            startup_recovery_task.cancel()
            await asyncio.gather(startup_recovery_task, return_exceptions=True)
            self._startup_recovery_task = None
        # Same for the detached jobs migration (issue #559): short-lived, but
        # shutdown must never race it mid-copy.
        jobs_migration_task = getattr(self, "_jobs_migration_task", None)
        if jobs_migration_task is not None:
            jobs_migration_task.cancel()
            await asyncio.gather(jobs_migration_task, return_exceptions=True)
            self._jobs_migration_task = None

        # The outer recovery task may have been cancelled while a synchronous
        # artifact sweep is still running in a worker thread. Cancel and await
        # the owned asyncio tasks so shutdown observes their cancellation and
        # never leaves an orphaned task behind. ``asyncio.to_thread`` cannot
        # interrupt the underlying call; production sweeps use a standalone
        # connection, so this gather remains bounded while that thread drains.
        artifact_sweep_tasks = getattr(self, "_artifact_sweep_tasks", None)
        if artifact_sweep_tasks:
            owned_artifact_tasks = tuple(artifact_sweep_tasks)
            for task in owned_artifact_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*owned_artifact_tasks, return_exceptions=True)
            artifact_sweep_tasks.clear()

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
            # The atom (multimodal) enrichment queue is drained too, so queued
            # artifacts are not silently dropped on a graceful shutdown.
            atom_queue = getattr(self, "atom_enrichment_queue", None)
            if atom_queue is not None:
                try:
                    await asyncio.wait_for(atom_queue.join(), timeout=timeout)
                except asyncio.TimeoutError:
                    logger.warning(
                        "Atom enrichment queue did not drain within timeout"
                    )
        # Phase 1b (issue #559): in lease mode, let workers drain the durable
        # queue before the shutdown flag stops claiming — the graceful
        # analogue of "pending queue items ARE processed before shutdown".
        # Deferred (run_after in the future) rows are excluded from the wait
        # (no worker can claim them mid-deferral); they survive as pending
        # and resume on the next lease-enabled boot.
        if getattr(self, "_ingest_lease_enabled", False):
            try:
                await asyncio.wait_for(
                    self._wait_ingest_jobs_settled(), timeout=timeout
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Ingestion jobs did not settle within timeout; "
                    "unclaimed leases will be released for the next boot"
                )
        self.shutdown_event.set()
        if self._worker_tasks:
            for task in self._worker_tasks:
                task.cancel()
            await asyncio.gather(*self._worker_tasks, return_exceptions=True)
        if self._enrichment_worker_task:
            self._enrichment_worker_task.cancel()
            await asyncio.gather(self._enrichment_worker_task, return_exceptions=True)
        atom_task = getattr(self, "_atom_enrichment_worker_task", None)
        if atom_task:
            atom_task.cancel()
            await asyncio.gather(atom_task, return_exceptions=True)
        if self._vector_delete_sweep_task:
            self._vector_delete_sweep_task.cancel()
            await asyncio.gather(self._vector_delete_sweep_task, return_exceptions=True)
        if getattr(self, "_artifact_delete_sweep_task", None):
            self._artifact_delete_sweep_task.cancel()
            await asyncio.gather(self._artifact_delete_sweep_task, return_exceptions=True)
        if self._reindex_worker_task:
            self._reindex_start_gate.set()
            self._reindex_worker_task.cancel()
            await asyncio.gather(self._reindex_worker_task, return_exceptions=True)
            # Reindex lifecycle ownership (issue #513 W12): with the worker
            # cancelled, a 'running' job row would be abandoned forever —
            # mark it 'interrupted' (terminal, operator-visible).
            self._mark_running_reindex_jobs_interrupted(
                "Interrupted by processor shutdown"
            )
        # Deferred-retry scheduler (issue #513 W11): cancel the deliverer, then
        # discard any still-pending tickets with a warning — shutdown never
        # hangs on the backlog and never silently delivers post-stop retries.
        if getattr(self, "_retry_scheduler_task", None):
            self._retry_scheduler_task.cancel()
            await asyncio.gather(self._retry_scheduler_task, return_exceptions=True)
            self._retry_scheduler_task = None
        if getattr(self, "_orphan_rescan_task", None):
            self._orphan_rescan_task.cancel()
            await asyncio.gather(self._orphan_rescan_task, return_exceptions=True)
            self._orphan_rescan_task = None
        if getattr(self, "_ingest_janitor_task", None):
            self._ingest_janitor_task.cancel()
            await asyncio.gather(self._ingest_janitor_task, return_exceptions=True)
            self._ingest_janitor_task = None
        # Shutdown lease release (issue #559): after the workers are gone, a
        # still-'running' ingestion row would wait out the reclaim timeout on
        # the next boot for no reason — hand every lease back immediately.
        if getattr(self, "_ingest_lease_enabled", False):
            try:
                released = await asyncio.to_thread(self._release_all_ingest_leases)
                if released:
                    logger.info(
                        "Released %d ingestion lease(s) at shutdown", released
                    )
            except Exception:  # noqa: BLE001 — janitor reclaims on next boot
                logger.warning("Shutdown lease release failed", exc_info=True)
        # Guarded like every other lifecycle attribute above: ``stop`` must be
        # safe on partially constructed instances (tests and shutdown paths
        # build minimal processors without running ``__init__``).
        retry_backlog = getattr(self, "_retry_backlog", None)
        if retry_backlog is not None:
            pending_retries = retry_backlog.qsize()
            if pending_retries:
                logger.warning(
                    "Discarding %d pending deferred retry ticket(s) at shutdown",
                    pending_retries,
                )
                while True:
                    try:
                        retry_backlog.get_nowait()
                    except asyncio.QueueEmpty:
                        break

        # Phase 3: Flush optimize on VectorStore if available
        if hasattr(self.processor, 'vector_store') and self.processor.vector_store is not None:
            try:
                from app.config import settings as _settings
                if _settings.optimize_on_shutdown:
                    await self.processor.vector_store.flush_optimize()
            except Exception as e:
                logger.warning("Failed to flush vector store on shutdown: %s", e)

        # Keep shutdown safe for the minimal processor doubles used by tests and
        # partial-construction paths that predate these ownership registries.
        for state_name in (
            "_recovery_file_ids",
            "_queued_file_ids",
            "_reindex_job_ids",
        ):
            state = getattr(self, state_name, None)
            if state is not None:
                state.clear()
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
        file_hash: Optional[str] = None,
        *,
        _maintenance_checked: bool = False,
        _recovery_claim: bool = False,
    ) -> bool:
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
            file_hash: Content hash already computed by the enqueueing route
                (issue #513 W8 — single content hash). When provided it is
                passed through to ``process_existing_file`` so the file is
                hashed once; reindex/recovery callers leave it None and the
                processor computes it.

        Note:
            If the processor is not running, the item will still be queued
            and processed when start() is called.

        Returns:
            ``True`` when a new queue item was added, or ``False`` when the
            file is already owned by the recovery path.
        """
        reservation_added = False
        if file_id is not None:
            async with self._active_file_ids_lock:
                if _recovery_claim:
                    if file_id not in self._recovery_file_ids:
                        if (
                            file_id in self._active_file_ids
                            or file_id in self._queued_file_ids
                        ):
                            return False
                        self._recovery_file_ids.add(file_id)
                        reservation_added = True
                elif file_id in self._recovery_file_ids:
                    logger.debug(
                        "Skipping route enqueue for file_id=%s claimed by recovery",
                        file_id,
                    )
                    return False

        if self.maintenance_service and not _maintenance_checked:
            # Issue #549 C02 defect class (merged with #591's recovery
            # reservations): the pooled SQLite read must stay off the event
            # loop inside this async method, served from the short TTL cache
            # (no per-call pooled checkout), and a read failure must fail
            # open (WARNING) instead of raising out of enqueue.
            try:
                # Prefer the TTL-cached read when the service provides it;
                # fall back to the raw get_flag contract (older providers and
                # test doubles implement only that).
                read = getattr(
                    self.maintenance_service, "get_flag_cached", None
                ) or self.maintenance_service.get_flag
                flag = await asyncio.to_thread(read)
            except Exception:
                logger.warning(
                    "maintenance flag read failed during enqueue; failing open",
                    exc_info=True,
                )
                flag = None
            if flag and flag.enabled:
                if reservation_added:
                    await self._release_recovery_file(file_id)
                raise DocumentProcessingError("Maintenance mode prevents enqueueing")
        job_id: Optional[int] = None
        if getattr(self, "_ingest_lease_enabled", False):
            # Durable claim row first (issue #559): the jobs row is the
            # authoritative queue entry; the asyncio.Queue below is only the
            # legacy transport and this method's return contract. A failed
            # insert falls back to the legacy transport for this item.
            payload = {
                "file_path": file_path,
                "vault_id": vault_id,
                "source": source,
                "email_subject": email_subject,
                "email_sender": email_sender,
                "file_id": file_id,
                "file_hash": file_hash,
            }

            def _insert_job() -> int:
                with self.processor.pool.connection() as conn:
                    lease = self._make_ingest_lease(conn)
                    return lease.enqueue(INGESTION_QUEUE, payload)

            try:
                job_id = await asyncio.to_thread(_insert_job)
            except Exception:  # noqa: BLE001 — degrade to legacy transport
                logger.warning(
                    "Jobs-row insert failed for %s; using in-memory queue only",
                    file_path,
                    exc_info=True,
                )
                job_id = None
        task = TaskItem(
            file_path=file_path,
            attempt=1,
            source=source,
            email_subject=email_subject,
            email_sender=email_sender,
            vault_id=vault_id,
            file_id=file_id,
            file_hash=file_hash,
            recovery_claim=_recovery_claim,
            job_id=job_id,
        )
        if job_id is not None:
            # Lease mode: the DB row is the queue entry; workers poll-claim it.
            # No in-memory put — a put here would double-process the row.
            logger.debug(f"Enqueued file to jobs lease: {file_path} (job_id={job_id})")
            return True
        if file_id is not None:
            async with self._active_file_ids_lock:
                self._mark_file_queued_locked(file_id)
        try:
            await self.queue.put(task)
        except BaseException:
            if file_id is not None:
                async with self._active_file_ids_lock:
                    self._unmark_file_queued_locked(file_id)
            if reservation_added:
                await self._release_recovery_file(file_id)
            raise
        logger.debug(f"Enqueued file: {file_path} (file_id={file_id})")
        return True

    def cancel_pending_jobs(self, **match: object) -> int:
        """Best-effort cancellation of queued, not-yet-started ingestion tasks
        (issue #516 / DRAFT-023).

        Marks every :class:`TaskItem` still sitting on the ingestion queue
        whose attributes match ``match`` (e.g. ``file_id=42`` — each keyword
        must compare equal on the item) as ``cancelled``. The worker-skip
        contract: the ingestion worker checks the flag at the top of item
        handling and drops cancelled items without processing them; the
        enqueueing side (draft promotion compensation) has already deleted the
        ``files`` row and the bytes, and the worker's existing missing-file
        failure path remains as a second guard for an item a worker already
        claimed past the flag check. Only the ingestion queue is scanned —
        enrichment/reindex queues hold post-index work, never a
        not-yet-started ingestion. Matching applies only to items still
        QUEUED: one already claimed by a worker is past cancellation.

        ``asyncio.Queue`` supports no removal, so the queue is drained and
        refilled in FIFO order. This method is deliberately synchronous — it
        never awaits, so the drain-and-refill is atomic with respect to the
        event-loop workers (no coroutine can run mid-drain and observe a
        partially drained queue, and a concurrently-blocked producer cannot
        interleave). ``task_done()``/``put_nowait()`` pairs keep the
        ``queue.join()`` unfinished-task bookkeeping balanced.

        An empty ``match`` is a caller bug (it would match everything) and is
        refused. Never raises — a cancellation failure is logged and the
        count so far is returned — because callers invoke it from
        failure-path compensation where a second exception would mask the
        original one.

        Returns the number of items marked cancelled.
        """
        if not match:
            logger.error("cancel_pending_jobs called with an empty match; refusing")
            return 0
        cancelled = 0
        # Lease mode (issue #559): queued-but-unclaimed work lives in the jobs
        # table, not on the in-memory queue. Cancel matching pending rows; a
        # claimed row is past cancellation exactly like a dequeued TaskItem.
        if (
            getattr(self, "_ingest_lease_enabled", False)
            and self.processor is not None
            and self.processor.pool is not None
        ):
            try:
                with self.processor.pool.connection() as conn:
                    rows = conn.execute(
                        "SELECT id, payload_json FROM jobs "
                        "WHERE queue = ? AND status = 'pending'",
                        (INGESTION_QUEUE,),
                    ).fetchall()
                    for row in rows:
                        payload = json.loads(row["payload_json"] or "{}")
                        if not all(
                            payload.get(key) == value
                            for key, value in match.items()
                        ):
                            continue
                        conn.execute(
                            "UPDATE jobs SET status = 'cancelled', "
                            "worker_id = NULL, heartbeat_at = NULL, "
                            "completed_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (row["id"],),
                        )
                        cancelled += 1
                    conn.commit()
            except Exception as exc:  # noqa: BLE001 — must never raise
                logger.warning(
                    "cancel_pending_jobs jobs-row pass failed after %d "
                    "item(s): %s",
                    cancelled,
                    exc,
                )
            if cancelled:
                logger.info(
                    "Cancelled %d queued ingestion job row(s) matching %s",
                    cancelled,
                    match,
                )
            return cancelled
        try:
            drained: List[TaskItem] = []
            while True:
                try:
                    drained.append(self.queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            for item in drained:
                if all(
                    getattr(item, key, None) == value
                    for key, value in match.items()
                ):
                    item.cancelled = True
                    cancelled += 1
                # Balance the unfinished-task bookkeeping for the get_nowait
                # above, then requeue in FIFO order (capacity was freed by the
                # get, so put_nowait cannot hit QueueFull here).
                self.queue.task_done()
                self.queue.put_nowait(item)
        except Exception as exc:  # noqa: BLE001 — must never raise
            logger.warning(
                "cancel_pending_jobs(%s) failed after %d item(s): %s",
                match,
                cancelled,
                exc,
            )
        if cancelled:
            logger.info(
                "Cancelled %d queued ingestion task(s) matching %s",
                cancelled,
                match,
            )
        return cancelled

    async def enqueue_enrichment(self, item: EnrichmentTaskItem) -> None:
        """Add a post-index enrichment job to the enrichment queue."""
        await self.enrichment_queue.put(item)
        logger.debug("Enqueued enrichment for file_id=%s", item.file_id)

    async def enqueue_reindex(self, job_id: int) -> None:
        """Add a reindex job to the reindex queue."""
        job_id = int(job_id)
        if job_id in self._reindex_job_ids:
            logger.debug("Skipping duplicate reindex enqueue for job_id=%s", job_id)
            return
        self._reindex_job_ids.add(job_id)
        try:
            await self.reindex_queue.put(ReindexTaskItem(job_id=job_id))
        except BaseException:
            self._reindex_job_ids.discard(job_id)
            raise
        logger.debug("Enqueued reindex job: %s", job_id)

    async def _worker_loop(self) -> None:
        """
        Main worker loop that processes items from the queue.

        Continuously processes tasks until shutdown_event is set AND queue is empty.
        Ensures all pending tasks are processed before shutdown.
        Handles retries with exponential backoff on failure.

        Dual transport (issue #559): legacy in-memory TaskItems (flag off,
        pool-less tests, or an item whose jobs-row insert failed) are consumed
        from the queue exactly as before; when the lease is enabled, an idle
        poll claims the oldest claimable ingestion row from the shared jobs
        table and processes it under its lease.
        """
        worker_id = f"ingest-{uuid.uuid4().hex[:8]}"
        while True:
            # Check if we should shutdown: shutdown_event is set AND queue is empty
            if self.shutdown_event.is_set() and self.queue.empty():
                break

            task = None
            try:
                # Wait for task with timeout to check shutdown periodically.
                # In lease mode the poll doubles as the claim tick, so the
                # idle wait is short.
                task = await asyncio.wait_for(
                    self.queue.get(),
                    timeout=(
                        0.25
                        if getattr(self, "_ingest_lease_enabled", False)
                        else 0.5
                    ),
                )
            except asyncio.TimeoutError:
                task = None

            if task is not None:
                # E3 admission (issue #518): ingestion work shares the background
                # device budget. Under interactive pressure the admit is rejected
                # (foreground preference) — defer through the retry scheduler.
                # Balanced bookkeeping (swarm review F-007): the get() above
                # consumed a join() unit, so task_done() must run on EVERY path
                # that does not reach _process_task_wrapper (whose finally calls
                # it); the re-delivery is a producer-side put by the scheduler,
                # never an inline re-put from this consumer.
                try:
                    async with get_admission_controller().admit(
                        AdmissionClass.BACKGROUND, foreground=False
                    ):
                        await self._process_task_wrapper(task)
                except AdmissionRejected as exc:
                    self.queue.task_done()
                    if not self._schedule_retry(
                        queue=self.queue, item=task, delay=0.5
                    ):
                        # Retry backlog full or shutdown began: fail the task
                        # outright rather than wedge the queue drain.
                        logger.error(
                            "Ingestion admission deferral for %s could not be "
                            "scheduled (retry backlog full or shutdown); "
                            "treating as permanent failure",
                            task.file_path,
                        )
                        self._mark_task_permanently_failed(
                            task, f"admission rejected: {exc.reason}"
                        )
                continue

            # Lease transport (issue #559): claim-driven processing. Gated on
            # the migration barrier so no claim is served before the boot
            # migration has created the rows (Round-2 R2-3).
            if getattr(self, "_ingest_lease_enabled", False) is False:
                continue
            if self.shutdown_event.is_set():
                continue
            barrier = getattr(self, "_jobs_barrier", None)
            if barrier is not None and not barrier.is_set():
                await barrier.wait()
            job = await self._claim_next_ingest_job(worker_id)
            if job is None:
                continue
            await self._process_ingest_job_row(job, worker_id)

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
                async with get_admission_controller().admit(
                    AdmissionClass.BACKGROUND, foreground=False
                ):
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
                    # Deferred retry (issue #513 W11 / RC-6): the backoff runs in
                    # the dedicated scheduler, NOT in this worker's consume
                    # slot, so other queued jobs keep being processed and the
                    # requeue can never self-deadlock a full bounded queue.
                    new_item = EnrichmentTaskItem(
                        file_id=item.file_id,
                        file_path=item.file_path,
                        vault_id=item.vault_id,
                        file_hash=item.file_hash,
                        chunks=item.chunks,
                        document_text=item.document_text,
                        attempt=item.attempt + 1,
                    )
                    if not self._schedule_retry(
                        queue=self.enrichment_queue, item=new_item, delay=delay
                    ):
                        # Retry backlog full (or shutdown began): escalate to
                        # the permanent-failure path instead of blocking.
                        logger.error(
                            "Enrichment retry for file_id=%s could not be "
                            "scheduled (retry backlog full); treating as "
                            "permanent failure",
                            item.file_id,
                        )
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
        await self._reindex_start_gate.wait()
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
                self._reindex_job_ids.discard(item.job_id)

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

            # Step 4b: Dimension probe (issue #513 W13 / RC-8). Embed one probe
            # text BEFORE iterating files; when the target dimension differs
            # from the live table's dimension, every per-file vector write is
            # routed into a rebuild temp table and the live index is replaced
            # by a validated atomic swap only after ALL files succeed. On any
            # failure the temp table is dropped and the old index is preserved.
            vector_store = self.processor.vector_store
            rebuild_handle = None
            probe_dim: Optional[int] = None
            if vector_store is not None and vaults_files:
                emb_service = self.processor.embedding_service
                if emb_service is not None:
                    probe_embeddings, _probe_failed = await emb_service.embed_batch(
                        ["dimension_probe"], fail_fast=True
                    )
                    if probe_embeddings and probe_embeddings[0] is not None:
                        probe_dim = len(probe_embeddings[0])
                live_dim = await vector_store.get_live_embedding_dim()
                if probe_dim is not None and live_dim is not None and probe_dim != live_dim:
                    rebuild_handle = await vector_store.begin_dimension_rebuild(probe_dim)
                    logger.info(
                        "Reindex job %d: embedding dimension %d != live table "
                        "dimension %d — rebuilding into temp table '%s' "
                        "(live index untouched until commit)",
                        job_id,
                        probe_dim,
                        live_dim,
                        getattr(rebuild_handle, "table_name", "?"),
                    )

            # Step 5: Initialize counters
            total_files = 0
            processed_files = 0
            failed_files = 0
            failed_details: list[str] = []

            try:
                for vault_id_sorted in sorted(vaults_files.keys()):
                    file_list = vaults_files[vault_id_sorted]
                    for file_id, file_path, vault_id_file in file_list:
                        total_files += 1
                        logger.info("Re-embedding file_id=%d in vault_id=%d", file_id, vault_id_file)
                        reprocess_kwargs = (
                            {"vector_target": rebuild_handle}
                            if rebuild_handle is not None
                            else {}
                        )
                        try:
                            await self.processor.process_existing_file(
                                file_id, file_path, vault_id_file, **reprocess_kwargs
                            )
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

                # Same-dimension reindex: nothing to swap; unchanged behavior.
                if rebuild_handle is not None:
                    if failed_files > 0:
                        raise VectorStoreError(
                            f"dimension rebuild aborted: {failed_files}/{total_files} "
                            f"file(s) failed to re-embed at dimension {probe_dim}"
                        )
                    await vector_store.commit_dimension_rebuild(rebuild_handle)
                    rebuild_handle = None
            except Exception:
                # Old index preserved: drop the temp table, then let the outer
                # handler fail the job.
                if rebuild_handle is not None:
                    await vector_store.abort_dimension_rebuild(rebuild_handle)
                    rebuild_handle = None
                raise

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
                # After a committed dimension rebuild (W13) the recorded dim is
                # the probe-observed dim the new index was actually built at.
                try:
                    vector_store = self.processor.vector_store
                    if vector_store is not None:
                        await vector_store.record_embedding_metadata(
                            probe_dim or settings.embedding_dim, raise_on_error=True
                        )
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
        raises an exception or continues early. It also maintains the live-job
        lease (issue #513 W25): the file id is registered from dequeue until
        settle so the periodic orphan rescan never steals a row a worker is
        actively processing — even one stuck for hours in a long parse.
        """
        leased_id = task.file_id
        lease_registered = False
        try:
            if leased_id is not None:
                async with self._active_file_ids_lock:
                    # This dequeue consumes exactly one ownership count. Keep
                    # any sibling same-file task visible until it is dequeued.
                    self._unmark_file_queued_locked(leased_id)
                    # A route task already queued when recovery claimed this
                    # row must be consumed as a no-op; the recovery-owned task
                    # is the sole processor for the row.
                    if (
                        not task.recovery_claim
                        and leased_id in self._recovery_file_ids
                    ):
                        return
                    self._active_file_ids.add(leased_id)
                    lease_registered = True
            await self._process_task(task)
        finally:
            self.queue.task_done()
            if leased_id is not None and lease_registered:
                async with self._active_file_ids_lock:
                    self._active_file_ids.discard(leased_id)
                    if task.recovery_claim:
                        self._recovery_file_ids.discard(leased_id)

    async def _run_task_processing(self, task: TaskItem) -> None:
        """Processing core shared by both transports (issue #559).

        Performs the work and the success-side enrichment fan-out and RAISES
        on failure — retry/lease settlement is the caller's decision. The
        legacy transport wraps this with in-memory retry handling; the lease
        transport settles the durable row from the raised outcome.
        """
        if task.file_id is not None:
            # Async upload path: the row already exists with status='pending'
            # / phase='queued' and the duplicate check has already passed.
            # The route-computed content hash (issue #513 W8) is forwarded
            # when present so the file is hashed exactly once.
            kwargs = {}
            if task.file_hash is not None:
                kwargs["file_hash"] = task.file_hash
            result = await self.processor.process_existing_file(
                file_id=task.file_id,
                file_path=task.file_path,
                vault_id=task.vault_id,
                **kwargs,
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
        if result is not None:
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
            if self.multimodal_service is not None:
                self.enqueue_atom_enrichment(
                    file_id=result.file_id,
                    vault_id=result.vault_id,
                    file_hash=result.file_hash,
                    document_title=os.path.basename(result.file_path or ""),
                )
        logger.info(f"Successfully processed: {task.file_path}")

    async def _process_task(self, task: TaskItem) -> None:
        """
        Process a single task with retry logic.

        Args:
            task: TaskItem containing file path, attempt count, and optional email metadata

        On failure, requeues the task with incremented attempt count
        and exponential backoff delay if retries remain.
        """
        if task.cancelled:
            # Worker-skip contract (BackgroundProcessor.cancel_pending_jobs,
            # issue #516 / DRAFT-023): this item was cancelled while queued.
            # The enqueueing side has compensated — deleted the files row and
            # the bytes this task points at — so drop it instead of running
            # it into missing state. The queue's task_done() is still called
            # by _process_task_wrapper's finally block.
            logger.info(
                "Skipping cancelled ingestion task for %s (file_id=%s)",
                task.file_path,
                task.file_id,
            )
            return
        logger.info(
            f"Processing file: {task.file_path} (attempt {task.attempt}, file_id={task.file_id})"
        )

        try:
            await self._run_task_processing(task)
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

        Schedules a deferred retry with incremented attempt count if retries
        remain (issue #513 W11 / RC-6: the backoff sleeps in the dedicated
        retry scheduler, never in the worker's consume slot, and the requeue
        is delivered by the scheduler as a queue PRODUCER — a full bounded
        queue can no longer self-deadlock the sole consumer). When retries are
        exhausted (permanent failure), the retry backlog is full, or shutdown
        is in progress, and ``task.file_id`` is set, writes ``status='error'``,
        ``error_message``, and ``phase='error'`` to the corresponding ``files``
        row so the file is not left stuck in 'processing'.
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

            # Requeue with incremented attempt count, preserving metadata
            new_task = TaskItem(
                file_path=task.file_path,
                attempt=task.attempt + 1,
                source=task.source,
                email_subject=task.email_subject,
                email_sender=task.email_sender,
                vault_id=task.vault_id,
                file_id=task.file_id,
                file_hash=task.file_hash,
                recovery_claim=task.recovery_claim,
            )
            retry_scheduled = self._schedule_retry(
                queue=self.queue, item=new_task, delay=delay
            )
            if not retry_scheduled:
                # Retry backlog full (or shutdown began between the checks):
                # escalate to the permanent-failure path instead of blocking.
                logger.error(
                    "Task retry for %s could not be scheduled (retry backlog "
                    "full); treating as permanent failure: %s",
                    task.file_path,
                    error_message,
                )
                self._mark_task_permanently_failed(task, error_message)
            elif task.file_id is not None:
                # The retry ticket owns a queue slot even while it waits in the
                # deferred scheduler. This prevents recovery from claiming the
                # same row after the active attempt settles.
                async with self._active_file_ids_lock:
                    self._mark_file_queued_locked(task.file_id)
                if task.recovery_claim:
                    # Transfer the reservation to the deferred recovery retry;
                    # the wrapper must not release it when this attempt settles.
                    task.recovery_claim = False
            elif task.recovery_claim:
                # Transfer the reservation to the deferred recovery retry;
                # the wrapper must not release it when this attempt settles.
                task.recovery_claim = False
        else:
            self._mark_task_permanently_failed(task, error_message)

    def _mark_task_permanently_failed(self, task: TaskItem, error_message: str) -> None:
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
