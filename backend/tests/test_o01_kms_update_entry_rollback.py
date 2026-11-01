"""Regression test for issue #791 — Workstream O, PR 1.

Pins the accepted KMS contract: update_entry on a missing id returns
None and leaves no open transaction on the connection.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path


def _make_env():
    from app.models.database import run_migrations

    td = tempfile.mkdtemp()
    db_path = str(Path(td) / "test.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'Test')")
    conn.commit()
    return conn


class TestKMSEntryUpdateTransaction(unittest.TestCase):
    """KMSStore.update_entry on a missing id closes its transaction."""

    def setUp(self):
        self.conn = _make_env()

    def tearDown(self):
        self.conn.close()

    def test_update_entry_not_found_leaves_no_open_transaction(self):
        from app.services.kms_store import KMSStore

        kms = KMSStore(self.conn)
        res = kms.update_entry(99999, 1, title="x")
        assert res is None
        assert self.conn.in_transaction is False
