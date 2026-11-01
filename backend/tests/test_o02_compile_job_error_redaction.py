"""Issue #792 (Workstream O, PR 2) acceptance checks: compile-job error
redaction on the wiki/KMS lease queues.

A compile worker whose dispatch raises currently passes ``str(exc)`` straight
into ``fail_job``; the lease settle writes that raw text into ``jobs.error``
and the terminal ``job_failed`` SSE event re-broadcasts it. Whatever the
exception carried (internal paths, upstream payloads, debug sentinels) then
leaves the process through GET /wiki/jobs, GET /kms/jobs, and the wiki event
stream.

Checks (lease mode on, ``jobs_max_attempts=1`` so the first dispatch failure
settles terminally):

- AC1: GET /wiki/jobs response ``error`` field carries no raw exception text.
- AC2: the ``job_failed`` SSE event ``error`` field carries no raw exception
  text.
- AC3: GET /kms/jobs response ``error`` field carries no raw exception text.

RED at base (the dispatch sentinel string reappears verbatim in the jobs
routes and the job_failed event because the worker hands ``str(exc)`` to the
store unchecked); GREEN after the fix (the raw exception text is redacted at
the failure-recording boundary, so the sentinel no longer reaches the job
row, the jobs routes, or the event payload).

Harness: a real WikiCompileProcessor / KMSCompileProcessor poll-loop
iteration over a temp ``run_migrations`` DB behind a SQLiteConnectionPool,
with the instance's ``_dispatch`` patched to raise a sentinel-bearing
RuntimeError; assertions read the job back through the public route handlers
(called directly with a permissive evaluate) and through a recording event
bus patched in place of ``get_wiki_event_bus``.
"""

import asyncio
import unittest
from pathlib import Path
from tempfile import mkdtemp
from unittest.mock import patch

from app.config import settings

TERMINAL_STATUSES = ("failed", "completed", "cancelled")
SETTLE_TIMEOUT_SECONDS = 15.0
SETTLE_POLL_INTERVAL_SECONDS = 0.1

_USER = {"id": 1, "username": "checker", "role": "superadmin", "is_active": True}


async def _allow_all(user, resource_type, resource_id, action):
    del user, resource_type, resource_id, action
    return True


def _make_pool():
    from app.models.database import SQLiteConnectionPool, run_migrations

    db_path = str(Path(mkdtemp()) / "o02-redaction.db")
    run_migrations(db_path)  # legacy tables AND the shared jobs table
    pool = SQLiteConnectionPool(db_path, max_size=2)
    with pool.connection() as conn:
        conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'V1')")
        conn.commit()
    return pool


class _RecordingBus:
    """Event-bus stand-in that records every publish for inspection."""

    def __init__(self):
        self.events = []

    def publish(self, vault_id, payload):
        self.events.append({"vault_id": vault_id, **payload})


class TestO02CompileJobErrorRedaction(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        lease_patcher = patch.object(settings, "wiki_kms_job_lease_enabled", True)
        lease_patcher.start()
        self.addCleanup(lease_patcher.stop)
        cap_patcher = patch.object(settings, "jobs_max_attempts", 1)
        cap_patcher.start()
        self.addCleanup(cap_patcher.stop)
        self.pool = _make_pool()
        self.addCleanup(self.pool.close_all)

    async def _settle_job(self, queue):
        """Run the poll loop until the queue's single enqueued job settles.

        Bounded by SETTLE_TIMEOUT_SECONDS; polls the jobs table directly
        every SETTLE_POLL_INTERVAL_SECONDS so the wait is deterministic.
        """
        deadline = asyncio.get_running_loop().time() + SETTLE_TIMEOUT_SECONDS
        while True:
            with self.pool.connection() as conn:
                row = conn.execute(
                    "SELECT status FROM jobs WHERE queue = ?", (queue,)
                ).fetchone()
            status = dict(row)["status"] if row is not None else None
            if status in TERMINAL_STATUSES:
                return
            if asyncio.get_running_loop().time() > deadline:
                self.fail(
                    f"queue {queue!r} did not settle terminally within "
                    f"{SETTLE_TIMEOUT_SECONDS}s (last status: {status!r})"
                )
            await asyncio.sleep(SETTLE_POLL_INTERVAL_SECONDS)

    async def test_wiki_jobs_route_error_omits_raw_exception_text(self):
        # check: AC1 — the wiki jobs route response carries no raw exception
        # text from the failed dispatch.
        from app.api.routes.wiki import list_wiki_jobs
        from app.services.wiki_compile_processor import WikiCompileProcessor
        from app.services.wiki_store import WikiStore

        with self.pool.connection() as conn:
            WikiStore(conn).create_job(vault_id=1, trigger_type="manual")

        proc = WikiCompileProcessor(self.pool)

        def _boom(job):
            del job
            raise RuntimeError("dispatch exploded: O02-SENTINEL-WIKI")

        proc._dispatch = _boom
        try:
            await proc.start()
            await self._settle_job("wiki")
        finally:
            await proc.stop()

        with self.pool.connection() as conn:
            resp = await list_wiki_jobs(
                vault_id=1,
                status=None,
                limit=None,
                db=conn,
                user=_USER,
                evaluate=_allow_all,
            )
        jobs = resp["jobs"]
        self.assertTrue(jobs, "expected the settled wiki job in the route response")
        job = jobs[0]
        leaked = "O02-SENTINEL-WIKI" in (job["error"] or "")
        assert leaked is False

    async def test_wiki_job_failed_event_omits_raw_exception_text(self):
        # check: AC2 — the job_failed SSE event carries no raw exception text
        # from the failed dispatch.
        from app.services.wiki_compile_processor import WikiCompileProcessor
        from app.services.wiki_store import WikiStore

        recorder = _RecordingBus()
        with patch(
            "app.services.wiki_events.get_wiki_event_bus", return_value=recorder
        ):
            with self.pool.connection() as conn:
                WikiStore(conn).create_job(vault_id=1, trigger_type="manual")

            proc = WikiCompileProcessor(self.pool)

            def _boom(job):
                del job
                raise RuntimeError("dispatch exploded: O02-SENTINEL-WIKI")

            proc._dispatch = _boom
            try:
                await proc.start()
                await self._settle_job("wiki")
            finally:
                await proc.stop()

        failed_events = [e for e in recorder.events if e.get("type") == "job_failed"]
        self.assertTrue(
            failed_events, "expected a job_failed event at terminal settle"
        )
        event = failed_events[0]
        leaked = "O02-SENTINEL-WIKI" in (event.get("error") or "")
        assert leaked is False

    async def test_kms_jobs_route_error_omits_raw_exception_text(self):
        # check: AC3 — the KMS jobs route response carries no raw exception
        # text from the failed dispatch.
        from app.api.routes.kms import list_kms_jobs
        from app.services.kms_compile_processor import KMSCompileProcessor
        from app.services.kms_store import KMSStore

        with self.pool.connection() as conn:
            KMSStore(conn).create_job(1, "manual")

        proc = KMSCompileProcessor(self.pool)

        def _boom(job):
            del job
            raise RuntimeError("dispatch exploded: O02-SENTINEL-KMS")

        proc._dispatch = _boom
        try:
            await proc.start()
            await self._settle_job("kms")
        finally:
            await proc.stop()

        with self.pool.connection() as conn:
            resp = await list_kms_jobs(
                vault_id=1,
                status=None,
                db=conn,
                user=_USER,
                evaluate=_allow_all,
                _=None,
            )
        jobs = resp["jobs"]
        self.assertTrue(jobs, "expected the settled kms job in the route response")
        job = jobs[0]
        leaked = "O02-SENTINEL-KMS" in (job["error"] or "")
        assert leaked is False


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
