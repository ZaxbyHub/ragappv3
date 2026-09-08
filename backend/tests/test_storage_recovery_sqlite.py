"""Non-destructive SQLite storage-recovery tests (issue #512).

Registered companions of the frozen issue-tracer checks C4/C5/C6/C10,
extended with the crash-state injections the frozen checks only simulate
as pre-built on-disk states:

- DB-001 (migrate_add_curator_claim_support): backup-only,
  backup+empty-destination, backup+partial-destination, and an injected
  mid-copy failure whose atomic transaction must leave the pre-swap state
  untouched so a retry completes.
- Lint analog (migrate_add_wiki_lint_findings_json_check): the same states.
- DB-002 (migrate_widen_wiki_claim_sources_source_kind): renamed-only
  restore and both-present parity disposition.
- DB-003 (migrate_add_wiki_claims_unique_claim_text): duplicate claims with
  distinct evidence survive on the surviving claim; foreign_key_check clean.
- SEARCH-005 (migrate_add_files_content_fts): failing rebuild rolls back,
  retry re-runs creation+backfill, document matches body FTS.

All fixtures are real sqlite3 temp files (repo convention for DB tests).
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.models.database import (  # noqa: E402
    migrate_add_curator_claim_support,
    migrate_add_files_content_fts,
    migrate_add_wiki_claims_unique_claim_text,
    migrate_add_wiki_lint_findings_json_check,
    migrate_widen_wiki_claim_sources_source_kind,
)

# ---------------------------------------------------------------------------
# Shared fixtures (frozen-check idioms)
# ---------------------------------------------------------------------------

_OLD_CLAIMS_DDL = """
CREATE TABLE {name} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_id INTEGER NOT NULL,
    page_id INTEGER,
    claim_text TEXT NOT NULL,
    claim_type TEXT NOT NULL DEFAULT 'fact',
    subject TEXT,
    predicate TEXT,
    object TEXT,
    source_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN (
        'active','contradicted','superseded','unverified','archived')),
    confidence REAL DEFAULT 0.0,
    created_by INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

_NEW_CLAIMS_DDL = """
CREATE TABLE {name} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_id INTEGER NOT NULL,
    page_id INTEGER,
    claim_text TEXT NOT NULL,
    claim_type TEXT NOT NULL DEFAULT 'fact',
    subject TEXT,
    predicate TEXT,
    object TEXT,
    source_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN (
        'active','contradicted','superseded','unverified','archived','needs_review')),
    confidence REAL DEFAULT 0.0,
    created_by INTEGER,
    created_by_kind TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

_CLAIMS_FTS_DDL = (
    "CREATE VIRTUAL TABLE wiki_claims_fts USING fts5("
    "claim_text, subject, predicate, object)"
)

_CLAIM_ROWS = [
    (1, 7, "Claim one text", "fact", "s1", "p1", "o1", "document", "active", 0.9),
    (2, 7, "Claim two text", "fact", "s2", "p2", "o2", "document", "active", 0.8),
    (3, 8, "Claim three text", "fact", "s3", "p3", "o3", "memory", "active", 0.7),
]

_OLD_LINT_DDL = """
CREATE TABLE {name} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_id INTEGER NOT NULL,
    finding_type TEXT NOT NULL CHECK (finding_type IN (
        'contradiction','stale','orphan','missing_page',
        'unsupported_claim','duplicate_entity','weak_provenance')),
    severity TEXT NOT NULL DEFAULT 'medium' CHECK (severity IN (
        'low','medium','high','critical')),
    title TEXT NOT NULL,
    details TEXT DEFAULT '',
    related_page_ids_json TEXT NOT NULL DEFAULT '[]',
    related_claim_ids_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN (
        'open','acknowledged','resolved','dismissed')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

_NEW_LINT_DDL = """
CREATE TABLE {name} (
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

_LINT_ROWS = [
    (1, 7, "stale", "high", "Finding one", "d1", "[1]", "[1]", "open"),
    (2, 7, "orphan", "low", "Finding two", "d2", "[2]", "[]", "open"),
    (3, 8, "contradiction", "critical", "Finding three", "d3", "[]", "[2]", "resolved"),
]

_OLD_SOURCES_DDL = """
CREATE TABLE {name} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER NOT NULL REFERENCES wiki_claims(id) ON DELETE CASCADE,
    source_kind TEXT NOT NULL CHECK (source_kind IN (
        'document','memory','chat_message','manual')),
    file_id INTEGER,
    chunk_id TEXT,
    memory_id INTEGER,
    chat_message_id INTEGER,
    source_label TEXT,
    quote TEXT,
    char_start INTEGER,
    char_end INTEGER,
    page_number INTEGER,
    confidence REAL DEFAULT 0.0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

_UNIQUE_TOKEN = "quokkazypher"
_DOC_TEXT = (
    f"Ledger entry: the {_UNIQUE_TOKEN} clause governs appendix Z "
    "and supersedes all prior drafts of the warranty section."
)


def _temp_db(prefix):
    return str(Path(tempfile.mkdtemp(prefix=prefix)) / "app.db")


def _table_exists(conn, name):
    return (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        is not None
    )


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608


def _insert_claims(conn, table, rows=None):
    for row in rows or _CLAIM_ROWS:
        conn.execute(
            f"INSERT INTO {table} (id, vault_id, claim_text, claim_type, "  # noqa: S608
            "subject, predicate, object, source_type, status, confidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )


def _insert_lint(conn, table, rows=None):
    for row in rows or _LINT_ROWS:
        conn.execute(
            f"INSERT INTO {table} (id, vault_id, finding_type, severity, title, "  # noqa: S608
            "details, related_page_ids_json, related_claim_ids_json, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )


class _FailOnceProxy:
    """Wraps a real sqlite3.Connection; fails the FIRST execute whose SQL
    matches ``needle`` (delegates everything else, including attribute
    writes like ``isolation_level`` — the migrations set it on the object
    sqlite3.connect hands them)."""

    _OWN = {"_real", "_needle", "_armed"}

    def __init__(self, real, needle):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_needle", needle)
        object.__setattr__(self, "_armed", True)

    @property
    def armed(self):
        return object.__getattribute__(self, "_armed")

    def execute(self, sql, params=()):
        if self._armed and self._needle in sql:
            object.__setattr__(self, "_armed", False)
            raise sqlite3.OperationalError(
                f"injected transient failure matching {self._needle!r}"
            )
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __setattr__(self, name, value):
        if name in self._OWN:
            object.__setattr__(self, name, value)
        else:
            setattr(self._real, name, value)


def _run_with_failing_execute(func, db_path, needle):
    """Run ``func(db_path)`` with the first matching execute failing once.

    Returns (raised_error, proxy_still_armed). The proxy is installed by
    wrapping sqlite3.connect exactly like the frozen C6 check does.
    """
    original_connect = sqlite3.connect
    proxy_holder = {}

    def _wrapped_connect(*args, **kwargs):
        real = original_connect(*args, **kwargs)
        proxy = _FailOnceProxy(real, needle)
        proxy_holder["proxy"] = proxy
        return proxy

    sqlite3.connect = _wrapped_connect
    error = None
    try:
        func(db_path)
    except Exception as exc:  # noqa: BLE001
        error = exc
    finally:
        sqlite3.connect = original_connect
    return error, proxy_holder.get("proxy")


# ---------------------------------------------------------------------------
# DB-001 — migrate_add_curator_claim_support
# ---------------------------------------------------------------------------


class TestCuratorClaimSupportRecovery(unittest.TestCase):
    def _build(self, kind):
        db_path = _temp_db("db001_")
        conn = sqlite3.connect(db_path)
        conn.execute(_OLD_CLAIMS_DDL.format(name="wiki_claims_old"))
        _insert_claims(conn, "wiki_claims_old")
        conn.execute(_CLAIMS_FTS_DDL)
        if kind == "backup_plus_empty_destination":
            conn.execute(_NEW_CLAIMS_DDL.format(name="wiki_claims"))
        elif kind == "backup_plus_partial_destination":
            conn.execute(_NEW_CLAIMS_DDL.format(name="wiki_claims"))
            _insert_claims(conn, "wiki_claims", rows=_CLAIM_ROWS[:1])
        conn.commit()
        conn.close()
        return db_path

    def _assert_recovered(self, db_path):
        conn = sqlite3.connect(db_path)
        try:
            self.assertTrue(_table_exists(conn, "wiki_claims"))
            self.assertFalse(_table_exists(conn, "wiki_claims_old"))
            self.assertEqual(_count(conn, "wiki_claims"), 3)
            cols = {
                r[1] for r in conn.execute("PRAGMA table_info(wiki_claims)").fetchall()
            }
            self.assertIn("created_by_kind", cols)
        finally:
            conn.close()

    def test_backup_only_state_is_restored(self):
        """C4(i) analog: wiki_claims_old present, canonical absent."""
        db_path = self._build("backup_only")
        migrate_add_curator_claim_support(db_path)
        self._assert_recovered(db_path)

    def test_backup_plus_empty_destination_restores_authoritative_backup(self):
        """C4(ii) analog: empty new-shape destination is a failed copy."""
        db_path = self._build("backup_plus_empty_destination")
        migrate_add_curator_claim_support(db_path)
        self._assert_recovered(db_path)

    def test_backup_plus_partial_destination_restores_authoritative_backup(self):
        """Partial (row-incomplete) new-shape destination is a failed copy."""
        db_path = self._build("backup_plus_partial_destination")
        migrate_add_curator_claim_support(db_path)
        self._assert_recovered(db_path)

    def test_injected_copy_failure_is_atomic_and_retry_completes(self):
        """A mid-copy failure rolls the WHOLE swap back to the pre-swap state
        (old shape, all rows, no backup table); the retry then completes."""
        db_path = self._temp = _temp_db("db001_atomic_")
        conn = sqlite3.connect(db_path)
        conn.execute(_OLD_CLAIMS_DDL.format(name="wiki_claims"))
        _insert_claims(conn, "wiki_claims")
        conn.execute(_CLAIMS_FTS_DDL)
        conn.commit()
        conn.close()

        error, proxy = _run_with_failing_execute(
            migrate_add_curator_claim_support,
            db_path,
            needle="INSERT INTO wiki_claims (",
        )
        self.assertIsNotNone(error, "injected copy failure must surface")
        self.assertIsNotNone(proxy)
        self.assertFalse(proxy.armed, "the failing execute must have fired")

        # Atomic rollback: pre-swap state fully intact.
        conn = sqlite3.connect(db_path)
        try:
            self.assertEqual(_count(conn, "wiki_claims"), 3)
            self.assertFalse(_table_exists(conn, "wiki_claims_old"))
            cols = {
                r[1] for r in conn.execute("PRAGMA table_info(wiki_claims)").fetchall()
            }
            self.assertNotIn("created_by_kind", cols)
        finally:
            conn.close()

        # Retry completes the migration with all rows preserved.
        migrate_add_curator_claim_support(db_path)
        conn = sqlite3.connect(db_path)
        try:
            self.assertEqual(_count(conn, "wiki_claims"), 3)
            self.assertFalse(_table_exists(conn, "wiki_claims_old"))
            ids = {
                r[0]
                for r in conn.execute("SELECT id FROM wiki_claims").fetchall()
            }
            self.assertEqual(ids, {1, 2, 3})
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Lint analog — migrate_add_wiki_lint_findings_json_check
# ---------------------------------------------------------------------------


class TestLintFindingsJsonCheckRecovery(unittest.TestCase):
    def _build(self, kind):
        db_path = _temp_db("lint_")
        conn = sqlite3.connect(db_path)
        conn.execute(_OLD_LINT_DDL.format(name="_wiki_lint_findings_old"))
        _insert_lint(conn, "_wiki_lint_findings_old")
        if kind == "backup_plus_empty_destination":
            conn.execute(_NEW_LINT_DDL.format(name="wiki_lint_findings"))
        elif kind == "backup_plus_partial_destination":
            conn.execute(_NEW_LINT_DDL.format(name="wiki_lint_findings"))
            _insert_lint(conn, "wiki_lint_findings", rows=_LINT_ROWS[:1])
        conn.commit()
        conn.close()
        return db_path

    def _assert_recovered(self, db_path):
        conn = sqlite3.connect(db_path)
        try:
            self.assertTrue(_table_exists(conn, "wiki_lint_findings"))
            self.assertFalse(_table_exists(conn, "_wiki_lint_findings_old"))
            self.assertEqual(_count(conn, "wiki_lint_findings"), 3)
        finally:
            conn.close()

    def test_backup_only_state_is_restored(self):
        db_path = self._build("backup_only")
        migrate_add_wiki_lint_findings_json_check(db_path)
        self._assert_recovered(db_path)

    def test_backup_plus_empty_destination_restores_authoritative_backup(self):
        db_path = self._build("backup_plus_empty_destination")
        migrate_add_wiki_lint_findings_json_check(db_path)
        self._assert_recovered(db_path)

    def test_backup_plus_partial_destination_restores_authoritative_backup(self):
        db_path = self._build("backup_plus_partial_destination")
        migrate_add_wiki_lint_findings_json_check(db_path)
        self._assert_recovered(db_path)

    def test_injected_copy_failure_is_atomic_and_retry_completes(self):
        db_path = _temp_db("lint_atomic_")
        conn = sqlite3.connect(db_path)
        conn.execute(_OLD_LINT_DDL.format(name="wiki_lint_findings"))
        _insert_lint(conn, "wiki_lint_findings")
        conn.commit()
        conn.close()

        error, proxy = _run_with_failing_execute(
            migrate_add_wiki_lint_findings_json_check,
            db_path,
            needle="INSERT INTO wiki_lint_findings",
        )
        self.assertIsNotNone(error, "injected copy failure must surface")
        self.assertFalse(proxy.armed, "the failing execute must have fired")

        conn = sqlite3.connect(db_path)
        try:
            self.assertEqual(_count(conn, "wiki_lint_findings"), 3)
            self.assertFalse(_table_exists(conn, "_wiki_lint_findings_old"))
        finally:
            conn.close()

        migrate_add_wiki_lint_findings_json_check(db_path)
        conn = sqlite3.connect(db_path)
        try:
            self.assertEqual(_count(conn, "wiki_lint_findings"), 3)
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='wiki_lint_findings'"
            ).fetchone()[0]
            self.assertIn("json_type(related_page_ids_json)", sql)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# DB-002 — migrate_widen_wiki_claim_sources_source_kind
# ---------------------------------------------------------------------------


class TestClaimSourcesWidenRecovery(unittest.TestCase):
    def _row(self, conn, table, id_, quote):
        conn.execute(
            f"INSERT INTO {table} (id, claim_id, source_kind, quote)"  # noqa: S608
            " VALUES (?, 5, 'document', ?)",
            (id_, quote),
        )

    def _make_db(self):
        """A faithful mini-schema: the FK parent must exist so the migration's
        post-swap foreign_key_check can pass on a healthy fixture."""
        db_path = _temp_db("db002_")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE wiki_claims (id INTEGER PRIMARY KEY, claim_text TEXT)"
        )
        conn.execute("INSERT INTO wiki_claims (id, claim_text) VALUES (5, 'parent')")
        return db_path, conn

    def test_renamed_only_state_is_restored(self):
        """C10 analog: crash after RENAME left only the _old table."""
        db_path, conn = self._make_db()
        conn.execute(_OLD_SOURCES_DDL.format(name="wiki_claim_sources_old"))
        self._row(conn, "wiki_claim_sources_old", 1, "the evidence quote")
        conn.commit()
        conn.close()

        migrate_widen_wiki_claim_sources_source_kind(db_path)

        conn = sqlite3.connect(db_path)
        try:
            self.assertTrue(_table_exists(conn, "wiki_claim_sources"))
            self.assertFalse(_table_exists(conn, "wiki_claim_sources_old"))
            self.assertEqual(
                conn.execute(
                    "SELECT quote FROM wiki_claim_sources"
                ).fetchone()[0],
                "the evidence quote",
            )
            self.assertEqual(
                conn.execute(
                    "PRAGMA foreign_key_check(wiki_claim_sources)"
                ).fetchall(),
                [],
            )
        finally:
            conn.close()

    def test_both_present_incomplete_destination_restores_backup(self):
        db_path, conn = self._make_db()
        # Old-shaped backup holding both rows.
        conn.execute(_OLD_SOURCES_DDL.format(name="wiki_claim_sources_old"))
        self._row(conn, "wiki_claim_sources_old", 1, "quote one")
        self._row(conn, "wiki_claim_sources_old", 2, "quote two")
        # Partial new-shape destination holding only one of the two rows.
        conn.execute(_OLD_SOURCES_DDL.format(name="wiki_claim_sources"))
        self._row(conn, "wiki_claim_sources", 1, "quote one")
        conn.commit()
        conn.close()

        migrate_widen_wiki_claim_sources_source_kind(db_path)

        conn = sqlite3.connect(db_path)
        try:
            self.assertTrue(_table_exists(conn, "wiki_claim_sources"))
            self.assertFalse(_table_exists(conn, "wiki_claim_sources_old"))
            self.assertEqual(_count(conn, "wiki_claim_sources"), 2)
        finally:
            conn.close()

    def test_both_present_complete_destination_drops_backup(self):
        db_path, conn = self._make_db()
        conn.execute(_OLD_SOURCES_DDL.format(name="wiki_claim_sources_old"))
        self._row(conn, "wiki_claim_sources_old", 1, "quote one")
        # New-shape (widened CHECK) destination holding every backup row PLUS
        # one row written after the swap (the crash window the guard protects:
        # "swap completed, only the final DROP of the backup didn't run").
        conn.execute(
            _OLD_SOURCES_DDL.format(name="wiki_claim_sources").replace(
                "'document','memory','chat_message','manual'",
                "'document','memory','chat_message','manual','wiki'",
            )
        )
        self._row(conn, "wiki_claim_sources", 1, "quote one")
        self._row(conn, "wiki_claim_sources", 2, "quote two")
        conn.commit()
        conn.close()

        migrate_widen_wiki_claim_sources_source_kind(db_path)

        conn = sqlite3.connect(db_path)
        try:
            self.assertTrue(_table_exists(conn, "wiki_claim_sources"))
            self.assertFalse(_table_exists(conn, "wiki_claim_sources_old"))
            # The complete NEW destination is trusted: the post-swap row (id 2)
            # survives — a wrong disposition (restore-from-backup) would drop it.
            self.assertEqual(_count(conn, "wiki_claim_sources"), 2)
            ids = {
                row[0]
                for row in conn.execute("SELECT id FROM wiki_claim_sources").fetchall()
            }
            self.assertEqual(ids, {1, 2})
            # And the surviving table is the new-shaped destination (widened
            # CHECK includes 'wiki'), pinning which branch actually ran.
            dest_sql = conn.execute(
                "SELECT sql FROM sqlite_master"
                " WHERE type='table' AND name='wiki_claim_sources'"
            ).fetchone()[0]
            self.assertIn("'wiki'", dest_sql)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# DB-003 — migrate_add_wiki_claims_unique_claim_text
# ---------------------------------------------------------------------------


class TestClaimsUniqueDedupRemapsEvidence(unittest.TestCase):
    def test_duplicate_claims_distinct_evidence_survivor_carries_both(self):
        db_path = _temp_db("db003_")
        conn = sqlite3.connect(db_path)
        conn.execute(_NEW_CLAIMS_DDL.format(name="wiki_claims"))
        # Two duplicate (vault_id, claim_text) claims; id 2 is the survivor.
        conn.execute(
            "INSERT INTO wiki_claims (id, vault_id, claim_text, source_type)"
            " VALUES (1, 7, 'dup', 'document')"
        )
        conn.execute(
            "INSERT INTO wiki_claims (id, vault_id, claim_text, source_type)"
            " VALUES (2, 7, 'dup', 'document')"
        )
        conn.execute(
            "INSERT INTO wiki_claims (id, vault_id, claim_text, source_type)"
            " VALUES (3, 8, 'unique', 'document')"
        )
        conn.execute(_OLD_SOURCES_DDL.format(name="wiki_claim_sources"))
        conn.execute(
            "INSERT INTO wiki_claim_sources (id, claim_id, source_kind, quote)"
            " VALUES (10, 1, 'document', 'evidence on doomed twin')"
        )
        conn.execute(
            "INSERT INTO wiki_claim_sources (id, claim_id, source_kind, quote)"
            " VALUES (11, 2, 'document', 'evidence on survivor')"
        )
        conn.commit()
        conn.close()

        migrate_add_wiki_claims_unique_claim_text(db_path)

        conn = sqlite3.connect(db_path)
        try:
            claims = conn.execute("SELECT id FROM wiki_claims").fetchall()
            self.assertEqual(sorted(r[0] for r in claims), [2, 3])
            # BOTH evidence rows now point at the surviving claim id 2.
            sources = conn.execute(
                "SELECT id, claim_id, quote FROM wiki_claim_sources ORDER BY id"
            ).fetchall()
            self.assertEqual(sources, [(10, 2, "evidence on doomed twin"), (11, 2, "evidence on survivor")])
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            idx = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
                " AND name='idx_wiki_claims_unique_vault_claim'"
            ).fetchone()
            self.assertIsNotNone(idx)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# SEARCH-005 — migrate_add_files_content_fts
# ---------------------------------------------------------------------------


class TestFilesContentFtsAtomicRetry(unittest.TestCase):
    def test_failed_rebuild_rolls_back_and_retry_finds_match(self):
        db_path = _temp_db("search005_")
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE files (
                id INTEGER PRIMARY KEY,
                file_name TEXT,
                file_type TEXT,
                status TEXT,
                source TEXT,
                parsed_text TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO files (id, file_name, file_type, status, source, parsed_text)"
            " VALUES (1, 'doc.txt', 'txt', 'indexed', 'upload', ?)",
            (_DOC_TEXT,),
        )
        conn.commit()
        conn.close()

        # Attempt 1: transient failure during the rebuild INSERT.
        error, proxy = _run_with_failing_execute(
            migrate_add_files_content_fts,
            db_path,
            needle="VALUES('rebuild')",
        )
        self.assertIsNotNone(error, "injected rebuild failure must surface")
        self.assertFalse(proxy.armed, "the failing execute must have fired")

        conn = sqlite3.connect(db_path)
        try:
            # Atomic rollback: nothing half-migrated is left behind.
            self.assertFalse(_table_exists(conn, "files_content_fts"))
        finally:
            conn.close()

        # Attempt 2: retry re-runs creation + backfill.
        migrate_add_files_content_fts(db_path)

        conn = sqlite3.connect(db_path)
        try:
            self.assertTrue(_table_exists(conn, "files_content_fts"))
            matches = conn.execute(
                "SELECT COUNT(*) FROM files_content_fts WHERE files_content_fts"
                " MATCH ?",
                (_UNIQUE_TOKEN,),
            ).fetchone()[0]
            self.assertGreaterEqual(matches, 1)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
