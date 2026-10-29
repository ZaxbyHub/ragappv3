"""Issue #765 acceptance checks (frozen): wiki status vocabulary for a
PENDING ingest job (C1) and duplicate enqueue on a second manual compile
while one is already pending (C2).

Fixture strategy mirrors backend/tests/test_559_s2_c4_readers_unified.py
(TestC3WikiSemanticStatusMapping): a temp DB created through run_migrations,
the files row the compile route requires planted with plain SQL, everything
else planted through public WikiStore methods, and every assertion made
through the public route handlers called directly with a permissive
evaluate — never by re-reading the planted SQL.

C1 (DISCRIMINATING): a PENDING ingest job is transient work in flight, so the
semantic status endpoint must surface it as such — it must NOT read as
``not_compiled`` (the "never touched this document" verdict). At base the
semantic mapping folds ``pending`` into the ``not_compiled`` bucket, so the
UI offers a fresh Compile while a compile is already queued. RED at base,
GREEN after the fix.

C2 (DISCRIMINATING): a second manual compile while an ingest job for the same
file is already pending must not enqueue a duplicate job. At base every call
to the compile route inserts a fresh pending row unconditionally. RED at
base, GREEN after the fix.

Amended after review round 1 (falsifiability gaps):

- C1 additionally pins the EXACT transient verdict (``compiling``), not just
  "not not_compiled", and the temp DB directory is removed in tearDown.
- C2 additionally asserts the idempotent-return RESPONSE PAYLOAD on the
  second click (the existing job's id and status, not a fresh job), and a
  sibling test covers the RUNNING arm of the same guard.
- Harness note: the fixed compile route holds a ``BEGIN IMMEDIATE``
  transaction across the handler (PRR-012) and the unified storage arm
  deliberately leaves the commit to the caller's request boundary. The
  direct-call harness below therefore performs that boundary commit
  (``self.conn.commit()``) after each handler call — the connection must not
  enter the NEXT call still inside the route's write transaction.
- Concurrency pin skipped on purpose: the "lookup -> create is atomic across
  processes" property lives in the route's ``BEGIN IMMEDIATE`` framing, which
  a single-connection unit harness cannot falsify either way (there is no
  second connection to race). That property is enforced by CI-reviewed code,
  not by a unit pin here.
"""

import json
import shutil
import sqlite3
import unittest
from pathlib import Path
from tempfile import mkdtemp

_USER = {"id": 1, "username": "checker", "role": "superadmin", "is_active": True}


async def _allow_all(user, resource_type, resource_id, action):
    del user, resource_type, resource_id, action
    return True


def _connect(db_path):
    # check_same_thread=False: matches the production pool connections the
    # route handlers run against.
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _make_db():
    from app.models.database import run_migrations

    db_path = str(Path(mkdtemp()) / "k765-wiki-status.db")
    run_migrations(db_path)
    conn = _connect(db_path)
    conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'V1')")
    conn.commit()
    return db_path, conn


def _plant_probe_file(conn):
    """Plant the files row the compile route requires (id 71, vault 1)."""
    conn.execute(
        "INSERT INTO files (id, vault_id, file_path, file_name, file_size) "
        "VALUES (71, 1, 'data/probe.pdf', 'probe.pdf', 1024)"
    )
    conn.commit()


def _plant_running_ingest_job(conn):
    """Plant a RUNNING wiki ingest job for file 71, as a worker would leave it.

    Mirrors the _plant_job harness of backend/tests/test_559_s2_c4_readers_
    unified.py (worker_id + heartbeat + started_at on a unified jobs row).
    """
    now = "2026-01-01T00:00:00"
    payload = {
        "vault_id": 1,
        "trigger_type": "ingest",
        "trigger_id": "file:71",
        "input_json": {"file_id": 71, "vault_id": 1},
    }
    cur = conn.execute(
        "INSERT INTO jobs (queue, payload_json, status, worker_id, "
        "lease_generation, attempts, error, result_json, created_at, "
        "started_at, heartbeat_at, completed_at) "
        "VALUES (?, ?, 'running', 'w1', 0, 0, NULL, '{}', ?, ?, ?, NULL)",
        ("wiki", json.dumps(payload), now, now, now),
    )
    conn.commit()
    return int(cur.lastrowid)


class TestC1PendingIngestTransientStatus(unittest.IsolatedAsyncioTestCase):
    # check: C1 (DISCRIMINATING) — a pending ingest job must surface as a
    # transient wiki status, not as "not_compiled". RED at base, GREEN after
    # the fix.

    async def asyncSetUp(self):
        from app.api.routes.wiki import get_document_wiki_status
        from app.services.wiki_store import WikiStore

        self.db_path, self.conn = _make_db()
        self._tmp_root = Path(self.db_path).parent
        _plant_probe_file(self.conn)
        self._get_document_wiki_status = get_document_wiki_status
        self.store = WikiStore(self.conn)

    async def asyncTearDown(self):
        self.conn.close()
        shutil.rmtree(self._tmp_root, ignore_errors=True)

    async def test_pending_ingest_job_reports_transient_status(self):
        # Enqueue an ingest job through the public store (the same call the
        # compile route makes).
        self.store.create_job(
            vault_id=1,
            trigger_type="ingest",
            trigger_id="file:71",
            input_json={"file_id": 71, "vault_id": 1},
        )
        self.conn.commit()

        status = await self._get_document_wiki_status(
            file_id=71,
            vault_id=1,
            db=self.conn,
            user=_USER,
            evaluate=_allow_all,
        )
        # Guard: the pending job IS served as the latest job (guards against
        # the two-table write path where the store and the reader disagree on
        # storage — in that world this assert fails for the wrong reason).
        self.assertIsNotNone(status["latest_job"])
        assert status["latest_job"]["status"] == "pending"
        # Discriminating assertion: pending is transient work in flight — the
        # semantic status must not collapse it onto "not_compiled".
        # RED at base, GREEN after the fix.
        assert status["wiki_status"] != "not_compiled"
        # Exact vocabulary pin (review round 1): the transient verdict is
        # "compiling" — the same value the UI polls on — not merely "anything
        # other than not_compiled".
        self.assertEqual(status["wiki_status"], "compiling")


class TestC2SecondCompileWhilePending(unittest.IsolatedAsyncioTestCase):
    # check: C2 (DISCRIMINATING) — a second manual compile while an ingest
    # job for the same file is already pending must not enqueue a duplicate.
    # RED at base, GREEN after the fix.

    async def asyncSetUp(self):
        from app.api.routes.wiki import compile_document_wiki
        from app.services.wiki_store import WikiStore

        self.db_path, self.conn = _make_db()
        self._tmp_root = Path(self.db_path).parent
        _plant_probe_file(self.conn)
        self._compile_document_wiki = compile_document_wiki
        self.store = WikiStore(self.conn)

    async def asyncTearDown(self):
        self.conn.close()
        shutil.rmtree(self._tmp_root, ignore_errors=True)

    def _pending_count(self):
        jobs = self.store.list_jobs(
            1, trigger_type="ingest", trigger_id="file:71"
        )
        return sum(1 for job in jobs if job.status == "pending")

    def _job_count(self):
        return len(
            self.store.list_jobs(1, trigger_type="ingest", trigger_id="file:71")
        )

    async def _compile(self):
        response = await self._compile_document_wiki(
            file_id=71,
            vault_id=1,
            db=self.conn,
            user=_USER,
            evaluate=_allow_all,
            _csrf_token="x",
        )
        # Request boundary (see the harness note in the module docstring):
        # the handler's write transaction ends here, exactly as it would at
        # the edge of a real request.
        self.conn.commit()
        return response

    async def test_second_compile_while_pending_enqueues_no_duplicate(self):
        first = await self._compile()
        # Guard: the first compile actually enqueued exactly one pending job.
        assert self._pending_count() == 1
        # Response payload pin (review round 1): the first click reports the
        # job IT created, with the pending status it created it in.
        self.assertEqual(first["status"], "pending")
        first_job_id = first["job_id"]

        second = await self._compile()
        # Discriminating assertion: the second compile while the first job is
        # still pending must not enqueue a second pending job.
        # RED at base, GREEN after the fix.
        assert self._pending_count() == 1
        # Response payload pin: the idempotent return is the EXISTING pending
        # job — same id, same status — not a fresh job and not a synthetic
        # echo. At base the second response carries a brand-new job_id.
        self.assertEqual(
            second, {"job_id": first_job_id, "status": "pending"}
        )

    async def test_second_compile_while_running_returns_existing(self):
        # RUNNING arm of the same guard (review round 1): a worker already
        # picked the job up — a second Compile must return THAT job (status
        # "running") and must not enqueue anything.
        planted_id = _plant_running_ingest_job(self.conn)
        jobs_before = self._job_count()

        response = await self._compile()

        self.assertEqual(
            response, {"job_id": planted_id, "status": "running"}
        )
        # No duplicate was created: the vault's ingest-job count is unchanged.
        assert self._job_count() == jobs_before
