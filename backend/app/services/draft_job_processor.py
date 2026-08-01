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
  immediately before committing parsed text; observed cancellation discards
  the extracted text rather than persisting it. For compile jobs,
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
from app.services.document_extraction import DocumentExtractionError
from app.services.draft_events import build_event, get_draft_event_bus
from app.services.draft_store import DraftNotFoundError, DraftStore, sha256_text

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
        # Strong references to detached background tasks so CPython does not
        # garbage-collect them mid-flight, mirroring WikiCompileProcessor.
        self._bg_tasks: set[asyncio.Task] = set()

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
        self._running = True
        try:
            # Startup recovery must complete before the poll loop begins and
            # before HTTP traffic is accepted (SPEC section 10.1 item 6).
            await asyncio.to_thread(self._recover_on_startup)
            self._task = asyncio.create_task(self._poll_loop())
        except BaseException:
            # The caller wraps this in a timeout and swallows the result, so
            # without this reset a cancelled recovery would leave _running=True
            # with no poll loop: a processor that reports started, accepts
            # stop(), and silently never runs a job. CancelledError is a
            # BaseException, hence the broad catch.
            self._running = False
            raise
        logger.info("DraftJobProcessor started")

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
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
                await self._run_job(job)
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

    def _claim_next_job(self) -> Optional["DraftJobRecord"]:
        with self._pool.connection() as conn:
            store = DraftStore(conn)
            job = store.claim_next_parse_job()
            if job is not None:
                return job
            return store.claim_next_compile_job()

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
            await asyncio.to_thread(self._commit_success, job, extracted)
        except Exception as exc:
            logger.error(
                "DraftJobProcessor: job id=%d could not commit parsed text (%s)",
                job.id,
                type(exc).__name__,
            )
            await self._fail_input_and_job(job, code=CODE_INTERNAL_ERROR)
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
            await draft_pipeline.run_compile(job_id=job.id, pool=self._pool, deps=deps)
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
                store.set_job_status(job_id=job.id, target="failed", error_code=code)
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

    def _commit_success(self, job: "DraftJobRecord", extracted) -> None:
        with self._pool.connection() as conn:
            store = DraftStore(conn)
            store.set_input_parse_status(
                input_id=job.input_id,
                target="ready",
                parsed_text=extracted.text,
                parsed_text_sha256=sha256_text(extracted.text),
                parsed_char_count=extracted.character_count,
            )
            store.set_job_status(job_id=job.id, target="completed", progress_percent=100.0)

    def _fail_input_and_job_sync(
        self, job: "DraftJobRecord", *, code: str, message: Optional[str]
    ) -> None:
        with self._pool.connection() as conn:
            store = DraftStore(conn)
            store.set_input_parse_status(
                input_id=job.input_id, target="failed", parse_error=code
            )
            store.set_job_status(
                job_id=job.id, target="failed", error_code=code, error_message=message
            )

    def _fail_job_sync(self, job: "DraftJobRecord", *, code: str) -> None:
        with self._pool.connection() as conn:
            store = DraftStore(conn)
            store.set_job_status(job_id=job.id, target="failed", error_code=code)

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
            )

    def _cancel_job_and_input_sync(self, job: "DraftJobRecord") -> None:
        with self._pool.connection() as conn:
            store = DraftStore(conn)
            # The input may already be pending/parsing; only 'parsing' can move
            # to 'cancelled' via the ordinary table, which is the state it is
            # in at this point in the flow.
            store.set_input_parse_status(input_id=job.input_id, target="cancelled")
            store.set_job_status(job_id=job.id, target="cancelled")

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
