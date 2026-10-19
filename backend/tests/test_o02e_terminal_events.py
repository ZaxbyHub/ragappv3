"""Issue #955 acceptance checks: terminal wiki-job lifecycle events.

Two event gaps on the wiki compile job surface:

1. ``POST /wiki/jobs/{id}/cancel`` settles the row ``cancelled`` but never
   publishes a ``job_cancelled`` event — SSE consumers never refetch, so the
   user-visible state stalls (the only non-test publisher is the
   wiki-disabled branch of the compile processor).
2. The failure arm commits the terminal ``_fail_job`` write and only THEN
   calls ``_publish_event``; a poll-task cancellation (``stop()`` cancelling
   the task) delivered at that await leaves a terminal row with no terminal
   event, and nothing reconciles terminal-but-unpublished rows later.

Checks:

- AC7: calling the ``cancel_wiki_job`` route handler directly (with the wiki
  event bus patched by a recorder) publishes a ``job_cancelled`` event for
  the vault, and the row settles ``cancelled``.
- AC8: shutdown window — with the processor running a failing dispatch
  (``jobs_max_attempts=1``, lease enabled) and its ``_publish_event`` patched
  to raise ``asyncio.CancelledError`` on its FIRST call only (simulating
  ``stop()`` cancelling the poll task at that await), the row settles
  terminally failed; after restoring the real ``_publish_event`` and
  stopping the processor, a FRESH ``WikiCompileProcessor`` on the same
  pool/db publishes the ``job_failed`` event for that job id within a
  bounded wait (publish-inside-boundary and reconcile-on-start both satisfy
  this — the check is arm-agnostic).
- AC9 (PRESERVING): the wiki-disabled branch still publishes
  ``job_cancelled`` — a real processor (real ``_dispatch``) with
  ``wiki_enabled=False`` cancels the job at dispatch and publishes
  ``job_cancelled``; the row settles ``cancelled``.

RED at base for AC7 (no event published) and AC8 (no reconciliation — the
fresh processor publishes nothing for the already-terminal row); GREEN after
the fix. The AC9 check is GREEN at base and must stay GREEN.

Harness: real ``WikiCompileProcessor`` poll loops over temp
``run_migrations`` DBs behind a SQLiteConnectionPool, route handlers awaited
directly with a permissive evaluate, and a recording event bus patched in
place of ``get_wiki_event_bus`` (mirrors the frozen o02 suites and the
Phase-2 probes).
"""

import asyncio
import os
import shutil
import unittest
from pathlib import Path
from tempfile import mkdtemp
from unittest.mock import patch

os.environ.setdefault("USERS_ENABLED", "false")
os.environ.setdefault(
    "ADMIN_SECRET_TOKEN", "o02e-token-0123456789abcdef0123456789abcdef"
)

from app.config import settings  # noqa: E402

TERMINAL_STATUSES = ("failed", "completed", "cancelled")
SETTLE_TIMEOUT_SECONDS = 15.0
RECONCILE_TIMEOUT_SECONDS = 10.0
POLL_INTERVAL_SECONDS = 0.1

DISPATCH_SENTINEL = "O02E-SENTINEL-DISPATCH-BOOM pw=hunter2"

_USER = {"id": 1, "username": "checker", "role": "superadmin", "is_active": True}


async def _allow_all(user, resource_type, resource_id, action):
    del user, resource_type, resource_id, action
    return True


def _make_pool():
    from app.models.database import SQLiteConnectionPool, run_migrations

    tmp_dir = mkdtemp(prefix="o02e-")
    db_path = str(Path(tmp_dir) / "o02e.db")
    run_migrations(db_path)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    with pool.connection() as conn:
        conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'V1')")
        conn.commit()
    return pool, tmp_dir


class _RecordingBus:
    """Event-bus stand-in that records every publish for inspection."""

    def __init__(self):
        self.events = []

    def publish(self, vault_id, payload):
        self.events.append({"vault_id": vault_id, **payload})


class _EventWaitMixin:
    async def _wait_row_terminal(self, pool, job_id: int) -> str:
        deadline = asyncio.get_running_loop().time() + SETTLE_TIMEOUT_SECONDS
        while True:
            with pool.connection() as conn:
                row = conn.execute(
                    "SELECT status FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()
            status = dict(row)["status"] if row is not None else None
            if status in TERMINAL_STATUSES:
                return status
            if asyncio.get_running_loop().time() > deadline:
                self.fail(
                    f"job {job_id} did not settle terminally within"
                    f" {SETTLE_TIMEOUT_SECONDS}s (last status: {status!r})"
                )
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def _wait_event(self, bus, event_type: str, job_id: int):
        deadline = asyncio.get_running_loop().time() + RECONCILE_TIMEOUT_SECONDS
        while True:
            matches = [
                e
                for e in bus.events
                if e.get("type") == event_type and e.get("job_id") == job_id
            ]
            if matches:
                return matches[0]
            if asyncio.get_running_loop().time() > deadline:
                self.fail(
                    f"no {event_type} event for job {job_id} within"
                    f" {RECONCILE_TIMEOUT_SECONDS}s (events seen:"
                    f" {[e.get('type') for e in bus.events]!r})"
                )
            await asyncio.sleep(POLL_INTERVAL_SECONDS)


class TestCancelRoutePublishesEvent(unittest.IsolatedAsyncioTestCase):
    """AC7 (#955): the user cancel route publishes job_cancelled."""

    def setUp(self):
        lease_patcher = patch.object(settings, "wiki_kms_job_lease_enabled", True)
        lease_patcher.start()
        self.addCleanup(lease_patcher.stop)
        self.pool, tmp_dir = _make_pool()
        self.addCleanup(self.pool.close_all)
        self.addCleanup(shutil.rmtree, tmp_dir, True)

    async def test_cancel_route_publishes_job_cancelled_event(self):
        # check: AC7 — RED at base, GREEN after the fix.
        from app.api.routes.wiki import cancel_wiki_job
        from app.services.wiki_store import WikiStore

        with self.pool.connection() as conn:
            job = WikiStore(conn).create_job(vault_id=1, trigger_type="manual")

        bus = _RecordingBus()
        with patch(
            "app.services.wiki_events.get_wiki_event_bus", return_value=bus
        ):
            with self.pool.connection() as conn:
                resp = await cancel_wiki_job(
                    job_id=job.id,
                    vault_id=1,
                    db=conn,
                    user=_USER,
                    evaluate=_allow_all,
                    _csrf_token="o02e-csrf",
                )
        self.assertEqual(resp, {"job_id": job.id, "status": "cancelled"})

        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT status FROM jobs WHERE id = ?", (job.id,)
            ).fetchone()
        self.assertEqual(
            dict(row)["status"], "cancelled", "the row must settle cancelled"
        )

        cancelled_events = [
            e
            for e in bus.events
            if e.get("type") == "job_cancelled"
            and e.get("vault_id") == 1
            and e.get("job_id") == job.id
        ]
        self.assertTrue(
            cancelled_events,
            "the cancel route must publish a job_cancelled event on the wiki"
            " event bus for the vault (#955); events seen:"
            f" {[e.get('type') for e in bus.events]!r}",
        )


class TestShutdownWindowReconciles(_EventWaitMixin, unittest.IsolatedAsyncioTestCase):
    """AC8 (#955): a terminal write whose publish was cancelled is still
    evented (or reconciled) — the DB row must not outlive its event silence."""

    def setUp(self):
        self.pool, tmp_dir = _make_pool()
        self.addCleanup(self.pool.close_all)
        self.addCleanup(shutil.rmtree, tmp_dir, True)

    async def test_terminal_event_published_after_shutdown_window(self):
        # check: AC8 — RED at base, GREEN after the fix.
        from app.services.wiki_compile_processor import WikiCompileProcessor
        from app.services.wiki_store import WikiStore

        with self.pool.connection() as conn:
            job = WikiStore(conn).create_job(vault_id=1, trigger_type="manual")
        job_id = job.id

        proc = WikiCompileProcessor(self.pool)

        def _boom(_job):
            raise RuntimeError(f"dispatch exploded: {DISPATCH_SENTINEL}")

        proc._dispatch = _boom
        real_publish = proc._publish_event
        publish_calls = {"n": 0}

        def _cancelled_first_publish(*args, **kwargs):
            publish_calls["n"] += 1
            if publish_calls["n"] == 1:
                # Simulate stop() cancelling the poll task exactly at this
                # await point — after the terminal _fail_job commit.
                raise asyncio.CancelledError()
            return real_publish(*args, **kwargs)

        bus = _RecordingBus()
        with patch.object(settings, "wiki_kms_job_lease_enabled", True), \
                patch.object(settings, "jobs_max_attempts", 1), \
                patch(
                    "app.services.wiki_events.get_wiki_event_bus",
                    return_value=bus,
                ):
            proc._publish_event = _cancelled_first_publish
            await proc.start()
            try:
                status = await self._wait_row_terminal(self.pool, job_id)
            finally:
                proc._publish_event = real_publish
                await proc.stop()
            self.assertEqual(status, "failed")

            # The poll task died at the cancelled publish: at base the row is
            # now terminally failed with NO terminal event and nothing
            # reconciles it. A FRESH processor on the same pool/db must make
            # the matching job_failed event appear within a bounded wait.
            fresh = WikiCompileProcessor(self.pool)
            try:
                await fresh.start()
                event = await self._wait_event(bus, "job_failed", job_id)
            finally:
                await fresh.stop()
        self.assertEqual(event.get("job_id"), job_id)


class TestWikiDisabledBranchStillPublishes(
    _EventWaitMixin, unittest.IsolatedAsyncioTestCase
):
    """AC9 (PRESERVING): the wiki-disabled branch's job_cancelled publish.

    GREEN at base and must stay GREEN — the existing
    ``_dispatch -> {"cancelled": "wiki_compile_disabled"} -> publish``
    branch (wiki_compile_processor) is the one non-test job_cancelled
    publisher and must keep publishing.
    """

    def setUp(self):
        lease_patcher = patch.object(settings, "wiki_kms_job_lease_enabled", True)
        lease_patcher.start()
        self.addCleanup(lease_patcher.stop)
        self.pool, tmp_dir = _make_pool()
        self.addCleanup(self.pool.close_all)
        self.addCleanup(shutil.rmtree, tmp_dir, True)

    async def test_wiki_disabled_dispatch_publishes_job_cancelled(self):
        # check: AC9 — GREEN at base, must stay GREEN after the fix.
        from app.services.wiki_compile_processor import WikiCompileProcessor
        from app.services.wiki_store import WikiStore

        with self.pool.connection() as conn:
            job = WikiStore(conn).create_job(vault_id=1, trigger_type="manual")

        bus = _RecordingBus()
        with patch.object(settings, "wiki_enabled", False), \
                patch(
                    "app.services.wiki_events.get_wiki_event_bus",
                    return_value=bus,
                ):
            proc = WikiCompileProcessor(self.pool)
            await proc.start()
            try:
                event = await self._wait_event(bus, "job_cancelled", job.id)
                status = await self._wait_row_terminal(self.pool, job.id)
            finally:
                await proc.stop()

        self.assertEqual(event.get("vault_id"), 1)
        self.assertEqual(status, "cancelled")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
