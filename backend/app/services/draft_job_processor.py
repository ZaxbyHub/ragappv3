"""DraftJobProcessor: asyncio background worker that drains ``draft_jobs``.

PR 1 of Draft Room (issue #435, ``specs/draft-room/SPEC.md`` section 10)
dispatches only ``parse_input`` jobs; ``compile`` dispatch arrives in a later
issue. Modeled on :class:`app.services.wiki_compile_processor.WikiCompileProcessor`
but async because later dispatch work awaits model calls.

Hard invariants (SPEC section 10.1):

* Never hold a SQLite connection or transaction across parsing, filesystem
  I/O, or an ``await``. Every DB step acquires a connection via
  ``self._pool.connection()``, does its work, and releases it before the next
  blocking or async step.
* Claim jobs atomically through ``DraftStore.claim_next_parse_job`` (uses
  ``BEGIN IMMEDIATE``) so two processors can never double-run a job.
* Cooperative cancellation is checked before starting extraction and again
  immediately before committing parsed text; observed cancellation discards
  the extracted text rather than persisting it.
* Failure codes are drawn from a small stable set and never carry raw
  exception text, response bodies, request content, manuscript text, prompts,
  headers, or absolute paths.
* SSE events are published only after the state-changing transaction commits,
  and a publish failure must never fail the job.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

from app.api.deps import _evaluate_policy
from app.config import settings
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
# error code from this module.
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
    ) -> None:
        self._pool = pool
        self._storage = storage
        self._extraction = extraction
        self._poll_interval = (
            poll_interval
            if poll_interval is not None
            else settings.draft_poll_interval_seconds
        )
        self._running = False
        self._task: Optional[asyncio.Task] = None
        # Strong references to detached background tasks so CPython does not
        # garbage-collect them mid-flight, mirroring WikiCompileProcessor.
        self._bg_tasks: set[asyncio.Task] = set()

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
            return DraftStore(conn).claim_next_parse_job()

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

        if job.job_type != "parse_input":
            # PR 1 dispatches only parse_input; anything else here indicates a
            # store/claim invariant violation, not user input.
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
