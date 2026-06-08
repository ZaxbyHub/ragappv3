"""
WikiCompileProcessor: asyncio background worker that drains wiki_compile_jobs.

Single worker per process. Polls every POLL_INTERVAL seconds.
On startup, resets orphaned 'running' jobs back to 'pending' (crash recovery).
All DB work runs in threads via asyncio.to_thread() since SQLite is sync.
"""

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from app.models.database import SQLiteConnectionPool

logger = logging.getLogger(__name__)

POLL_INTERVAL = 5        # seconds between polls when queue is empty
MAX_RETRIES = 3           # max automatic retries before a job is permanently failed
RESET_RETRY_ATTEMPTS = 3  # attempts to recover a job from 'failed' → 'pending' after a transient DB error
RESET_RETRY_BASE_DELAY = 0.5  # seconds; doubled per attempt (0.5, 1.0, 2.0)


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

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        await asyncio.to_thread(self._reset_orphans)
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("WikiCompileProcessor started")

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("WikiCompileProcessor stopped")

    # ------------------------------------------------------------------
    # Startup orphan recovery
    # ------------------------------------------------------------------

    def _reset_orphans(self) -> None:
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

                logger.info(
                    "WikiCompileProcessor: claimed job id=%d type=%s vault=%d",
                    job.id,
                    job.trigger_type,
                    job.vault_id,
                )

                try:
                    result = await asyncio.to_thread(self._dispatch, job)
                    await asyncio.to_thread(self._complete_job, job.id, result)
                    logger.info("WikiCompileProcessor: completed job id=%d", job.id)
                    self._publish_event(job, "job_completed", result=result)
                except Exception as exc:
                    logger.exception(
                        "WikiCompileProcessor: job id=%d failed: %s", job.id, exc
                    )
                    try:
                        new_retry_count = await asyncio.to_thread(
                            self._fail_job, job.id, str(exc)
                        )
                        if new_retry_count < MAX_RETRIES:
                            backoff = 2.0 ** new_retry_count
                            logger.info(
                                "WikiCompileProcessor: job id=%d will auto-retry (%d/%d) in %.0fs",
                                job.id, new_retry_count, MAX_RETRIES, backoff,
                            )
                            await asyncio.sleep(backoff)
                            await self._reset_job_to_pending_with_retry(job.id)
                        else:
                            logger.error(
                                "WikiCompileProcessor: job id=%d permanently failed after %d retries",
                                job.id, new_retry_count,
                            )
                            self._publish_event(job, "job_failed", error=str(exc))
                    except Exception as e2:
                        logger.error(
                            "WikiCompileProcessor: could not mark job id=%d failed: %s", job.id, e2
                        )

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
            return WikiStore(conn).claim_next_pending_job()

    def _complete_job(self, job_id: int, result: dict) -> None:
        from app.services.wiki_store import WikiStore

        with self._pool.connection() as conn:
            WikiStore(conn).complete_job(job_id, result)

    def _fail_job(self, job_id: int, error: str) -> int:
        """Fail a job and return the new retry_count."""
        from app.services.wiki_store import WikiStore

        with self._pool.connection() as conn:
            return WikiStore(conn).fail_job(job_id, error)

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
        assert last_exc is not None
        raise last_exc

    def _publish_event(self, job, event_type: str, *, result: Optional[dict] = None, error: Optional[str] = None) -> None:
        """Fan out a terminal-state event to SSE subscribers for the vault.

        Best-effort: never raise out of the poll loop. The event payload is
        intentionally small — clients refetch the canonical state via the
        existing REST endpoints on receipt.
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
            logger.debug("WikiCompileProcessor: event publish failed: %s", exc)

    def _dispatch(self, job) -> dict:
        """Dispatch a job to the appropriate handler. Runs in a thread."""
        from app.services.wiki_compiler import WikiCompiler
        from app.services.wiki_store import WikiStore

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
                return compiler.promote_memory(memory_id=memory_id, vault_id=job.vault_id)

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
            return compiler.promote_memory(memory_id=memory_id, vault_id=vault_id)

        stale_claims = store.list_claims(vault_id=vault_id, status="superseded")
        reprocessed = 0
        for claim in stale_claims:
            for src in claim.sources or []:
                if src.source_kind == "memory" and src.memory_id:
                    try:
                        compiler.promote_memory(memory_id=src.memory_id, vault_id=vault_id)
                        reprocessed += 1
                    except Exception as exc:
                        logger.warning(
                            "WikiCompileProcessor reindex: promote_memory(%d) failed: %s",
                            src.memory_id,
                            exc,
                        )

        return {"reprocessed": reprocessed, "stale_count": len(stale_claims)}
