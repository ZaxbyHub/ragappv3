"""
WikiCompileProcessor: asyncio background worker that drains wiki_compile_jobs.

Single worker per process. Polls every POLL_INTERVAL seconds.
On startup, resets orphaned 'running' jobs back to 'pending' (crash recovery).
All DB work runs in threads via asyncio.to_thread() since SQLite is sync.
"""

import asyncio
import functools
import json
import logging
from typing import TYPE_CHECKING, Optional

from app.config import settings
from app.services.admission import AdmissionClass, get_admission_controller
from app.services.job_lease import JobLease

if TYPE_CHECKING:
    from app.models.database import SQLiteConnectionPool

logger = logging.getLogger(__name__)

POLL_INTERVAL = 5        # seconds between polls when queue is empty
MAX_RETRIES = 3           # max automatic retries before a job is permanently failed
RESET_RETRY_ATTEMPTS = 3  # attempts to recover a job from 'failed' → 'pending' after a transient DB error
RESET_RETRY_BASE_DELAY = 0.5  # seconds; doubled per attempt (0.5, 1.0, 2.0)

# Issue #792: the error code handed to _fail_job and (lease mode) the
# job_failed SSE event when a dispatch raises. Reader-visible surfaces (GET
# /wiki/jobs, the event stream) carry this stable code only — the full
# exception text stays in the server log via logger.exception above. Same
# convention as the #562 ingest error codes and draft_job_processor's
# CODE_* set; the lease cap settles it as
# "attempt_cap_exceeded: <this code>".
FAILED_ERROR_CODE = "COMPILE_FAILED: wiki compile dispatch failed; see server logs"

# Issue #792 feedback: the error code for a claimed job whose payload_json
# degraded at claim (unparseable or non-object JSON). Such a row is settled
# terminally failed without dispatch; unlike a dispatch failure it is never
# requeued, so the lease cap composition does not apply.
PAYLOAD_MALFORMED_ERROR = (
    "PAYLOAD_MALFORMED: compile job payload is corrupt; see server logs"
)

# Issue #955: on start(), re-announce wiki jobs that went terminal within
# this window so a shutdown/crash between a terminal commit and its event
# publish cannot strand the SSE UI on stale state (bounded: consumers are
# refetch-driven, a duplicate terminal event is a benign extra refetch).
WIKI_EVENT_RECONCILE_WINDOW_SECONDS = 30

# PRR-004: the deferred fan-out delay — long enough for SSE clients to
# reconnect after a restart, short enough that the UI converges quickly.
_RECONCILE_DELAY_SECONDS = 5.0

# PRR-006: bound the boot scans so the cost is O(window), not O(table).
_RECONCILE_MAX_ROWS = 500


def _lease_enabled() -> bool:
    """Stage-2 lease switch (issue #559): env-only, process-start-stable."""
    return bool(getattr(settings, "wiki_kms_job_lease_enabled", False))


class WikiCompileProcessor:
    """
    Background worker that processes wiki_compile_jobs from the SQLite queue.

    One worker per process. A connection is acquired per-job and released
    before each sleep, so it never holds a connection across the idle period.
    """

    def __init__(self, pool: "SQLiteConnectionPool") -> None:
        self._pool = pool
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._generation = 0
        self._startup_reset_task: Optional[asyncio.Task] = None
        # Lease-mode worker identity (issue #559 stage 2): every claim/
        # complete/fail is fenced on this id, so a reclaimed worker's late
        # write can never mutate the new owner's row.
        self._worker_id = f"wiki-compile-{id(self)}"
        # Issue #955 review: the app event loop, captured at start() so the
        # threaded fail-and-publish / boot-reconcile units can marshal their
        # event publishes onto the loop the bus and SSE subscribers run on
        # (the bus's put_nowait is same-loop only).
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Strong references to detached background tasks (e.g. delayed
        # auto-retry resets) so CPython does not garbage-collect them
        # mid-flight (issue #276 E2-3). Tasks remove themselves on completion
        # via a done-callback.
        self._bg_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        if self._running:
            return
        self._generation += 1
        generation = self._generation
        self._running = True
        self._loop = asyncio.get_running_loop()
        try:
            # The startup reset task is created unconditionally in BOTH modes
            # so the lifecycle contract holds (reset task published first,
            # before the poll task — pinned by
            # tests/test_compile_processor_startup_lifecycle.py). In lease
            # mode the reset BODY no-ops: the janitor owns orphan
            # settlement, and the blanket orphan reset must not run.
            reset_task = self._startup_reset_task
            if reset_task is None or reset_task.done():
                reset_coro = asyncio.to_thread(self._startup_reset_and_reconcile)
                try:
                    reset_task = asyncio.create_task(reset_coro)
                except BaseException:
                    reset_coro.close()
                    raise
                self._startup_reset_task = reset_task
                reset_task.add_done_callback(self._consume_startup_reset)
            # A thread-backed reset cannot be cancelled; sharing this task
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
            # A cancelled or failed orphan reset must not leave a false-running
            # processor that prevents the next startup attempt from retrying.
            if generation == self._generation:
                self._running = False
                self._task = None
            raise
        logger.info("WikiCompileProcessor started")

    async def stop(self) -> None:
        self._generation += 1
        self._running = False
        task = self._task
        self._task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        # Cancel any in-flight detached per-job reset tasks so they do not
        # outlive the processor (issue #276 E2-3). Startup reset ownership is
        # intentionally retained above so a retry cannot overlap its thread.
        for bg in list(self._bg_tasks):
            bg.cancel()
        for bg in list(self._bg_tasks):
            try:
                await bg
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug(
                    "WikiCompileProcessor: detached task failed during shutdown",
                    exc_info=True,
                )
        self._bg_tasks.clear()
        logger.info("WikiCompileProcessor stopped")

    def _consume_startup_reset(self, task: asyncio.Task) -> None:
        """Observe detached reset failures and clear only the owned task."""
        try:
            if not task.cancelled():
                error = task.exception()
                if error is not None:
                    logger.debug(
                        "WikiCompileProcessor startup reset failed",
                        exc_info=(type(error), error, error.__traceback__),
                    )
        except asyncio.CancelledError:
            pass
        except BaseException:
            logger.debug(
                "WikiCompileProcessor startup reset result could not be consumed",
                exc_info=True,
            )
        if getattr(self, "_startup_reset_task", None) is task:
            self._startup_reset_task = None

    # ------------------------------------------------------------------
    # Startup orphan recovery
    # ------------------------------------------------------------------

    def _reset_orphans(self) -> None:
        if _lease_enabled():
            # Lease mode (issue #559 stage 2): the janitor owns expired-lease
            # settlement; the blanket orphan reset must not strip live
            # leases. The task itself still exists so the startup lifecycle
            # (reset-before-poll ordering) is identical in both modes.
            logger.debug("WikiCompileProcessor: lease mode; startup orphan reset skipped")
            return
        from app.services.wiki_store import WikiStore

        with self._pool.connection() as conn:
            n = WikiStore(conn).reset_running_jobs()
        if n:
            logger.warning("WikiCompileProcessor: reset %d orphaned running jobs to pending", n)

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                job = await asyncio.to_thread(self._claim_next_job)
                if job is None:
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                # Issue #792 feedback: a row whose payload degraded to the
                # empty mapping at claim (unparseable or non-object
                # payload_json — every writer stores a dict carrying
                # vault_id) can never be dispatched meaningfully. Settle it
                # terminally failed instead of dispatching it as a
                # vault-less "manual" job, which would complete as a false
                # success.
                if job.vault_id is None:
                    logger.warning(
                        "WikiCompileProcessor: job id=%d has a malformed "
                        "payload (no vault_id); settling failed",
                        job.id,
                    )
                    settled = await asyncio.to_thread(
                        self._settle_malformed_payload, job
                    )
                    if settled:
                        self._publish_event(
                            job, "job_failed", error=PAYLOAD_MALFORMED_ERROR
                        )
                    continue

                logger.info(
                    "WikiCompileProcessor: claimed job id=%d type=%s vault=%s",
                    job.id,
                    job.trigger_type,
                    job.vault_id,
                )

                # Lease mode (issue #559 stage 2): renew the lease while the
                # dispatch runs so a slow-but-alive worker never looks dead;
                # every settlement write is fenced on the worker id either way.
                heartbeat_task: Optional[asyncio.Task] = None
                if _lease_enabled():
                    heartbeat_task = asyncio.create_task(
                        self._heartbeat_loop(job.id, self._worker_id),
                        name=f"wiki-heartbeat-{job.id}",
                    )
                try:
                    # E3 admission (issue #518): background budget for compile
                    # work; rejection flows into the bounded retry handler.
                    async with get_admission_controller().admit(
                        AdmissionClass.BACKGROUND, foreground=False
                    ):
                        result = await asyncio.to_thread(self._dispatch, job)
                    if result.get("cancelled") == "wiki_compile_disabled":
                        # WIKI-001 (#515): the flags went off between enqueue
                        # and claim — _dispatch already marked the job
                        # cancelled; do not try to complete it (complete_job
                        # would no-op on the cancelled row anyway) and surface
                        # the terminal state as a cancellation, not a
                        # completion.
                        logger.info(
                            "WikiCompileProcessor: cancelled job id=%d (wiki compile disabled at dispatch)",
                            job.id,
                        )
                        self._publish_event(job, "job_cancelled", result=result)
                    else:
                        completed = await asyncio.to_thread(
                            self._complete_job, job.id, result, self._worker_id
                        )
                        if not completed:
                            # The fenced complete was rejected: either the
                            # lease was lost (janitor reclaimed) or a cancel
                            # won the race. The row's terminal state was set
                            # by whoever owns it now — publishing
                            # "completed" here would be wrong (and would
                            # mislead operators) in both cases.
                            logger.info(
                                "WikiCompileProcessor: job id=%d complete "
                                "rejected (lease lost or cancelled); no "
                                "completion published",
                                job.id,
                            )
                        else:
                            logger.info("WikiCompileProcessor: completed job id=%d", job.id)
                            self._publish_event(job, "job_completed", result=result)
                except Exception as exc:
                    logger.exception(
                        "WikiCompileProcessor: job id=%d failed: %s", job.id, exc
                    )
                    try:
                        # Issue #955: the terminal commit AND its event
                        # publish run as ONE threaded unit — the publish
                        # used to sit after the await in the poll task, so
                        # a stop() cancellation delivered at that await
                        # committed the terminal row but skipped the event
                        # (executor threads are not interrupted by task
                        # cancellation, so both steps complete together).
                        #
                        # PRR-020: the legacy auto-retry scheduling rides a
                        # done-callback on the same future instead of the
                        # post-await code path, so a stop() cancellation
                        # delivered at the await can no longer discard it —
                        # the callback fires on the loop when the executor
                        # thread finishes, cancelled poll task or not.
                        unit = asyncio.ensure_future(
                            asyncio.to_thread(
                                self._fail_job_and_publish, job, self._worker_id
                            )
                        )

                        def _schedule_legacy_retry(
                            _fut, _job_id=job.id
                        ) -> None:
                            try:
                                spawn_reset, retry_count = _fut.result()
                            except Exception:  # noqa: BLE001 — logged by caller
                                return
                            if not spawn_reset:
                                return
                            backoff = 2.0 ** retry_count
                            logger.info(
                                "WikiCompileProcessor: job id=%d will auto-retry (%d/%d) in %.0fs",
                                _job_id, retry_count, MAX_RETRIES, backoff,
                            )
                            # Detach the backoff+reset so the single poll loop
                            # can keep claiming other pending jobs instead of
                            # head-of-line blocking them for the full backoff
                            # (issue #276 E2-3). The task is retained in
                            # self._bg_tasks so it is not GC'd mid-flight; the
                            # A6-1 status guard makes a cancel-during-delay a
                            # safe no-op.
                            self._spawn_delayed_reset(_job_id, backoff)

                        unit.add_done_callback(_schedule_legacy_retry)
                        await asyncio.shield(unit)
                    except Exception as e2:
                        logger.error(
                            "WikiCompileProcessor: could not mark job id=%d failed: %s", job.id, e2
                        )
                finally:
                    if heartbeat_task is not None:
                        heartbeat_task.cancel()
                        await asyncio.gather(heartbeat_task, return_exceptions=True)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("WikiCompileProcessor: poll loop error: %s", exc)
                await asyncio.sleep(POLL_INTERVAL)

    # ------------------------------------------------------------------
    # Synchronous helpers (run in thread)
    # ------------------------------------------------------------------

    def _claim_next_job(self):
        from app.services.wiki_store import WikiStore

        with self._pool.connection() as conn:
            return WikiStore(conn).claim_next_pending_job(self._worker_id)

    def _complete_job(
        self, job_id: int, result: dict, worker_id: Optional[str] = None
    ) -> bool:
        from app.services.wiki_store import WikiStore

        with self._pool.connection() as conn:
            return WikiStore(conn).complete_job(job_id, result, worker_id=worker_id)

    def _settle_malformed_payload(self, job) -> bool:
        """Terminally fail a claimed job whose payload_json was degraded.

        Lease mode fences the terminal write on the claiming worker id;
        legacy mode's ``fail_job`` UPDATE is terminal by construction. The
        row is never requeued: a corrupt payload cannot succeed on retry.
        """
        from app.services.job_lease import JobLease
        from app.services.wiki_store import WikiStore

        with self._pool.connection() as conn:
            if _lease_enabled():
                return bool(
                    JobLease(conn).fail(
                        job.id, self._worker_id, PAYLOAD_MALFORMED_ERROR
                    )
                )
            WikiStore(conn).fail_job(
                job.id, PAYLOAD_MALFORMED_ERROR, worker_id=self._worker_id
            )
            return True

    def _fail_job_and_publish(self, job, worker_id: str) -> tuple[bool, int]:
        """Fail the job and publish its terminal event in ONE threaded unit.

        Issue #955: runs ``_fail_job`` (the terminal commit) and, when the
        row settles terminally, the ``job_failed`` publish together inside
        the worker thread the poll loop awaits — a task cancellation
        delivered at the ``await asyncio.to_thread(...)`` can no longer
        land between the two (executor threads run to completion).
        Returns ``(spawn_reset, new_retry_count)``: ``spawn_reset`` is True
        only for the legacy-mode below-cap retry arm, whose delayed reset
        is asyncio work the caller must schedule.
        """
        new_retry_count = self._fail_job(
            job.id, FAILED_ERROR_CODE, worker_id
        )
        if _lease_enabled():
            # fail_job already requeued the row with a durable
            # ``run_after`` backoff, or settled it terminally
            # at jobs_max_attempts — no detached reset here.
            if new_retry_count >= max(int(settings.jobs_max_attempts), 1):
                self._marshal_publish(
                    functools.partial(
                        self._publish_event,
                        job,
                        "job_failed",
                        error=FAILED_ERROR_CODE,
                    )
                )
            return False, new_retry_count
        if new_retry_count < MAX_RETRIES:
            return True, new_retry_count
        logger.error(
            "WikiCompileProcessor: job id=%d permanently failed after %d retries",
            job.id, new_retry_count,
        )
        self._marshal_publish(
            functools.partial(
                self._publish_event,
                job,
                "job_failed",
                error=FAILED_ERROR_CODE,
            )
        )
        return False, new_retry_count

    def _startup_reset_and_reconcile(self) -> None:
        """Startup thread body: the orphan reset, then the #955 terminal-
        event reconcile — ONE threaded unit so start() keeps exactly one
        offload call (the lifecycle contract) and the reconcile runs
        before the poll loop starts."""
        self._reset_orphans()
        self._reconcile_recent_terminal_events()

    def _reconcile_recent_terminal_events(self) -> int:
        """Re-announce wiki jobs that reached a terminal state recently.

        Issue #955: a worker stopped (or crashed) between a terminal commit
        and its event publish leaves a terminal row the SSE UI never hears
        about — consumers only refetch on events, and the SSE route sends no
        state on (re)connect. On start, re-publish the terminal event for
        every wiki job that went terminal within the reconcile window, in
        both stores (the jobs table serves lease mode, the legacy table
        serves lease-off mode).

        PRR-004 review fix: the fan-out is DEFERRED past startup
        (``call_soon_threadsafe`` + ``call_later``) because this method runs
        inside ASGI lifespan startup, when no SSE subscriber can exist yet —
        the bus drops publishes with an empty subscriber set, so a boot-time
        fan-out reached nobody. Deferring by ``_RECONCILE_DELAY_SECONDS``
        lets clients reconnect and then refetches them.

        PRR-006 review fix: both scans are bounded by ``_RECONCILE_MAX_ROWS``
        so the cost is O(window), not O(table), and the startup critical path
        stays inside its budget.

        Rows that DID get their event are re-announced too: consumers are
        refetch-driven, so a duplicate terminal event is a benign extra
        refetch (disclosed in the release note). Best-effort — never raises
        into start().
        """
        announced = 0
        try:
            from app.services.wiki_events import get_wiki_event_bus

            event_for_status = {
                "completed": "job_completed",
                "failed": "job_failed",
                "cancelled": "job_cancelled",
            }
            cutoff = f"-{WIKI_EVENT_RECONCILE_WINDOW_SECONDS} seconds"
            with self._pool.connection() as conn:
                # jobs rows carry the domain fields in payload_json (with
                # the #792 json_valid guard); the legacy table has real
                # columns. Rows without a readable vault_id cannot be
                # fanned out to a bus subscriber and are skipped.
                rows = conn.execute(
                    "SELECT id, status, completed_at, "
                    "CASE WHEN json_valid(payload_json) "
                    "THEN CAST(json_extract(payload_json, '$.vault_id') "
                    "AS INTEGER) END AS vault_id, "
                    "CASE WHEN json_valid(payload_json) "
                    "THEN json_extract(payload_json, '$.trigger_type') "
                    "END AS trigger_type "
                    "FROM jobs WHERE queue = 'wiki' "
                    "AND status IN ('completed','failed','cancelled') "
                    "AND completed_at IS NOT NULL "
                    # datetime() normalizes BOTH stored formats (SQLite
                    # CURRENT_TIMESTAMP space-separated and Python
                    # isoformat 'T'-separated) so the window compares
                    # time-of-day, not byte order (review finding 1).
                    "AND datetime(completed_at) >= datetime('now', ?) "
                    "AND CASE WHEN json_valid(payload_json) "
                    "THEN CAST(json_extract(payload_json, '$.vault_id') "
                    "AS INTEGER) END IS NOT NULL "
                    # PRR-006: bound the scan so the cost is O(window), not
                    # O(table) — this runs on the startup critical path.
                    "ORDER BY completed_at DESC LIMIT ?",
                    (cutoff, _RECONCILE_MAX_ROWS),
                ).fetchall()
                legacy_rows = conn.execute(
                    "SELECT id, status, vault_id, trigger_type, completed_at "
                    "FROM wiki_compile_jobs "
                    "WHERE status IN ('completed','failed','cancelled') "
                    "AND completed_at IS NOT NULL "
                    "AND datetime(completed_at) >= datetime('now', ?) "
                    "ORDER BY completed_at DESC LIMIT ?",
                    (cutoff, _RECONCILE_MAX_ROWS),
                ).fetchall()
            bus = get_wiki_event_bus()
            loop = self._loop
            payloads = []
            for row in (*rows, *legacy_rows):
                event_type = event_for_status.get(row["status"])
                if event_type is None:
                    continue
                payloads.append(
                    (
                        row["vault_id"],
                        {
                            "type": event_type,
                            "job_id": row["id"],
                            "vault_id": row["vault_id"],
                            "trigger_type": row["trigger_type"] or "manual",
                        },
                    )
                )
            if payloads and loop is not None and loop.is_running():
                # PRR-004: defer the fan-out past lifespan startup so
                # reconnected SSE subscribers actually receive it. PRR-016:
                # count only what was handed to the loop, and log the drop.
                try:
                    # Two hops: the thread-safe hop onto the loop, then the
                    # delay (call_later is loop-only, so it must run ON the
                    # loop) so the fan-out lands after SSE clients reconnect.
                    loop.call_soon_threadsafe(
                        functools.partial(
                            loop.call_later,
                            _RECONCILE_DELAY_SECONDS,
                            functools.partial(
                                self._drain_reconciled_events, bus, payloads
                            ),
                        )
                    )
                    announced = len(payloads)
                except RuntimeError:
                    logger.warning(
                        "WikiCompileProcessor: terminal-event reconcile dropped"
                        " %d event(s) — event loop closed before the fan-out"
                        " could be scheduled",
                        len(payloads),
                    )
            elif payloads:
                logger.warning(
                    "WikiCompileProcessor: terminal-event reconcile dropped %d"
                    " event(s) — no running event loop to fan out on",
                    len(payloads),
                )
        except Exception as exc:  # noqa: BLE001 — reconcile is best-effort
            logger.warning(
                "WikiCompileProcessor: terminal-event reconcile failed: %s", exc
            )
        return announced

    @staticmethod
    def _drain_reconciled_events(bus, payloads: list) -> None:
        """Fan out deferred reconcile events on the app loop (PRR-004).

        Best-effort: a late failure must not escape into the loop's callback
        machinery (it would surface as an "Exception in callback" and the
        remaining rows would never be announced).
        """
        try:
            for vault_id, payload in payloads:
                bus.publish(vault_id, payload)
        except Exception as exc:  # noqa: BLE001 — fan-out is best-effort
            logger.warning(
                "WikiCompileProcessor: deferred reconcile fan-out failed: %s",
                exc,
            )

    def _fail_job(
        self, job_id: int, error: str, worker_id: Optional[str] = None
    ) -> int:
        """Fail a job and return the durable attempt count.

        Lease mode: the store requeues with ``run_after`` backoff or settles
        terminally at the cap (fenced on ``worker_id``). Legacy mode: the
        old ``retry_count`` increment; the processor schedules the reset.
        """
        from app.services.wiki_store import WikiStore

        with self._pool.connection() as conn:
            return WikiStore(conn).fail_job(job_id, error, worker_id=worker_id)

    async def _heartbeat_loop(self, job_id: int, worker_id: str) -> None:
        """Renew a held lease every heartbeat interval until it is lost.

        A compile dispatch can outlive the reclaim timeout on a slow store;
        the per-job renewal keeps a live worker's lease fresh. Exits when a
        renewal is rejected (lease lost) — the fenced settlement will be
        rejected too.
        """
        interval = max(1.0, float(settings.jobs_heartbeat_interval_seconds))

        while True:
            await asyncio.sleep(interval)

            def _renew():
                with self._pool.connection() as conn:
                    return JobLease(
                        conn,
                        reclaim_timeout_seconds=settings.jobs_lease_reclaim_timeout_seconds,
                        max_attempts=settings.jobs_max_attempts,
                    ).heartbeat(job_id, worker_id)

            try:
                renewed = await asyncio.to_thread(_renew)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a missed beat is not fatal
                logger.debug(
                    "%s: heartbeat renewal failed for job %s",
                    WikiCompileProcessor,
                    job_id,
                )
                continue
            if not renewed:
                return
    def _reset_job_to_pending(self, job_id: int) -> None:
        from app.services.wiki_store import WikiStore

        with self._pool.connection() as conn:
            WikiStore(conn).reset_job_to_pending(job_id)

    async def _reset_job_to_pending_with_retry(self, job_id: int) -> None:
        """Reset a 'failed' job to 'pending' with bounded retry on transient DB errors.

        A single transient failure (e.g. SQLite 'database is locked') on the original
        one-shot call would leave the job stuck in 'failed' until manual API retry.
        This helper retries the reset a few times with short exponential backoff so
        the auto-retry pipeline self-heals. Non-recoverable failures propagate to the
        outer poll-loop guard, which logs and continues with the next job.
        """
        last_exc: Optional[BaseException] = None
        for attempt in range(1, RESET_RETRY_ATTEMPTS + 1):
            try:
                await asyncio.to_thread(self._reset_job_to_pending, job_id)
                if attempt > 1:
                    logger.warning(
                        "WikiCompileProcessor: job id=%d reset recovered on attempt %d/%d",
                        job_id, attempt, RESET_RETRY_ATTEMPTS,
                    )
                return
            except Exception as exc:  # noqa: BLE001 — propagate only after final attempt
                last_exc = exc
                if attempt < RESET_RETRY_ATTEMPTS:
                    delay = RESET_RETRY_BASE_DELAY * (2 ** (attempt - 1))
                    logger.warning(
                        "WikiCompileProcessor: job id=%d reset attempt %d/%d failed: %s; retrying in %.1fs",
                        job_id, attempt, RESET_RETRY_ATTEMPTS, exc, delay,
                    )
                    await asyncio.sleep(delay)
        if last_exc is None:
            raise RuntimeError("WikiCompileProcessor reset retry exhausted without an error")
        raise last_exc

    def _spawn_delayed_reset(self, job_id: int, backoff: float) -> None:
        """Schedule a delayed reset of a failed job as a detached background task.

        Keeps a strong reference in ``self._bg_tasks`` so the task is not
        garbage-collected mid-flight, and so ``stop()`` can cancel it cleanly
        (issue #276 E2-3). The poll loop returns immediately after spawning so
        it can keep claiming other pending jobs during the backoff window.
        """
        task = asyncio.create_task(self._delayed_reset(job_id, backoff))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _delayed_reset(self, job_id: int, backoff: float) -> None:
        """Sleep the backoff, then reset the failed job to pending for auto-retry."""
        try:
            await asyncio.sleep(backoff)
            await self._reset_job_to_pending_with_retry(job_id)
        except asyncio.CancelledError:
            # Processor is shutting down; abandon the reset.
            raise
        except Exception as exc:  # noqa: BLE001 — detached task must not propagate
            logger.error(
                "WikiCompileProcessor: delayed reset of job id=%d failed: %s",
                job_id, exc,
            )

    def _marshal_publish(self, publish_fn) -> None:
        """Run a terminal-event publish on the app event loop.

        Issue #955 review: the wiki event bus is same-loop only (its
        ``put_nowait`` wakeup is not thread-safe), and the threaded
        fail-and-publish unit publishes from a worker thread — marshal
        via ``call_soon_threadsafe`` (the self-pipe wakeup ``put_nowait``
        lacks cross-thread). ``call_soon_threadsafe`` takes no kwargs, so
        callers pass a ``functools.partial`` of the pinned-constant
        ``_publish_event`` invocation (the o02 census contract). If the
        loop is gone (processor never started, or closed at shutdown),
        the event is dropped with a warning — subscribers are gone with
        it, and SSE clients refetch canonical state on reconnect.
        """
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(publish_fn)
                return
            except RuntimeError:
                # Loop closed between the is_running() check and the
                # cross-thread call — same disposition as no loop: the
                # terminal row is already committed and the boot
                # reconcile re-announces it, so drop with a warning
                # instead of escaping into the poll loop's
                # could-not-mark-failed handler.
                pass
        logger.warning(
            "WikiCompileProcessor: terminal event dropped — no running"
            " event loop to marshal the publish onto"
        )

    def _publish_event(self, job, event_type: str, *, result: Optional[dict] = None, error: Optional[str] = None) -> None:
        """Fan out a terminal-state event to SSE subscribers for the vault.

        Best-effort: never raise out of the poll loop. The event payload is
        intentionally small — clients refetch the canonical state via the
        existing REST endpoints on receipt.

        Threaded callers go through ``_marshal_publish`` (issue #955
        review): the bus's ``put_nowait`` is same-loop only, so a publish
        from a worker thread is marshalled onto the app loop first.
        """
        try:
            from app.services.wiki_events import get_wiki_event_bus

            payload: dict = {
                "type": event_type,
                "job_id": job.id,
                "vault_id": job.vault_id,
                "trigger_type": job.trigger_type,
            }
            if result is not None:
                payload["result"] = {
                    "page": result.get("page"),
                    "claims_count": len(result.get("claims") or []),
                    "entities_count": len(result.get("entities") or []),
                    "skipped": bool(result.get("skipped")),
                }
            if error is not None:
                payload["error"] = error
            get_wiki_event_bus().publish(job.vault_id, payload)
        except Exception as exc:  # noqa: BLE001 — fan-out must never fail the loop
            # WARNING (not DEBUG): these are terminal-state events (job
            # completed/failed) that subscribers expect in real time. A dropped
            # notification is operationally significant — surface it so the
            # event is visible to operators, not only at debug verbosity.
            logger.warning(
                "WikiCompileProcessor: event publish failed (job_id=%s vault_id=%s event=%s): %s",
                job.id,
                job.vault_id,
                event_type,
                exc,
                exc_info=True,
            )

    def _dispatch(self, job) -> dict:
        """Dispatch a job to the appropriate handler. Runs in a thread."""
        from app.config import settings
        from app.services.wiki_compiler import WikiCompiler
        from app.services.wiki_store import WikiStore

        # WIKI-001 (#515): the enqueue-side gate can race a settings flip —
        # a job created while the flags were on may be claimed after they
        # were turned off. Re-check at claim time and cancel the job WITHOUT
        # running the compiler, so no wiki rows are written from a disabled
        # subsystem. ``wiki_enabled`` gates every trigger; the
        # ``wiki_compile_on_query`` toggle only gates chat-answer ("query")
        # compilation — manual/ingest compiles stay allowed when just that
        # toggle is off.
        if (not settings.wiki_enabled) or (
            job.trigger_type == "query" and not settings.wiki_compile_on_query
        ):
            result = {
                "cancelled": "wiki_compile_disabled",
                "skipped": True,
                "reason": (
                    "wiki compilation disabled at dispatch "
                    "(wiki_enabled or wiki_compile_on_query is off)"
                ),
            }
            logger.info(
                "WikiCompileProcessor: cancelling job id=%d type=%s — wiki compile disabled",
                job.id, job.trigger_type,
            )
            with self._pool.connection() as conn:
                WikiStore(conn).cancel_job(job.id, job.vault_id, result_json=result)
            return result

        # Issue #723 (T1-21-S-07): a job whose vault was deleted must never
        # re-persist wiki rows for a vault_id that no longer exists. Checked
        # at dispatch (not enqueue) because a vault delete can land between
        # enqueue and claim; the compiler entry points re-check for jobs that
        # were already running when the vault delete committed.
        with self._pool.connection() as conn:
            vault_row = conn.execute(
                "SELECT 1 FROM vaults WHERE id = ?", (job.vault_id,)
            ).fetchone()
            if vault_row is None:
                result = {
                    "cancelled": "vault_deleted",
                    "skipped": True,
                    "reason": f"vault {job.vault_id} no longer exists",
                }
                logger.info(
                    "WikiCompileProcessor: cancelling job id=%d type=%s — vault deleted",
                    job.id,
                    job.trigger_type,
                )
                WikiStore(conn).cancel_job(job.id, job.vault_id, result_json=result)
                return result

        input_json: dict = {}
        if job.input_json:
            try:
                input_json = json.loads(job.input_json) if isinstance(job.input_json, str) else job.input_json
            except (json.JSONDecodeError, TypeError):
                input_json = {}

        with self._pool.connection() as conn:
            store = WikiStore(conn)
            compiler = WikiCompiler(conn, store)

            if job.trigger_type == "query":
                return compiler.compile_query_job(vault_id=job.vault_id, input_json=input_json)

            if job.trigger_type == "ingest":
                return compiler.compile_ingest_job(vault_id=job.vault_id, input_json=input_json)

            if job.trigger_type == "memory":
                memory_id = input_json.get("memory_id")
                if not memory_id:
                    return {"skipped": True, "reason": "no memory_id in input_json"}
                # is_admin=True: this is a trusted system background job, not a
                # user-context call. The interactive /wiki/promote-memory route
                # (the only user-facing caller) passes the role-derived value;
                # here we run as a privileged worker so global memories may be
                # promoted (issue #404).
                return compiler.promote_memory(
                    memory_id=memory_id, vault_id=job.vault_id, is_admin=True
                )

            if job.trigger_type in ("manual", "settings_reindex"):
                return self._handle_reindex(job, store, compiler, input_json)

            logger.warning(
                "WikiCompileProcessor: unknown trigger_type %r for job id=%d",
                job.trigger_type,
                job.id,
            )
            return {"skipped": True, "reason": f"unknown trigger_type: {job.trigger_type}"}

    @staticmethod
    def _handle_reindex(job, store, compiler, input_json: dict) -> dict:
        """Re-promote stale memory-sourced claims in the vault."""
        vault_id = job.vault_id
        memory_id = input_json.get("memory_id")
        if memory_id:
            # Trusted system job — see is_admin note at the trigger_type=="memory" call above.
            return compiler.promote_memory(
                memory_id=memory_id, vault_id=vault_id, is_admin=True
            )

        stale_claims = store.list_claims(vault_id=vault_id, status="superseded")
        # Dedup memory_id across claims/sources: a single memory commonly
        # promotes into multiple claims, so re-promoting each occurrence would
        # fire redundant promote_memory (and LLM curator) calls and inflate the
        # reprocessed counter (issue #276 A6-4). reprocessed counts distinct
        # memories promoted.
        seen_memory_ids: set[int] = set()
        for claim in stale_claims:
            for src in claim.sources or []:
                if src.source_kind == "memory" and src.memory_id and src.memory_id not in seen_memory_ids:
                    seen_memory_ids.add(src.memory_id)

        reprocessed = 0
        for memory_id in seen_memory_ids:
            try:
                # Trusted system job — see is_admin note at the trigger_type=="memory" call above.
                compiler.promote_memory(memory_id=memory_id, vault_id=vault_id, is_admin=True)
                reprocessed += 1
            except Exception as exc:
                logger.warning(
                    "WikiCompileProcessor reindex: promote_memory(%d) failed: %s",
                    memory_id,
                    exc,
                )

        return {"reprocessed": reprocessed, "stale_count": len(stale_claims)}
