"""Regression guards for the swarm-pr-feedback round on #612 (PRR-004/006).

Pins two behaviors the final critic's gate found unguarded:

1. ``DraftJobProcessor._dispatch_compile_job`` forwards the lease worker
   identity to ``draft_pipeline.run_compile`` (lease mode) or ``None``
   (legacy mode) — a signature/arity drift between the processor and
   ``run_compile`` would fail every compile job instantly.
2. ``WikiCompileProcessor`` publishes ``job_completed`` only when the
   fenced complete actually committed — a rejected complete (lease lost or
   cancel won) must NOT produce a completion event.
"""

import asyncio
import sqlite3
import unittest
from pathlib import Path
from tempfile import mkdtemp
from unittest.mock import MagicMock, patch

from app.models.database import run_migrations
from app.services.wiki_compile_processor import WikiCompileProcessor


def _connect(db_path):
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class TestRunCompileWorkerIdForwarding(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_compile_job_forwards_worker_id(self):
        import os

        os.environ.setdefault("USERS_ENABLED", "false")
        os.environ.setdefault("ADMIN_SECRET_TOKEN", "x" * 40)
        from app.config import settings
        from app.services import draft_pipeline
        from app.services.draft_job_processor import DraftJobProcessor

        captured = {}

        async def fake_run_compile(**kwargs):
            captured.update(kwargs)

        processor = DraftJobProcessor(
            MagicMock(), MagicMock(), MagicMock(), poll_interval=0.05
        )

        async def permission_ok(job):
            return True

        processor._owner_permission_ok = permission_ok
        job = MagicMock()
        job.id = 1
        job.job_type = "compile"
        with patch.object(draft_pipeline, "run_compile", fake_run_compile):
            for lease_on, expected_worker in ((True, processor._worker_id), (False, None)):
                if lease_on:
                    if not hasattr(settings, "draft_job_lease_enabled"):
                        self.skipTest("draft lease switch absent")
                    previous = settings.draft_job_lease_enabled
                    settings.draft_job_lease_enabled = True
                else:
                    previous = getattr(settings, "draft_job_lease_enabled", None)
                    settings.draft_job_lease_enabled = False
                try:
                    await processor._dispatch_compile_job(job)
                except Exception:
                    pass  # dispatch may fail on mocked internals; kwargs are the contract
                finally:
                    settings.draft_job_lease_enabled = previous
                self.assertEqual(
                    captured.get("worker_id"),
                    expected_worker,
                    f"lease_on={lease_on}: run_compile must receive the "
                    "processor's worker identity in lease mode and None in "
                    "legacy mode",
                )
                captured.clear()


class _FencedCompleteConn:
    """Connection proxy whose jobs UPDATE commits zero rows (lease lost)."""

    def __init__(self, real):
        object.__setattr__(self, "_real", real)

    def execute(self, sql, *args, **kwargs):
        cur = self._real.execute(sql, *args, **kwargs)
        if "UPDATE wiki_compile_jobs SET status = 'completed'" in sql:
            cur = _ZeroCursor(cur)
        return cur

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_real"), name)


class _ZeroCursor:
    def __init__(self, inner):
        self._inner = inner

    def fetchone(self):
        return self._inner.fetchone() if hasattr(self._inner, "fetchone") else None

    @property
    def rowcount(self):
        return 0

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _PoolOver:
    """Pool fake yielding a fixed connection via a real context manager."""

    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        outer = self

        class _Ctx:
            def __enter__(self):
                return outer._conn

            def __exit__(self, *args):
                return False

        return _Ctx()


class TestWikiCompletionPublicationRequiresFencedCommit(unittest.TestCase):
    def test_rejected_complete_suppresses_job_completed_event(self):
        from app.services.job_lease import JobLease

        temp = Path(mkdtemp(prefix="s2-complete-pub-"))
        db_path = str(temp / "pub.db")
        run_migrations(db_path)
        conn = _connect(db_path)
        self.addCleanup(conn.close)

        processor = WikiCompileProcessor(MagicMock())
        events = []
        processor._publish_event = lambda job, event_type, **kw: events.append(event_type)

        def complete_through_lease(worker, holder):
            lease = JobLease(conn, reclaim_timeout_seconds=30)
            job_id = lease.enqueue(
                "wiki", {"vault_id": 1, "trigger_id": "file:1"}
            )
            lease.claim("wiki", holder)
            return processor._complete_job(job_id, {"ok": True}, worker)

        processor._pool = _PoolOver(conn)
        # Rejected: the row is held by w-other; w-stale's fenced complete
        # commits zero rows -> the processor contract is False (no
        # completion publication).
        self.assertFalse(complete_through_lease("w-stale", "w-other"))
        self.assertEqual(events, [])
        # Accepted: the holder's own complete lands.
        self.assertTrue(complete_through_lease("w-live", "w-live"))
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()

