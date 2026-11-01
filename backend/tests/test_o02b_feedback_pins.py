"""Issue #792 feedback-round positive pins (PR #951 review findings PRR-001,
PRR-002, PRR-008, PRR-010, PRR-011, PRR-018, PRR-020).

The frozen redaction checks assert sentinel ABSENCE only; this file pins the
POSITIVE contracts they cannot see, plus the malformed-payload disposition:

- exact composed codes at the reader boundary (route returns
  ``attempt_cap_exceeded: COMPILE_FAILED: ...`` with ``status='failed'``;
  the wiki event carries the bare constant as its ``error`` value);
- a malformed (unparseable) or valid-JSON-non-object payload row is settled
  TERMINALLY FAILED at claim with ``PAYLOAD_MALFORMED`` — never dispatched
  as a vault-less "manual" job that would complete as a false success;
- a valid job still claims, dispatches and completes (the store guards and
  the claim-time disposition do not over-block or swallow claims);
- the below-cap requeue branch persists the code (the common production
  failure path);
- legacy (lease-disabled) mode redacts identically, including the legacy
  ``job_failed`` publish;
- the remaining guarded SQL reader paths behave (list/get/cancel/retry) on
  a malformed row without raising.

GREEN at the feedback head; each test names the mutation it kills in a
comment. Harness mirrors the frozen o02 files (real poll loops, temp
run_migrations DB, recording event bus, direct route calls).
"""

import asyncio
import shutil
import unittest
from pathlib import Path
from tempfile import mkdtemp
from unittest.mock import patch

from app.config import settings

TERMINAL_STATUSES = ("failed", "completed", "cancelled")
SETTLE_TIMEOUT_SECONDS = 15.0
SETTLE_POLL_INTERVAL_SECONDS = 0.1

_WIKI_CODE = "COMPILE_FAILED: wiki compile dispatch failed; see server logs"
_KMS_CODE = "COMPILE_FAILED: KMS compile dispatch failed; see server logs"
_PAYLOAD_CODE = (
    "PAYLOAD_MALFORMED: compile job payload is corrupt; see server logs"
)

_USER = {"id": 1, "username": "checker", "role": "superadmin", "is_active": True}


async def _allow_all(user, resource_type, resource_id, action):
    del user, resource_type, resource_id, action
    return True


def _make_pool():
    from app.models.database import SQLiteConnectionPool, run_migrations

    tmp_dir = mkdtemp(prefix="o02b-")
    db_path = str(Path(tmp_dir) / "o02b-pins.db")
    run_migrations(db_path)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    with pool.connection() as conn:
        conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'V1')")
        conn.commit()
    return pool, tmp_dir


class _RecordingBus:
    def __init__(self):
        self.events = []

    def publish(self, vault_id, payload):
        self.events.append({"vault_id": vault_id, **payload})


class _FeedbackPins(unittest.IsolatedAsyncioTestCase):
    def _lease_patches(self, lease_on=True, cap=1):
        lease_patcher = patch.object(
            settings, "wiki_kms_job_lease_enabled", lease_on
        )
        lease_patcher.start()
        self.addCleanup(lease_patcher.stop)
        cap_patcher = patch.object(settings, "jobs_max_attempts", cap)
        cap_patcher.start()
        self.addCleanup(cap_patcher.stop)

    def _make_pool_with_cleanup(self):
        pool, tmp_dir = _make_pool()
        self.addCleanup(self.pool_cleanup, tmp_dir)
        self.addCleanup(pool.close_all)
        return pool

    def pool_cleanup(self, tmp_dir):
        shutil.rmtree(tmp_dir, ignore_errors=True)

    def _plant_row(self, queue, payload):
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO jobs (queue, payload_json, status) VALUES (?, ?, 'pending')",
                (queue, payload),
            )
            conn.commit()

    def _fetch_row(self, queue):
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT status, error FROM jobs WHERE queue = ?", (queue,)
            ).fetchone()
        return dict(row) if row is not None else None

    async def _wait_terminal(self, queue):
        deadline = asyncio.get_running_loop().time() + SETTLE_TIMEOUT_SECONDS
        while True:
            row = self._fetch_row(queue)
            if row is not None and row["status"] in TERMINAL_STATUSES:
                return row
            if asyncio.get_running_loop().time() > deadline:
                self.fail(
                    f"queue {queue!r} did not settle terminally within "
                    f"{SETTLE_TIMEOUT_SECONDS}s (last: {row!r})"
                )
            await asyncio.sleep(SETTLE_POLL_INTERVAL_SECONDS)

    async def _wait_event(self, bus, event_type):
        deadline = asyncio.get_running_loop().time() + SETTLE_TIMEOUT_SECONDS
        while True:
            matches = [e for e in bus.events if e.get("type") == event_type]
            if matches:
                return matches[0]
            if asyncio.get_running_loop().time() > deadline:
                self.fail(f"no {event_type} event published within {SETTLE_TIMEOUT_SECONDS}s")
            await asyncio.sleep(SETTLE_POLL_INTERVAL_SECONDS)


class TestPositiveCodePins(_FeedbackPins):
    """Exact-code pins at the reader boundary (kills blanked/null/error-key
    omission mutations the absence-only frozen checks cannot see)."""

    def setUp(self):
        self._lease_patches(lease_on=True, cap=1)
        self.pool = self._make_pool_with_cleanup()
        self.bus = _RecordingBus()
        bus_patcher = patch(
            "app.services.wiki_events.get_wiki_event_bus", return_value=self.bus
        )
        bus_patcher.start()
        self.addCleanup(bus_patcher.stop)

    def _failing_proc(self, processor_cls, sentinel):
        proc = processor_cls(self.pool)

        def boom(_job):
            raise RuntimeError(f"sentinel-{sentinel}")

        proc._dispatch = boom
        return proc

    async def test_wiki_route_error_is_exact_composed_code(self):
        from app.api.routes.wiki import list_wiki_jobs
        from app.services.wiki_compile_processor import WikiCompileProcessor

        with self.pool.connection() as conn:
            from app.services.wiki_store import WikiStore

            WikiStore(conn).create_job(vault_id=1, trigger_type="manual")

        proc = self._failing_proc(WikiCompileProcessor, "WIKI-POS")
        await proc.start()
        try:
            row = await self._wait_terminal("wiki")
        finally:
            await proc.stop()

        # kills: error dropped/nulled/blanked at the settle boundary
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error"], f"attempt_cap_exceeded: {_WIKI_CODE}")

        with self.pool.connection() as conn:
            resp = await list_wiki_jobs(
                vault_id=1, status=None, limit=None, db=conn,
                user=_USER, evaluate=_allow_all,
            )
        job = next(j for j in resp["jobs"] if j["trigger_type"] == "manual")
        # kills: the route serializer dropping or blanking the error field
        self.assertEqual(job["error"], f"attempt_cap_exceeded: {_WIKI_CODE}")

    async def test_wiki_event_error_is_exact_code(self):
        from app.services.wiki_compile_processor import WikiCompileProcessor

        with self.pool.connection() as conn:
            from app.services.wiki_store import WikiStore

            WikiStore(conn).create_job(vault_id=1, trigger_type="manual")

        proc = self._failing_proc(WikiCompileProcessor, "WIKI-EVT")
        await proc.start()
        try:
            await self._wait_terminal("wiki")
            event = await self._wait_event(self.bus, "job_failed")
        finally:
            await proc.stop()

        # kills: omitting the error key entirely (absence checks pass on
        # a missing key; this requires the exact constant value)
        self.assertEqual(event.get("error"), _WIKI_CODE)

    async def test_kms_route_error_is_exact_composed_code(self):
        from app.api.routes.kms import list_kms_jobs
        from app.services.kms_compile_processor import KMSCompileProcessor

        with self.pool.connection() as conn:
            from app.services.kms_store import KMSStore

            KMSStore(conn).create_job(1, "manual")

        proc = self._failing_proc(KMSCompileProcessor, "KMS-POS")
        await proc.start()
        try:
            row = await self._wait_terminal("kms")
        finally:
            await proc.stop()

        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error"], f"attempt_cap_exceeded: {_KMS_CODE}")

        with self.pool.connection() as conn:
            resp = await list_kms_jobs(
                vault_id=1, status=None, db=conn,
                user=_USER, evaluate=_allow_all, _=None,
            )
        job = next(j for j in resp["jobs"] if j["trigger_type"] == "manual")
        self.assertEqual(job["error"], f"attempt_cap_exceeded: {_KMS_CODE}")


class TestMalformedPayloadDisposition(_FeedbackPins):
    """The PRR-002 disposition: degraded rows settle failed, never complete
    as a vault-less false success (kills the dispatch-as-manual mutation and
    the guard-removed AttributeError crash)."""

    def setUp(self):
        self._lease_patches(lease_on=True, cap=1)
        self.pool = self._make_pool_with_cleanup()

    async def test_malformed_payload_settles_failed_wiki(self):
        from app.services.wiki_compile_processor import WikiCompileProcessor

        self._plant_row("wiki", "{not json")
        proc = WikiCompileProcessor(self.pool)
        await proc.start()
        try:
            row = await self._wait_terminal("wiki")
        finally:
            await proc.stop()

        # kills: dispatching the degraded row as a "manual" job (would be
        # status='completed', error=NULL) and the pre-fix claim-time crash
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error"], _PAYLOAD_CODE)

    async def test_malformed_payload_settles_failed_kms(self):
        from app.services.kms_compile_processor import KMSCompileProcessor

        self._plant_row("kms", "{not json")
        proc = KMSCompileProcessor(self.pool)
        await proc.start()
        try:
            row = await self._wait_terminal("kms")
        finally:
            await proc.stop()

        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error"], _PAYLOAD_CODE)

    def test_nonobject_payload_claim_degrades_in_the_store(self):
        # kills: the pre-feedback guard tuple (JSONDecodeError/TypeError
        # only), under which these shapes raised AttributeError out of the
        # claim after the lease committed
        from app.services.wiki_store import WikiStore

        for shape in ("5", "null", '"x"', "[]"):
            with self.subTest(payload=shape):
                self._plant_row("wiki", shape)
                with self.pool.connection() as conn:
                    claimed = WikiStore(conn).claim_next_pending_job("pin-worker")
                    self.assertIsNotNone(claimed)
                    self.assertIsNone(claimed.vault_id)

    async def test_nonobject_payloads_settle_failed(self):
        from app.services.wiki_compile_processor import WikiCompileProcessor

        for shape in ("5", "null", '"x"', "[]"):
            self._plant_row("wiki", shape)

        proc = WikiCompileProcessor(self.pool)
        await proc.start()
        try:
            deadline = asyncio.get_running_loop().time() + SETTLE_TIMEOUT_SECONDS
            while True:
                with self.pool.connection() as conn:
                    unsettled = conn.execute(
                        "SELECT COUNT(*) AS n FROM jobs WHERE queue = 'wiki' "
                        "AND status != 'failed'"
                    ).fetchone()["n"]
                if unsettled == 0:
                    break
                if asyncio.get_running_loop().time() > deadline:
                    self.fail("non-object payload rows did not all settle failed")
                await asyncio.sleep(SETTLE_POLL_INTERVAL_SECONDS)
        finally:
            await proc.stop()
            with self.pool.connection() as conn:
                statuses = [
                    dict(r)["status"]
                    for r in conn.execute(
                        "SELECT status FROM jobs WHERE queue = 'wiki'"
                    ).fetchall()
                ]
        self.assertEqual(set(statuses), {"failed"})


class TestValidFlowAndBranches(_FeedbackPins):
    """Positive flow + branch pins (PRR-010/011/018/020)."""

    async def test_valid_job_still_claims_dispatches_and_completes(self):
        # kills: a broadened claim-time except that swallows every claim
        # (the job would never settle), and any over-blocking shape check
        from app.services.wiki_compile_processor import WikiCompileProcessor
        from app.services.wiki_store import WikiStore

        self._lease_patches(lease_on=True, cap=1)
        self.pool = self._make_pool_with_cleanup()
        with self.pool.connection() as conn:
            job = WikiStore(conn).create_job(vault_id=1, trigger_type="manual")

        proc = WikiCompileProcessor(self.pool)
        await proc.start()
        try:
            deadline = asyncio.get_running_loop().time() + SETTLE_TIMEOUT_SECONDS
            while True:
                row = self._fetch_row("wiki")
                if row is not None and row["status"] == "completed":
                    break
                if asyncio.get_running_loop().time() > deadline:
                    self.fail(f"valid job did not complete (last: {row!r})")
                await asyncio.sleep(SETTLE_POLL_INTERVAL_SECONDS)
        finally:
            await proc.stop()
        self.assertIsNotNone(job)

    async def test_requeue_branch_persists_the_code_below_cap(self):
        # kills: dropping/blanking error on the below-cap requeue branch
        # (the common production failure path with the default cap of 3)
        from app.services.job_lease import JobLease
        from app.services.wiki_store import WikiStore

        self._lease_patches(lease_on=True, cap=3)
        self.pool = self._make_pool_with_cleanup()
        with self.pool.connection() as conn:
            job = WikiStore(conn).create_job(vault_id=1, trigger_type="manual")
            self.assertIsNotNone(JobLease(conn).claim("wiki", "pin-worker"))
            attempts = WikiStore(conn).fail_job(
                job.id, _WIKI_CODE, worker_id="pin-worker"
            )
            self.assertLess(attempts, 3)
            row = conn.execute(
                "SELECT status, error FROM jobs WHERE id = ?", (job.id,)
            ).fetchone()
        data = dict(row)
        self.assertEqual(data["status"], "pending")
        self.assertEqual(data["error"], _WIKI_CODE)

    async def test_legacy_mode_redacts_and_publishes(self):
        # kills: a legacy-branch regression to str(exc) at either sink (the
        # legacy store write and the legacy job_failed publish had no
        # behavioral coverage)
        from app.services import wiki_compile_processor as wcp
        from app.services.wiki_compile_processor import WikiCompileProcessor
        from app.services.wiki_store import WikiStore

        self._lease_patches(lease_on=False, cap=1)
        retries_patcher = patch.object(wcp, "MAX_RETRIES", 0)
        retries_patcher.start()
        self.addCleanup(retries_patcher.stop)
        self.pool = self._make_pool_with_cleanup()
        bus = _RecordingBus()
        bus_patcher = patch(
            "app.services.wiki_events.get_wiki_event_bus", return_value=bus
        )
        bus_patcher.start()
        self.addCleanup(bus_patcher.stop)

        with self.pool.connection() as conn:
            WikiStore(conn).create_job(vault_id=1, trigger_type="manual")

        def boom(_job):
            raise RuntimeError("legacy-sentinel")

        proc = WikiCompileProcessor(self.pool)
        proc._dispatch = boom
        await proc.start()
        try:
            event = await self._wait_event(bus, "job_failed")
        finally:
            await proc.stop()

        self.assertEqual(event.get("error"), _WIKI_CODE)
        with self.pool.connection() as conn:
            legacy = conn.execute(
                "SELECT status, error FROM wiki_compile_jobs LIMIT 1"
            ).fetchone()
        data = dict(legacy)
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["error"], _WIKI_CODE)


class TestMalformedRowReaderPaths(_FeedbackPins):
    """Behavioral pins for the guarded SQL reader paths (PRR-020): the
    remaining sites must exclude/skip a malformed row without raising
    (kills any unwrapped-CASE mutation the census count alone might miss)."""

    def setUp(self):
        self._lease_patches(lease_on=True, cap=1)
        self.pool = self._make_pool_with_cleanup()

    async def test_wiki_get_cancel_retry_on_malformed_row(self):
        from app.services.wiki_store import WikiStore

        self._plant_row("wiki", "{not json")
        with self.pool.connection() as conn:
            store = WikiStore(conn)
            self.assertIsNone(store.get_job(999, 1))
            self.assertFalse(store.cancel_job(999, 1))
            self.assertIsNone(store.retry_job(999, 1))
            # a malformed row itself is excluded from the scoping filters,
            # so even its real id cannot be cancelled/retried by vault scope
            row_id = conn.execute(
                "SELECT id FROM jobs WHERE queue = 'wiki'"
            ).fetchone()["id"]
            self.assertFalse(store.cancel_job(row_id, 1))
            self.assertIsNone(store.retry_job(row_id, 1))

    async def test_kms_list_jobs_skips_malformed_row(self):
        from app.services.kms_store import KMSStore

        with self.pool.connection() as conn:
            KMSStore(conn).create_job(1, "manual")
        self._plant_row("kms", "{not json")
        with self.pool.connection() as conn:
            jobs = KMSStore(conn).list_jobs(1)
        self.assertEqual([j.id for j in jobs], [1])

    async def test_wiki_list_jobs_skips_malformed_row(self):
        from app.services.wiki_store import WikiStore

        with self.pool.connection() as conn:
            WikiStore(conn).create_job(vault_id=1, trigger_type="manual")
        self._plant_row("wiki", "{not json")
        with self.pool.connection() as conn:
            jobs = WikiStore(conn).list_jobs(1)
        self.assertEqual([j.id for j in jobs], [1])


if __name__ == "__main__":
    unittest.main()
