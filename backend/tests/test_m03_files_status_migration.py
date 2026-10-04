"""Issue #783 migration convergence: migrate_add_files_status_cancelled
rebuilds the files table with the widened status CHECK while preserving
every canonical column's data (extraction_diagnostics above all — the
widen migration's own hard-coded list predates it), the five indexes, and
the partial unique idx_files_hash_vault_indexed recreated by the block
registered after it; and the fresh-DB path admits 'cancelled' from the
schema constant directly.
"""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.models.database import (
    init_db,
    migrate_add_files_status_cancelled,
    run_migrations,
)


def _old_shape_files_ddl() -> str:
    """The pre-#783 files shape: 5-value status CHECK, all canonical columns."""
    return """
    CREATE TABLE files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        vault_id INTEGER NOT NULL,
        file_path TEXT NOT NULL,
        file_name TEXT NOT NULL,
        file_hash TEXT,
        file_size INTEGER NOT NULL,
        file_type TEXT,
        chunk_count INTEGER DEFAULT 0,
        chunks_failed INTEGER NOT NULL DEFAULT 0,
        partial_embeddings INTEGER NOT NULL DEFAULT 0,
        status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'indexed', 'partial', 'error')),
        error_message TEXT,
        source TEXT DEFAULT 'upload',
        email_subject TEXT,
        email_sender TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        processed_at TIMESTAMP,
        modified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        document_date TEXT,
        supersedes_file_id INTEGER,
        ingestion_version INTEGER DEFAULT 1,
        active_generation_hash TEXT,
        phase TEXT,
        phase_message TEXT,
        progress_percent REAL,
        processed_units INTEGER,
        total_units INTEGER,
        unit_label TEXT,
        phase_started_at TIMESTAMP,
        processing_started_at TIMESTAMP,
        wiki_pending INTEGER NOT NULL DEFAULT 0,
        enrichment_status TEXT,
        enrichment_error TEXT,
        enrichment_updated_at TIMESTAMP,
        enrichment_enabled INTEGER,
        extraction_diagnostics TEXT,
        folder_id INTEGER REFERENCES folders(id) ON DELETE SET NULL,
        parsed_text TEXT,
        FOREIGN KEY (vault_id) REFERENCES vaults(id)
    )
    """


class TestFilesStatusCancelledMigration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="m03_migration_")
        self.db = os.path.join(self.tmp, "app.db")

    def tearDown(self):
        # Windows: close-before-unwind is enforced by the per-test finally
        # blocks; remove what the OS lets us.
        try:
            os.remove(self.db)
        except OSError:
            pass
        try:
            os.rmdir(self.tmp)
        except OSError:
            pass

    def test_fresh_db_admits_cancelled(self):
        init_db(self.db)
        run_migrations(self.db)
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(
                "INSERT INTO files (vault_id, file_path, file_name, file_size, "
                "status) VALUES (1, 'a', 'a', 1, 'cancelled')"
            )
            conn.commit()
            ddl = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='files'"
            ).fetchone()[0]
            self.assertIn("'cancelled'", ddl)
        finally:
            conn.close()

    def test_upgrade_rebuild_preserves_columns_and_widens_check(self):
        init_db(self.db)
        run_migrations(self.db)  # brings the two files FTS projections up
        conn = sqlite3.connect(self.db)
        conn.isolation_level = None
        try:
            # Regress files to the pre-#783 shape with seeded data in the
            # columns the critic flagged as droppable by a naive mirror.
            conn.execute("BEGIN IMMEDIATE")
            for trig in (
                "files_search_fts_insert",
                "files_search_fts_delete",
                "files_search_fts_update",
                "files_content_fts_insert",
                "files_content_fts_delete",
                "files_content_fts_update",
            ):
                conn.execute(f"DROP TRIGGER IF EXISTS {trig}")
            conn.execute("DROP TABLE files")
            conn.execute(_old_shape_files_ddl())
            conn.execute(
                "INSERT INTO files (vault_id, file_path, file_name, file_size, "
                "status, extraction_diagnostics, folder_id, parsed_text) "
                "VALUES (1, 'a', 'a', 1, 'processing', 'diag-payload', NULL, "
                "'the parsed text')"
            )
            conn.execute("COMMIT")
        finally:
            conn.close()

        migrate_add_files_status_cancelled(self.db)
        # Idempotence: a second run must be a no-op.
        migrate_add_files_status_cancelled(self.db)

        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT status, extraction_diagnostics, folder_id, parsed_text "
                "FROM files WHERE file_name = 'a'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "processing")  # data unchanged
            self.assertEqual(row["extraction_diagnostics"], "diag-payload")
            self.assertEqual(row["parsed_text"], "the parsed text")

            ddl = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='files'"
            ).fetchone()[0]
            self.assertIn("'cancelled'", ddl)

            # The widened CHECK admits the new terminal value.
            conn.execute(
                "UPDATE files SET status = 'cancelled' WHERE file_name = 'a'"
            )
            conn.commit()

            # The five plain indexes survive the rebuild...
            names = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND tbl_name = 'files'"
                )
            }
            for idx in (
                "idx_files_status",
                "idx_files_hash_vault_status",
                "idx_files_vault_id",
                "idx_files_source",
                "idx_files_folder_id",
            ):
                self.assertIn(idx, names)
            # ...and run_migrations' unique-index block still owns the
            # partial unique (absent until that block runs).
            self.assertNotIn("idx_files_hash_vault_indexed", names)
        finally:
            conn.close()

        # Full migration pass recreates the partial unique index on the
        # rebuilt table.
        run_migrations(self.db)
        conn = sqlite3.connect(self.db)
        try:
            idx = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name = 'idx_files_hash_vault_indexed'"
            ).fetchone()
            self.assertIsNotNone(idx)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
