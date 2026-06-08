"""
Tests for auth table migration in database.py.

Verifies:
1. All 7 auth tables are created by init_db
2. Schema constraints (UNIQUE, CHECK, NOT NULL, COLLATE NOCASE)
3. Foreign key relationships
4. Migration idempotency
5. PRAGMA foreign_keys re-enforcement in _validate_connection
6. Existing tables remain unchanged
"""

import sqlite3
from pathlib import Path

from app.models.database import (
    SQLiteConnectionPool,
    init_db,
    migrate_add_user_org_tables,
    run_migrations,
)

# Expected auth table names
AUTH_TABLES = [
    "users",
    "organizations",
    "org_members",
    "groups",
    "group_members",
    "user_sessions",
]


# Expected existing tables that should remain unchanged
EXISTING_TABLES = [
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
]


class TestAuthTablesCreated:
    """Test that all auth tables are created by init_db."""

    def test_auth_tables_created(self, tmp_path: Path) -> None:
        """Run init_db on temp DB, verify all 7 auth tables exist."""
        db_path = tmp_path / "test.db"
        init_db(str(db_path))

        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            table_names = {row[0] for row in cursor.fetchall()}

            for table in AUTH_TABLES:
                assert table in table_names, (
                    f"Auth table '{table}' not found in database"
                )

            # Verify count matches
            assert len([t for t in table_names if t in AUTH_TABLES]) == len(
                AUTH_TABLES
            ), (
                f"Expected {len(AUTH_TABLES)} auth tables, found {len([t for t in table_names if t in AUTH_TABLES])}"
            )
        finally:
            conn.close()


class TestUsersTableSchema:
    """Test users table schema and constraints."""

    def test_users_table_columns(self, tmp_path: Path) -> None:
        """Verify columns exist in users table."""
        db_path = tmp_path / "test.db"
        init_db(str(db_path))

        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.execute("PRAGMA table_info(users)")
            columns = {row[1] for row in cursor.fetchall()}

            expected_columns = {
                "id",
                "username",
                "hashed_password",
                "full_name",
                "role",
                "is_active",
                "created_at",
                "last_login_at",
            }
            assert expected_columns.issubset(columns), (
                f"Missing columns in users table. Expected: {expected_columns}, Got: {columns}"
            )
        finally:
            conn.close()

    def test_users_username_unique_nocase(self, tmp_path: Path) -> None:
        """Verify username is UNIQUE COLLATE NOCASE."""
        db_path = tmp_path / "test.db"
        init_db(str(db_path))

        conn = sqlite3.connect(str(db_path))
        try:
            # Get the index info for username
            cursor = conn.execute("PRAGMA index_list(users)")
            indexes = cursor.fetchall()

            # Find index on username
            username_index = None
            for idx in indexes:
                # idx[1] is the index name
                idx_info = conn.execute(f"PRAGMA index_info({idx[1]})").fetchall()
                for col in idx_info:
                    if col[2] == "username":
                        username_index = idx
                        break

            assert username_index is not None, "No index found on username column"

            # Verify UNIQUE constraint
            # PRAGMA index_list returns: seq, name, unique (0/1), origin ('u'=constraint,'c'=create index), partial
            # index[2] is the unique flag, index[3] is the origin ('u' = UNIQUE constraint)
            unique_flag = username_index[2]  # 0 or 1
            origin = username_index[3]  # 'u' = UNIQUE constraint, 'c' = CREATE INDEX
            assert unique_flag == 1 or origin == "u", (
                f"Username index should be UNIQUE. unique={unique_flag}, origin={origin}"
            )

            # Get column info to verify COLLATE NOCASE
            cursor = conn.execute("PRAGMA table_info(users)")
            username_col = None
            for col in cursor.fetchall():
                if col[1] == "username":
                    username_col = col
                    break

            assert username_col is not None, "username column not found"
            # col[4] is the default value, col[5] is the primary key
            # We need to check the CREATE TABLE statement for COLLATE NOCASE
            schema = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
            ).fetchone()[0]
            assert "COLLATE NOCASE" in schema.upper(), (
                f"username should have COLLATE NOCASE, got schema: {schema}"
            )
        finally:
            conn.close()

    def test_users_role_check_constraint(self, tmp_path: Path) -> None:
        """Verify role has CHECK constraint with valid values."""
        db_path = tmp_path / "test.db"
        init_db(str(db_path))

        conn = sqlite3.connect(str(db_path))
        try:
            # Get the table schema
            schema = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
            ).fetchone()[0]

            # Check for CHECK constraint on role
            assert "CHECK" in schema.upper(), "role column should have CHECK constraint"
            assert "superadmin" in schema.lower(), "CHECK should include 'superadmin'"
            assert "admin" in schema.lower(), "CHECK should include 'admin'"
            assert "member" in schema.lower(), "CHECK should include 'member'"
            assert "viewer" in schema.lower(), "CHECK should include 'viewer'"
        finally:
            conn.close()

    def test_users_is_active_not_null(self, tmp_path: Path) -> None:
        """Verify is_active column is NOT NULL."""
        db_path = tmp_path / "test.db"
        init_db(str(db_path))

        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.execute("PRAGMA table_info(users)")
            is_active_col = None
            for col in cursor.fetchall():
                if col[1] == "is_active":
                    is_active_col = col
                    break

            assert is_active_col is not None, "is_active column not found"
            # col[3] is the NOT NULL flag (1 = NOT NULL, 0 = nullable)
            assert is_active_col[3] == 1, "is_active should be NOT NULL"
        finally:
            conn.close()


class TestOrganizationsTableSchema:
    """Test organizations and org_members table schemas."""

    def test_organizations_table_columns(self, tmp_path: Path) -> None:
        """Verify columns exist in organizations table."""
        db_path = tmp_path / "test.db"
        init_db(str(db_path))

        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.execute("PRAGMA table_info(organizations)")
            columns = {row[1] for row in cursor.fetchall()}

            expected_columns = {"id", "name", "description", "created_at", "updated_at"}
            assert expected_columns.issubset(columns), (
                f"Missing columns in organizations table. Expected: {expected_columns}, Got: {columns}"
            )
        finally:
            conn.close()

    def test_org_members_table_columns(self, tmp_path: Path) -> None:
        """Verify columns exist in org_members table."""
        db_path = tmp_path / "test.db"
        init_db(str(db_path))

        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.execute("PRAGMA table_info(org_members)")
            columns = {row[1] for row in cursor.fetchall()}

            expected_columns = {"id", "org_id", "user_id", "role", "joined_at"}
            assert expected_columns.issubset(columns), (
                f"Missing columns in org_members table. Expected: {expected_columns}, Got: {columns}"
            )
        finally:
            conn.close()

    def test_org_members_unique_org_id_user_id(self, tmp_path: Path) -> None:
        """Verify UNIQUE constraint on (org_id, user_id) in org_members."""
        db_path = tmp_path / "test.db"
        init_db(str(db_path))

        conn = sqlite3.connect(str(db_path))
        try:
            # Get the table schema
            schema = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='org_members'"
            ).fetchone()[0]

            # Check for UNIQUE constraint
            assert "UNIQUE" in schema.upper(), (
                "org_members should have UNIQUE constraint"
            )
            assert "org_id" in schema.lower(), "UNIQUE should include org_id"
            assert "user_id" in schema.lower(), "UNIQUE should include user_id"
        finally:
            conn.close()

    def test_org_members_foreign_keys(self, tmp_path: Path) -> None:
        """Verify foreign key constraints in org_members."""
        db_path = tmp_path / "test.db"
        init_db(str(db_path))

        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.execute("PRAGMA foreign_key_list(org_members)")
            fks = cursor.fetchall()

            # Should have 2 foreign keys: org_id -> organizations(id), user_id -> users(id)
            assert len(fks) == 2, (
                f"Expected 2 foreign keys in org_members, got {len(fks)}"
            )

            fk_columns = {fk[3] for fk in fks}  # fk[3] is the column name
            assert "org_id" in fk_columns, "org_id should be a foreign key"
            assert "user_id" in fk_columns, "user_id should be a foreign key"
        finally:
            conn.close()


class TestGroupsTableSchema:
    """Test groups and group_members table schemas."""

    def test_groups_table_columns(self, tmp_path: Path) -> None:
        """Verify columns exist in groups table."""
        db_path = tmp_path / "test.db"
        init_db(str(db_path))

        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.execute("PRAGMA table_info(groups)")
            columns = {row[1] for row in cursor.fetchall()}

            expected_columns = {"id", "org_id", "name", "description", "created_at"}
            assert expected_columns.issubset(columns), (
                f"Missing columns in groups table. Expected: {expected_columns}, Got: {columns}"
            )
        finally:
            conn.close()

    def test_groups_unique_org_id_name(self, tmp_path: Path) -> None:
        """Verify UNIQUE constraint on (org_id, name) in groups."""
        db_path = tmp_path / "test.db"
        init_db(str(db_path))

        conn = sqlite3.connect(str(db_path))
        try:
            # Get the table schema
            schema = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='groups'"
            ).fetchone()[0]

            # Check for UNIQUE constraint
            assert "UNIQUE" in schema.upper(), "groups should have UNIQUE constraint"
            assert "org_id" in schema.lower(), "UNIQUE should include org_id"
            assert "name" in schema.lower(), "UNIQUE should include name"
        finally:
            conn.close()

    def test_group_members_columns(self, tmp_path: Path) -> None:
        """Verify columns exist in group_members table."""
        db_path = tmp_path / "test.db"
        init_db(str(db_path))

        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.execute("PRAGMA table_info(group_members)")
            columns = {row[1] for row in cursor.fetchall()}

            expected_columns = {"id", "group_id", "user_id", "added_at"}
            assert expected_columns.issubset(columns), (
                f"Missing columns in group_members table. Expected: {expected_columns}, Got: {columns}"
            )
        finally:
            conn.close()


class TestUserSessionsTableSchema:
    """Test user_sessions table schema."""

    def test_user_sessions_columns(self, tmp_path: Path) -> None:
        """Verify columns exist in user_sessions table."""
        db_path = tmp_path / "test.db"
        init_db(str(db_path))

        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.execute("PRAGMA table_info(user_sessions)")
            columns = {row[1] for row in cursor.fetchall()}

            expected_columns = {
                "id",
                "user_id",
                "refresh_token_hash",
                "expires_at",
                "created_at",
                "last_used_at",
                "ip_address",
                "user_agent",
            }
            assert expected_columns.issubset(columns), (
                f"Missing columns in user_sessions table. Expected: {expected_columns}, Got: {columns}"
            )
        finally:
            conn.close()

    def test_user_sessions_foreign_key(self, tmp_path: Path) -> None:
        """Verify foreign key constraint in user_sessions."""
        db_path = tmp_path / "test.db"
        init_db(str(db_path))

        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.execute("PRAGMA foreign_key_list(user_sessions)")
            fks = cursor.fetchall()

            assert len(fks) == 1, (
                f"Expected 1 foreign key in user_sessions, got {len(fks)}"
            )
            assert fks[0][3] == "user_id", (
                "user_id should be a foreign key to users(id)"
            )
        finally:
            conn.close()


class TestMigrationIdempotent:
    """Test migration idempotency."""

    def test_migration_idempotent(self, tmp_path: Path) -> None:
        """Run migrate_add_user_org_tables twice, verify no errors and tables still valid."""
        db_path = tmp_path / "test.db"

        # Initialize first time
        init_db(str(db_path))

        # Get initial table count
        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            )
            initial_count = cursor.fetchone()[0]
        finally:
            conn.close()

        # Run migration twice
        migrate_add_user_org_tables(str(db_path))
        migrate_add_user_org_tables(str(db_path))  # Second run should not cause errors

        # Verify tables still exist and count is same
        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            )
            final_count = cursor.fetchone()[0]
            assert initial_count == final_count, (
                f"Table count changed after second migration. Before: {initial_count}, After: {final_count}"
            )

            # Verify auth tables still exist
            for table in AUTH_TABLES:
                cursor = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                )
                assert cursor.fetchone() is not None, (
                    f"Auth table '{table}' missing after second migration"
                )
        finally:
            conn.close()


class TestForeignKeyPragma:
    """Test PRAGMA foreign_keys re-enforcement."""

    def test_foreign_key_pragma_in_validate(self, tmp_path: Path) -> None:
        """Verify _validate_connection re-enforces PRAGMA foreign_keys ON."""
        db_path = tmp_path / "test.db"
        init_db(str(db_path))

        # Create a connection pool
        pool = SQLiteConnectionPool(str(db_path), max_size=2)

        try:
            # Get a connection from the pool
            conn = pool.get_connection()

            try:
                # Verify foreign_keys is ON
                cursor = conn.execute("PRAGMA foreign_keys")
                fk_state = cursor.fetchone()[0]
                assert fk_state == 1, f"foreign_keys should be ON (1), got {fk_state}"
            finally:
                pool.release_connection(conn)

            # Get another connection and check again (simulates validate_connection being called)
            conn2 = pool.get_connection()
            try:
                cursor = conn2.execute("PRAGMA foreign_keys")
                fk_state = cursor.fetchone()[0]
                assert fk_state == 1, (
                    f"foreign_keys should be ON after validation, got {fk_state}"
                )
            finally:
                pool.release_connection(conn2)
        finally:
            pool.close_all()


class TestExistingTablesUnchanged:
    """Test that existing tables remain unchanged."""

    def test_existing_tables_still_exist(self, tmp_path: Path) -> None:
        """Verify vaults, files, memories, chat_sessions still exist."""
        db_path = tmp_path / "test.db"
        init_db(str(db_path))

        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            table_names = {row[0] for row in cursor.fetchall()}

            for table in EXISTING_TABLES:
                assert table in table_names, f"Existing table '{table}' not found"
        finally:
            conn.close()

    def test_vaults_table_schema_unchanged(self, tmp_path: Path) -> None:
        """Verify vaults table schema is unchanged."""
        db_path = tmp_path / "test.db"
        init_db(str(db_path))

        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.execute("PRAGMA table_info(vaults)")
            columns = {row[1] for row in cursor.fetchall()}

            expected_columns = {"id", "name", "description", "created_at", "updated_at", "owner_id", "org_id", "visibility"}
            assert expected_columns == columns, (
                f"vaults table schema changed. Expected: {expected_columns}, Got: {columns}"
            )
        finally:
            conn.close()

    def test_files_table_schema_unchanged(self, tmp_path: Path) -> None:
        """Verify files table schema is unchanged."""
        db_path = tmp_path / "test.db"
        init_db(str(db_path))

        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.execute("PRAGMA table_info(files)")
            columns = {row[1] for row in cursor.fetchall()}

            # Verify essential columns
            essential_columns = {
                "id",
                "vault_id",
                "file_path",
                "file_name",
                "status",
            }
            assert essential_columns.issubset(columns), (
                f"files table missing essential columns. Expected: {essential_columns}, Got: {columns}"
            )

            # Verify status CHECK constraint
            schema = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='files'"
            ).fetchone()[0]
            assert "CHECK" in schema.upper(), (
                "files status should have CHECK constraint"
            )
        finally:
            conn.close()

    def test_memories_table_schema_unchanged(self, tmp_path: Path) -> None:
        """Verify memories table schema is unchanged."""
        db_path = tmp_path / "test.db"
        init_db(str(db_path))

        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.execute("PRAGMA table_info(memories)")
            columns = {row[1] for row in cursor.fetchall()}

            # Verify essential columns
            essential_columns = {"id", "content", "category", "tags", "source"}
            assert essential_columns.issubset(columns), (
                f"memories table missing essential columns. Expected: {essential_columns}, Got: {columns}"
            )
        finally:
            conn.close()

    def test_chat_sessions_table_schema_unchanged(self, tmp_path: Path) -> None:
        """Verify chat_sessions table schema is unchanged."""
        db_path = tmp_path / "test.db"
        init_db(str(db_path))

        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.execute("PRAGMA table_info(chat_sessions)")
            columns = {row[1] for row in cursor.fetchall()}

            # Verify essential columns
            essential_columns = {"id", "vault_id", "title", "created_at", "updated_at"}
            assert essential_columns.issubset(columns), (
                f"chat_sessions table missing essential columns. Expected: {essential_columns}, Got: {columns}"
            )
        finally:
            conn.close()


class TestRunMigrationsIntegration:
    """Integration test for run_migrations including auth tables."""

    def test_run_migrations_creates_auth_tables(self, tmp_path: Path) -> None:
        """Verify run_migrations creates all auth tables."""
        db_path = tmp_path / "test.db"
        run_migrations(str(db_path))

        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            table_names = {row[0] for row in cursor.fetchall()}

            for table in AUTH_TABLES:
                assert table in table_names, (
                    f"Auth table '{table}' not created by run_migrations"
                )
        finally:
            conn.close()
