"""Issue #793 feedback-round pins (swarm-pr-review F-1/F-2): the ineligible
statuses the release note names — ``cancelled``, ``pending``, ``processing`` —
really stay excluded from a vault-wide KMS recompile, and a selected file with
empty ``parsed_text`` skips per-file while still counting in ``total`` and
``skipped``.

These are additive companions to ``test_o03_kms_compile_vault_status.py``
(that file is checkpoint-frozen): here a widening of the eligibility set to
any of the three remaining non-terminal statuses fails the entries/total
assertions, and the ``_compile_file`` skip branch ("no parsed_text to
compile") is exercised instead of dead.
"""

import sqlite3
import unittest
from pathlib import Path
from tempfile import mkdtemp

from app.models.database import run_migrations
from app.services.kms_compile_processor import KMSCompileProcessor
from app.services.kms_store import KMSStore


class TestO03bNegativeStatusPins(unittest.TestCase):
    def _make_fixture(self, rows):
        db_path = str(Path(mkdtemp()) / "o03b-kms-negative-status.db")
        run_migrations(db_path)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'V1')")
        conn.executemany(
            "INSERT INTO files (id, vault_id, file_path, file_name, file_size,"
            " status, parsed_text) VALUES (?, 1, ?, ?, 1, ?, ?)",
            rows,
        )
        conn.commit()
        processor = KMSCompileProcessor.__new__(KMSCompileProcessor)
        return conn, KMSStore(conn), processor

    def test_cancelled_pending_processing_stay_excluded(self):
        # check: F-1 — none of the three remaining non-terminal statuses may
        # enter the vault recompile's selection.
        conn, store, processor = self._make_fixture(
            [
                (1, "docs/one.txt", "one.txt", "indexed", "body one"),
                (2, "docs/two.txt", "two.txt", "partial", "body two"),
                (3, "docs/three.txt", "three.txt", "error", "body three"),
                (4, "docs/four.txt", "four.txt", "cancelled", "stale body"),
                (5, "docs/five.txt", "five.txt", "pending", "pending body"),
                (6, "docs/six.txt", "six.txt", "processing", "processing body"),
            ]
        )
        result = processor._compile_vault(conn, store, 1)
        self.assertEqual(sorted(e.file_id for e in store.list_entries(1)), [1, 2])
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["compiled"], 2)
        self.assertEqual(result["skipped"], 0)

    def test_empty_parsed_text_partial_skips_but_counts(self):
        # check: F-2 — a selected file with empty parsed_text produces no
        # entry, is counted in `skipped`, and still counts in `total`.
        conn, store, processor = self._make_fixture(
            [
                (1, "docs/one.txt", "one.txt", "indexed", "body one"),
                (2, "docs/two.txt", "two.txt", "partial", ""),
                (3, "docs/three.txt", "three.txt", "partial", "   "),
            ]
        )
        result = processor._compile_vault(conn, store, 1)
        self.assertEqual(sorted(e.file_id for e in store.list_entries(1)), [1])
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["compiled"], 1)
        self.assertEqual(result["skipped"], 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
