"""Issue #793 (Workstream O, PR 3) acceptance checks: vault-wide KMS
recompile file-status eligibility.

``KMSCompileProcessor._compile_vault`` selects vault files with
``status = 'indexed'`` only, while the per-file path ``_compile_file`` has no
status filter and compiles any row whose parsed_text is non-empty. A file in
the content-available ``partial`` state (non-empty parsed_text) is therefore
silently skipped by a vault-wide KMS recompile, and the result dict's
``total`` (= len(rows)) under-counts it.

Checks:

- AC1: a vault-wide compile produces KMS entries for both the 'indexed' and
  the 'partial' file, and none for the 'error' file.
- AC2: the ``_compile_vault`` result ``total`` counts the partial file
  (2 eligible files, not 1).

RED at base (the indexed-only filter skips the partial file, so the store
holds only the indexed entry and total == 1); GREEN after the fix (the vault
selection is aligned to the per-file path's eligibility — indexed + partial —
so both content-available rows compile and total == 2).

Harness: a temp ``run_migrations`` DB addressed through plain sqlite3; the
processor is built with ``__new__`` (bypassing the pool-bound ``__init__``)
because ``_compile_vault`` is sync and needs only a connection and a store.
"""

import sqlite3
import unittest
from pathlib import Path
from tempfile import mkdtemp

from app.models.database import run_migrations
from app.services.kms_compile_processor import KMSCompileProcessor
from app.services.kms_store import KMSStore


class TestO03KmsCompileVaultStatus(unittest.TestCase):
    def _make_fixture(self):
        """Temp DB with vault 1 holding indexed/partial/error files, each
        with non-empty parsed_text; returns (conn, store, processor)."""
        db_path = str(Path(mkdtemp()) / "o03-kms-vault-status.db")
        run_migrations(db_path)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'V1')")
        conn.executemany(
            "INSERT INTO files (id, vault_id, file_path, file_name, file_size,"
            " status, parsed_text) VALUES (?, 1, ?, ?, 1, ?, ?)",
            [
                (1, "docs/one.txt", "one.txt", "indexed", "body one"),
                (2, "docs/two.txt", "two.txt", "partial", "body two"),
                (3, "docs/three.txt", "three.txt", "error", "body three"),
            ],
        )
        conn.commit()
        processor = KMSCompileProcessor.__new__(KMSCompileProcessor)
        return conn, KMSStore(conn), processor

    def test_compile_vault_includes_partial_and_excludes_error_files(self):
        # check: AC1 — the vault-wide compile emits entries for the indexed
        # and partial files, and none for the error file.
        conn, store, processor = self._make_fixture()
        processor._compile_vault(conn, store, 1)
        self.assertEqual(sorted(e.file_id for e in store.list_entries(1)), [1, 2])

    def test_compile_vault_total_counts_partial_files(self):
        # check: AC2 — the result total counts the partial file as eligible.
        conn, store, processor = self._make_fixture()
        result = processor._compile_vault(conn, store, 1)
        self.assertEqual(result["total"], 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
