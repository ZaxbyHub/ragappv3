"""Unit tests for database schema initialization."""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.models.database import (
    init_db,
    migrate_add_draft_room_promotions,
    migrate_add_files_search_fts,
    migrate_add_fork_columns,
    run_migrations,
)


class TestDatabaseSchema(unittest.TestCase):
    """Test cases for database schema initialization."""

    def setUp(self):
        """Create a temporary database file for each test."""
        self.temp_fd, self.temp_db_path = tempfile.mkstemp(suffix='.db')
        os.close(self.temp_fd)

    def tearDown(self):
        """Clean up the temporary database file."""
        if os.path.exists(self.temp_db_path):
            os.remove(self.temp_db_path)

    def test_init_db_creates_required_tables(self):
        """Test that init_db creates all required tables and FTS virtual table."""
        # Initialize the database
        init_db(self.temp_db_path)

        # Connect and query sqlite_master for tables
        conn = sqlite3.connect(self.temp_db_path)
        cursor = conn.cursor()

        # Get all tables and virtual tables
        cursor.execute(
            "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'virtual table')"
        )
        results = cursor.fetchall()
        conn.close()

        # Extract table names
        table_names = {name for name, _ in results}

        # Assert all required tables exist
        required_tables = {
            'files',
            'memories',
            'memories_fts',
            'chat_sessions',
            'chat_messages'
        }

        for table in required_tables:
            self.assertIn(
                table,
                table_names,
                f"Required table '{table}' was not created by init_db()"
            )

    def test_init_db_is_idempotent(self):
        """Test that init_db can be called multiple times without error."""
        # Initialize twice
        init_db(self.temp_db_path)
        init_db(self.temp_db_path)

        # Verify tables still exist (query both table and virtual table types)
        conn = sqlite3.connect(self.temp_db_path)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'virtual table')"
        )
        results = cursor.fetchall()
        conn.close()

        # Extract table names
        table_names = {name for name, _ in results}

        # Assert all required tables exist
        required_tables = {
            'files',
            'memories',
            'memories_fts',
            'chat_sessions',
            'chat_messages'
        }

        for table in required_tables:
            self.assertIn(
                table,
                table_names,
                f"Required table '{table}' was not found after idempotent init_db() calls"
            )

    def test_run_migrations_upgrades_legacy_files_table_before_files_fts(self):
        """Legacy files schema should not trip FTS triggers that reference new columns."""
        conn = sqlite3.connect(self.temp_db_path)
        try:
            conn.executescript(
                """
                CREATE TABLE files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    file_hash TEXT,
                    file_size INTEGER NOT NULL,
                    chunk_count INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'pending',
                    error_message TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    processed_at TIMESTAMP
                );
                """
            )
            conn.execute(
                "INSERT INTO files (file_path, file_name, file_size, status) VALUES (?, ?, ?, ?)",
                ("/tmp/legacy.txt", "legacy.txt", 10, "indexed"),
            )
            conn.commit()
        finally:
            conn.close()

        run_migrations(self.temp_db_path)

        conn = sqlite3.connect(self.temp_db_path)
        try:
            row = conn.execute(
                "SELECT rowid FROM files_search_fts WHERE files_search_fts MATCH ?",
                ("legacy*",),
            ).fetchone()
        finally:
            conn.close()

        self.assertIsNotNone(row)

    def test_files_search_fts_migration_rebuilds_stale_legacy_triggers(self):
        """Legacy files FTS triggers are dropped before missing metadata columns are added."""
        conn = sqlite3.connect(self.temp_db_path)
        try:
            conn.executescript(
                """
                CREATE TABLE files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    file_hash TEXT,
                    file_size INTEGER NOT NULL,
                    chunk_count INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'pending',
                    error_message TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    processed_at TIMESTAMP
                );

                CREATE VIRTUAL TABLE files_search_fts USING fts5(
                    file_name,
                    file_type,
                    status,
                    source,
                    email_subject,
                    email_sender,
                    document_date,
                    content='files',
                    content_rowid='id'
                );

                CREATE TRIGGER files_search_fts_insert AFTER INSERT ON files BEGIN
                    INSERT INTO files_search_fts(rowid, file_name, file_type, status, source, email_subject, email_sender, document_date)
                    VALUES (new.id, new.file_name, new.file_type, new.status, new.source, new.email_subject, new.email_sender, new.document_date);
                END;
                """
            )
            conn.commit()
        finally:
            conn.close()

        migrate_add_files_search_fts(self.temp_db_path)

        conn = sqlite3.connect(self.temp_db_path)
        try:
            conn.execute(
                "INSERT INTO files (file_path, file_name, file_size, status) VALUES (?, ?, ?, ?)",
                ("/tmp/recovered.txt", "recovered.txt", 10, "indexed"),
            )
            conn.commit()
            row = conn.execute(
                "SELECT rowid FROM files_search_fts WHERE files_search_fts MATCH ?",
                ("recovered*",),
            ).fetchone()
        finally:
            conn.close()

        self.assertIsNotNone(row)

    def test_init_db_creates_fork_columns_on_chat_sessions(self):
        """Fresh databases should include fork metadata without migrations."""
        init_db(self.temp_db_path)

        conn = sqlite3.connect(self.temp_db_path)
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(chat_sessions)")
        columns = {row[1] for row in cursor.fetchall()}
        conn.close()

        self.assertIn("forked_from_session_id", columns)
        self.assertIn("fork_message_index", columns)

    def test_migrate_add_fork_columns_preserves_existing_sessions(self):
        """Legacy chat_sessions tables should get nullable fork metadata."""
        conn = sqlite3.connect(self.temp_db_path)
        conn.execute("""
            CREATE TABLE chat_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vault_id INTEGER NOT NULL DEFAULT 1,
                user_id INTEGER,
                title TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute(
            "INSERT INTO chat_sessions (vault_id, user_id, title) VALUES (?, ?, ?)",
            (1, 1, "Legacy"),
        )
        conn.commit()
        conn.close()

        migrate_add_fork_columns(self.temp_db_path)

        conn = sqlite3.connect(self.temp_db_path)
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(chat_sessions)")
        columns = {row[1] for row in cursor.fetchall()}
        cursor.execute(
            "SELECT title, forked_from_session_id, fork_message_index FROM chat_sessions"
        )
        row = cursor.fetchone()
        conn.close()

        self.assertIn("forked_from_session_id", columns)
        self.assertIn("fork_message_index", columns)
        self.assertEqual(row, ("Legacy", None, None))

    def test_migrate_draft_room_promotions_rebuilds_legacy_fk_shape(self):
        """An earlier revision of this migration shipped `draft_promotions.file_id`
        as `REFERENCES files(id) ON DELETE CASCADE`, which erases a promotion's
        provenance the moment the promoted document is deleted. Simulate a
        database that already ran that shape and confirm the current migration
        detects and rebuilds it -- preserving existing rows -- rather than
        `CREATE TABLE IF NOT EXISTS` silently leaving the old, cascading shape
        in place (issue #437 review finding)."""
        init_db(self.temp_db_path)
        run_migrations(self.temp_db_path)

        # IDs pinned at 101+ so they never collide with any default rows
        # `init_db`/`run_migrations` themselves seed.
        conn = sqlite3.connect(self.temp_db_path)
        try:
            conn.execute(
                "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) "
                "VALUES (101,'promo-owner','x','Owner','member',1)"
            )
            conn.execute("INSERT INTO vaults (id, name) VALUES (101,'Promo Vault')")
            conn.execute(
                "INSERT INTO drafts (id, vault_id, created_by, title, mode) "
                "VALUES (101,101,101,'D','rewrite')"
            )
            conn.execute(
                "INSERT INTO files (id, vault_id, file_path, file_name, file_size) "
                "VALUES (101,101,'/tmp/x.txt','x.txt',10)"
            )
            # Replace the current (already-fixed) table with the OLD,
            # FK-having shape -- including its index, exactly as the
            # original migration created both together -- as if this
            # database had run an earlier revision of the migration.
            conn.execute("DROP INDEX IF EXISTS idx_draft_promotions_draft")
            conn.execute("DROP TABLE draft_promotions")
            conn.executescript("""
                CREATE TABLE draft_promotions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    draft_id INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
                    source_type TEXT NOT NULL CHECK (source_type IN ('input','revision')),
                    source_id INTEGER NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    vault_id INTEGER NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
                    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                    filename TEXT NOT NULL,
                    promoted_by INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX idx_draft_promotions_draft
                    ON draft_promotions(draft_id);
            """)
            conn.execute(
                "INSERT INTO draft_promotions "
                "(id, draft_id, source_type, source_id, source_sha256, vault_id, "
                "file_id, filename, promoted_by) "
                "VALUES (101,101,'input',101,'deadbeef',101,101,'x.txt',101)"
            )
            conn.commit()

            # Confirm the simulated legacy shape actually has the index,
            # otherwise the assertion below would trivially pass without
            # ever exercising the bug it exists to catch.
            before_indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index' "
                    "AND tbl_name = 'draft_promotions'"
                ).fetchall()
            }
            self.assertIn("idx_draft_promotions_draft", before_indexes)
        finally:
            conn.close()

        # Re-run the migration: it must detect the legacy FK shape and rebuild.
        migrate_add_draft_room_promotions(self.temp_db_path)

        conn = sqlite3.connect(self.temp_db_path)
        conn.row_factory = sqlite3.Row
        try:
            fk_rows = conn.execute("PRAGMA foreign_key_list(draft_promotions)").fetchall()
            self.assertFalse(
                any(row["table"] == "files" for row in fk_rows),
                "file_id must no longer be a foreign key on files(id)",
            )

            # The index must survive the rebuild -- `ALTER TABLE ... RENAME`
            # does not rename the index bound to the old table, so a naive
            # rebuild silently loses it (issue #437 review finding).
            after_indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index' "
                    "AND tbl_name = 'draft_promotions'"
                ).fetchall()
            }
            self.assertIn(
                "idx_draft_promotions_draft",
                after_indexes,
                "the rebuild must not silently drop the draft_id index",
            )

            row = conn.execute(
                "SELECT * FROM draft_promotions WHERE id = 101"
            ).fetchone()
            self.assertIsNotNone(row, "existing promotion row must survive the rebuild")
            self.assertEqual(row["file_id"], 101)
            self.assertEqual(row["source_sha256"], "deadbeef")

            # The actual behavior this exists to guarantee: deleting the
            # promoted file must not erase its provenance row.
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("DELETE FROM files WHERE id = 101")
            conn.commit()
            survivor = conn.execute(
                "SELECT id FROM draft_promotions WHERE id = 101"
            ).fetchone()
            self.assertIsNotNone(survivor, "provenance row must survive file deletion")
        finally:
            conn.close()

        # Idempotent: re-running the (now up to date) migration again is a no-op.
        migrate_add_draft_room_promotions(self.temp_db_path)

    def test_migrate_draft_room_promotions_rebuild_is_atomic_on_failure(self):
        """`conn.executescript()` implicitly commits any pending transaction
        first, so a naive rebuild that used it for the recreate step would
        make `ALTER TABLE RENAME`/`DROP INDEX` permanent before the row copy
        could even run -- if the copy then failed (e.g. a legacy row that
        violates the current CHECK constraint), the old data would be
        stranded in `draft_promotions_legacy_fk` and a later re-run would
        see the new-shape table and silently treat the migration as done,
        permanently losing that data. The rebuild must instead run as one
        explicit transaction: on failure, everything rolls back and the
        database is left exactly as it was -- the old FK'd table, indexed,
        with all of its original rows intact (issue #437 review finding)."""
        init_db(self.temp_db_path)
        run_migrations(self.temp_db_path)

        conn = sqlite3.connect(self.temp_db_path)
        try:
            conn.execute(
                "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) "
                "VALUES (101,'promo-owner','x','Owner','member',1)"
            )
            conn.execute("INSERT INTO vaults (id, name) VALUES (101,'Promo Vault')")
            conn.execute(
                "INSERT INTO drafts (id, vault_id, created_by, title, mode) "
                "VALUES (101,101,101,'D','rewrite')"
            )
            conn.execute(
                "INSERT INTO files (id, vault_id, file_path, file_name, file_size) "
                "VALUES (101,101,'/tmp/x.txt','x.txt',10)"
            )
            conn.execute("DROP INDEX IF EXISTS idx_draft_promotions_draft")
            conn.execute("DROP TABLE draft_promotions")
            conn.executescript("""
                CREATE TABLE draft_promotions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    draft_id INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
                    source_type TEXT NOT NULL CHECK (source_type IN ('input','revision')),
                    source_id INTEGER NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    vault_id INTEGER NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
                    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                    filename TEXT NOT NULL,
                    promoted_by INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX idx_draft_promotions_draft
                    ON draft_promotions(draft_id);
            """)
            # A valid row plus one that will violate the CURRENT (rebuilt)
            # table's CHECK constraint -- bypass the legacy table's own
            # CHECK (it has the same constraint) via writable_schema so the
            # bad row can exist here at all, exactly as a real legacy
            # database with pre-constraint data could.
            conn.execute(
                "INSERT INTO draft_promotions "
                "(id, draft_id, source_type, source_id, source_sha256, vault_id, "
                "file_id, filename, promoted_by) "
                "VALUES (101,101,'input',101,'deadbeef',101,101,'x.txt',101)"
            )
            conn.execute("PRAGMA writable_schema = ON")
            conn.execute(
                "UPDATE sqlite_master SET sql = replace(sql, "
                "\"CHECK (source_type IN ('input','revision'))\", '') "
                "WHERE name = 'draft_promotions'"
            )
            conn.execute("PRAGMA writable_schema = OFF")
            conn.commit()
            conn.close()

            conn = sqlite3.connect(self.temp_db_path)
            conn.execute(
                "INSERT INTO draft_promotions "
                "(id, draft_id, source_type, source_id, source_sha256, vault_id, "
                "file_id, filename, promoted_by) "
                "VALUES (102,101,'BAD_TYPE',102,'cafebabe',101,101,'y.txt',101)"
            )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(sqlite3.IntegrityError):
            migrate_add_draft_room_promotions(self.temp_db_path)

        conn = sqlite3.connect(self.temp_db_path)
        conn.row_factory = sqlite3.Row
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name LIKE 'draft_promotions%'"
                ).fetchall()
            }
            self.assertEqual(
                tables,
                {"draft_promotions"},
                "a failed rebuild must not strand a _legacy_fk table",
            )

            fk_rows = conn.execute("PRAGMA foreign_key_list(draft_promotions)").fetchall()
            self.assertTrue(
                any(row["table"] == "files" for row in fk_rows),
                "on rollback the OLD FK'd shape must still be in place",
            )

            rows = conn.execute(
                "SELECT id FROM draft_promotions ORDER BY id"
            ).fetchall()
            self.assertEqual(
                [r["id"] for r in rows],
                [101, 102],
                "both original rows must survive the rolled-back attempt",
            )

            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index' "
                    "AND tbl_name = 'draft_promotions'"
                ).fetchall()
            }
            self.assertIn("idx_draft_promotions_draft", indexes)
        finally:
            conn.close()

    def test_migrate_draft_room_promotions_fk_rebuild_converges_with_init_db(self):
        """`init_db()` and every `migrate_add_*` function share the same DDL
        constants by construction for the ordinary path, so fresh-versus-
        migrated convergence is guaranteed there. The FK-rebuild branch is
        different: it does not execute `_DRAFT_ROOM_PROMOTIONS_TABLE_DDL`
        directly against a fresh database -- it renames the old table away,
        recreates, and copies rows across. Prove that path actually produces
        a structurally identical table rather than assuming it from the
        constants alone (issue #437 review finding)."""
        fresh_db = self.temp_db_path + ".fresh.db"
        self.addCleanup(lambda: os.path.exists(fresh_db) and os.remove(fresh_db))
        init_db(fresh_db)

        # migrated_db: build the OLD FK'd shape (with its index), then let
        # migrate_add_draft_room_promotions() detect it and rebuild.
        init_db(self.temp_db_path)
        run_migrations(self.temp_db_path)
        conn = sqlite3.connect(self.temp_db_path)
        try:
            conn.execute("DROP INDEX IF EXISTS idx_draft_promotions_draft")
            conn.execute("DROP TABLE draft_promotions")
            conn.executescript("""
                CREATE TABLE draft_promotions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    draft_id INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
                    source_type TEXT NOT NULL CHECK (source_type IN ('input','revision')),
                    source_id INTEGER NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    vault_id INTEGER NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
                    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                    filename TEXT NOT NULL,
                    promoted_by INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX idx_draft_promotions_draft
                    ON draft_promotions(draft_id);
            """)
            conn.commit()
        finally:
            conn.close()

        migrate_add_draft_room_promotions(self.temp_db_path)

        def _table_info(db_path):
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                # (cid excluded -- column *order* is not a claimed guarantee,
                # only name/type/notnull/default/pk are).
                return [
                    (row["name"], row["type"], row["notnull"], row["dflt_value"], row["pk"])
                    for row in conn.execute("PRAGMA table_info(draft_promotions)").fetchall()
                ]
            finally:
                conn.close()

        def _index_info(db_path):
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                indexes = conn.execute("PRAGMA index_list(draft_promotions)").fetchall()
                result = []
                for idx in indexes:
                    columns = conn.execute(
                        f"PRAGMA index_info({idx['name']})"
                    ).fetchall()
                    result.append(
                        (
                            idx["name"],
                            bool(idx["unique"]),
                            tuple(c["name"] for c in columns),
                        )
                    )
                return sorted(result)
            finally:
                conn.close()

        fresh_columns = _table_info(fresh_db)
        migrated_columns = _table_info(self.temp_db_path)
        self.assertEqual(
            set(fresh_columns),
            set(migrated_columns),
            "the FK-rebuild path must produce column definitions identical "
            "to init_db()'s fresh-install schema",
        )

        fresh_indexes = _index_info(fresh_db)
        migrated_indexes = _index_info(self.temp_db_path)
        self.assertEqual(
            fresh_indexes,
            migrated_indexes,
            "the FK-rebuild path must produce indexes identical to "
            "init_db()'s fresh-install schema",
        )

        # And the rebuilt table must genuinely have no FK on files(id) --
        # convergence with a schema that still had one would be a bug this
        # comparison must not paper over.
        conn = sqlite3.connect(self.temp_db_path)
        try:
            fk_rows = conn.execute("PRAGMA foreign_key_list(draft_promotions)").fetchall()
        finally:
            conn.close()
        self.assertFalse(any(row[2] == "files" for row in fk_rows))


if __name__ == '__main__':
    unittest.main()
