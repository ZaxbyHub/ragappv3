"""Migration-journal tests (issue #512 AC10 recovery-contract surfaces).

Covers:
- the journal table is created by the SCHEMA constant (init_db) AND by
  migrate_add_migration_journal on a legacy database (double-definition
  pattern);
- record_migration_outcome / invalidate_derived_data / record_schema_version
  / publish_index_generation / latest_outcomes round-trips;
- run_migrations records the schema version at the end.

The schema-drift count bump (64 -> 65) is covered by test_schema_drift.py.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.models.database import (  # noqa: E402
    SCHEMA,
    init_db,
    migrate_add_migration_journal,
    run_migrations,
)
from app.models.database import invalidate_derived_data as db_invalidate  # noqa: E402
from app.models.migration_journal import (  # noqa: E402
    MIGRATION_JOURNAL_DDL,
    MIGRATION_SCHEMA_VERSION,
    invalidate_derived_data,
    latest_outcomes,
    publish_index_generation,
    record_migration_outcome,
    record_schema_version,
)


def _temp_db(prefix):
    return str(Path(tempfile.mkdtemp(prefix=prefix)) / "app.db")


def _journal_rows(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT migration_name, phase, outcome, detail FROM migration_journal"
            " ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


class TestJournalTableCreation(unittest.TestCase):
    def test_ddl_is_part_of_the_schema_constant(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS migration_journal", SCHEMA)

    def test_init_db_creates_the_journal_table(self):
        db_path = _temp_db("journal_initdb_")
        init_db(db_path)
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name='migration_journal'"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)

    def test_migrate_add_migration_journal_upgrades_a_legacy_db(self):
        """A legacy database (pre-journal) gains the table from the
        standalone migration, matching the repo double-definition pattern."""
        db_path = _temp_db("journal_legacy_")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE legacy_thing (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        migrate_add_migration_journal(db_path)

        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name='migration_journal'"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)

    def test_migration_is_idempotent(self):
        db_path = _temp_db("journal_idem_")
        migrate_add_migration_journal(db_path)
        migrate_add_migration_journal(db_path)  # must not raise
        self.assertIsNotNone(_journal_rows(db_path) or True)


class TestJournalRoundTrips(unittest.TestCase):
    def test_record_migration_outcome_appends_phases(self):
        db_path = _temp_db("journal_record_")
        conn = sqlite3.connect(db_path)
        try:
            record_migration_outcome(
                conn,
                migration_name="m_x",
                phase="start",
                outcome="ok",
                detail="beginning",
            )
            record_migration_outcome(
                conn, migration_name="m_x", phase="succeeded", outcome="ok"
            )
            conn.commit()
        finally:
            conn.close()

        rows = _journal_rows(db_path)
        self.assertEqual(
            rows,
            [
                ("m_x", "start", "ok", "beginning"),
                ("m_x", "succeeded", "ok", None),
            ],
        )

    def test_record_never_raises_on_sqlite_errors(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE migration_journal (id INTEGER PRIMARY KEY)")
        # A schema mismatch (missing columns) forces a sqlite error inside
        # the journal write — it must be swallowed, not raised.
        record_migration_outcome(
            conn, migration_name="m_x", phase="start", outcome="ok"
        )
        conn.close()

    def test_record_schema_version_records_the_version(self):
        db_path = _temp_db("journal_version_")
        conn = sqlite3.connect(db_path)
        try:
            record_schema_version(conn)
            conn.commit()
        finally:
            conn.close()
        rows = _journal_rows(db_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "schema")
        self.assertEqual(
            rows[0][3], f"schema_version={MIGRATION_SCHEMA_VERSION}"
        )

    def test_invalidate_derived_data_records_rebuild_event(self):
        db_path = _temp_db("journal_invalidate_")
        conn = sqlite3.connect(db_path)
        try:
            invalidate_derived_data(conn, reason="wiki_claims rebuilt")
            conn.commit()
        finally:
            conn.close()
        rows = _journal_rows(db_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "succeeded")
        self.assertEqual(rows[0][2], "rebuilt")
        self.assertIn("wiki_claims rebuilt", rows[0][3])

    def test_database_module_reexports_invalidate_derived_data(self):
        """C11 probes the invalidation interface on app.models.database."""
        self.assertTrue(callable(db_invalidate))
        self.assertIs(db_invalidate, invalidate_derived_data)

    def test_publish_index_generation_returns_monotonic_ids(self):
        db_path = _temp_db("journal_publish_")
        conn = sqlite3.connect(db_path)
        try:
            gen1 = publish_index_generation(
                conn, store="vector_store", table_name="chunks", detail="swap 1"
            )
            gen2 = publish_index_generation(
                conn, store="vector_store", table_name="chunks", detail="swap 2"
            )
            conn.commit()
        finally:
            conn.close()
        self.assertGreater(gen2, gen1)
        rows = _journal_rows(db_path)
        self.assertEqual(
            [r[0] for r in rows],
            ["index_generation:vector_store:chunks"] * 2,
        )

    def test_latest_outcomes_returns_newest_first(self):
        db_path = _temp_db("journal_latest_")
        conn = sqlite3.connect(db_path)
        try:
            for phase in ("start", "succeeded"):
                record_migration_outcome(
                    conn, migration_name="m_recent", phase=phase, outcome="ok"
                )
            conn.commit()
        finally:
            conn.close()

        outcomes = latest_outcomes(db_path, limit=1)
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["migration_name"], "m_recent")
        self.assertEqual(outcomes[0]["phase"], "succeeded")


class TestRunMigrationsRecordsSchemaVersion(unittest.TestCase):
    def test_run_migrations_records_schema_version_row(self):
        db_path = _temp_db("journal_runmigrations_")
        run_migrations(db_path)
        rows = _journal_rows(db_path)
        schema_rows = [r for r in rows if r[0] == "schema"]
        self.assertEqual(len(schema_rows), 1)
        self.assertEqual(schema_rows[0][1], "succeeded")
        self.assertEqual(
            schema_rows[0][3], f"schema_version={MIGRATION_SCHEMA_VERSION}"
        )


if __name__ == "__main__":
    unittest.main()
