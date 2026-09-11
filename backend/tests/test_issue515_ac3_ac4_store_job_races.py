"""Issue #515 acceptance tests — AC3 (WIKI-003) and AC4 (WIKI-004).

AC3: WikiStore.list_versions orders by ``created_at DESC`` only, so versions
saved within the same timestamp are returned in an unspecified (currently
ascending-insertion) order and ``limit=1`` returns the OLDEST version.
Post-fix the ordering must tie-break on descending insertion id: [3, 2, 1]
and limit=1 -> newest (id 3).

AC4: WikiStore.complete_job / KMSStore.complete_job guard against a
concurrent cancellation with a read-then-write (SELECT status, then an
unconditional UPDATE). A cancellation landing between the SELECT and the
UPDATE is silently overwritten: the job ends 'completed' even though the
user cancelled it. Post-fix complete_job must use a conditional UPDATE
(WHERE status != 'cancelled') so the durable status stays 'cancelled'.

The interleaving is forced deterministically with a connection proxy that
fires cancel_job (on a second real connection) immediately before
complete_job's UPDATE statement executes on the wrapped connection — the
same deterministic-gate technique as ``test_issue514_backend.py``'s
``_GatedConnection``.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow  # noqa: F401
except ImportError:
    import types

    sys.modules["pyarrow"] = types.ModuleType("pyarrow")


def _make_db() -> tuple[str, sqlite3.Connection]:
    """Temp DB with migrations applied and a vault row seeded."""
    from app.models.database import run_migrations

    td = tempfile.mkdtemp()
    db_path = str(Path(td) / "test.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'Test')")
    conn.commit()
    return db_path, conn


class TestIssue515Ac3ListVersionsTieBreak(unittest.TestCase):
    """AC3 (WIKI-003): equal created_at must tie-break on insertion id."""

    def setUp(self):
        self.db_path, self.conn = _make_db()
        from app.services.wiki_store import WikiStore

        self.store = WikiStore(self.conn)

    def tearDown(self):
        self.conn.close()

    def _insert_version(self, page_id: int, title: str, created_at: str) -> int:
        cur = self.conn.execute(
            """INSERT INTO wiki_page_versions
               (page_id, vault_id, title, markdown, summary, status,
                confidence, edited_by, created_at)
               VALUES (?, 1, ?, '', '', 'draft', 0.0, NULL, ?)""",
            (page_id, title, created_at),
        )
        self.conn.commit()
        return cur.lastrowid

    def test_issue515_ac3_equal_timestamps_tie_break_descending_ids(self):
        page = self.store.create_page(
            vault_id=1, title="Tie Break Page", page_type="entity"
        )
        same_ts = "2026-01-01T00:00:00"
        v1 = self._insert_version(page.id, "v1", same_ts)
        v2 = self._insert_version(page.id, "v2", same_ts)
        v3 = self._insert_version(page.id, "v3", same_ts)

        versions = self.store.list_versions(page.id)
        self.assertEqual(
            [v.id for v in versions],
            [v3, v2, v1],
            "versions with identical created_at must be returned newest-first "
            "(descending insertion id); "
            f"got {[v.id for v in versions]}",
        )

    def test_issue515_ac3_limit_one_returns_newest_version(self):
        page = self.store.create_page(
            vault_id=1, title="Limit One Page", page_type="entity"
        )
        same_ts = "2026-02-02T00:00:00"
        v1 = self._insert_version(page.id, "v1", same_ts)
        v2 = self._insert_version(page.id, "v2", same_ts)
        v3 = self._insert_version(page.id, "v3", same_ts)
        self.assertTrue(v3 > v2 > v1, "insertion ids must be ascending")

        newest = self.store.list_versions(page.id, limit=1)
        self.assertEqual(
            [v.id for v in newest],
            [v3],
            f"limit=1 must return the NEWEST version (id {v3}); "
            f"got {[v.id for v in newest]}",
        )


class _CancelRacingConnection:
    """sqlite3.Connection proxy that runs ``hook`` immediately before the
    next UPDATE against the compile-jobs table executes (deterministic
    interleave between complete_job's status read and its UPDATE)."""

    def __init__(self, conn, table: str, hook):
        self._rc_conn = conn
        self._rc_prefix = f"UPDATE {table}"
        self._rc_hook = hook
        self._rc_armed = True

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split()).upper()
        if self._rc_armed and normalized.startswith(self._rc_prefix.upper()):
            self._rc_armed = False
            self._rc_hook()
        return self._rc_conn.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._rc_conn, name)

    def __setattr__(self, name, value):
        if name.startswith("_rc_"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._rc_conn, name, value)


class _CompleteJobRaceTestBase(unittest.TestCase):
    """Shared harness: a 'running' job row plus a deterministic cancel race."""

    TABLE = "wiki_compile_jobs"  # overridden by the KMS subclass
    TRIGGER_TYPE = "query"  # KMS jobs CHECK-constrain trigger_type values

    def setUp(self):
        self.db_path, self.conn = _make_db()

    def tearDown(self):
        self.conn.close()
        self._cancel_conn.close()

    def _seed_running_job(self) -> int:
        from datetime import datetime

        now = datetime.utcnow().isoformat()
        cur = self.conn.execute(
            f"""INSERT INTO {self.TABLE}
                (vault_id, trigger_type, trigger_id, status, input_json, created_at, started_at)
                VALUES (1, ?, 'race', 'running', '{{}}', ?, ?)""",
            (self.TRIGGER_TYPE, now, now),
        )
        self.conn.commit()
        return cur.lastrowid

    def _durable_status(self, job_id: int) -> str:
        fresh = sqlite3.connect(self.db_path)
        try:
            return fresh.execute(
                f"SELECT status FROM {self.TABLE} WHERE id = ?", (job_id,)
            ).fetchone()[0]
        finally:
            fresh.close()


class TestIssue515Ac4WikiCompleteJobRace(_CompleteJobRaceTestBase):
    """AC4 (WIKI-004) — WikiStore.complete_job."""

    TABLE = "wiki_compile_jobs"

    def test_issue515_ac4_wiki_complete_job_cannot_overwrite_cancellation(self):
        from app.services.wiki_store import WikiStore

        job_id = self._seed_running_job()
        self.assertEqual(self._durable_status(job_id), "running")

        self._cancel_conn = sqlite3.connect(self.db_path)
        hook_fired = {"called": False, "cancelled": None}

        def _cancel_concurrently():
            hook_fired["called"] = True
            hook_fired["cancelled"] = WikiStore(
                self._cancel_conn
            ).cancel_job(job_id, vault_id=1)

        racing_conn = _CancelRacingConnection(
            self.conn, self.TABLE, _cancel_concurrently
        )
        WikiStore(racing_conn).complete_job(job_id, {"pages": []})

        self.assertTrue(hook_fired["called"], "race hook never fired")
        self.assertTrue(
            hook_fired["cancelled"],
            "cancel_job must succeed while the job is still running",
        )
        self.assertEqual(
            self._durable_status(job_id),
            "cancelled",
            "complete_job's UPDATE must not overwrite a concurrent "
            "cancellation; durable status should stay 'cancelled'",
        )


class TestIssue515Ac4KmsCompleteJobRace(_CompleteJobRaceTestBase):
    """AC4 (WIKI-004) — KMSStore.complete_job mirror check."""

    TABLE = "kms_compile_jobs"
    TRIGGER_TYPE = "ingest"  # KMS CHECK constraint allows this trigger type

    def test_issue515_ac4_kms_complete_job_cannot_overwrite_cancellation(self):
        from app.services.kms_store import KMSStore

        job_id = self._seed_running_job()
        self.assertEqual(self._durable_status(job_id), "running")

        self._cancel_conn = sqlite3.connect(self.db_path)
        hook_fired = {"called": False, "cancelled": None}

        def _cancel_concurrently():
            hook_fired["called"] = True
            hook_fired["cancelled"] = KMSStore(
                self._cancel_conn
            ).cancel_job(job_id, vault_id=1)

        racing_conn = _CancelRacingConnection(
            self.conn, self.TABLE, _cancel_concurrently
        )
        KMSStore(racing_conn).complete_job(job_id, {"entries": []})

        self.assertTrue(hook_fired["called"], "race hook never fired")
        self.assertTrue(
            hook_fired["cancelled"],
            "cancel_job must succeed while the job is still running",
        )
        self.assertEqual(
            self._durable_status(job_id),
            "cancelled",
            "KMSStore.complete_job's UPDATE must not overwrite a concurrent "
            "cancellation; durable status should stay 'cancelled'",
        )


if __name__ == "__main__":
    unittest.main()
