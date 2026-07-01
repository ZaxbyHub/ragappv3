"""Tests for the idx_wiki_claims_vault_normalized SCHEMA removal (issue #209).

Verifies three things:
1. SCHEMA constant no longer contains idx_wiki_claims_vault_normalized.
2. migrate_add_wiki_claims_normalized_text() creates the index correctly.
3. init_db() succeeds against a pre-migration wiki_claims table (no normalized_text col).
"""

import os
import re
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.models.database import (
    SCHEMA,
    init_db,
    migrate_add_wiki_claims_normalized_text,
    run_migrations,
)


class TestIdxWikiClaimsVaultNormalizedFix(unittest.TestCase):
    """Regression tests for the idx_wiki_claims_vault_normalized SCHEMA removal."""

    def setUp(self):
        """Create a temporary database file for each test."""
        self.temp_fd, self.temp_db_path = tempfile.mkstemp(suffix='.db')
        os.close(self.temp_fd)

    def tearDown(self):
        """Clean up the temporary database file."""
        if os.path.exists(self.temp_db_path):
            os.remove(self.temp_db_path)

    def test_SCHEMA_does_not_reference_idx_wiki_claims_vault_normalized(self):
        """SCHEMA constant must not contain idx_wiki_claims_vault_normalized.

        The index is created idempotently by migrate_add_wiki_claims_normalized_text().
        Having it in SCHEMA causes init_db() to abort all migrations on existing DBs
        that have a wiki_claims table but lack the normalized_text column.
        """
        self.assertIsNone(
            re.search(r'idx_wiki_claims_vault_normalized', SCHEMA),
            "SCHEMA must not reference idx_wiki_claims_vault_normalized; "
            "it is created by migrate_add_wiki_claims_normalized_text()"
        )

    def test_migration_creates_the_normalized_text_index(self):
        """migrate_add_wiki_claims_normalized_text() creates the index on vault_id+normalized_text."""
        conn = sqlite3.connect(self.temp_db_path)
        conn.execute("""
            CREATE TABLE wiki_claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vault_id INTEGER NOT NULL,
                claim_text TEXT,
                status TEXT DEFAULT 'active'
            )
        """)
        conn.execute(
            "INSERT INTO wiki_claims (vault_id, claim_text) VALUES (?, ?)",
            (1, "Sample claim text"),
        )
        conn.commit()
        conn.close()

        migrate_add_wiki_claims_normalized_text(self.temp_db_path)

        conn = sqlite3.connect(self.temp_db_path)
        try:
            result = conn.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type='index' AND name='idx_wiki_claims_vault_normalized'
                """
            ).fetchone()
        finally:
            conn.close()

        self.assertIsNotNone(
            result,
            "migrate_add_wiki_claims_normalized_text() must create "
            "idx_wiki_claims_vault_normalized"
        )

    def test_init_db_succeeds_with_legacy_wiki_claims_table(self):
        """init_db() must not abort when wiki_claims exists without normalized_text.

        This is the core regression: before the fix, executing SCHEMA (via
        conn.executescript(SCHEMA)) would fail with:
          OperationalError: no such column: normalized_text
        because SCHEMA contained:
          CREATE INDEX IF NOT EXISTS idx_wiki_claims_vault_normalized
              ON wiki_claims(vault_id, normalized_text);
        while the actual wiki_claims table (from an old migration) had no
        normalized_text column.
        """
        # Simulate a legacy database: full schema up to wiki_claims WITHOUT
        # the normalized_text column or the index.  The simplest way is to
        # build everything before wiki_claims via init_db, then inject a
        # bare-bones wiki_claims table lacking the column.
        init_db(self.temp_db_path)

        conn = sqlite3.connect(self.temp_db_path)
        try:
            # Drop the current wiki_claims if it exists and replace with a legacy
            # version that predates the normalized_text column.
            conn.execute("DROP TABLE IF EXISTS wiki_claims")
            conn.execute("""
                CREATE TABLE wiki_claims (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    page_id INTEGER,
                    vault_id INTEGER NOT NULL,
                    claim_text TEXT,
                    status TEXT DEFAULT 'active',
                    source TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute(
                "INSERT INTO wiki_claims (page_id, vault_id, claim_text) VALUES (?, ?, ?)",
                (1, 1, "Legacy claim without normalized_text"),
            )
            conn.commit()
        finally:
            conn.close()

        # Re-run init_db on the legacy database — must not raise.
        try:
            init_db(self.temp_db_path)
        except sqlite3.OperationalError as exc:
            self.fail(f"init_db() raised OperationalError on legacy wiki_claims: {exc}")

    def test_run_migrations_includes_normalized_text_index(self):
        """run_migrations() must result in idx_wiki_claims_vault_normalized existing."""
        run_migrations(self.temp_db_path)

        conn = sqlite3.connect(self.temp_db_path)
        try:
            result = conn.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type='index' AND name='idx_wiki_claims_vault_normalized'
                """
            ).fetchone()
        finally:
            conn.close()

        self.assertIsNotNone(
            result,
            "After run_migrations(), idx_wiki_claims_vault_normalized must exist"
        )


if __name__ == '__main__':
    unittest.main()
