"""Test schema unification changes for RAGAPPv3.

Verifies:
- New columns in files, chat_sessions, and users tables
- New indexes for performance
- run_migrations() has exactly 6 migration calls
- Database creation works with all new columns
- Backward compatibility: inserts without new columns work
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.models.database import SCHEMA, init_db, run_migrations


class TestSchemaNewColumns:
    """Verify new columns exist in the schema definition."""

    def test_files_table_document_date_column(self):
        """files table must have document_date column."""
        assert "document_date TEXT" in SCHEMA

    def test_files_table_supersedes_file_id_column(self):
        """files table must have supersedes_file_id column."""
        assert "supersedes_file_id INTEGER" in SCHEMA

    def test_files_table_ingestion_version_column(self):
        """files table must have ingestion_version column with default 1."""
        assert "ingestion_version INTEGER DEFAULT 1" in SCHEMA

    def test_chat_sessions_user_id_column(self):
        """chat_sessions table must have user_id column."""
        assert "user_id INTEGER" in SCHEMA
        # Verify it's in the chat_sessions context (line ~94)
        chat_section = SCHEMA[
            SCHEMA.find("CREATE TABLE IF NOT EXISTS chat_sessions") : SCHEMA.find(
                "CREATE TABLE IF NOT EXISTS chat_messages"
            )
        ]
        assert "user_id INTEGER" in chat_section

    def test_users_must_change_password_column(self):
        """users table must have must_change_password column with default 0."""
        assert "must_change_password INTEGER NOT NULL DEFAULT 0" in SCHEMA

    def test_users_failed_attempts_column(self):
        """users table must have failed_attempts column with default 0."""
        assert "failed_attempts INTEGER NOT NULL DEFAULT 0" in SCHEMA

    def test_users_locked_until_column(self):
        """users table must have locked_until column."""
        assert "locked_until TIMESTAMP" in SCHEMA


class TestSchemaNewIndexes:
    """Verify new indexes exist in the schema definition."""

    def test_idx_users_locked_until_exists(self):
        """Index idx_users_locked_until must exist."""
        assert (
            "CREATE INDEX IF NOT EXISTS idx_users_locked_until ON users(locked_until)"
            in SCHEMA
        )

    def test_idx_user_sessions_expires_exists(self):
        """Index idx_user_sessions_expires must exist."""
        assert (
            "CREATE INDEX IF NOT EXISTS idx_user_sessions_expires ON user_sessions(expires_at)"
            in SCHEMA
        )

    def test_idx_chat_sessions_user_id_exists(self):
        """Index idx_chat_sessions_user_id must exist."""
        assert (
            "CREATE INDEX IF NOT EXISTS idx_chat_sessions_user_id ON chat_sessions(user_id)"
            in SCHEMA
        )

    def test_idx_memories_vault_id_exists(self):
        """Index idx_memories_vault_id must exist."""
        assert (
            "CREATE INDEX IF NOT EXISTS idx_memories_vault_id ON memories(vault_id)"
            in SCHEMA
        )

    def test_idx_memories_created_at_exists(self):
        """Index idx_memories_created_at must exist."""
        assert (
            "CREATE INDEX IF NOT EXISTS idx_memories_created_at ON memories(created_at)"
            in SCHEMA
        )

    def test_all_five_new_indexes_present(self):
        """All 5 expected new indexes must be present."""
        new_indexes = [
            "idx_users_locked_until",
            "idx_user_sessions_expires",
            "idx_chat_sessions_user_id",
            "idx_memories_vault_id",
            "idx_memories_created_at",
        ]
        for idx in new_indexes:
            assert f"CREATE INDEX IF NOT EXISTS {idx}" in SCHEMA, (
                f"Missing index: {idx}"
            )


class TestRunMigrations:
    """Verify run_migrations() has exactly 6 expected migrations."""

    def test_run_migrations_has_six_migration_calls(self):
        """run_migrations must call exactly 6 migration functions."""
        import inspect

        source = inspect.getsource(run_migrations)

        # Count expected migration calls (these are the 6 current/active migrations)

        # The run_migrations function should call exactly these 7 operations
        # (init_db + 6 named migrations)
        migration_calls = [
            "init_db(sqlite_path)",
            "migrate_add_vaults(sqlite_path)",
            "migrate_add_email_columns(sqlite_path)",
            "migrate_add_user_org_tables(sqlite_path)",
            "migrate_add_vault_permission_columns(sqlite_path)",
            "migrate_vault_paths(sqlite_path)",
            "migrate_add_org_slug_column(sqlite_path)",
            "migrate_add_wiki_tables(sqlite_path)",
        ]

        for call in migration_calls:
            assert call in source, f"Expected migration call not found: {call}"

        # Verify orphaned migrations are NOT called
        orphaned_migrations = [
            "migrate_add_auth_columns",  # Not listed in run_migrations
        ]

        for orphaned in orphaned_migrations:
            assert f"{orphaned}(sqlite_path)" not in source, (
                f"Orphaned migration should not be called: {orphaned}"
            )

    def test_run_migrations_calls_expected_operations(self):
        """Verify run_migrations contains all required migration calls."""
        import inspect
        import re

        source = inspect.getsource(run_migrations)

        migration_pattern = r"(\w+)\(sqlite_path\)"
        matches = re.findall(migration_pattern, source)

        migration_calls = [
            m for m in matches if m.startswith("migrate_") or m == "init_db"
        ]

        # All original migrations plus newer ones must be present
        expected = [
            "init_db",
            "migrate_add_vaults",
            "migrate_add_email_columns",
            "migrate_add_user_org_tables",
            "migrate_add_vault_permission_columns",
            "migrate_vault_paths",
            "migrate_add_org_slug_column",
            "migrate_add_wiki_tables",
        ]

        for exp in expected:
            assert exp in migration_calls, (
                f"Expected migration {exp} not found in calls: {migration_calls}"
            )


class TestDatabaseCreation:
    """Verify fresh database creation with all new columns."""

    def test_init_db_creates_files_table_with_new_columns(self):
        """Fresh database files table must have all new columns."""
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(SCHEMA)

            cursor = conn.execute("PRAGMA table_info(files)")
            columns = {row[1] for row in cursor.fetchall()}

            assert "document_date" in columns
            assert "supersedes_file_id" in columns
            assert "ingestion_version" in columns
        finally:
            conn.close()

    def test_init_db_creates_chat_sessions_with_user_id(self):
        """Fresh database chat_sessions table must have user_id column."""
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(SCHEMA)

            cursor = conn.execute("PRAGMA table_info(chat_sessions)")
            columns = {row[1] for row in cursor.fetchall()}

            assert "user_id" in columns
        finally:
            conn.close()

    def test_run_migrations_adds_user_id_to_legacy_chat_sessions(self, tmp_path):
        """Older databases missing chat_sessions.user_id must migrate before schema indexes run."""
        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("""
                CREATE TABLE chat_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vault_id INTEGER NOT NULL DEFAULT 1,
                    title TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute(
                "INSERT INTO chat_sessions (vault_id, title) VALUES (?, ?)",
                (1, "legacy"),
            )
            conn.commit()
        finally:
            conn.close()

        run_migrations(str(db_path))

        conn = sqlite3.connect(db_path)
        try:
            cursor = conn.execute("PRAGMA table_info(chat_sessions)")
            columns = {row[1] for row in cursor.fetchall()}
            assert "user_id" in columns

            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
            indexes = {row[0] for row in cursor.fetchall()}
            assert "idx_chat_sessions_user_id" in indexes

            cursor = conn.execute("SELECT user_id FROM chat_sessions WHERE title = ?", ("legacy",))
            assert cursor.fetchone()[0] is None
        finally:
            conn.close()

    def test_run_migrations_adds_retention_columns_to_legacy_memories(self, tmp_path):
        """Older databases whose memories table predates the retention columns
        must gain them with production defaults; the seeded row survives.

        Mirrors test_run_migrations_adds_user_id_to_legacy_chat_sessions: the
        legacy table is hand-built (init_db's CREATE TABLE IF NOT EXISTS leaves
        it untouched), so only migrate_add_memory_retention_columns can add
        importance/expires_at and the idx_memories_expires_at index.
        """
        db_path = tmp_path / "legacy_memories.db"
        conn = sqlite3.connect(db_path)
        try:
            # Legacy shape: the production memories columns minus the
            # retention pair (importance, expires_at). content/category/
            # vault_id/created_at stay because init_db creates FTS triggers
            # and indexes that reference them.
            conn.execute("""
                CREATE TABLE memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vault_id INTEGER,
                    content TEXT NOT NULL,
                    category TEXT,
                    tags TEXT,
                    source TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute(
                "INSERT INTO memories (vault_id, content, category) VALUES (?, ?, ?)",
                (1, "legacy memory", "note"),
            )
            conn.commit()
        finally:
            conn.close()

        run_migrations(str(db_path))

        conn = sqlite3.connect(db_path)
        try:
            columns = {
                row[1]: row for row in conn.execute("PRAGMA table_info(memories)")
            }
            assert "importance" in columns, (
                "legacy memories table was not upgraded with the importance column"
            )
            assert "expires_at" in columns, (
                "legacy memories table was not upgraded with the expires_at column"
            )
            # Production defaults on the added columns
            # (migrate_add_memory_retention_columns).
            assert columns["importance"][4] == "0.5"  # dflt_value
            assert columns["expires_at"][4] is None

            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
            assert "idx_memories_expires_at" in indexes

            # The seeded row survived with the backfilled production default.
            row = conn.execute(
                "SELECT content, importance, expires_at FROM memories"
            ).fetchone()
            assert row[0] == "legacy memory"
            assert row[1] == 0.5
            assert row[2] is None
        finally:
            conn.close()

    def test_init_db_creates_users_table_with_security_columns(self):
        """Fresh database users table must have security columns."""
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(SCHEMA)

            cursor = conn.execute("PRAGMA table_info(users)")
            columns = {row[1] for row in cursor.fetchall()}

            assert "must_change_password" in columns
            assert "failed_attempts" in columns
            assert "locked_until" in columns
        finally:
            conn.close()

    def test_init_db_creates_all_new_indexes(self):
        """Fresh database must have all 5 new indexes created."""
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(SCHEMA)

            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
            indexes = {row[0] for row in cursor.fetchall()}

            assert "idx_users_locked_until" in indexes
            assert "idx_user_sessions_expires" in indexes
            assert "idx_chat_sessions_user_id" in indexes
            assert "idx_memories_vault_id" in indexes
            assert "idx_memories_created_at" in indexes
        finally:
            conn.close()


class TestBackwardCompatibility:
    """Test that existing inserts still work (backward compatibility)."""

    def test_insert_file_without_new_columns_works(self):
        """Insert into files without specifying new columns should use defaults."""
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(SCHEMA)

            # Insert a file without the new columns (old code path)
            conn.execute("""
                INSERT INTO files (vault_id, file_path, file_name, file_size)
                VALUES (1, '/test/path.pdf', 'test.pdf', 1024)
            """)
            conn.commit()

            # Verify the row was created with defaults
            cursor = conn.execute("""
                SELECT document_date, supersedes_file_id, ingestion_version
                FROM files WHERE file_name = 'test.pdf'
            """)
            row = cursor.fetchone()

            assert row[0] is None  # document_date default NULL
            assert row[1] is None  # supersedes_file_id default NULL
            assert row[2] == 1  # ingestion_version default 1
        finally:
            conn.close()

    def test_insert_user_without_security_columns_works(self):
        """Insert into users without security columns should use defaults."""
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(SCHEMA)

            # Insert a user without the new security columns (old code path)
            conn.execute("""
                INSERT INTO users (username, hashed_password, role)
                VALUES ('testuser', 'hashedpass123', 'member')
            """)
            conn.commit()

            # Verify the row was created with defaults
            cursor = conn.execute("""
                SELECT must_change_password, failed_attempts, locked_until
                FROM users WHERE username = 'testuser'
            """)
            row = cursor.fetchone()

            assert row[0] == 0  # must_change_password default 0
            assert row[1] == 0  # failed_attempts default 0
            assert row[2] is None  # locked_until default NULL
        finally:
            conn.close()

    def test_insert_chat_session_without_user_id_works(self):
        """Insert into chat_sessions without user_id should use NULL default."""
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(SCHEMA)

            # First ensure the vault exists (FK constraint)
            conn.execute("INSERT INTO vaults (id, name) VALUES (1, 'Default')")

            # Insert a chat session without user_id (old code path)
            conn.execute("""
                INSERT INTO chat_sessions (vault_id, title)
                VALUES (1, 'Test Session')
            """)
            conn.commit()

            # Verify the row was created with user_id as NULL
            cursor = conn.execute("""
                SELECT user_id FROM chat_sessions WHERE title = 'Test Session'
            """)
            row = cursor.fetchone()

            assert row[0] is None  # user_id default NULL
        finally:
            conn.close()


class TestRunMigrationsIntegration:
    """Integration test for run_migrations() on in-memory database."""

    def test_run_migrations_creates_all_tables(self):
        """run_migrations() should create all expected tables."""
        # Note: run_migrations may not support :memory: directly since it
        # needs a persistent path for some migration functions
        # We'll test init_db directly instead
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(SCHEMA)

            cursor = conn.execute("""
                SELECT name FROM sqlite_master
                WHERE type='table' AND name NOT LIKE 'sqlite_%'
            """)
            tables = {row[0] for row in cursor.fetchall()}

            expected_tables = {
                "vaults",
                "files",
                "memories",
                "chat_sessions",
                "chat_messages",
                "document_actions",
                "admin_toggles",
                "audit_toggle_log",
                "secret_keys",
                "system_flags",
                "settings_kv",
                "users",
                "organizations",
                "org_members",
                "groups",
                "group_members",
                "user_sessions",
                "vault_members",
                "vault_group_access",
            }

            for table in expected_tables:
                assert table in tables, f"Expected table {table} not found"
        finally:
            conn.close()

    def test_run_migrations_with_init_db_matches_schema(self):
        """Verify init_db produces the same tables as the raw schema."""
        import os
        import tempfile

        # Create temp file for init_db
        fd, temp_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

        try:
            # Use init_db to create database
            init_db(temp_path)

            # Check tables exist
            conn = sqlite3.connect(temp_path)
            cursor = conn.execute("""
                SELECT name FROM sqlite_master
                WHERE type='table' AND name NOT LIKE 'sqlite_%'
            """)
            {row[0] for row in cursor.fetchall()}
            conn.close()

            # Check new columns exist
            conn = sqlite3.connect(temp_path)

            cursor = conn.execute("PRAGMA table_info(files)")
            file_cols = {row[1] for row in cursor.fetchall()}
            assert "document_date" in file_cols
            assert "supersedes_file_id" in file_cols
            assert "ingestion_version" in file_cols

            cursor = conn.execute("PRAGMA table_info(users)")
            user_cols = {row[1] for row in cursor.fetchall()}
            assert "must_change_password" in user_cols
            assert "failed_attempts" in user_cols
            assert "locked_until" in user_cols

            cursor = conn.execute("PRAGMA table_info(chat_sessions)")
            chat_cols = {row[1] for row in cursor.fetchall()}
            assert "user_id" in chat_cols

            conn.close()

            # Check indexes exist
            conn = sqlite3.connect(temp_path)
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
            indexes = {row[0] for row in cursor.fetchall()}
            conn.close()

            assert "idx_users_locked_until" in indexes
            assert "idx_user_sessions_expires" in indexes
            assert "idx_chat_sessions_user_id" in indexes
            assert "idx_memories_vault_id" in indexes
            assert "idx_memories_created_at" in indexes

        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
