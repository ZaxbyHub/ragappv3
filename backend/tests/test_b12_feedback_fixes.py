"""Issue #701 PR #856 feedback round — regression pins for the review findings
(swarm-pr-review run pr856-20261005, comment 5999510179).

- PRR-001: the wiki_claims normalized_text backfill brackets its bulk UPDATE
  with the wiki_claims_fts_update trigger (files-sibling symmetry) and
  rebuilds the index, so an index-diverged database converges instead of
  raising FTS5's "database disk image is malformed" every boot.
- PRR-005/006: a corrupt sqlite_sequence seq (non-integer or out-of-range)
  is skipped by the high-water helper instead of aborting the migration.
- PRR-007: BLOB claim_text rows decode instead of wedging the backfill.
- PRR-008: a direct migrate_add_file_metadata_columns call on a database
  without files_search_fts leaves NO trigger behind (pre-PR behavior).
- PRR-010/012: behavioral high-water pins for three draft_claims recovery
  branches — rename-only restore and authoritative restore against
  pre-NOT NULL legacy shapes, and the relaxed stale-drop (which by branch
  necessity operates on an already-relaxed canonical; its backup still
  carries the authoritative counter). The remaining disposition
  (relaxed restore) is dispositioned by the #699 recovery suite without a
  high-water assertion.
"""
import sqlite3

from app.models.database import (
    migrate_add_file_metadata_columns,
    migrate_add_wiki_claims_normalized_text,
    migrate_relax_draft_claims_span_not_null,
    run_migrations,
)

_RELAXED_DRAFT_CLAIMS_DDL = """
CREATE TABLE draft_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id INTEGER NOT NULL REFERENCES draft_revisions(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    claim_text TEXT NOT NULL,
    claim_sha256 TEXT,
    span_start INTEGER,
    span_end INTEGER,
    claim_type TEXT NOT NULL,
    status TEXT NOT NULL,
    severity TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    retrieval_audit_json TEXT NOT NULL DEFAULT '{}',
    resolution TEXT NOT NULL DEFAULT 'open',
    resolved_by INTEGER,
    resolved_at TIMESTAMP,
    resolution_note TEXT,
    UNIQUE(revision_id, ordinal)
)
"""

_LEGACY_DRAFT_CLAIMS_DDL = """
CREATE TABLE {name} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id INTEGER NOT NULL REFERENCES draft_revisions(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    claim_text TEXT NOT NULL,
    claim_sha256 TEXT NOT NULL,
    span_start INTEGER NOT NULL,
    span_end INTEGER NOT NULL,
    claim_type TEXT NOT NULL,
    status TEXT NOT NULL,
    severity TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    retrieval_audit_json TEXT NOT NULL DEFAULT '{{}}',
    resolution TEXT NOT NULL DEFAULT 'open',
    resolved_by INTEGER,
    resolved_at TIMESTAMP,
    resolution_note TEXT,
    UNIQUE(revision_id, ordinal)
)
"""


def _seed_revision(db):
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE draft_revisions (id INTEGER PRIMARY KEY,"
            " draft_id INTEGER, revision_no INTEGER, source TEXT,"
            " content_md TEXT, content_sha256 TEXT)"
        )
        # The migration's post-swap foreign_key_check(draft_claims) requires
        # the referenced revision row to exist.
        conn.execute(
            "INSERT INTO draft_revisions (id, draft_id, revision_no, source,"
            " content_md, content_sha256) VALUES (1, 1, 1, 'manual', 'x', 'h')"
        )
        conn.commit()
    finally:
        conn.close()


def _insert_claim(conn, ordinal=1, text="claim"):
    cursor = conn.execute(
        "INSERT INTO draft_claims (revision_id, ordinal, claim_text,"
        " claim_type, status, severity)"
        " VALUES (1, ?, ?, 'factual', 'supported', 'info')",
        (ordinal, text),
    )
    return cursor.lastrowid


# ---------------------------------------------------------------------------
# PRR-001 — index-diverged wiki_claims_fts no longer wedges the backfill
# ---------------------------------------------------------------------------


def test_normalized_text_backfill_heals_diverged_fts_index(tmp_path):
    db = str(tmp_path / "fb_fts_diverged.db")
    run_migrations(db)

    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO wiki_claims (vault_id, claim_text, source_type)"
            " VALUES (7, 'Drifted claim', 'document')"
        )
        # Manufacture the drift shape: the FTS index loses the row while the
        # content row keeps normalized_text NULL. Pre-fix, the backfill's
        # UPDATE fired the external-content 'delete' command for the missing
        # rowid and raised "database disk image is malformed".
        conn.execute("DELETE FROM wiki_claims_fts")
        conn.commit()
    finally:
        conn.close()

    migrate_add_wiki_claims_normalized_text(db)

    conn = sqlite3.connect(db)
    try:
        value = conn.execute(
            "SELECT normalized_text FROM wiki_claims WHERE claim_text ="
            " 'Drifted claim'"
        ).fetchone()[0]
        assert value == "drifted claim"
        # The sync trigger is back in place.
        trigger = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name"
            " = 'wiki_claims_fts_update'"
        ).fetchone()
        assert trigger is not None
        # The index was rebuilt: the drifted row is queryable again.
        hits = conn.execute(
            "SELECT rowid FROM wiki_claims_fts WHERE wiki_claims_fts MATCH"
            " 'drifted'"
        ).fetchall()
        assert hits, "rebuilt index must contain the healed row"
        # And a normal UPDATE works again (Control B's runtime failure mode).
        conn.execute(
            "UPDATE wiki_claims SET claim_text = 'Drifted claim 2'"
            " WHERE claim_text = 'Drifted claim'"
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# PRR-008 — direct call without files_search_fts leaves no dangling trigger
# ---------------------------------------------------------------------------


def test_files_direct_call_without_fts_creates_no_trigger(tmp_path):
    db = str(tmp_path / "fb_files_no_fts.db")
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            """
            CREATE TABLE files (
                id INTEGER PRIMARY KEY,
                file_path TEXT,
                file_name TEXT,
                file_hash TEXT,
                status TEXT,
                chunk_count INTEGER,
                error_message TEXT,
                created_at TIMESTAMP,
                processed_at TIMESTAMP
            )
            """
        )
        conn.execute(
            "INSERT INTO files (id, file_path, file_name, file_hash, status,"
            " chunk_count, created_at) VALUES (1, 'p', 'n', 'h', 'indexed',"
            " 0, '2024-01-01 00:00:00')"
        )
        conn.commit()
    finally:
        conn.close()

    migrate_add_file_metadata_columns(db)

    conn = sqlite3.connect(db)
    try:
        value = conn.execute(
            "SELECT modified_at FROM files WHERE id = 1"
        ).fetchone()[0]
        assert value == "2024-01-01 00:00:00"
        dangling = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name"
            " = 'files_search_fts_update'"
        ).fetchone()
        assert dangling is None, (
            "no FTS table means no trigger may be recreated (a dangling "
            "trigger would fail every later UPDATE files)"
        )
        # A later UPDATE works — pre-fix this raised 'no such table'.
        conn.execute("UPDATE files SET status = 'error' WHERE id = 1")
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# PRR-005/006 — corrupt sqlite_sequence values are skipped, not fatal
# ---------------------------------------------------------------------------


def test_corrupt_text_seq_is_skipped_by_high_water_helper(tmp_path):
    db = str(tmp_path / "fb_seq_text.db")
    _seed_revision(db)
    conn = sqlite3.connect(db)
    try:
        conn.execute(_RELAXED_DRAFT_CLAIMS_DDL)
        for ordinal in range(1, 4):
            _insert_claim_lean(conn, ordinal)
        conn.commit()
    finally:
        conn.close()

    migrate_relax_draft_claims_span_not_null(db)  # no-op: already relaxed shape

    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE draft_claims_old (id INTEGER PRIMARY KEY"
            " AUTOINCREMENT, revision_id INTEGER, ordinal INTEGER,"
            " claim_text TEXT, claim_type TEXT, status TEXT, severity TEXT)"
        )
        conn.execute(
            "INSERT INTO sqlite_sequence (name, seq) VALUES"
            " ('draft_claims_old', 'not-a-number')"
        )
        conn.commit()
    finally:
        conn.close()

    # The stale-backup disposition drops the backup; the corrupt seq must be
    # skipped (pre-fix this raised ValueError out of the migration).
    migrate_relax_draft_claims_span_not_null(db)

    conn = sqlite3.connect(db)
    try:
        leftover = conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'draft_claims_old'"
        ).fetchone()
        assert leftover is None
        cursor = conn.execute(
            "INSERT INTO draft_claims (revision_id, ordinal, claim_text,"
            " claim_type, status, severity) VALUES (1, 99, 'after',"
            " 'factual', 'supported', 'info')"
        )
        conn.commit()
        assert cursor.lastrowid == 4
    finally:
        conn.close()


def _insert_claim_lean(conn, ordinal):
    conn.execute(
        "INSERT INTO draft_claims (revision_id, ordinal, claim_text,"
        " claim_type, status, severity) VALUES (1, ?, ?, 'factual',"
        " 'supported', 'info')",
        (ordinal, f"claim {ordinal}"),
    )


def test_huge_seq_is_not_copied_onto_canonical(tmp_path):
    db = str(tmp_path / "fb_seq_huge.db")
    _seed_revision(db)
    conn = sqlite3.connect(db)
    try:
        conn.execute(_RELAXED_DRAFT_CLAIMS_DDL)
        for ordinal in range(1, 4):
            _insert_claim_lean(conn, ordinal)
        conn.execute(
            "CREATE TABLE draft_claims_old (id INTEGER PRIMARY KEY"
            " AUTOINCREMENT, revision_id INTEGER, ordinal INTEGER,"
            " claim_text TEXT, claim_type TEXT, status TEXT, severity TEXT)"
        )
        conn.execute(
            "INSERT INTO draft_claims_old (id, revision_id, ordinal,"
            " claim_text, claim_type, status, severity)"
            " VALUES (1, 1, 1, 'stale', 'factual', 'supported', 'info')"
        )
        # A corrupt counter near the int64 ceiling must not be copied onto
        # the canonical table (pre-fix the raise was unconditional).
        conn.execute(
            "INSERT INTO sqlite_sequence (name, seq) VALUES"
            " ('draft_claims_old', 9223372036854775807)"
        )
        conn.commit()
    finally:
        conn.close()

    migrate_relax_draft_claims_span_not_null(db)

    conn = sqlite3.connect(db)
    try:
        seq = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'draft_claims'"
        ).fetchone()
        assert seq is not None and seq[0] <= 2 ** 62, (
            "the canonical counter must never inherit an id-exhausting value"
        )
        cursor = conn.execute(
            "INSERT INTO draft_claims (revision_id, ordinal, claim_text,"
            " claim_type, status, severity) VALUES (1, 98, 'after',"
            " 'factual', 'supported', 'info')"
        )
        conn.commit()
        assert cursor.lastrowid == 4
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# PRR-007 — BLOB claim_text decodes instead of wedging every boot
# ---------------------------------------------------------------------------


def test_blob_claim_text_backfills_via_decode(tmp_path):
    db = str(tmp_path / "fb_blob_claim.db")
    run_migrations(db)

    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO wiki_claims (vault_id, claim_text, source_type)"
            " VALUES (7, ?, 'document')",
            (b"Blobbed claim",),
        )
        conn.commit()
    finally:
        conn.close()

    # Pre-fix: TypeError from re.sub, repeating on every boot.
    migrate_add_wiki_claims_normalized_text(db)

    conn = sqlite3.connect(db)
    try:
        value = conn.execute(
            "SELECT normalized_text FROM wiki_claims WHERE claim_text = ?",
            (b"Blobbed claim",),
        ).fetchone()[0]
        assert value == "blobbed claim"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# PRR-010/012 — draft_claims recovery branches, pre-NOT NULL shapes,
# with high-water assertions
# ---------------------------------------------------------------------------


def _seed_legacy_pair(db, canonical_ids, backup_ids, backup_seq_from=None):
    """Legacy NOT NULL draft_claims (canonical) + legacy draft_claims_old
    (backup), both in the full 16-column pre-NOT NULL shape."""
    _seed_revision(db)
    conn = sqlite3.connect(db)
    try:
        conn.execute(_LEGACY_DRAFT_CLAIMS_DDL.format(name="draft_claims"))
        conn.execute(_LEGACY_DRAFT_CLAIMS_DDL.format(name="draft_claims_old"))
        for name, ids in (("draft_claims", canonical_ids), ("draft_claims_old", backup_ids)):
            for ordinal in ids:
                conn.execute(
                    f"INSERT INTO {name} (revision_id, ordinal, claim_text,"
                    " claim_sha256, span_start, span_end, claim_type, status,"
                    " severity) VALUES (1, ?, ?, ?, 0, 1, 'factual',"
                    " 'supported', 'info')",
                    (ordinal, f"claim {ordinal}", f"sha-{ordinal}"),
                )
        conn.commit()
    finally:
        conn.close()


def _next_id(db):
    conn = sqlite3.connect(db)
    try:
        cursor = conn.execute(
            "INSERT INTO draft_claims (revision_id, ordinal, claim_text,"
            " claim_type, status, severity)"
            " VALUES (1, 500, 'post-migration', 'factual', 'supported',"
            " 'info')"
        )
        new_id = cursor.lastrowid
        conn.commit()
        rows = conn.execute(
            "SELECT COUNT(*) FROM draft_claims"
        ).fetchone()[0]
        return new_id, rows
    finally:
        conn.close()


def test_rename_only_restore_then_swap_preserves_high_water(tmp_path):
    """Crash-after-rename shape: only draft_claims_old (pre-NOT NULL, seq 5)
    exists. The migration restores the canonical name, then swaps — the
    restored high-water must survive the swap."""
    db = str(tmp_path / "fb_rename_restore.db")
    _seed_revision(db)
    conn = sqlite3.connect(db)
    try:
        conn.execute(_LEGACY_DRAFT_CLAIMS_DDL.format(name="draft_claims_old"))
        for ordinal in range(1, 6):
            conn.execute(
                "INSERT INTO draft_claims_old (revision_id, ordinal,"
                " claim_text, claim_sha256, span_start, span_end, claim_type,"
                " status, severity) VALUES (1, ?, ?, ?, 0, 1, 'factual',"
                " 'supported', 'info')",
                (ordinal, f"claim {ordinal}", f"sha-{ordinal}"),
            )
        conn.execute("DELETE FROM draft_claims_old WHERE id IN (4, 5)")
        conn.commit()
        seq = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'draft_claims_old'"
        ).fetchone()
        assert seq[0] == 5
    finally:
        conn.close()

    migrate_relax_draft_claims_span_not_null(db)

    new_id, rows = _next_id(db)
    assert rows == 4, "3 restored claims + this insert"
    assert new_id == 6, "restore-then-swap must carry the backup's seq 5"


def test_authoritative_restore_then_swap_preserves_high_water(tmp_path):
    """Failed partial copy: canonical holds a strict subset of the backup.
    The backup is authoritative — restored, then swapped; high-water 5
    (from its deleted ids 4-5) must issue id 6, not the copied max 3."""
    db = str(tmp_path / "fb_authoritative_restore.db")
    _seed_legacy_pair(db, canonical_ids=[1, 2], backup_ids=[1, 2, 3])
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO draft_claims_old (revision_id, ordinal, claim_text,"
            " claim_sha256, span_start, span_end, claim_type, status,"
            " severity) VALUES (1, 4, 'four', 'sha-4', 0, 1, 'factual',"
            " 'supported', 'info')"
        )
        conn.execute(
            "INSERT INTO draft_claims_old (revision_id, ordinal, claim_text,"
            " claim_sha256, span_start, span_end, claim_type, status,"
            " severity) VALUES (1, 5, 'five', 'sha-5', 0, 1, 'factual',"
            " 'supported', 'info')"
        )
        conn.execute("DELETE FROM draft_claims_old WHERE id IN (4, 5)")
        conn.commit()
        seq = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'draft_claims_old'"
        ).fetchone()
        assert seq[0] == 5
    finally:
        conn.close()

    migrate_relax_draft_claims_span_not_null(db)

    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM draft_claims").fetchone()[0] == 3
        leftover = conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'draft_claims_old'"
        ).fetchone()
        assert leftover is None
    finally:
        conn.close()
    new_id, _ = _next_id(db)
    assert new_id == 6


def test_relaxed_canonical_stale_backup_drop_repairs_sequence(tmp_path):
    """Stale backup beside a complete relaxed canonical: the stale drop now
    repairs an already-reset canonical sequence from the backup's record."""
    db = str(tmp_path / "fb_stale_drop.db")
    _seed_revision(db)
    conn = sqlite3.connect(db)
    try:
        conn.execute(_RELAXED_DRAFT_CLAIMS_DDL)
        for ordinal in range(1, 4):
            _insert_claim(conn, ordinal)
        conn.execute(
            """
            CREATE TABLE draft_claims_old (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                revision_id INTEGER NOT NULL,
                ordinal INTEGER NOT NULL,
                claim_text TEXT NOT NULL,
                claim_sha256 TEXT,
                span_start INTEGER,
                span_end INTEGER,
                claim_type TEXT NOT NULL,
                status TEXT NOT NULL,
                severity TEXT NOT NULL
            )
            """
        )
        for ordinal in range(1, 10):
            conn.execute(
                "INSERT INTO draft_claims_old (revision_id, ordinal,"
                " claim_text, claim_type, status, severity)"
                " VALUES (1, ?, ?, 'factual', 'supported', 'info')",
                (ordinal, f"stale {ordinal}"),
            )
        # The stale backup once held 9 rows: shrink it to the canonical's
        # surviving ids so the parity guard passes while its sqlite_sequence
        # legitimately records the true high-water (9), which the
        # canonical's reset counter (3) must regain before the drop.
        conn.execute("DELETE FROM draft_claims_old WHERE id > 3")
        conn.commit()
    finally:
        conn.close()

    migrate_relax_draft_claims_span_not_null(db)

    conn = sqlite3.connect(db)
    try:
        leftover = conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'draft_claims_old'"
        ).fetchone()
        assert leftover is None
    finally:
        conn.close()
    new_id, _ = _next_id(db)
    assert new_id == 10, (
        "the stale backup's high-water 9 must repair the canonical's "
        "already-reset counter before the drop"
    )
