"""Backfill companion pins (issue #1118; complements the frozen
test_lint_legacy_backfill.py).

The frozen file pins the core semantics (pre-cutoff resolved flips,
curator-shaped re-label, idempotence+journal, rule-3 reopen). These
companions add the legs the plan critic's review required:

- mixed timestamps: old created_at + post-cutoff updated_at — the curator
  re-label still applies (it keys on CREATION), and the resolved flip
  correctly does NOT (both timestamps required);
- the schema's space-format CURRENT_TIMESTAMP stamps parse and compare
  correctly on the cutoff day (julianday, not lexical);
- a real 'acknowledged' legacy row is untouched;
- a combined legacy resolved+curator-shaped row gets BOTH repairs;
- journal silence: a mutating direct call adds exactly one succeeded row;
  a no-op second call and a clean second run_migrations add ZERO rows
  (test_b10 clean-second-boot contract).
"""

import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, __file__.rsplit("backend", 1)[0] + "backend")

from app.models.database import (  # noqa: E402
    _LEGACY_LINT_CUTOFF,
    migrate_wiki_lint_findings_legacy_backfill,
    run_migrations,
)
from app.services.wiki_store import WikiStore  # noqa: E402

_CUTOFF = datetime.fromisoformat(_LEGACY_LINT_CUTOFF.replace("Z", "+00:00"))
_CURATOR_DETAILS = json.dumps({"subtype": "curator", "claim_text": "probe"})


def _make_db():
    td = tempfile.mkdtemp()
    db_path = str(Path(td) / "test.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'T')")
    conn.commit()
    return conn, db_path, td


def _insert(conn, *, finding_type, title, status, origin, created, updated,
            details=""):
    cur = conn.execute(
        "INSERT INTO wiki_lint_findings "
        "(vault_id, finding_type, severity, title, details, "
        " related_page_ids_json, related_claim_ids_json, status, origin, "
        " created_at, updated_at) "
        "VALUES (1, ?, 'medium', ?, ?, '[]', '[]', ?, ?, ?, ?)",
        (finding_type, title, details, status, origin, created, updated),
    )
    conn.commit()
    return cur.lastrowid


def _status(conn, row_id):
    row = conn.execute(
        "SELECT status FROM wiki_lint_findings WHERE id = ?", (row_id,)
    ).fetchone()
    return dict(row)["status"] if row else None


def _origin(conn, row_id):
    row = conn.execute(
        "SELECT origin FROM wiki_lint_findings WHERE id = ?", (row_id,)
    ).fetchone()
    return dict(row)["origin"] if row else None


def _journal_rows(conn):
    return conn.execute(
        "SELECT COUNT(*) n FROM migration_journal "
        "WHERE migration_name = 'migrate_wiki_lint_findings_legacy_backfill'"
    ).fetchone()["n"]


class TestMixedTimestamps(unittest.TestCase):
    def test_old_created_post_cutoff_updated_curator_row_relabeled(self):
        conn, db_path, td = _make_db()
        self.addCleanup(shutil.rmtree, td, True)
        self.addCleanup(conn.close)
        old = (_CUTOFF - timedelta(days=30)).replace(tzinfo=None).isoformat()
        new = (_CUTOFF + timedelta(days=5)).replace(tzinfo=None).isoformat()
        rid = _insert(
            conn, finding_type="unsupported_claim",
            title="Curator claim missing source_quote: probe",
            status="auto_resolved", origin="linter",
            created=old, updated=new, details=_CURATOR_DETAILS,
        )
        migrate_wiki_lint_findings_legacy_backfill(db_path)
        self.assertEqual(
            _origin(conn, rid), "curator",
            "origin repair keys on legacy CREATION: a row the linter "
            "resolve pass touched after the cutoff is still mislabeled "
            "and must still be repaired",
        )

    def test_old_created_post_cutoff_updated_resolved_row_stays(self):
        conn, db_path, td = _make_db()
        self.addCleanup(shutil.rmtree, td, True)
        self.addCleanup(conn.close)
        old = (_CUTOFF - timedelta(days=30)).replace(tzinfo=None).isoformat()
        new = (_CUTOFF + timedelta(days=5)).replace(tzinfo=None).isoformat()
        rid = _insert(
            conn, finding_type="orphan", title="Orphan: mixed stamps",
            status="resolved", origin="linter", created=old, updated=new,
        )
        migrate_wiki_lint_findings_legacy_backfill(db_path)
        self.assertEqual(
            _status(conn, rid), "resolved",
            "the resolved flip is conservative (BOTH timestamps legacy); "
            "a recently-touched row stays user-terminal",
        )


class TestSpaceFormatStamps(unittest.TestCase):
    """The schema default writes CURRENT_TIMESTAMP ('YYYY-MM-DD HH:MM:SS').
    Lexical comparison against the 'T'-format cutoff misclassifies
    same-day rows; julianday must classify them by instant."""

    def test_space_format_same_day_after_cutoff_not_flipped(self):
        conn, db_path, td = _make_db()
        self.addCleanup(shutil.rmtree, td, True)
        self.addCleanup(conn.close)
        # 2026-10-16 12:00:00 is AFTER midnight but lexically sorts BEFORE
        # '2026-10-16T00:00:00Z' (space < 'T').
        rid = _insert(
            conn, finding_type="orphan", title="Orphan: space-format",
            status="resolved", origin="linter",
            created="2026-10-16 12:00:00", updated="2026-10-16 12:00:00",
        )
        migrate_wiki_lint_findings_legacy_backfill(db_path)
        self.assertEqual(
            _status(conn, rid), "resolved",
            "space-format stamp on the ship day is post-cutoff by instant "
            "and must not flip despite sorting lexically before the cutoff",
        )

    def test_space_format_before_cutoff_flips(self):
        conn, db_path, td = _make_db()
        self.addCleanup(shutil.rmtree, td, True)
        self.addCleanup(conn.close)
        rid = _insert(
            conn, finding_type="orphan", title="Orphan: space-format old",
            status="resolved", origin="linter",
            created="2026-09-16 00:00:00", updated="2026-09-16 00:00:00",
        )
        migrate_wiki_lint_findings_legacy_backfill(db_path)
        self.assertEqual(_status(conn, rid), "auto_resolved")


class TestAcknowledgedAndCombined(unittest.TestCase):
    def test_legacy_acknowledged_row_untouched(self):
        conn, db_path, td = _make_db()
        self.addCleanup(shutil.rmtree, td, True)
        self.addCleanup(conn.close)
        old = (_CUTOFF - timedelta(days=40)).replace(tzinfo=None).isoformat()
        rid = _insert(
            conn, finding_type="orphan", title="Orphan: acknowledged",
            status="acknowledged", origin="linter", created=old, updated=old,
        )
        migrate_wiki_lint_findings_legacy_backfill(db_path)
        self.assertEqual(
            _status(conn, rid), "acknowledged",
            "acknowledged is user-only vocabulary and never re-evaluated",
        )

    def test_combined_resolved_curator_row_gets_both_repairs(self):
        conn, db_path, td = _make_db()
        self.addCleanup(shutil.rmtree, td, True)
        self.addCleanup(conn.close)
        old = (_CUTOFF - timedelta(days=40)).replace(tzinfo=None).isoformat()
        title = "Curator claim missing source_quote: combined"
        rid = _insert(
            conn, finding_type="unsupported_claim", title=title,
            status="resolved", origin="linter",
            created=old, updated=old, details=_CURATOR_DETAILS,
        )
        migrate_wiki_lint_findings_legacy_backfill(db_path)
        self.assertEqual(
            _origin(conn, rid), "curator",
            "origin re-label runs FIRST so the status flip's updated_at "
            "cannot evict the row from the origin cohort",
        )
        self.assertEqual(_status(conn, rid), "auto_resolved")
        # And a curator recurrence now reopens the SAME row via rule 3.
        store = WikiStore(conn)
        result = store.upsert_lint_findings(
            1,
            [{
                "finding_type": "unsupported_claim",
                "title": title,
                "severity": "medium",
                "details": "recurrence",
                "related_page_ids": [],
                "related_claim_ids": [],
            }],
            origin="curator",
        )
        self.assertEqual(len(result), 1)
        row = conn.execute(
            "SELECT id, status FROM wiki_lint_findings WHERE id = ?", (rid,)
        ).fetchone()
        self.assertEqual(dict(row)["id"], rid)
        self.assertEqual(dict(row)["status"], "open")


class TestLaggingUpgrade(unittest.TestCase):
    """Final-critic B-2: an install that kept running the pre-#791
    reconciler AFTER 2026-10-16 holds legacy rows dated after the
    constant. The origin migration's journal row records the actual
    upgrade instant, and the effective cutoff is the LATER of the two —
    so those rows are repaired too. Built with the real pre-#791 table
    shape (no origin column, no auto_resolved in the status CHECK)."""

    _OLD_TABLE = (
        "CREATE TABLE wiki_lint_findings (\n"
        "    id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
        "    vault_id INTEGER NOT NULL,\n"
        "    finding_type TEXT NOT NULL CHECK (finding_type IN (\n"
        "        'contradiction','stale','orphan','missing_page',\n"
        "        'unsupported_claim','duplicate_entity','weak_provenance')),\n"
        "    severity TEXT NOT NULL DEFAULT 'medium' CHECK (severity IN (\n"
        "        'low','medium','high','critical')),\n"
        "    title TEXT NOT NULL,\n"
        "    details TEXT DEFAULT '',\n"
        "    related_page_ids_json TEXT NOT NULL DEFAULT '[]'\n"
        "        CHECK (json_type(related_page_ids_json) = 'array'),\n"
        "    related_claim_ids_json TEXT NOT NULL DEFAULT '[]'\n"
        "        CHECK (json_type(related_claim_ids_json) = 'array'),\n"
        "    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN (\n"
        "        'open','acknowledged','resolved','dismissed')),\n"
        "    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n"
        "    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n"
        ");"
    )

    def test_post_constant_pre_upgrade_rows_are_repaired(self):
        from datetime import datetime, timedelta

        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, True)
        db_path = str(Path(td) / "lagging.db")
        conn = sqlite3.connect(db_path)
        conn.execute(self._OLD_TABLE)
        # A row written by the pre-#791 reconciler on an install that had
        # NOT yet upgraded: dated AFTER the constant, BEFORE the upgrade.
        post_constant = (
            datetime.fromisoformat(
                _LEGACY_LINT_CUTOFF.replace("Z", "+00:00")
            ) + timedelta(days=16)
        ).replace(tzinfo=None).isoformat()
        cur = conn.execute(
            "INSERT INTO wiki_lint_findings "
            "(vault_id, finding_type, severity, title, details, "
            " related_page_ids_json, related_claim_ids_json, status, "
            " created_at, updated_at) "
            "VALUES (1, 'orphan', 'medium', 'Orphan: lagging upgrade', '', "
            "        '[]', '[]', 'resolved', ?, ?)",
            (post_constant, post_constant),
        )
        rid = cur.lastrowid
        conn.commit()
        conn.close()
        # The full upgrade chain runs NOW: the origin migration rebuilds
        # the table and journals its own success at the current instant;
        # the backfill then treats everything before THAT instant as
        # legacy — including our post-constant row.
        run_migrations(db_path)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            self.assertEqual(
                dict(
                    conn.execute(
                        "SELECT status, origin FROM wiki_lint_findings "
                        "WHERE id = ?",
                        (rid,),
                    ).fetchone()
                ),
                {"status": "auto_resolved", "origin": "linter"},
                "a legacy row written after the constant but before the "
                "database's actual upgrade must still be repaired",
            )
        finally:
            conn.close()


class TestJournalSilence(unittest.TestCase):
    def test_mutating_call_journals_once_noop_call_journals_zero(self):
        conn, db_path, td = _make_db()
        self.addCleanup(shutil.rmtree, td, True)
        self.addCleanup(conn.close)
        self.assertEqual(
            _journal_rows(conn), 0,
            "the initial run_migrations over an empty table mutates "
            "nothing and must journal nothing",
        )
        old = (_CUTOFF - timedelta(days=40)).replace(tzinfo=None).isoformat()
        _insert(
            conn, finding_type="orphan", title="Orphan: journal probe",
            status="resolved", origin="linter", created=old, updated=old,
        )
        migrate_wiki_lint_findings_legacy_backfill(db_path)
        self.assertEqual(
            _journal_rows(conn), 1,
            "a mutating invocation writes exactly one succeeded row",
        )
        migrate_wiki_lint_findings_legacy_backfill(db_path)
        self.assertEqual(
            _journal_rows(conn), 1,
            "a zero-mutation invocation is journal-silent",
        )
        # Clean second boot: run_migrations again over the converged DB.
        conn.close()
        run_migrations(db_path)
        conn2 = sqlite3.connect(db_path)
        conn2.row_factory = sqlite3.Row
        try:
            self.assertEqual(
                _journal_rows(conn2), 1,
                "a clean second run_migrations adds zero journal rows "
                "(test_b10 clean-second-boot contract)",
            )
        finally:
            conn2.close()


if __name__ == "__main__":
    unittest.main()
