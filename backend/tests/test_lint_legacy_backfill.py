"""Legacy lint-finding backfill checks (issue #1118 AC4 + AC5).

Harness: a temp DB created through ``run_migrations`` (full schema plus
the ``migration_journal`` table), rows seeded with plain SQL so the
legacy cohort's ``status`` / ``origin`` / timestamps are exactly what a
pre-#932 database would carry, and the migration driven by direct call
(the same boundary a startup hook uses).

Frozen spec the implementer must satisfy (NEW-SURFACE, issue #1118):

- ``backend/app/models/database.py`` gains a module-level cutoff
  constant ``_LEGACY_LINT_CUTOFF = "2026-10-16T00:00:00Z"`` -- the date
  the #932 reconcile semantics shipped -- and a migration function
  ``migrate_wiki_lint_findings_legacy_backfill(sqlite_path: str) -> None``
  in the same module. Both are imported by name by this file; the
  imports are part of the frozen spec.
- Legacy cohort discriminator: a ``wiki_lint_findings`` row whose
  ``created_at`` AND ``updated_at`` both precede the cutoff (the
  repo's prior migration docstring -- ``migrate_wiki_lint_findings_
  origin_auto_resolved``, database.py -- documents that pre-#791
  resolved rows are indistinguishable from user resolves, so the
  re-evaluation must be scoped by timestamps relative to the cutoff).
- AC4 semantics: legacy cohort rows with ``status='resolved'`` flip to
  the system-only ``'auto_resolved'`` (rule-3 reopenable), so a later
  detection of the same fingerprint reopens the row via rule 3 instead
  of being suppressed by rule 2. Post-cutoff ``'resolved'`` rows are
  untouched (still user-terminal, still suppressing). Legacy
  ``'dismissed'``/``'acknowledged'`` rows are user-only vocabulary and
  must NOT flip. ``WikiStore._USER_TERMINAL_LINT_STATUSES`` itself is
  unchanged (out of scope per the issue).
- AC5 semantics: legacy cohort rows backfilled as ``origin='linter'``
  whose shape is curator-exclusive (the curator emitters in
  wiki_curator.py are the only producers of titles prefixed
  "Curator..." with details carrying ``"subtype": "curator"`` -- by
  finding_type alone NOTHING is curator-exclusive, the linter also
  emits unsupported_claim and contradiction) re-label to the EXISTING
  CHECK vocabulary value ``origin='curator'``. Linter-shaped legacy
  rows stay ``origin='linter'`` (the data-indistinguishable boundary
  disclosed by the #791 backfill).
- The migration is idempotent (a second run changes no
  ``wiki_lint_findings`` row) and journal-recorded (a
  ``migration_journal`` row with migration_name
  ``migrate_wiki_lint_findings_legacy_backfill`` and phase
  ``succeeded`` -- the same ``record_migration_outcome`` mechanism
  ``migrate_wiki_lint_findings_origin_auto_resolved`` uses).

Checks in this file:

- C4 (AC4 of #1118): legacy resolved -> auto_resolved (rule-3
  reopenable), post-cutoff resolved untouched and still suppressing,
  legacy dismissed untouched, cutoff constant pinned, user-terminal
  vocabulary pinned. RED at base (ImportError: the migration does not
  exist), GREEN after the fix.
- C5 (AC5 of #1118, ``-k curator_origin`` slice): curator-shaped legacy
  rows re-label to origin='curator', linter-shaped stay 'linter';
  idempotent + journaled; rule-3 same-origin reopen works for a
  curator detection after the re-label. RED at base (ImportError),
  GREEN after the fix.
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

for _mod in ("lancedb", "pyarrow"):
    try:
        __import__(_mod)
    except ImportError:
        sys.modules[_mod] = types.ModuleType(_mod)

from app.models.database import run_migrations  # noqa: E402
from app.services.wiki_store import WikiStore  # noqa: E402

_MIGRATION_NAME = "migrate_wiki_lint_findings_legacy_backfill"
_CUTOFF_VALUE = "2026-10-16T00:00:00Z"


def _make_db():
    td = tempfile.mkdtemp()
    db_path = str(Path(td) / "test.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'T')")
    conn.commit()
    return conn, db_path, td


def _load_cutoff():
    """Import and parse the frozen cutoff constant (per-test import: at
    base the constant does not exist yet and collection must still
    succeed)."""
    from app.models.database import _LEGACY_LINT_CUTOFF

    return datetime.fromisoformat(_LEGACY_LINT_CUTOFF.replace("Z", "+00:00"))


def _before_cutoff(cutoff, days=30):
    return (cutoff - timedelta(days=days)).replace(tzinfo=None).isoformat()


def _after_cutoff(cutoff, days=30):
    return (cutoff + timedelta(days=days)).replace(tzinfo=None).isoformat()


def _insert_finding(conn, *, finding_type, title, status, origin, stamp,
                    details=""):
    """Seed one wiki_lint_findings row with explicit legacy-cohort
    columns (plain SQL: the store's writers would stamp now and force
    status='open')."""
    cur = conn.execute(
        "INSERT INTO wiki_lint_findings "
        "(vault_id, finding_type, severity, title, details, "
        " related_page_ids_json, related_claim_ids_json, status, origin, "
        " created_at, updated_at) "
        "VALUES (1, ?, 'medium', ?, ?, '[]', '[]', ?, ?, ?, ?)",
        (finding_type, title, details, status, origin, stamp, stamp),
    )
    conn.commit()
    return int(cur.lastrowid)


def _spec(finding_type, title):
    """Detection spec whose lint fingerprint matches a row seeded with
    the same (finding_type, title) and no related page ids."""
    return {
        "finding_type": finding_type,
        "severity": "medium",
        "title": title,
        "details": "backfill probe details",
        "related_page_ids": [],
        "related_claim_ids": [],
    }


def _by_title(conn, title):
    rows = conn.execute(
        "SELECT id, status, origin FROM wiki_lint_findings WHERE title = ?",
        (title,),
    ).fetchall()
    return rows[0] if rows else None


def _count_by_title(conn, title):
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM wiki_lint_findings WHERE title = ?",
        (title,),
    ).fetchone()
    return dict(row)["n"]


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


def _snapshot(conn):
    return conn.execute(
        "SELECT id, vault_id, finding_type, severity, title, details, "
        "related_page_ids_json, related_claim_ids_json, status, origin, "
        "created_at, updated_at FROM wiki_lint_findings ORDER BY id"
    ).fetchall()


# Curator-shaped rows mirror the ACTUAL wiki_curator.py emitters: every
# curator lint finding carries a "Curator..."-prefixed title and a
# details JSON with subtype "curator" (verified by source read -- by
# finding_type alone nothing is curator-exclusive; the linter also
# emits unsupported_claim and contradiction).
_CURATOR_DETAILS = json.dumps({"subtype": "curator", "claim_text": "probe"})


class TestLegacyResolvedReevaluation(unittest.TestCase):
    """C4 (AC4 of issue #1118, NEW-SURFACE): the backfill makes legacy
    resolved lint findings re-evaluable (flip to rule-3-reopenable
    auto_resolved) while post-cutoff resolved rows keep suppressing and
    the user-terminal vocabulary is untouched. RED at base (ImportError:
    the migration does not exist), GREEN after the fix."""

    def setUp(self):
        self.conn, self.db_path, self._td = _make_db()
        self.addCleanup(shutil.rmtree, self._td, True)

    def tearDown(self):
        self.conn.close()

    def test_legacy_resolved_row_becomes_rule3_reopenable(self):
        # check: C4 (AC4 of issue #1118, NEW-SURFACE) -- RED at base
        # (ImportError), GREEN after the fix.
        from app.models.database import (
            migrate_wiki_lint_findings_legacy_backfill,
        )

        cutoff = _load_cutoff()
        legacy_title = "Orphan claim: legacy resolved probe"
        modern_title = "Orphan claim: modern resolved probe"
        dismissed_title = "Orphan claim: legacy dismissed probe"
        legacy_id = _insert_finding(
            self.conn, finding_type="orphan", title=legacy_title,
            status="resolved", origin="linter",
            stamp=_before_cutoff(cutoff),
        )
        modern_id = _insert_finding(
            self.conn, finding_type="orphan", title=modern_title,
            status="resolved", origin="linter",
            stamp=_after_cutoff(cutoff),
        )
        dismissed_id = _insert_finding(
            self.conn, finding_type="orphan", title=dismissed_title,
            status="dismissed", origin="linter",
            stamp=_before_cutoff(cutoff),
        )

        migrate_wiki_lint_findings_legacy_backfill(self.db_path)

        # Legacy resolved rows flip to the system-only, rule-3-reopenable
        # status (re-evaluable under the defined semantics).
        self.assertEqual(
            _status(self.conn, legacy_id), "auto_resolved",
            "a pre-cutoff 'resolved' row must become re-evaluable "
            "(flipped to rule-3-reopenable 'auto_resolved')",
        )
        # Post-cutoff resolved rows are untouched: still user-terminal.
        self.assertEqual(
            _status(self.conn, modern_id), "resolved",
            "a post-cutoff 'resolved' row must stay user-terminal",
        )
        # dismissed/acknowledged are user-only vocabulary (the pre-#791
        # reconciler never wrote them) and must NOT flip.
        self.assertEqual(
            _status(self.conn, dismissed_id), "dismissed",
            "legacy 'dismissed' rows are user dispositions and must not "
            "be re-evaluated",
        )

        # Behavioral discriminator -- re-evaluable MEANS rule 3, not a
        # fresh row: a linter detection of the legacy fingerprint
        # reopens the SAME row.
        store = WikiStore(self.conn)
        result = store.upsert_lint_findings(
            1, [_spec("orphan", legacy_title)], origin="linter"
        )
        row = _by_title(self.conn, legacy_title)
        self.assertEqual(
            _count_by_title(self.conn, legacy_title), 1,
            "the recurring legacy fingerprint must reopen its existing "
            "row, not grow a new one",
        )
        self.assertEqual(dict(row)["status"], "open")
        self.assertEqual(dict(row)["id"], legacy_id)
        self.assertEqual(len(result), 1)

        # Behavioral control -- the post-cutoff row still suppresses
        # (rule 2): the same detection shape contributes nothing.
        result2 = store.upsert_lint_findings(
            1, [_spec("orphan", modern_title)], origin="linter"
        )
        self.assertEqual(
            result2, [],
            "a post-cutoff user resolve must keep suppressing its "
            "fingerprint (rule 2 preserved)",
        )
        row2 = _by_title(self.conn, modern_title)
        self.assertEqual(_count_by_title(self.conn, modern_title), 1)
        self.assertEqual(dict(row2)["status"], "resolved")

    def test_legacy_cutoff_constant_value(self):
        # check: C4 (AC4 of issue #1118, NEW-SURFACE) -- pins the frozen
        # cohort constant: the date the #932 reconcile semantics shipped.
        # RED at base (ImportError), GREEN after the fix.
        from app.models.database import _LEGACY_LINT_CUTOFF

        self.assertEqual(_LEGACY_LINT_CUTOFF, _CUTOFF_VALUE)

    def test_user_terminal_status_vocabulary_unchanged(self):
        # check: C4 pin (AC4 of issue #1118) -- the backfill must not
        # touch the user-terminal vocabulary itself (out of scope per
        # the issue). GREEN at base (pin), GREEN after the fix.
        self.assertEqual(
            WikiStore._USER_TERMINAL_LINT_STATUSES,
            ("dismissed", "resolved", "acknowledged"),
        )


class TestCuratorOriginBackfill(unittest.TestCase):
    """C5 (AC5 of issue #1118, NEW-SURFACE, ``-k curator_origin``
    slice): legacy curator-produced rows mislabeled origin='linter' by
    the #791 backfill re-label to origin='curator' (truthful, EXISTING
    CHECK vocabulary value) so same-origin rule-3 reopening works for
    curator detections; linter-shaped legacy rows stay 'linter'; the
    migration is idempotent and journal-recorded. RED at base
    (ImportError: the migration does not exist), GREEN after the fix."""

    def setUp(self):
        self.conn, self.db_path, self._td = _make_db()
        self.addCleanup(shutil.rmtree, self._td, True)

    def tearDown(self):
        self.conn.close()

    def test_curator_origin_relabeled_for_curator_shaped_legacy_rows(self):
        # check: C5 (AC5 of issue #1118, NEW-SURFACE) -- RED at base
        # (ImportError), GREEN after the fix.
        from app.models.database import (
            migrate_wiki_lint_findings_legacy_backfill,
        )

        cutoff = _load_cutoff()
        stamp = _before_cutoff(cutoff)
        # Curator-shaped legacy rows (the shapes wiki_curator.py emits):
        # "Curator..."-prefixed titles + details subtype "curator".
        curator_rows = [
            _insert_finding(
                self.conn, finding_type="contradiction",
                title="Curator-detected contradiction: Alpha vs Beta",
                status="auto_resolved", origin="linter", stamp=stamp,
                details=_CURATOR_DETAILS,
            ),
            _insert_finding(
                self.conn, finding_type="unsupported_claim",
                title="Curator claim missing source_quote: Gamma",
                status="auto_resolved", origin="linter", stamp=stamp,
                details=_CURATOR_DETAILS,
            ),
        ]
        # Linter-shaped legacy rows (the shapes wiki_linter.py emits):
        # plain titles, no curator details subtype.
        linter_rows = [
            _insert_finding(
                self.conn, finding_type="orphan",
                title="Orphan claim: Delta",
                status="auto_resolved", origin="linter", stamp=stamp,
                details="Claim id=7 has no parent page (page was deleted).",
            ),
            _insert_finding(
                self.conn, finding_type="contradiction",
                title="Conflicting claims: subjectA - predicateB",
                status="auto_resolved", origin="linter", stamp=stamp,
                details="Multiple objects for subject='subjectA'.",
            ),
        ]

        migrate_wiki_lint_findings_legacy_backfill(self.db_path)

        for row_id in curator_rows:
            self.assertEqual(
                _origin(self.conn, row_id), "curator",
                "curator-shaped legacy rows must be truthfully "
                "re-labeled to the EXISTING vocabulary value "
                "origin='curator'",
            )
        for row_id in linter_rows:
            self.assertEqual(
                _origin(self.conn, row_id), "linter",
                "linter-shaped legacy rows are data-indistinguishable "
                "from real linter rows and must stay origin='linter' "
                "(disclosed boundary)",
            )

    def test_curator_origin_backfill_idempotent_and_journaled(self):
        # check: C5 (AC5 of issue #1118, NEW-SURFACE) -- RED at base
        # (ImportError), GREEN after the fix.
        from app.models.database import (
            migrate_wiki_lint_findings_legacy_backfill,
        )

        cutoff = _load_cutoff()
        stamp = _before_cutoff(cutoff)
        _insert_finding(
            self.conn, finding_type="orphan",
            title="Orphan claim: idempotency legacy resolved",
            status="resolved", origin="linter", stamp=stamp,
        )
        _insert_finding(
            self.conn, finding_type="orphan",
            title="Orphan claim: idempotency modern resolved",
            status="resolved", origin="linter",
            stamp=_after_cutoff(cutoff),
        )
        _insert_finding(
            self.conn, finding_type="contradiction",
            title="Curator-detected contradiction: Eta vs Theta",
            status="auto_resolved", origin="linter", stamp=stamp,
            details=_CURATOR_DETAILS,
        )
        _insert_finding(
            self.conn, finding_type="orphan",
            title="Orphan claim: idempotency linter row",
            status="auto_resolved", origin="linter", stamp=stamp,
        )

        migrate_wiki_lint_findings_legacy_backfill(self.db_path)
        first = [tuple(r) for r in _snapshot(self.conn)]
        self.assertTrue(first, "fixture must seed rows")

        # Idempotent: the second run changes nothing.
        migrate_wiki_lint_findings_legacy_backfill(self.db_path)
        second = [tuple(r) for r in _snapshot(self.conn)]
        self.assertEqual(
            second, first,
            "the backfill must be idempotent: a second run may not "
            "change any wiki_lint_findings row",
        )

        # Journal-recorded: one succeeded row for the migration name,
        # via the same migration_journal mechanism the prior migrations
        # use (record_migration_outcome).
        journaled = self.conn.execute(
            "SELECT COUNT(*) AS n FROM migration_journal "
            "WHERE migration_name = ? AND phase = 'succeeded'",
            (_MIGRATION_NAME,),
        ).fetchone()
        self.assertGreaterEqual(
            dict(journaled)["n"], 1,
            "the backfill must be journal-recorded under its migration "
            f"name ({_MIGRATION_NAME})",
        )

    def test_curator_origin_rule3_reopen_after_relabel(self):
        # check: C5 preserving arm (AC5 of issue #1118, NEW-SURFACE
        # pin) -- after the re-label, a curator detection of the same
        # fingerprint reopens the re-labeled row via rule 3 instead of
        # growing a new history row per flap. RED at base (ImportError),
        # GREEN after the fix.
        from app.models.database import (
            migrate_wiki_lint_findings_legacy_backfill,
        )

        cutoff = _load_cutoff()
        title = "Curator-detected contradiction: Iota vs Kappa"
        row_id = _insert_finding(
            self.conn, finding_type="contradiction", title=title,
            status="auto_resolved", origin="linter",
            stamp=_before_cutoff(cutoff), details=_CURATOR_DETAILS,
        )

        migrate_wiki_lint_findings_legacy_backfill(self.db_path)
        self.assertEqual(_origin(self.conn, row_id), "curator")

        # A curator recurrence on the re-labeled row: same fingerprint,
        # origin='curator' -- rule 3's same-origin gate must now match.
        store = WikiStore(self.conn)
        result = store.upsert_lint_findings(
            1, [_spec("contradiction", title)], origin="curator"
        )
        row = _by_title(self.conn, title)
        self.assertEqual(
            _count_by_title(self.conn, title), 1,
            "a curator detection on a re-labeled row must reopen it "
            "(rule 3), not grow a new row per recurrence (rule 4)",
        )
        self.assertEqual(dict(row)["status"], "open")
        self.assertEqual(dict(row)["id"], row_id)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, row_id)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
