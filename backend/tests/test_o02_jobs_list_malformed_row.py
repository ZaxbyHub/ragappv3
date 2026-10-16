"""Issue #792 (Workstream O, PR 2) acceptance check: lease-mode list_jobs
skips a malformed jobs payload row.

In lease mode the wiki job list filters the shared ``jobs`` table in SQL
with ``json_extract(payload_json, '$.vault_id')``. One row whose payload is
not valid JSON makes SQLite raise ``OperationalError: malformed JSON`` for
the whole listing, so a single corrupted row blinds the vault's entire job
history.

Check (lease mode on, vault 1 with one valid job plus one planted
``queue='wiki'`` row with ``payload_json='{not json'``):

- AC11: ``WikiStore.list_jobs`` returns exactly the valid job's id and does
  not raise.

RED at base (the SQL ``json_extract`` filter evaluates the malformed payload
and raises OperationalError, so the listing never returns); GREEN after the
fix (the malformed row is excluded from the listing and the valid jobs come
back).
"""

import sqlite3
import unittest
from pathlib import Path
from tempfile import mkdtemp
from unittest.mock import patch

from app.config import settings


def _make_db():
    from app.models.database import run_migrations

    db_path = str(Path(mkdtemp()) / "o02-list.db")
    run_migrations(db_path)  # legacy tables AND the shared jobs table
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'V1')")
    conn.commit()
    return conn


class TestO02JobsListMalformedRow(unittest.TestCase):
    def setUp(self):
        lease_patcher = patch.object(settings, "wiki_kms_job_lease_enabled", True)
        lease_patcher.start()
        self.addCleanup(lease_patcher.stop)

    def test_wiki_list_jobs_skips_malformed_payload_row(self):
        # check: AC11 — the listing returns only the valid job instead of
        # raising on the neighboring malformed row.
        from app.services.wiki_store import WikiStore

        conn = _make_db()
        self.addCleanup(conn.close)
        store = WikiStore(conn)
        store.create_job(1, "manual")  # id 1 on a fresh DB
        conn.execute(
            "INSERT INTO jobs (queue, payload_json, status) VALUES (?, ?, 'pending')",
            ("wiki", "{not json"),
        )
        conn.commit()

        try:
            outcome = [j.id for j in store.list_jobs(1)]
        except Exception as e:  # noqa: BLE001 — the defect IS the raise itself
            outcome = type(e).__name__
        assert outcome == [1]


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
