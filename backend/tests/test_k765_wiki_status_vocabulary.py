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
"""

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


class TestC1PendingIngestTransientStatus(unittest.IsolatedAsyncioTestCase):
    # check: C1 (DISCRIMINATING) — a pending ingest job must surface as a
    # transient wiki status, not as "not_compiled". RED at base, GREEN after
    # the fix.

    async def asyncSetUp(self):
        from app.api.routes.wiki import get_document_wiki_status
        from app.services.wiki_store import WikiStore

        self.db_path, self.conn = _make_db()
        _plant_probe_file(self.conn)
        self._get_document_wiki_status = get_document_wiki_status
        self.store = WikiStore(self.conn)

    async def asyncTearDown(self):
        self.conn.close()

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


class TestC2SecondCompileWhilePending(unittest.IsolatedAsyncioTestCase):
    # check: C2 (DISCRIMINATING) — a second manual compile while an ingest
    # job for the same file is already pending must not enqueue a duplicate.
    # RED at base, GREEN after the fix.

    async def asyncSetUp(self):
        from app.api.routes.wiki import compile_document_wiki
        from app.services.wiki_store import WikiStore

        self.db_path, self.conn = _make_db()
        _plant_probe_file(self.conn)
        self._compile_document_wiki = compile_document_wiki
        self.store = WikiStore(self.conn)

    async def asyncTearDown(self):
        self.conn.close()

    def _pending_count(self):
        jobs = self.store.list_jobs(
            1, trigger_type="ingest", trigger_id="file:71"
        )
        return sum(1 for job in jobs if job.status == "pending")

    async def test_second_compile_while_pending_enqueues_no_duplicate(self):
        await self._compile_document_wiki(
            file_id=71,
            vault_id=1,
            db=self.conn,
            user=_USER,
            evaluate=_allow_all,
            _csrf_token="x",
        )
        # Guard: the first compile actually enqueued exactly one pending job.
        assert self._pending_count() == 1

        await self._compile_document_wiki(
            file_id=71,
            vault_id=1,
            db=self.conn,
            user=_USER,
            evaluate=_allow_all,
            _csrf_token="x",
        )
        # Discriminating assertion: the second compile while the first job is
        # still pending must not enqueue a second pending job.
        # RED at base, GREEN after the fix.
        assert self._pending_count() == 1
