"""Issue #559 stage-2 acceptance check C4 (DISCRIMINATING): readers serve
unchanged response shapes from the unified job store.

After stage 2, wiki and KMS compile jobs live in the shared ``jobs`` table
(``queue='wiki'`` / ``queue='kms'``, payload carrying vault_id /
trigger_type / trigger_id / input_json). The reader surface must not change:

- the wiki semantic status endpoint still maps job state to
  ``not_compiled|compiling|compiled|failed|skipped`` (cancelled ->
  not_compiled, completed+skipped result -> skipped);
- the batched document status route still reports per-file
  wiki_status/kms_status;
- the wiki job list/get/retry/cancel endpoints and the KMS job list/get
  endpoints keep their response fields (trigger_type, trigger_id, status,
  error, result_json, created_at, started_at, completed_at, vault scoping);
- historical (completed/failed/cancelled) rows copied by the boot migration
  remain visible through the list endpoints.

Fixture strategy: rows are planted directly in the shared ``jobs`` table in
exactly the shape the stage-2 migration produces (the same planting pattern
the stage-1 lease tests use for the primitive). This includes terminal
history rows — the end state of what the boot migration copies. Assertions
are made exclusively through the public route handlers (called directly with
a permissive evaluate), never by re-reading the planted SQL.

Expectation: RED at the stage-2 base — the readers still query the legacy
``wiki_compile_jobs`` / ``kms_compile_jobs`` tables, so unified rows are
invisible (statuses come back wrong/None, jobs lists are empty, job lookups
raise 404). GREEN post-migration.
"""

import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import mkdtemp

import pytest
from fastapi import HTTPException

_USER = {"id": 1, "username": "checker", "role": "superadmin", "is_active": True}


async def _allow_all(user, resource_type, resource_id, action):
    del user, resource_type, resource_id, action
    return True


def _connect(db_path):
    # check_same_thread=False: the batched status route executes on the conn
    # via asyncio.to_thread, like the production pool connections it uses.
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _make_db():
    from app.models.database import run_migrations

    db_path = str(Path(mkdtemp()) / "s2-readers.db")
    run_migrations(db_path)  # legacy tables AND the shared jobs table
    conn = _connect(db_path)
    conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'V1')")
    conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (2, 'V2')")
    conn.commit()
    return db_path, conn


def _plant_job(
    conn,
    queue,
    *,
    vault_id,
    trigger_type,
    trigger_id,
    status,
    result_json="{}",
    error=None,
    input_json=None,
    created_at="2026-01-01T00:00:00",
    started_at=None,
    completed_at=None,
    worker_id=None,
    heartbeat_at=None,
):
    """Plant one row in the unified store exactly as stage 2 shapes it.

    Fixture setup only (the stage-1 tests plant ``jobs`` rows the same way);
    every assertion below reads through the public route handlers.
    """
    payload = {
        "vault_id": vault_id,
        "trigger_type": trigger_type,
        "trigger_id": trigger_id,
        "input_json": input_json if input_json is not None else {},
    }
    cur = conn.execute(
        "INSERT INTO jobs (queue, payload_json, status, worker_id, "
        "lease_generation, attempts, error, result_json, created_at, "
        "started_at, heartbeat_at, completed_at) "
        "VALUES (?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, ?)",
        (
            queue,
            json.dumps(payload),
            status,
            worker_id,
            error,
            result_json,
            created_at,
            started_at,
            heartbeat_at,
            completed_at,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _carried_input(job_dict):
    """input_json as a dict whether the API echoes it dict- or string-form."""
    raw = job_dict.get("input_json")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return raw if isinstance(raw, dict) else {}


class TestC3WikiSemanticStatusMapping(unittest.IsolatedAsyncioTestCase):
    # check: C4 (DISCRIMINATING) — the wiki semantic status endpoint maps
    # unified job states onto the unchanged five-value surface.

    async def asyncSetUp(self):
        from app.api.routes.wiki import get_document_wiki_status
        from app.services.wiki_store import WikiStore

        self.db_path, self.conn = _make_db()
        self._get_document_wiki_status = get_document_wiki_status
        self.store = WikiStore(self.conn)

    async def asyncTearDown(self):
        self.conn.close()

    async def _status(self, file_id):
        return await self._get_document_wiki_status(
            file_id=file_id,
            vault_id=1,
            db=self.conn,
            user=_USER,
            evaluate=_allow_all,
        )

    async def test_job_state_to_semantic_status_mapping(self):
        now = "2026-01-01T00:00:00"
        _plant_job(
            self.conn,
            "wiki",
            vault_id=1,
            trigger_type="ingest",
            trigger_id="file:71",
            status="running",
            worker_id="w1",
            heartbeat_at=now,
            started_at=now,
        )
        _plant_job(
            self.conn,
            "wiki",
            vault_id=1,
            trigger_type="ingest",
            trigger_id="file:72",
            status="failed",
            error="compile exploded",
            started_at=now,
            completed_at=now,
        )
        _plant_job(
            self.conn,
            "wiki",
            vault_id=1,
            trigger_type="ingest",
            trigger_id="file:73",
            status="cancelled",
            completed_at=now,
        )
        _plant_job(
            self.conn,
            "wiki",
            vault_id=1,
            trigger_type="ingest",
            trigger_id="file:74",
            status="completed",
            result_json=json.dumps({"skipped": True}),
            completed_at=now,
        )
        _plant_job(
            self.conn,
            "wiki",
            vault_id=1,
            trigger_type="ingest",
            trigger_id="file:75",
            status="completed",
            result_json="{}",
            completed_at=now,
        )
        # A claim sourced from file 75 makes the completed job "compiled"
        # (public store methods, not fixture SQL).
        claim = self.store.create_claim(
            vault_id=1,
            claim_text="File 75 produced this claim",
            source_type="document",
        )
        self.store.attach_source(
            claim_id=claim.id,
            source_kind="document",
            file_id=75,
            source_label="file:75",
        )
        self.conn.commit()

        # file 70 has no job at all -> not_compiled (folded in here so the
        # test stays RED at base for the right reason).
        self.assertEqual((await self._status(70))["wiki_status"], "not_compiled")
        self.assertEqual((await self._status(71))["wiki_status"], "compiling")
        self.assertEqual((await self._status(72))["wiki_status"], "failed")
        # cancelled jobs read as never-compiled
        self.assertEqual((await self._status(73))["wiki_status"], "not_compiled")
        # completed + skipped result -> skipped
        self.assertEqual((await self._status(74))["wiki_status"], "skipped")
        self.assertEqual((await self._status(75))["wiki_status"], "compiled")

    async def test_latest_job_echoes_unified_row(self):
        now = "2026-01-01T00:00:00"
        _plant_job(
            self.conn,
            "wiki",
            vault_id=1,
            trigger_type="ingest",
            trigger_id="file:71",
            status="running",
            worker_id="w1",
            heartbeat_at=now,
            started_at=now,
        )
        resp = await self._status(71)
        latest = resp["latest_job"]
        self.assertIsNotNone(latest, "the unified running row must be served")
        self.assertEqual(latest["status"], "running")
        self.assertEqual(latest["trigger_type"], "ingest")
        self.assertEqual(latest["trigger_id"], "file:71")
        self.assertIsNotNone(latest["started_at"])


class TestC3BatchedDocumentStatus(unittest.IsolatedAsyncioTestCase):
    # check: C4 (DISCRIMINATING) — the batched document status route reports
    # per-file wiki_status/kms_status from the unified store.

    async def asyncSetUp(self):
        from app.api.routes.documents import get_documents_status_batched

        self.db_path, self.conn = _make_db()
        self._get_documents_status_batched = get_documents_status_batched
        for fid in (81, 82):
            self.conn.execute(
                "INSERT INTO files (id, vault_id, file_path, file_name, "
                "file_size, status, phase, source) VALUES (?, 1, ?, ?, 1, "
                "'indexed', 'queued', 'upload')",
                (fid, f"/tmp/f{fid}.txt", f"f{fid}.txt"),
            )
        self.conn.commit()

    async def asyncTearDown(self):
        self.conn.close()

    async def test_batched_status_reports_per_file_wiki_and_kms_statuses(self):
        now = "2026-01-01T00:00:00"
        _plant_job(
            self.conn,
            "wiki",
            vault_id=1,
            trigger_type="ingest",
            trigger_id="file:81",
            status="running",
            worker_id="w",
            heartbeat_at=now,
        )
        _plant_job(
            self.conn,
            "wiki",
            vault_id=1,
            trigger_type="ingest",
            trigger_id="file:82",
            status="failed",
            error="boom",
        )
        _plant_job(
            self.conn,
            "kms",
            vault_id=1,
            trigger_type="ingest",
            trigger_id="file:81",
            status="completed",
            result_json='{"entries": 3}',
            completed_at=now,
        )
        _plant_job(
            self.conn,
            "kms",
            vault_id=1,
            trigger_type="ingest",
            trigger_id="file:82",
            status="pending",
        )

        resp = await self._get_documents_status_batched(
            ids="81,82",
            vault_id=1,
            conn=self.conn,
            user=_USER,
            evaluate=_allow_all,
        )
        entries = {entry.id: entry for entry in resp.results}
        self.assertEqual(set(entries), {81, 82})
        self.assertEqual(entries[81].wiki_status, "running")
        self.assertEqual(entries[81].kms_status, "completed")
        self.assertEqual(entries[82].wiki_status, "failed")
        self.assertEqual(entries[82].kms_status, "pending")


class TestC3WikiJobEndpoints(unittest.IsolatedAsyncioTestCase):
    # check: C4 (DISCRIMINATING) — wiki job list/get/retry/cancel endpoints
    # keep their response fields and vault scoping over the unified store.

    async def asyncSetUp(self):
        from app.api.routes.wiki import (
            cancel_wiki_job,
            get_wiki_job,
            list_wiki_jobs,
            retry_wiki_job,
        )

        self.db_path, self.conn = _make_db()
        self._list_wiki_jobs = list_wiki_jobs
        self._get_wiki_job = get_wiki_job
        self._retry_wiki_job = retry_wiki_job
        self._cancel_wiki_job = cancel_wiki_job
        now = "2026-01-01T00:00:00"
        self.pending_id = _plant_job(
            self.conn,
            "wiki",
            vault_id=1,
            trigger_type="ingest",
            trigger_id="file:90",
            status="pending",
            input_json={"file_id": 90},
            created_at="2026-01-01T00:00:04",
        )
        self.failed_id = _plant_job(
            self.conn,
            "wiki",
            vault_id=1,
            trigger_type="ingest",
            trigger_id="file:91",
            status="failed",
            error="compile exploded",
            input_json={"file_id": 91},
            started_at=now,
            completed_at=now,
            created_at="2026-01-01T00:00:03",
        )
        self.completed_id = _plant_job(
            self.conn,
            "wiki",
            vault_id=1,
            trigger_type="ingest",
            trigger_id="file:92",
            status="completed",
            result_json='{"page": null}',
            input_json={"file_id": 92},
            completed_at=now,
            created_at="2026-01-01T00:00:02",
        )
        self.cancelled_id = _plant_job(
            self.conn,
            "wiki",
            vault_id=1,
            trigger_type="ingest",
            trigger_id="file:93",
            status="cancelled",
            completed_at=now,
            created_at="2026-01-01T00:00:01",
        )
        self.other_vault_id = _plant_job(
            self.conn,
            "wiki",
            vault_id=2,
            trigger_type="ingest",
            trigger_id="file:290",
            status="failed",
            error="other vault",
            created_at="2026-01-01T00:00:00",
        )

    async def asyncTearDown(self):
        self.conn.close()

    async def _list(self):
        resp = await self._list_wiki_jobs(
            vault_id=1,
            status=None,
            db=self.conn,
            user=_USER,
            evaluate=_allow_all,
        )
        return {job["id"]: job for job in resp["jobs"]}

    async def test_list_wiki_jobs_serves_unified_rows_with_full_fields(self):
        jobs = await self._list()
        self.assertIn(
            self.failed_id, jobs, "unified wiki rows must appear in the job list"
        )
        failed = jobs[self.failed_id]
        self.assertEqual(failed["vault_id"], 1)
        self.assertEqual(failed["trigger_type"], "ingest")
        self.assertEqual(failed["trigger_id"], "file:91")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"], "compile exploded")
        self.assertIn("result_json", failed)
        self.assertIsNotNone(failed["created_at"])
        self.assertIsNotNone(failed["started_at"])
        self.assertIsNotNone(failed["completed_at"])
        self.assertEqual(_carried_input(failed).get("file_id"), 91)
        # history of every terminal shape stays visible
        self.assertIn(self.completed_id, jobs)
        self.assertEqual(jobs[self.completed_id]["status"], "completed")
        self.assertIn(self.cancelled_id, jobs)
        self.assertEqual(jobs[self.cancelled_id]["status"], "cancelled")
        # vault scoping: vault 2's row is invisible to vault 1's list
        self.assertNotIn(self.other_vault_id, jobs)

    async def test_get_wiki_job_serves_unified_row_and_scopes_vault(self):
        got = await self._get_wiki_job(
            job_id=self.failed_id,
            vault_id=1,
            db=self.conn,
            user=_USER,
            evaluate=_allow_all,
        )
        self.assertEqual(got["status"], "failed")
        self.assertEqual(got["error"], "compile exploded")
        self.assertEqual(got["trigger_id"], "file:91")

        with pytest.raises(HTTPException) as ctx:
            await self._get_wiki_job(
                job_id=self.failed_id,
                vault_id=2,
                db=self.conn,
                user=_USER,
                evaluate=_allow_all,
            )
        self.assertEqual(ctx.value.status_code, 404)

    async def test_retry_wiki_job_resets_failed_unified_row(self):
        resp = await self._retry_wiki_job(
            job_id=self.failed_id,
            vault_id=1,
            db=self.conn,
            user=_USER,
            evaluate=_allow_all,
            _csrf_token="test",
        )
        self.assertEqual(resp["status"], "pending")
        got = await self._get_wiki_job(
            job_id=self.failed_id,
            vault_id=1,
            db=self.conn,
            user=_USER,
            evaluate=_allow_all,
        )
        self.assertEqual(got["status"], "pending")
        # only failed jobs are retryable — a completed one 404s
        with pytest.raises(HTTPException) as ctx:
            await self._retry_wiki_job(
                job_id=self.completed_id,
                vault_id=1,
                db=self.conn,
                user=_USER,
                evaluate=_allow_all,
                _csrf_token="test",
            )
        self.assertEqual(ctx.value.status_code, 404)

    async def test_cancel_wiki_job_cancels_pending_unified_row(self):
        resp = await self._cancel_wiki_job(
            job_id=self.pending_id,
            vault_id=1,
            db=self.conn,
            user=_USER,
            evaluate=_allow_all,
            _csrf_token="test",
        )
        self.assertEqual(resp, {"job_id": self.pending_id, "status": "cancelled"})
        got = await self._get_wiki_job(
            job_id=self.pending_id,
            vault_id=1,
            db=self.conn,
            user=_USER,
            evaluate=_allow_all,
        )
        self.assertEqual(got["status"], "cancelled")
        # terminal rows are not cancellable — a completed one 404s
        with pytest.raises(HTTPException) as ctx:
            await self._cancel_wiki_job(
                job_id=self.completed_id,
                vault_id=1,
                db=self.conn,
                user=_USER,
                evaluate=_allow_all,
                _csrf_token="test",
            )
        self.assertEqual(ctx.value.status_code, 404)


class TestC3KmsJobEndpoints(unittest.IsolatedAsyncioTestCase):
    # check: C4 (DISCRIMINATING) — KMS job list/get endpoints keep their
    # response fields and vault scoping over the unified store.

    async def asyncSetUp(self):
        from app.api.routes.kms import get_kms_job, list_kms_jobs

        self.db_path, self.conn = _make_db()
        self._list_kms_jobs = list_kms_jobs
        self._get_kms_job = get_kms_job
        now = "2026-01-01T00:00:00"
        self.completed_id = _plant_job(
            self.conn,
            "kms",
            vault_id=1,
            trigger_type="ingest",
            trigger_id="file:95",
            status="completed",
            result_json='{"entries": 5}',
            input_json={"file_id": 95},
            completed_at=now,
            created_at="2026-01-01T00:00:02",
        )
        self.failed_id = _plant_job(
            self.conn,
            "kms",
            vault_id=1,
            trigger_type="ingest",
            trigger_id="file:96",
            status="failed",
            error="kms compile exploded",
            started_at=now,
            completed_at=now,
            created_at="2026-01-01T00:00:01",
        )
        self.other_vault_id = _plant_job(
            self.conn,
            "kms",
            vault_id=2,
            trigger_type="manual",
            trigger_id="vault:2",
            status="completed",
            created_at="2026-01-01T00:00:00",
        )

    async def asyncTearDown(self):
        self.conn.close()

    async def test_list_kms_jobs_serves_unified_rows_with_full_fields(self):
        resp = await self._list_kms_jobs(
            vault_id=1,
            status=None,
            db=self.conn,
            user=_USER,
            evaluate=_allow_all,
            _=None,
        )
        jobs = {job["id"]: job for job in resp["jobs"]}
        self.assertIn(
            self.failed_id, jobs, "unified kms rows must appear in the job list"
        )
        failed = jobs[self.failed_id]
        self.assertEqual(failed["vault_id"], 1)
        self.assertEqual(failed["trigger_type"], "ingest")
        self.assertEqual(failed["trigger_id"], "file:96")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"], "kms compile exploded")
        self.assertIsNotNone(failed["created_at"])
        self.assertIsNotNone(failed["started_at"])
        self.assertIsNotNone(failed["completed_at"])
        completed = jobs[self.completed_id]
        self.assertEqual(completed["status"], "completed")
        self.assertIn("entries", completed["result_json"])
        self.assertEqual(_carried_input(completed).get("file_id"), 95)
        # vault scoping: vault 2's row is invisible to vault 1's list
        self.assertNotIn(self.other_vault_id, jobs)

    async def test_get_kms_job_serves_unified_row_and_scopes_vault(self):
        got = await self._get_kms_job(
            job_id=self.completed_id,
            vault_id=1,
            db=self.conn,
            user=_USER,
            evaluate=_allow_all,
            _=None,
        )
        self.assertEqual(got["status"], "completed")
        self.assertEqual(got["trigger_id"], "file:95")
        self.assertIn("entries", got["result_json"])

        with pytest.raises(HTTPException) as ctx:
            await self._get_kms_job(
                job_id=self.completed_id,
                vault_id=2,
                db=self.conn,
                user=_USER,
                evaluate=_allow_all,
                _=None,
            )
        self.assertEqual(ctx.value.status_code, 404)


class TestC3MigratedHistoryVisibility(unittest.IsolatedAsyncioTestCase):
    # check: C4 (DISCRIMINATING) — historical terminal rows as copied by the
    # boot migration (completed/failed/cancelled in the unified store) remain
    # visible through the wiki and KMS list endpoints.

    async def asyncSetUp(self):
        from app.api.routes.kms import list_kms_jobs
        from app.api.routes.wiki import list_wiki_jobs

        self.db_path, self.conn = _make_db()
        self._list_wiki_jobs = list_wiki_jobs
        self._list_kms_jobs = list_kms_jobs
        now = "2025-12-31T00:00:00"  # older than any live job
        self.wiki_ids = {
            "completed": _plant_job(
                self.conn,
                "wiki",
                vault_id=1,
                trigger_type="ingest",
                trigger_id="file:101",
                status="completed",
                result_json='{"page": null}',
                completed_at=now,
                created_at=now,
            ),
            "failed": _plant_job(
                self.conn,
                "wiki",
                vault_id=1,
                trigger_type="ingest",
                trigger_id="file:102",
                status="failed",
                error="historical failure",
                completed_at=now,
                created_at=now,
            ),
            "cancelled": _plant_job(
                self.conn,
                "wiki",
                vault_id=1,
                trigger_type="ingest",
                trigger_id="file:103",
                status="cancelled",
                completed_at=now,
                created_at=now,
            ),
        }
        self.kms_ids = {
            "completed": _plant_job(
                self.conn,
                "kms",
                vault_id=1,
                trigger_type="ingest",
                trigger_id="file:104",
                status="completed",
                result_json='{"entries": 1}',
                completed_at=now,
                created_at=now,
            ),
            "failed": _plant_job(
                self.conn,
                "kms",
                vault_id=1,
                trigger_type="ingest",
                trigger_id="file:105",
                status="failed",
                error="historical kms failure",
                completed_at=now,
                created_at=now,
            ),
            "cancelled": _plant_job(
                self.conn,
                "kms",
                vault_id=1,
                trigger_type="ingest",
                trigger_id="file:106",
                status="cancelled",
                completed_at=now,
                created_at=now,
            ),
        }

    async def asyncTearDown(self):
        self.conn.close()

    async def test_migrated_terminal_history_remains_visible_in_list_endpoints(self):
        wiki_resp = await self._list_wiki_jobs(
            vault_id=1,
            status=None,
            db=self.conn,
            user=_USER,
            evaluate=_allow_all,
        )
        wiki_jobs = {job["id"]: job for job in wiki_resp["jobs"]}
        for shape, job_id in self.wiki_ids.items():
            self.assertIn(
                job_id,
                wiki_jobs,
                f"migrated {shape} wiki history must stay visible",
            )
            self.assertEqual(wiki_jobs[job_id]["status"], shape)

        kms_resp = await self._list_kms_jobs(
            vault_id=1,
            status=None,
            db=self.conn,
            user=_USER,
            evaluate=_allow_all,
            _=None,
        )
        kms_jobs = {job["id"]: job for job in kms_resp["jobs"]}
        for shape, job_id in self.kms_ids.items():
            self.assertIn(
                job_id,
                kms_jobs,
                f"migrated {shape} kms history must stay visible",
            )
            self.assertEqual(kms_jobs[job_id]["status"], shape)
