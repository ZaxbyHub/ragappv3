"""Migration tests for issue #791 (Workstream O, PR 1).

wiki_lint_findings gains an ``origin`` producer column and the system-only
``auto_resolved`` status via a rename-rebuild migration modeled on
migrate_add_wiki_lint_findings_json_check. These tests pin the fresh-init
shape, the legacy backfill, idempotence, the AUTOINCREMENT high-water, and
the interrupted-rename / failed-partial-copy recovery paths.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.models.database import (
    migrate_wiki_lint_findings_origin_auto_resolved,
    run_migrations,
)

# Post-json-check, pre-#791 shape: 4-value status CHECK, no origin column.
_OLD_LINT_DDL = """
CREATE TABLE wiki_lint_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_id INTEGER NOT NULL,
    finding_type TEXT NOT NULL CHECK (finding_type IN (
        'contradiction','stale','orphan','missing_page',
        'unsupported_claim','duplicate_entity','weak_provenance')),
    severity TEXT NOT NULL DEFAULT 'medium' CHECK (severity IN (
        'low','medium','high','critical')),
    title TEXT NOT NULL,
    details TEXT DEFAULT '',
    related_page_ids_json TEXT NOT NULL DEFAULT '[]'
        CHECK (json_type(related_page_ids_json) = 'array'),
    related_claim_ids_json TEXT NOT NULL DEFAULT '[]'
        CHECK (json_type(related_claim_ids_json) = 'array'),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN (
        'open','acknowledged','resolved','dismissed')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""


def _tmp_db():
    return str(Path(tempfile.mkdtemp()) / "o01.db")


class TestO01FreshInit(unittest.TestCase):
    def test_fresh_init_carries_origin_and_auto_resolved(self):
        db = _tmp_db()
        run_migrations(db)
        conn = sqlite3.connect(db)
        try:
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(wiki_lint_findings)").fetchall()
            }
            self.assertIn("origin", cols)
            conn.execute(
                "INSERT INTO wiki_lint_findings (vault_id, finding_type, title,"
                " status, origin) VALUES (1, 'orphan', 't1', 'auto_resolved', 'linter')"
            )
            conn.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO wiki_lint_findings (vault_id, finding_type, title,"
                    " status, origin)"
                    " VALUES (1, 'orphan', 'bad-origin', 'open', 'bogus_origin')"
                )
            conn.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO wiki_lint_findings (vault_id, finding_type, title,"
                    " status, origin)"
                    " VALUES (1, 'orphan', 'bad-status', 'bogus_status', 'linter')"
                )
            conn.rollback()
        finally:
            conn.close()

    def test_migration_idempotent_after_run_migrations(self):
        db = _tmp_db()
        run_migrations(db)
        # Explicit second call must be a no-op (marker early-return).
        migrate_wiki_lint_findings_origin_auto_resolved(db)
        conn = sqlite3.connect(db)
        try:
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(wiki_lint_findings)").fetchall()
            }
            self.assertIn("origin", cols)
        finally:
            conn.close()


class TestO01UpgradeBackfill(unittest.TestCase):
    def _seed_old_rows(self, conn):
        rows = [
            # (finding_type, title, status) -> expected origin
            ("unsupported_claim",
             "Unverified claim: extracted from query answer with no per-claim citations",
             "open", "compiler"),
            ("unsupported_claim", "Unsupported claim: some claim text", "open", "linter"),
            ("stale", "Claim stale: source document deleted (file_id=7)",
             "dismissed", "invalidation"),
            ("weak_provenance", "Weak provenance: source document deleted (file_id=7)",
             "open", "invalidation"),
            ("contradiction", "Conflicting claims: x", "resolved", "linter"),
        ]
        for finding_type, title, status, _ in rows:
            conn.execute(
                "INSERT INTO wiki_lint_findings"
                " (vault_id, finding_type, severity, title, details, status)"
                " VALUES (1, ?, 'medium', ?, '', ?)",
                (finding_type, title, status),
            )
        conn.commit()
        return rows

    def test_backfill_classifies_origins_and_preserves_rows(self):
        db = _tmp_db()
        conn = sqlite3.connect(db)
        try:
            conn.execute(_OLD_LINT_DDL)
            rows = self._seed_old_rows(conn)
        finally:
            conn.close()

        migrate_wiki_lint_findings_origin_auto_resolved(db)

        conn = sqlite3.connect(db)
        try:
            got = conn.execute(
                "SELECT finding_type, title, status, origin FROM wiki_lint_findings"
                " ORDER BY id"
            ).fetchall()
            self.assertEqual(len(got), len(rows))
            for (finding_type, title, status, _), row in zip(rows, got):
                self.assertEqual(row[0], finding_type)
                self.assertEqual(row[1], title)
                self.assertEqual(row[2], status)
            origins = {row[1]: row[3] for row in got}
            self.assertEqual(origins["Unsupported claim: some claim text"], "linter")
            self.assertEqual(
                origins[
                    "Unverified claim: extracted from query answer with no per-claim citations"
                ],
                "compiler",
            )
            self.assertEqual(
                origins["Claim stale: source document deleted (file_id=7)"],
                "invalidation",
            )
            self.assertEqual(
                origins["Weak provenance: source document deleted (file_id=7)"],
                "invalidation",
            )
            self.assertEqual(origins["Conflicting claims: x"], "linter")
            # auto_resolved becomes writable post-migration.
            conn.execute(
                "UPDATE wiki_lint_findings SET status = 'auto_resolved' WHERE id = 2"
            )
            conn.commit()
        finally:
            conn.close()

    def test_high_water_preserved_when_top_ids_deleted(self):
        db = _tmp_db()
        conn = sqlite3.connect(db)
        try:
            conn.execute(_OLD_LINT_DDL)
            for i in range(1, 6):
                conn.execute(
                    "INSERT INTO wiki_lint_findings (vault_id, finding_type, title)"
                    " VALUES (1, 'stale', ?)",
                    (f"finding {i}",),
                )
            conn.execute("DELETE FROM wiki_lint_findings WHERE id IN (4, 5)")
            conn.commit()
        finally:
            conn.close()

        migrate_wiki_lint_findings_origin_auto_resolved(db)

        conn = sqlite3.connect(db)
        try:
            cur = conn.execute(
                "INSERT INTO wiki_lint_findings (vault_id, finding_type, title)"
                " VALUES (1, 'stale', 'post-rebuild')"
            )
            conn.commit()
            self.assertGreater(cur.lastrowid, 5)
        finally:
            conn.close()


class TestO01Recovery(unittest.TestCase):
    def test_interrupted_rename_restores_and_migrates(self):
        db = _tmp_db()
        conn = sqlite3.connect(db, isolation_level=None)
        try:
            conn.execute(_OLD_LINT_DDL)
            conn.execute(
                "INSERT INTO wiki_lint_findings (vault_id, finding_type, title)"
                " VALUES (1, 'stale', 'survivor')"
            )
            conn.execute("ALTER TABLE wiki_lint_findings RENAME TO _wiki_lint_findings_old")
        finally:
            conn.close()

        migrate_wiki_lint_findings_origin_auto_resolved(db)

        conn = sqlite3.connect(db)
        try:
            row = conn.execute(
                "SELECT origin FROM wiki_lint_findings WHERE title = 'survivor'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "invalidation")
        finally:
            conn.close()

    def test_failed_partial_copy_restores_authoritative_backup(self):
        db = _tmp_db()
        conn = sqlite3.connect(db, isolation_level=None)
        try:
            conn.execute(_OLD_LINT_DDL)
            conn.execute(
                "INSERT INTO wiki_lint_findings (vault_id, finding_type, title)"
                " VALUES (1, 'stale', 'kept')"
            )
            conn.execute("ALTER TABLE wiki_lint_findings RENAME TO _wiki_lint_findings_old")
            # New-shape destination with a MISSING id: a failed partial copy.
            conn.execute("""
                CREATE TABLE wiki_lint_findings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vault_id INTEGER NOT NULL,
                    finding_type TEXT NOT NULL,
                    severity TEXT NOT NULL DEFAULT 'medium',
                    title TEXT NOT NULL,
                    details TEXT DEFAULT '',
                    related_page_ids_json TEXT NOT NULL DEFAULT '[]',
                    related_claim_ids_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'open',
                    origin TEXT NOT NULL DEFAULT 'linter' CHECK (origin IN (
                        'linter','invalidation','compiler','curator')),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        finally:
            conn.close()

        migrate_wiki_lint_findings_origin_auto_resolved(db)

        conn = sqlite3.connect(db)
        try:
            row = conn.execute(
                "SELECT origin FROM wiki_lint_findings WHERE title = 'kept'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "invalidation")
            gone = conn.execute(
                "SELECT name FROM sqlite_master WHERE name = '_wiki_lint_findings_old'"
            ).fetchone()
            self.assertIsNone(gone)
        finally:
            conn.close()


class TestO01FreshMigratedConvergence(unittest.TestCase):
    """A pre-migration database walked through run_migrations must land on
    the same wiki_lint_findings shape a fresh database gets — the property
    the fresh-schema comment at the CREATE site declares a MUST (any future
    rebuild that silently drops a CHECK or the origin column fails here)."""

    @staticmethod
    def _normalized_shape(conn):
        import re

        def norm(sql):
            stripped = re.sub(r"--[^\n]*", "", sql or "")
            return re.sub(r"\s+", " ", stripped).strip()

        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='wiki_lint_findings'"
        ).fetchone()[0]
        indexes = sorted(
            row[0]
            for row in conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index'"
                " AND tbl_name='wiki_lint_findings' AND sql IS NOT NULL"
            )
        )
        columns = [
            (row[1], row[2], row[3], row[4], row[5])
            for row in conn.execute("PRAGMA table_info(wiki_lint_findings)")
        ]
        return norm(table_sql), [norm(i) for i in indexes], columns

    def test_migrated_shape_equals_fresh_shape(self):
        fresh_db = _tmp_db()
        run_migrations(fresh_db)

        upgraded_db = _tmp_db()
        conn = sqlite3.connect(upgraded_db)
        try:
            conn.execute(_OLD_LINT_DDL)
            conn.execute(
                "INSERT INTO wiki_lint_findings (vault_id, finding_type, title)"
                " VALUES (1, 'stale', 'legacy row')"
            )
            conn.commit()
        finally:
            conn.close()
        run_migrations(upgraded_db)

        fresh_conn = sqlite3.connect(fresh_db)
        upgraded_conn = sqlite3.connect(upgraded_db)
        try:
            self.assertEqual(
                self._normalized_shape(fresh_conn),
                self._normalized_shape(upgraded_conn),
            )
        finally:
            fresh_conn.close()
            upgraded_conn.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
