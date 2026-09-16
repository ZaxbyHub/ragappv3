"""DraftJobProcessor: asyncio background worker that drains ``draft_jobs``.

PR 1 of Draft Room (issue #435, ``specs/draft-room/SPEC.md`` section 10)
dispatched only ``parse_input`` jobs. This module (issue #436) adds ``compile``
dispatch: it drives :func:`app.services.draft_pipeline.run_compile`, the
single editorial-pipeline entry point, and owns the job-level policy around
it (claim, permission recheck, bounded automatic retry, SSE notification).
Modeled on :class:`app.services.wiki_compile_processor.WikiCompileProcessor`
but async because dispatch work awaits model calls.

Hard invariants (SPEC section 10.1):

* Never hold a SQLite connection or transaction across parsing, filesystem
  I/O, or an ``await``. Every DB step acquires a connection via
  ``self._pool.connection()``, does its work, and releases it before the next
  blocking or async step.
* Claim jobs atomically through ``DraftStore.claim_next_parse_job`` /
  ``DraftStore.claim_next_compile_job`` (both use ``BEGIN IMMEDIATE``) so two
  processors can never double-run a job.
* Cooperative cancellation is checked before starting extraction and again
  immediately before committing parsed text — and re-checked inside the very
  transaction that persists that text (issue #516 DRAFT-005), so a cancel
  racing the final pre-commit awaits still settles cancelled and discards
  the extracted output rather than persisting it. For compile jobs,
  ``draft_pipeline.run_compile`` performs the equivalent cancellation checks
  and discards any in-flight provider result itself (SPEC section 10.2); this
  module never resurrects a job it settles as ``cancelled``.
* Failure codes are drawn from a small stable set and never carry raw
  exception text, response bodies, request content, manuscript text, prompts,
  headers, or absolute paths.
* SSE events are published only after the state-changing transaction commits,
  and a publish failure must never fail the job.

Compile retry policy (SPEC section 10.2): ``run_compile`` already exhausts
its own bounded, stage-level transient retry before ever raising
``CompileFailure`` (see that module's law #11), and it persists the job's and
draft's terminal state itself before raising. ``CompileFailure.retryable`` is
therefore the *job-level* automatic-retry verdict this processor honors: it
is never re-derived, and a job is never automatically retried when it is
False (authorization, validation, content-size, provider-policy, and
hard-budget failures always set it False). When it is True and the failed
job's ``retry_count`` is still under ``settings.draft_transient_retry_limit``,
this processor inserts a brand-new child ``compile`` job
(``parent_job_id``/``attempt_no + 1``/``retry_count + 1``) carrying the same
request snapshot — mirroring user-initiated retry — rather than mutating the
terminal job back to pending, which the job state machine forbids.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Optional

from app.api.deps import _evaluate_policy
from app.config import settings
from app.services import draft_pipeline
from app.services.admission import AdmissionClass, get_admission_controller
from app.services.document_extraction import DocumentExtractionError
from app.services.draft_events import build_event, get_draft_event_bus
from app.services.draft_store import (
    DraftNotFoundError,
    DraftStore,
    _draft_job_lease,
    _draft_lease_enabled,
    _LeaseLostError,
    sha256_text,
)

if TYPE_CHECKING:
    from app.models.database import SQLiteConnectionPool
    from app.services.document_extraction import DocumentExtractionService
    from app.services.draft_input_storage import DraftInputStorage
    from app.services.draft_store import DraftJobRecord

logger = logging.getLogger(__name__)

# Stable, machine-readable failure codes this processor may write. No other
# string may ever reach ``set_job_status``/``set_input_parse_status`` as an
# error code from this module. Compile-stage failure codes come from
# ``draft_pipeline.CompileFailure.code`` instead and are passed through
# unchanged — this module never invents its own compile failure vocabulary.
CODE_INPUT_PARSE_FAILED = "input_parse_failed"
CODE_INPUT_FILE_MISSING = "input_file_missing"
CODE_PARSED_TEXT_LIMIT_EXCEEDED = "parsed_text_limit_exceeded"
CODE_JOB_TIMEOUT = "job_timeout"
CODE_INTERNAL_ERROR = "internal_error"
CODE_PERMISSION_REVOKED = "permission_revoked"

# How stale ``.incoming``/``.trash`` entries must be before startup
# reconciliation removes them.
_RECONCILE_MAX_AGE_SECONDS = 24 * 60 * 60


class DraftJobProcessor:
    """Background worker that processes ``parse_input`` jobs from ``draft_jobs``.

    One worker per process. A connection is acquired per DB step and released
    immediately — never held across parsing, filesystem I/O, or an ``await``.
    """

    def __init__(
        self,
        pool: "SQLiteConnectionPool",
        storage: "DraftInputStorage",
        extraction: "DocumentExtractionService",
        *,
        poll_interval: Optional[float] = None,
        engine: Optional[Any] = None,
    ) -> None:
        self._pool = pool
        self._storage = storage
        self._extraction = extraction
        self._poll_interval = (
            poll_interval
            if poll_interval is not None
            else settings.draft_poll_interval_seconds
        )
        # The live ``RAGEngine`` singleton (``app.state.rag_engine``), used for
        # compile dispatch. Optional because current lifespan wiring
        # constructs ``RAGEngine`` *after* this processor: see
        # :meth:`set_rag_engine`. While unset, compile retrieval fails closed
        # with ``retrieval_unavailable`` via ``draft_pipeline.default_deps`` —
        # it never silently returns an empty successful result.
        self._engine = engine
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._generation = 0
        self._startup_reset_task: Optional[asyncio.Task] = None
        # Throttle for the unwired-engine compile warning (issue #532 review,
        # PRR-024): the poll loop calls ``_claim_next_job`` every interval, so
        # without this flag the same "engine not wired" condition would log
        # once per poll for the whole deferral window.
        self._warned_unwired_compile_claim = False
        # Lease-mode identity + janitor handle (issue #559 stage 4). Claims
        # and every fenced terminal write carry this worker id; the janitor
        # loop recovers only leases whose heartbeat expired past the reclaim
        # timeout, so a live-but-slow worker is never stolen.
        self._worker_id = f"draft-proc-{id(self)}"
        self._janitor_task: Optional[asyncio.Task] = None
        # Strong references to detached background tasks so CPython does not
        # garbage-collect them mid-flight, mirroring WikiCompileProcessor.
        self._bg_tasks: set[asyncio.Task] = set()

    def _fence_kwargs(self) -> dict:
        """Terminal-write fence kwargs in lease mode (empty in legacy mode)."""
        if _draft_lease_enabled():
            return {"worker_id": self._worker_id}
        return {}

    def set_rag_engine(self, engine: Any) -> None:
        """Wire the live ``RAGEngine`` singleton in after construction.

        ``app.state.rag_engine`` is created after ``DraftJobProcessor`` in the
        current lifespan startup order, so an integrator calls this once the
        engine exists (e.g. ``app.state.draft_job_processor.set_rag_engine(
        app.state.rag_engine)`` right after ``RAGEngine`` is constructed).
        Safe to call at any time; it only affects the deps built for the next
        compile dispatch.
        """
        self._engine = engine

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._generation += 1
        generation = self._generation
        self._running = True
        try:
            # Startup recovery must complete before the poll loop begins and
            # before HTTP traffic is accepted (SPEC section 10.1 item 6).
            reset_task = self._startup_reset_task
            if reset_task is None or reset_task.done():
                reset_coro = asyncio.to_thread(self._recover_on_startup)
                try:
                    reset_task = asyncio.create_task(reset_coro)
                except BaseException:
                    reset_coro.close()
                    raise
                self._startup_reset_task = reset_task
                reset_task.add_done_callback(self._consume_startup_reset)
            # A thread-backed recovery cannot be cancelled; sharing this task
            # prevents a cancelled start from overlapping a later retry.
            await asyncio.shield(reset_task)
            if generation != self._generation or not self._running:
                return
            poll_coro = self._poll_loop()
            try:
                self._task = asyncio.create_task(poll_coro)
            except BaseException:
                # We own the coroutine until create_task accepts it. Closing
                # it here avoids a warning when publication itself fails.
                poll_coro.close()
                raise
        except BaseException:
            # The caller wraps this in a timeout and swallows the result, so
            # without this reset a cancelled recovery would leave _running=True
            # with no poll loop: a processor that reports started, accepts
            # stop(), and silently never runs a job. CancelledError is a
            # BaseException, hence the broad catch.
            if generation == self._generation:
                self._running = False
                self._task = None
            raise
        logger.info("DraftJobProcessor started")

    async def stop(self) -> None:
        self._generation += 1
        self._running = False
        task = self._task
        self._task = None
        janitor = self._janitor_task
        self._janitor_task = None
        if janitor and not janitor.done():
            janitor.cancel()
            await asyncio.gather(janitor, return_exceptions=True)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        for bg in list(self._bg_tasks):
            bg.cancel()
        for bg in list(self._bg_tasks):
            try:
                await bg
            except asyncio.CancelledError:
                pass
            except Exception:  # nosec B110 - best-effort shutdown drain of a cancelled
                # detached task; identical accepted pattern in
                # WikiCompileProcessor.stop(), which is already baselined.
                pass
        self._bg_tasks.clear()
        logger.info("DraftJobProcessor stopped")

    def _consume_startup_reset(self, task: asyncio.Task) -> None:
        """Observe detached recovery failures and clear only the owned task."""
        try:
            if not task.cancelled():
                error = task.exception()
                if error is not None:
                    logger.debug(
                        "DraftJobProcessor startup recovery failed",
                        exc_info=(type(error), error, error.__traceback__),
                    )
        except asyncio.CancelledError:
            pass
        except BaseException:
            logger.debug(
                "DraftJobProcessor startup recovery result could not be consumed",
                exc_info=True,
            )
        if getattr(self, "_startup_reset_task", None) is task:
            self._startup_reset_task = None

    # ------------------------------------------------------------------
    # Startup recovery
    # ------------------------------------------------------------------

    def _recover_on_startup(self) -> None:
        """Runs synchronously in a thread: orphan jobs, filesystem, orphan inputs.

        Closes the crash window between an input reservation commit and its
        job enqueue (SPEC section 6.2), and between a worker crash mid-parse
        and its job/input rows being reconciled (SPEC section 10.1 item 6).
        """
        with self._pool.connection() as conn:
            store = DraftStore(conn)
            reset = store.recover_orphaned_parse_jobs()
        if reset:
            logger.warning(
                "DraftJobProcessor: reset %d orphaned parse job(s) to pending", reset
            )

        with self._pool.connection() as conn:
            store = DraftStore(conn)
            compile_reset = store.recover_orphaned_compile_jobs()
        if compile_reset:
            logger.warning(
                "DraftJobProcessor: reset %d orphaned compile job(s) to pending",
                compile_reset,
            )

        with self._pool.connection() as conn:
            store = DraftStore(conn)
            valid_pairs = store.list_all_owner_draft_pairs()
        try:
            self._storage.reconcile(
                valid_pairs, max_age_seconds=_RECONCILE_MAX_AGE_SECONDS
            )
        except Exception as exc:
            logger.error(
                "DraftJobProcessor: startup storage reconciliation failed (%s)",
                type(exc).__name__,
            )

        with self._pool.connection() as conn:
            store = DraftStore(conn)
            orphaned_inputs = store.list_pending_inputs_without_active_job()

        for input_id, draft_id, owner_id, storage_relpath in orphaned_inputs:
            self._recover_orphaned_input(input_id, draft_id, owner_id, storage_relpath)

    def _recover_orphaned_input(
        self, input_id: int, draft_id: int, owner_id: int, storage_relpath: str
    ) -> None:
        try:
            file_present = self._storage.exists(storage_relpath)
        except Exception as exc:
            logger.error(
                "DraftJobProcessor: startup recovery could not check input file presence "
                "(input_id=%d, %s)",
                input_id,
                type(exc).__name__,
            )
            file_present = False

        if file_present:
            try:
                with self._pool.connection() as conn:
                    store = DraftStore(conn)
                    store.enqueue_parse_job_for_recovery(
                        input_id=input_id,
                        timeout_seconds=settings.draft_parse_timeout_seconds,
                    )
            except Exception as exc:
                logger.error(
                    "DraftJobProcessor: startup recovery could not re-enqueue "
                    "input_id=%d (%s)",
                    input_id,
                    type(exc).__name__,
                )
            return

        try:
            with self._pool.connection() as conn:
                store = DraftStore(conn)
                store.set_input_parse_status(
                    input_id=input_id,
                    target="failed",
                    parse_error=CODE_INPUT_FILE_MISSING,
                    allow_recovery=True,
                )
        except Exception as exc:
            logger.error(
                "DraftJobProcessor: startup recovery could not fail missing-file "
                "input_id=%d (%s)",
                input_id,
                type(exc).__name__,
            )

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        if _draft_lease_enabled() and self._janitor_task is None:
            # Lease janitor (issue #559 stage 4): recover only expired
            # leases; startup recovery already handled all rows. Spawned
            # here rather than in start() so start() keeps exactly two task
            # publications (reset -> poll), the lifecycle contract pinned by
            # test_compile_processor_startup_lifecycle_static.py.
            janitor_coro = self._janitor_loop()
            try:
                self._janitor_task = asyncio.create_task(
                    janitor_coro, name="draft-janitor"
                )
            except BaseException:
                janitor_coro.close()
                raise
        while self._running:
            try:
                job = await asyncio.to_thread(self._claim_next_job)
                if job is None:
                    await asyncio.sleep(self._poll_interval)
                    continue

                logger.info(
                    "DraftJobProcessor: claimed job id=%d draft_id=%d",
                    job.id,
                    job.draft_id,
                )
                # Lease mode (issue #559 stage 4): renew the held lease while
                # the job runs, so a long model call never looks like a dead
                # worker; every fenced write is bounded to this worker either
                # way.
                heartbeat_task: Optional[asyncio.Task] = None
                if _draft_lease_enabled():
                    heartbeat_task = asyncio.create_task(
                        self._heartbeat_loop(job.id),
                        name=f"draft-heartbeat-{job.id}",
                    )
                try:
                    # E3 admission (issue #518): background budget for draft
                    # jobs. Unlike the wiki/kms compile loops there is no
                    # per-job failure handler here: a rejection surfaces as a
                    # poll-loop error below (logged, backoff, keep polling) and
                    # the already-claimed row is recovered by the existing
                    # startup orphan recovery — no silent retry bookkeeping.
                    async with get_admission_controller().admit(
                        AdmissionClass.BACKGROUND, foreground=False
                    ):
                        await self._run_job(job)
                finally:
                    if heartbeat_task is not None:
                        heartbeat_task.cancel()
                        await asyncio.gather(
                            heartbeat_task, return_exceptions=True
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                # Per SPEC section 10.1 item 8: the poll loop survives any
                # unexpected processor-level exception (job claiming, dispatch
                # bookkeeping) and keeps polling. This is distinct from the
                # per-job extraction/commit error paths above, which never log
                # str(exc) because that boundary can carry manuscript content.
                logger.exception("DraftJobProcessor: poll loop error")
                await asyncio.sleep(self._poll_interval)

    async def _heartbeat_loop(self, job_id: int) -> None:
        """Renew a held draft lease every heartbeat interval until it is lost.

        The renewal runs through the shared lease primitive parameterized on
        ``draft_jobs`` — the same fenced predicate the claims and terminal
        writes use.
        """
        interval = max(1.0, float(settings.jobs_heartbeat_interval_seconds))

        while True:
            await asyncio.sleep(interval)

            def _renew():
                with self._pool.connection() as conn:
                    return _draft_job_lease(conn).heartbeat(
                        job_id, self._worker_id
                    )

            try:
                renewed = await asyncio.to_thread(_renew)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a missed beat is not fatal
                logger.debug(
                    "DraftJobProcessor: heartbeat renewal failed for job %s",
                    job_id,
                )
                continue
            if not renewed:
                return

    async def _janitor_loop(self) -> None:
        """Reclaim expired draft leases (issue #559 stage 4).

        Runs the business-aware orphan recovery scoped to leases whose
        ``heartbeat_at`` expired past the reclaim timeout — a live worker's
        row is never touched, and a reclaimed worker's late writes are fenced
        off by its worker id.
        """
        interval = max(5.0, float(settings.jobs_heartbeat_interval_seconds))
        while self._running:
            # Sleep BEFORE the first sweep: startup recovery already handled
            # every row at boot, and the deferred first pass keeps the
            # janitor's to_thread out of the startup lifecycle window the
            # lifecycle tests pin (reset -> poll publication order).
            await asyncio.sleep(interval)
            if not self._running:
                break
            try:
                await asyncio.to_thread(self._reclaim_expired_leases)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — janitor must never kill the loop
                logger.exception("DraftJobProcessor: janitor sweep failed")

    def _reclaim_expired_leases(self) -> int:
        cutoff = f"-{float(settings.jobs_lease_reclaim_timeout_seconds)} seconds"
        cap = max(int(settings.jobs_max_attempts), 1)
        with self._pool.connection() as conn:
            store = DraftStore(conn)
            # Attempts cap first (issue #559 stage 4): a crash-looping job
            # whose durable attempts counter reached the cap settles
            # terminally instead of being resurrected by the recovery —
            # the same `lease_attempt_cap_exceeded` contract the shared
            # janitor enforces on the jobs-table queues.
            expired = conn.execute(
                """
                SELECT id, draft_id, job_type, attempts FROM draft_jobs
                WHERE status = 'running'
                  AND heartbeat_at IS NOT NULL
                  AND heartbeat_at < datetime('now', ?)
                """,
                (cutoff,),
            ).fetchall()
            capped = 0
            for row in expired:
                if int(row["attempts"] or 0) < cap:
                    continue
                store.settle_expired_job_at_lease_cap(
                    job_id=int(row["id"]),
                    draft_id=int(row["draft_id"]),
                    job_type=row["job_type"],
                )
                capped += 1
            if capped:
                logger.warning(
                    "DraftJobProcessor: janitor settled %d expired draft "
                    "lease(s) at the attempts cap",
                    capped,
                )
            parse_reset = store.recover_orphaned_parse_jobs(heartbeat_cutoff=cutoff)
            compile_reset = store.recover_orphaned_compile_jobs(
                heartbeat_cutoff=cutoff
            )
        total = capped + parse_reset + compile_reset
        if total:
            logger.warning(
                "DraftJobProcessor: janitor reclaimed %d expired draft lease(s)",
                total,
            )
        return total

    def _claim_next_job(self) -> Optional["DraftJobRecord"]:
        with self._pool.connection() as conn:
            store = DraftStore(conn)
            job = store.claim_next_parse_job(worker_id=self._worker_id)
            if job is not None:
                return job
            # Compile jobs stay pending until the RAG engine is wired (issue
            # #516 DRAFT-007): lifespan starts this processor before the
            # engine exists, and dispatching a compile job in that window
            # would terminally fail queued work that only needed to wait.
            # ``set_rag_engine`` makes compile jobs claimable again;
            # ``_unwired_retrieval`` in ``draft_pipeline.default_deps``
            # remains fail-closed defense in depth. Surface the deferral once
            # per processor lifetime (never per poll) so an operator can see
            # why queued compile work is not being picked up.
            if self._engine is None:
                if not self._warned_unwired_compile_claim and (
                    store.has_pending_compile_job()
                ):
                    self._warned_unwired_compile_claim = True
                    logger.warning(
                        "DraftJobProcessor: compile jobs pending but RAG "
                        "engine not wired yet; compile claiming deferred"
                    )
                return None
            return store.claim_next_compile_job(worker_id=self._worker_id)

    # ------------------------------------------------------------------
    # Per-job dispatch
    # ------------------------------------------------------------------

    async def _run_job(self, job: "DraftJobRecord") -> None:
        """Job-boundary safety net around :meth:`_dispatch_job`.

        SPEC section 10.1 item 8: catch unexpected exceptions at the job
        boundary, store a sanitized failure code, and keep the poll loop
        alive. ``_dispatch_job`` already handles every expected failure mode
        with its own sanitized code; this net only exists so a bug in the
        processor itself cannot leave a job stuck ``running`` until the next
        restart, and never re-raises into the poll loop.
        """
        try:
            await self._dispatch_job(job)
        except asyncio.CancelledError:
            raise
        except _LeaseLostError:
            # Lease mode (issue #559 stage 4): the job was reclaimed or
            # settled by someone else while we ran — a disowned worker makes
            # NO terminal write of its own; the janitor owns settlement.
            logger.warning(
                "DraftJobProcessor: job id=%s lease lost mid-run; aborting "
                "without a terminal write",
                job.id,
            )
        except Exception as exc:
            logger.error(
                "DraftJobProcessor: job id=%d dispatch raised %s",
                job.id,
                type(exc).__name__,
            )
            try:
                await self._fail_job(job, code=CODE_INTERNAL_ERROR)
            except Exception:
                logger.error(
                    "DraftJobProcessor: job id=%d could not be marked failed after "
                    "an unexpected dispatch error",
                    job.id,
                )

    async def _dispatch_job(self, job: "DraftJobRecord") -> None:
        """Dispatch one claimed ``parse_input`` job end to end.

        Never holds a connection across the extraction step or any ``await``.
        """
        self._publish_event(job, "job_started", job_id=job.id, status="running")

        if job.job_type == "compile":
            await self._dispatch_compile_job(job)
            return

        if job.job_type != "parse_input":
            # Only parse_input and compile are claimed by this processor;
            # anything else here indicates a store/claim invariant violation,
            # not user input.
            await self._fail_job(job, code=CODE_INTERNAL_ERROR)
            return

        if job.input_id is None:
            await self._fail_job(job, code=CODE_INTERNAL_ERROR)
            return

        # Permission re-check #1 (SPEC section 9.1 rule 4): the creating user
        # must still be active and hold vault `read` right after the claim.
        if not await self._owner_permission_ok(job):
            await self._fail_input_and_job(job, code=CODE_PERMISSION_REVOKED)
            return

        try:
            input_record = await asyncio.to_thread(self._get_input, job)
        except DraftNotFoundError:
            await self._fail_job(job, code=CODE_INPUT_FILE_MISSING)
            return
        except Exception as exc:
            logger.error(
                "DraftJobProcessor: job id=%d could not load input (%s)",
                job.id,
                type(exc).__name__,
            )
            await self._fail_job(job, code=CODE_INTERNAL_ERROR)
            return

        try:
            await asyncio.to_thread(self._move_input_to_parsing, job.input_id)
        except Exception as exc:
            logger.error(
                "DraftJobProcessor: job id=%d could not move input to parsing (%s)",
                job.id,
                type(exc).__name__,
            )
            await self._fail_job(job, code=CODE_INTERNAL_ERROR)
            return

        # Cooperative cancellation check #1: before starting extraction.
        if await asyncio.to_thread(self._is_cancel_requested, job.id):
            await self._cancel_job_and_input(job)
            return

        timeout = job.timeout_seconds or settings.draft_parse_timeout_seconds
        try:
            extracted = await asyncio.wait_for(
                asyncio.to_thread(self._extract, input_record.storage_relpath),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            await self._fail_input_and_job(
                job, code=CODE_JOB_TIMEOUT, message="parse job exceeded its timeout"
            )
            return
        except DocumentExtractionError as exc:
            # DocumentExtractionService's own contract guarantees its message is
            # already a bounded, redacted "ClassName: reason" string, but this
            # processor treats extraction as an untrusted boundary and never
            # relies on that guarantee holding for every caller — only the
            # stable code is persisted here.
            await self._fail_input_and_job(
                job, code=exc.code or CODE_INPUT_PARSE_FAILED
            )
            return
        except Exception as exc:
            logger.error(
                "DraftJobProcessor: job id=%d extraction raised %s",
                job.id,
                type(exc).__name__,
            )
            await self._fail_input_and_job(job, code=CODE_INPUT_PARSE_FAILED)
            return

        # Cooperative cancellation check #2: immediately before committing.
        if await asyncio.to_thread(self._is_cancel_requested, job.id):
            await self._cancel_job_and_input(job)
            return

        try:
            over_limit = await asyncio.to_thread(
                self._exceeds_parsed_char_limit, job.draft_id, job.input_id, extracted
            )
        except Exception as exc:
            logger.error(
                "DraftJobProcessor: job id=%d could not evaluate the parsed-char limit (%s)",
                job.id,
                type(exc).__name__,
            )
            await self._fail_input_and_job(job, code=CODE_INTERNAL_ERROR)
            return

        if over_limit:
            await self._fail_over_limit(job, extracted.character_count)
            return

        # Permission re-check #2 (SPEC section 9.1 rule 4): re-verify
        # immediately before the final revision (parsed text) is stored, so a
        # revocation that lands mid-extraction still blocks the commit.
        if not await self._owner_permission_ok(job):
            await self._fail_input_and_job(job, code=CODE_PERMISSION_REVOKED)
            return

        try:
            committed = await asyncio.to_thread(self._commit_success, job, extracted)
        except Exception as exc:
            logger.error(
                "DraftJobProcessor: job id=%d could not commit parsed text (%s)",
                job.id,
                type(exc).__name__,
            )
            await self._fail_input_and_job(job, code=CODE_INTERNAL_ERROR)
            return

        if not committed:
            # A cancel landed inside the commit transaction: the extracted
            # text was discarded and the job/input settled cancelled there.
            self._publish_event(
                job, "job_cancelled", job_id=job.id, status="cancelled"
            )
            return

        logger.info("DraftJobProcessor: completed job id=%d", job.id)
        self._publish_event(job, "job_completed", job_id=job.id, status="completed")

    # ------------------------------------------------------------------
    # Per-job dispatch: compile
    # ------------------------------------------------------------------

    async def _dispatch_compile_job(self, job: "DraftJobRecord") -> None:
        """Dispatch one claimed ``compile`` job through ``draft_pipeline.run_compile``.

        ``run_compile`` is the sole editorial-pipeline entry point (owned by
        the pipeline module). It already persists terminal job/draft state
        (``failed``, ``cancelled``, ``completed``) itself before returning or
        raising :class:`draft_pipeline.CompileFailure`, so this method never
        re-derives or double-writes that state. Its only jobs are: the
        claim-time permission recheck (SPEC section 9.1 rule 4), deciding
        whether a retryable failure earns a bounded automatic retry (SPEC
        section 10.2), and publishing the terminal SSE notification.
        """
        # Permission re-check (SPEC section 9.1 rule 4): the creating user
        # must still be active and hold vault `read` right after the claim,
        # mirroring the parse_input path's first recheck.
        if not await self._owner_permission_ok(job):
            await asyncio.to_thread(
                self._fail_compile_job_sync, job, code=CODE_PERMISSION_REVOKED
            )
            self._publish_event(
                job,
                "job_failed",
                job_id=job.id,
                status="failed",
                error_code=CODE_PERMISSION_REVOKED,
                job_type="compile",
            )
            return

        deps = draft_pipeline.default_deps(engine=self._engine)
        try:
            await draft_pipeline.run_compile(
                job_id=job.id,
                pool=self._pool,
                deps=deps,
                worker_id=self._worker_id if _draft_lease_enabled() else None,
            )
        except draft_pipeline.CompileFailure as failure:
            if (
                failure.retryable
                and job.retry_count < settings.draft_transient_retry_limit
            ):
                try:
                    scheduled = await asyncio.to_thread(
                        self._schedule_compile_retry_sync, job
                    )
                except Exception:
                    logger.error(
                        "DraftJobProcessor: compile job id=%d automatic retry "
                        "scheduling raised unexpectedly",
                        job.id,
                    )
                    scheduled = False
                if scheduled:
                    logger.info(
                        "DraftJobProcessor: scheduled automatic retry for compile "
                        "job id=%d (retry %d/%d, code=%s)",
                        job.id,
                        job.retry_count + 1,
                        settings.draft_transient_retry_limit,
                        failure.code,
                    )
            # The failed job_id is terminal either way: a scheduled retry is a
            # brand-new child job (state machine forbids reviving this one),
            # and it will publish its own job_started/job_completed events
            # organically when this processor's poll loop claims it.
            #
            # A cancellation observed by run_compile settles the job/draft as
            # cancelled (not failed) and re-raises CompileFailure purely so
            # its code/retryable are available here; it must never be
            # retried and must surface as ``job_cancelled``, not
            # ``job_failed``, so this is the one code that maps to a
            # different terminal event type.
            if failure.code == draft_pipeline.CODE_JOB_CANCELLED:
                self._publish_event(
                    job,
                    "job_cancelled",
                    job_id=job.id,
                    status="cancelled",
                    job_type="compile",
                )
            else:
                self._publish_event(
                    job,
                    "job_failed",
                    job_id=job.id,
                    status="failed",
                    error_code=failure.code,
                    job_type="compile",
                )
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Defensive: run_compile's own job boundary already converts every
            # expected failure into CompileFailure. Anything else reaching
            # here is a bug in that boundary, not user input; _run_job's outer
            # net will also catch this, but fail explicitly here first so the
            # sanitized code (never str(exc)) is the one that gets persisted.
            logger.error(
                "DraftJobProcessor: compile job id=%d raised %s outside "
                "CompileFailure",
                job.id,
                type(exc).__name__,
            )
            raise

        logger.info("DraftJobProcessor: completed compile job id=%d", job.id)
        self._publish_event(
            job, "job_completed", job_id=job.id, status="completed", job_type="compile"
        )

    def _fail_compile_job_sync(self, job: "DraftJobRecord", *, code: str) -> None:
        """Fail a compile job before ``run_compile`` ever ran (e.g. permission recheck).

        Mirrors ``draft_pipeline._persist_failure``: sets the job terminal and
        moves the draft from ``queued``/``running`` to ``failed`` (SPEC
        section 10.3), since nothing else will settle the draft's state for a
        job that never reached the pipeline.
        """
        with self._pool.connection() as conn:
            store = DraftStore(conn)
            try:
                store.set_job_status(
                    job_id=job.id,
                    target="failed",
                    error_code=code,
                    **self._fence_kwargs(),
                )
            except Exception:
                logger.error(
                    "DraftJobProcessor: could not fail compile job id=%d", job.id
                )
            if conn.in_transaction:
                conn.rollback()
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT status FROM drafts WHERE id = ?", (job.draft_id,)
                ).fetchone()
                if row is not None and row[0] in ("queued", "running"):
                    conn.execute(
                        "UPDATE drafts SET status = 'failed', "
                        "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (job.draft_id,),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _schedule_compile_retry_sync(self, job: "DraftJobRecord") -> bool:
        """Insert a bounded automatic-retry child compile job (SPEC section 10.2).

        Runs inside one ``BEGIN IMMEDIATE`` transaction: move the draft back
        from ``failed`` to ``queued`` (SPEC section 10.3) and insert a new
        ``pending`` compile job carrying ``parent_job_id``/``attempt_no + 1``/
        ``retry_count + 1`` and the original request snapshot (``input_json``,
        ``brief_snapshot_json``, ``model_snapshot_json``,
        ``prompt_bundle_version``, ``compile_input_sha256``, budgets) — a
        terminal job is never mutated back to pending, matching how
        user-initiated retry creates a new job rather than reviving the old
        one. A fresh child job has no stage checkpoints of its own (they are
        recorded per ``job_id``), so it reruns the pipeline from Intake;
        that is safe and still bounded by the same per-job budgets.

        Returns:
            True if the retry job was inserted; False if the draft was no
            longer in the expected ``failed`` state (e.g. raced by a manual
            retry, a delete, or an archive) and no-ops were made instead.
        """
        with self._pool.connection() as conn:
            if conn.in_transaction:
                conn.rollback()
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT status FROM drafts WHERE id = ?", (job.draft_id,)
                ).fetchone()
                if row is None or row[0] != "failed":
                    conn.rollback()
                    return False
                conn.execute(
                    "UPDATE drafts SET status = 'queued', "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (job.draft_id,),
                )
                conn.execute(
                    "INSERT INTO draft_jobs ("
                    "draft_id, vault_id, created_by, job_type, parent_job_id, "
                    "attempt_no, retry_count, input_json, brief_snapshot_json, "
                    "model_snapshot_json, prompt_bundle_version, "
                    "compile_input_sha256, max_model_calls, timeout_seconds"
                    ") SELECT draft_id, vault_id, created_by, 'compile', id, "
                    "attempt_no + 1, retry_count + 1, input_json, "
                    "brief_snapshot_json, model_snapshot_json, "
                    "prompt_bundle_version, compile_input_sha256, "
                    "max_model_calls, timeout_seconds "
                    "FROM draft_jobs WHERE id = ?",
                    (job.id,),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return True

    # ------------------------------------------------------------------
    # Synchronous DB/filesystem helpers (each run in a thread; each opens
    # and releases exactly one connection)
    # ------------------------------------------------------------------

    def _get_input(self, job: "DraftJobRecord"):
        with self._pool.connection() as conn:
            store = DraftStore(conn)
            return store.get_input(
                draft_id=job.draft_id, owner_id=job.created_by, input_id=job.input_id
            )

    def _move_input_to_parsing(self, input_id: int) -> None:
        with self._pool.connection() as conn:
            store = DraftStore(conn)
            store.set_input_parse_status(input_id=input_id, target="parsing")

    def _is_cancel_requested(self, job_id: int) -> bool:
        with self._pool.connection() as conn:
            return DraftStore(conn).is_cancel_requested(job_id)

    async def _owner_permission_ok(self, job: "DraftJobRecord") -> bool:
        """Re-check that the job's owner is still active and can read the vault.

        Runs entirely inside a single ``asyncio.to_thread`` call so the
        pooled connection it opens is acquired and released synchronously in
        the worker thread — it never spans an ``await`` in this coroutine's
        frame, matching every other DB step in this file.
        """
        return await asyncio.to_thread(self._owner_permission_ok_sync, job)

    def _owner_permission_ok_sync(self, job: "DraftJobRecord") -> bool:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT is_active, role FROM users WHERE id = ?", (job.created_by,)
            ).fetchone()
            if row is None or not row[0]:
                return False
            principal = {"id": job.created_by, "role": row[1]}
            # ``_evaluate_policy`` is app.api.deps's real permission evaluator
            # (superadmin -> admin baseline -> vault_members ->
            # vault_group_access -> vault visibility). It is async because it
            # awaits asyncio.to_thread internally for its own DB reads, so it
            # is driven here via asyncio.run() inside this already-dedicated
            # worker thread (started by asyncio.to_thread above) — no event
            # loop is running in this thread, and the connection is opened
            # and closed entirely within this synchronous call, never
            # spanning an await in the caller's coroutine.
            return asyncio.run(
                _evaluate_policy(conn, principal, "vault", job.vault_id, "read")
            )

    def _extract(self, storage_relpath: str):
        """Resolve the stored path and run the (blocking) extraction. Runs in a thread."""
        path = self._storage.resolve(storage_relpath)
        return self._extraction.extract_text(path)

    def _exceeds_parsed_char_limit(self, draft_id: int, input_id: int, extracted) -> bool:
        with self._pool.connection() as conn:
            store = DraftStore(conn)
            total = store.total_parsed_chars(draft_id, excluding_input_id=input_id)
        return (total + extracted.character_count) > settings.draft_max_total_parsed_chars

    def _commit_success(self, job: "DraftJobRecord", extracted) -> bool:
        """Commit the parsed output, honoring a cancel raced at the wire.

        The ``cancel_requested_at`` re-check runs INSIDE the single
        ``BEGIN IMMEDIATE`` transaction that persists the input's outcome and
        the job's terminal state (issue #516 DRAFT-005). Cooperative check #2
        happens before the char-limit and permission awaits, so a cancel
        landing after it used to be overwritten by this success commit,
        persisting post-cancel output as ``status='completed'``. Under the
        write lock the read and the writes are serialized with any concurrent
        ``request_job_cancel``: a cancel committed first is observed and
        settled here (mirroring ``_cancel_job_and_input_sync``'s input+job
        cancellation settlement, extracted text discarded); a cancel arriving
        later finds the job already terminal.

        Returns:
            True when the success path ran; False when the run settled as
            cancelled instead.
        """
        with self._pool.connection() as conn:
            if conn.in_transaction:
                conn.rollback()
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT status, cancel_requested_at FROM draft_jobs WHERE id = ?",
                    (job.id,),
                ).fetchone()
                if row is not None and row[1] is not None:
                    # Same row effects as _cancel_job_and_input_sync's store
                    # calls, kept inside this transaction so the input's and
                    # the job's terminal outcomes settle together.
                    conn.execute(
                        "UPDATE draft_inputs SET parse_status = 'cancelled', "
                        "parse_error = NULL, updated_at = CURRENT_TIMESTAMP "
                        "WHERE id = ?",
                        (job.input_id,),
                    )
                    conn.execute(
                        "UPDATE draft_jobs SET status = 'cancelled', "
                        "error_code = NULL, error_message = NULL, "
                        "heartbeat_at = CURRENT_TIMESTAMP, "
                        "completed_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (job.id,),
                    )
                    # The same durable ``job_cancelled`` audit row the API
                    # cancel path writes (``request_job_cancel``), so the
                    # draft's event ledger carries the cancellation even when
                    # it is this transaction that settles it (issue #532
                    # review, PRR-022). Same event type and payload shape;
                    # the worker has no requesting-actor context, so the job's
                    # own creator stands in as the actor, exactly as the
                    # pipeline does for its revision events.
                    DraftStore(conn)._insert_event(
                        draft_id=job.draft_id,
                        event_type="job_cancelled",
                        actor_user_id=job.created_by,
                        job_id=job.id,
                        payload={"prior_status": row[0]},
                    )
                    conn.commit()
                    return False
                # Byte-identical row effects to the previous
                # set_input_parse_status('ready') + set_job_status('completed')
                # pair, now inside this transaction so the cancel check above
                # is serialized with them.
                conn.execute(
                    "UPDATE draft_inputs SET parse_status = 'ready', "
                    "parsed_text = ?, parsed_text_sha256 = ?, "
                    "parsed_char_count = ?, parse_error = NULL, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (
                        extracted.text,
                        sha256_text(extracted.text),
                        extracted.character_count,
                        job.input_id,
                    ),
                )
                success_params: list = [job.id]
                success_sql = (
                    "UPDATE draft_jobs SET status = 'completed', "
                    "error_code = NULL, error_message = NULL, "
                    "progress_percent = 100.0, "
                    "heartbeat_at = CURRENT_TIMESTAMP, "
                    "completed_at = CURRENT_TIMESTAMP WHERE id = ?"
                )
                if _draft_lease_enabled():
                    # Lease fence (issue #559 stage 4): a parse worker whose
                    # lease expired mid-extraction must not commit success
                    # over a row the janitor requeued or a new claimant owns.
                    # DISTINCT from the cancel-race False above: rowcount 0
                    # here raises so no completion is published at all.
                    success_sql += " AND worker_id = ? AND status = 'running'"
                    success_params.append(self._worker_id)
                success_cursor = conn.execute(success_sql, success_params)  # nosec B608 — fence fragment is a fixed literal
                if _draft_lease_enabled() and success_cursor.rowcount == 0:
                    raise _LeaseLostError(
                        f"job {job.id} no longer owned by worker "
                        f"{self._worker_id!r}"
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return True

    def _fail_input_and_job_sync(
        self, job: "DraftJobRecord", *, code: str, message: Optional[str]
    ) -> None:
        with self._pool.connection() as conn:
            store = DraftStore(conn)
            store.set_input_parse_status(
                input_id=job.input_id, target="failed", parse_error=code
            )
            store.set_job_status(
                job_id=job.id,
                target="failed",
                error_code=code,
                error_message=message,
                **self._fence_kwargs(),
            )

    def _fail_job_sync(self, job: "DraftJobRecord", *, code: str) -> None:
        with self._pool.connection() as conn:
            store = DraftStore(conn)
            store.set_job_status(
                job_id=job.id,
                target="failed",
                error_code=code,
                **self._fence_kwargs(),
            )

    def _fail_over_limit_sync(self, job: "DraftJobRecord", char_count: int) -> None:
        with self._pool.connection() as conn:
            store = DraftStore(conn)
            store.set_input_parse_status(
                input_id=job.input_id,
                target="failed",
                parsed_char_count=char_count,
                parse_error=CODE_PARSED_TEXT_LIMIT_EXCEEDED,
            )
            store.set_job_status(
                job_id=job.id,
                target="failed",
                error_code=CODE_PARSED_TEXT_LIMIT_EXCEEDED,
                **self._fence_kwargs(),
            )

    def _cancel_job_and_input_sync(self, job: "DraftJobRecord") -> None:
        with self._pool.connection() as conn:
            store = DraftStore(conn)
            # The input may already be pending/parsing; only 'parsing' can move
            # to 'cancelled' via the ordinary table, which is the state it is
            # in at this point in the flow.
            store.set_input_parse_status(input_id=job.input_id, target="cancelled")
            store.set_job_status(
                job_id=job.id, target="cancelled", **self._fence_kwargs()
            )

    # ------------------------------------------------------------------
    # Async wrappers that persist then publish (publish only after commit)
    # ------------------------------------------------------------------

    async def _fail_job(self, job: "DraftJobRecord", *, code: str) -> None:
        await asyncio.to_thread(self._fail_job_sync, job, code=code)
        self._publish_event(job, "job_failed", job_id=job.id, status="failed", error_code=code)

    async def _fail_input_and_job(
        self, job: "DraftJobRecord", *, code: str, message: Optional[str] = None
    ) -> None:
        await asyncio.to_thread(
            self._fail_input_and_job_sync, job, code=code, message=message
        )
        self._publish_event(job, "job_failed", job_id=job.id, status="failed", error_code=code)

    async def _fail_over_limit(self, job: "DraftJobRecord", char_count: int) -> None:
        await asyncio.to_thread(self._fail_over_limit_sync, job, char_count)
        self._publish_event(
            job,
            "job_failed",
            job_id=job.id,
            status="failed",
            error_code=CODE_PARSED_TEXT_LIMIT_EXCEEDED,
        )

    async def _cancel_job_and_input(self, job: "DraftJobRecord") -> None:
        await asyncio.to_thread(self._cancel_job_and_input_sync, job)
        self._publish_event(job, "job_cancelled", job_id=job.id, status="cancelled")

    # ------------------------------------------------------------------
    # SSE
    # ------------------------------------------------------------------

    def _publish_event(self, job: "DraftJobRecord", event_type: str, **fields) -> None:
        """Publish an SSE event after a commit. Never raises out of the caller."""
        try:
            event = build_event(event_type, draft_id=job.draft_id, **fields)
            get_draft_event_bus().publish(job.draft_id, event)
        except Exception:
            logger.warning(
                "DraftJobProcessor: event publish failed (job_id=%s draft_id=%s "
                "event=%s)",
                job.id,
                job.draft_id,
                event_type,
                exc_info=True,
            )
