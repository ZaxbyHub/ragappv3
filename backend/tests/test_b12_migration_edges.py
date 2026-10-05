"""Issue #701 (Workstream B PR 12) — dedup/rebuild edge coverage beyond the
frozen checks (plan-critic round-1 requested tests a-f).

Not a frozen-check file: these pins close the residuals the frozen C1-C9 do
not discriminate — NULL-keyed dedup survival in every nullable dimension,
identical-text NULL-object twins, true-duplicate collapse, the
sqlite_sequence INSERT branch (all rows deleted before a rebuild), the
modified_at damaged-population convergence, and a second rebuild site
(wiki_lint_findings) for the high-water fix.
"""
import sqlite3

from app.models.database import (
    migrate_add_file_metadata_columns,
    migrate_add_wiki_lint_findings_json_check,
    migrate_add_wiki_relations_unique,
    migrate_relax_draft_claims_span_not_null,
    run_migrations,
)

_OLD_LINT_DDL = """
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
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

_LEGACY_DRAFT_CLAIMS_DDL = """
CREATE TABLE draft_claims (
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
    retrieval_audit_json TEXT NOT NULL DEFAULT '{}',
    resolution TEXT NOT NULL DEFAULT 'open',
    resolved_by INTEGER,
    resolved_at TIMESTAMP,
    resolution_note TEXT,
    UNIQUE(revision_id, ordinal)
)
"""


def _seed_legacy_draft_claims(db):
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE draft_revisions (id INTEGER PRIMARY KEY, draft_id"
            " INTEGER, revision_no INTEGER, source TEXT, content_md TEXT,"
            " content_sha256 TEXT)"
        )
        conn.execute(_LEGACY_DRAFT_CLAIMS_DDL)
        for ordinal in range(1, 6):
            conn.execute(
                "INSERT INTO draft_claims (revision_id, ordinal, claim_text,"
                " claim_sha256, span_start, span_end, claim_type, status,"
                " severity) VALUES (1, ?, ?, ?, 0, 1, 'factual',"
                " 'supported', 'info')",
                (ordinal, f"claim {ordinal}", f"sha-{ordinal}"),
            )
        conn.commit()
    finally:
        conn.close()


# (a) NULL-subject twins survive — the dedup must exempt every NULL key
# column, not only object_entity_id (subject_entity_id is nullable too).
def test_relations_dedup_keeps_distinct_null_subject_rows(tmp_path):
    db = str(tmp_path / "edge_null_subject.db")
    run_migrations(db)
    conn = sqlite3.connect(db)
    try:
        conn.execute("DROP INDEX IF EXISTS idx_wiki_relations_unique_triple")
        conn.execute(
            "INSERT INTO wiki_entities (vault_id, canonical_name)"
            " VALUES (1, 'Target')"
        )
        for _ in range(2):
            conn.execute(
                "INSERT INTO wiki_relations (vault_id, subject_entity_id,"
                " predicate, object_entity_id, object_text)"
                " VALUES (1, NULL, 'part_of', 1, NULL)"
            )
        conn.commit()
    finally:
        conn.close()

    migrate_add_wiki_relations_unique(db)

    conn = sqlite3.connect(db)
    try:
        count = conn.execute("SELECT COUNT(*) FROM wiki_relations").fetchone()[0]
    finally:
        conn.close()
    assert count == 2


# (b) NULL-object twins with IDENTICAL object_text survive — NULL keys never
# collide under the UNIQUE index, so text equality must not matter either.
def test_relations_dedup_keeps_identical_null_object_twins(tmp_path):
    db = str(tmp_path / "edge_identical_null_object.db")
    run_migrations(db)
    conn = sqlite3.connect(db)
    try:
        conn.execute("DROP INDEX IF EXISTS idx_wiki_relations_unique_triple")
        conn.execute(
            "INSERT INTO wiki_entities (vault_id, canonical_name)"
            " VALUES (1, 'Germany')"
        )
        for _ in range(2):
            conn.execute(
                "INSERT INTO wiki_relations (vault_id, subject_entity_id,"
                " predicate, object_entity_id, object_text)"
                " VALUES (1, 1, 'capital_of', NULL, 'Berlin')"
            )
        conn.commit()
    finally:
        conn.close()

    migrate_add_wiki_relations_unique(db)

    conn = sqlite3.connect(db)
    try:
        count = conn.execute("SELECT COUNT(*) FROM wiki_relations").fetchone()[0]
    finally:
        conn.close()
    assert count == 2


# (c) A true non-NULL triple duplicate still collapses (guards against an
# over-keeping fix). The fresh SCHEMA's table-level UNIQUE would reject the
# duplicate at INSERT time, so the fixture builds a constraint-free legacy
# wiki_relations shape and calls the migration directly — which also proves
# the surviving rows satisfy the unique index the migration creates.
_MIN_RELATIONS_NO_UNIQUE_DDL = """
CREATE TABLE wiki_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_id INTEGER NOT NULL,
    subject_entity_id INTEGER,
    predicate TEXT NOT NULL,
    object_entity_id INTEGER,
    object_text TEXT,
    claim_id INTEGER,
    confidence REAL DEFAULT 0.0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""


def test_relations_dedup_still_collapses_true_duplicates(tmp_path):
    db = str(tmp_path / "edge_true_duplicate.db")
    conn = sqlite3.connect(db)
    try:
        conn.execute(_MIN_RELATIONS_NO_UNIQUE_DDL)
        for _ in range(2):
            conn.execute(
                "INSERT INTO wiki_relations (vault_id, subject_entity_id,"
                " predicate, object_entity_id)"
                " VALUES (1, 1, 'part_of', 2)"
            )
        conn.commit()
    finally:
        conn.close()

    migrate_add_wiki_relations_unique(db)

    conn = sqlite3.connect(db)
    try:
        count = conn.execute("SELECT COUNT(*) FROM wiki_relations").fetchone()[0]
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name ="
            " 'idx_wiki_relations_unique_triple'"
        ).fetchone()
    finally:
        conn.close()
    assert count == 1
    assert idx is not None


# (d) All rows deleted before the rebuild: 0 copied rows, so the canonical
# table has no sqlite_sequence row at all — the preservation helper's INSERT
# branch must seed it with the old high-water.
def test_rebuild_preserves_high_water_with_zero_copied_rows(tmp_path):
    db = str(tmp_path / "edge_zero_rows.db")
    _seed_legacy_draft_claims(db)

    conn = sqlite3.connect(db)
    try:
        conn.execute("DELETE FROM draft_claims")
        conn.commit()
        seq = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'draft_claims'"
        ).fetchone()
        assert seq is not None and seq[0] == 5
    finally:
        conn.close()

    migrate_relax_draft_claims_span_not_null(db)

    conn = sqlite3.connect(db)
    try:
        cursor = conn.execute(
            "INSERT INTO draft_claims (revision_id, ordinal, claim_text,"
            " claim_type, status, severity)"
            " VALUES (1, 1, 'first after rebuild', 'factual', 'supported',"
            " 'info')"
        )
        new_id = cursor.lastrowid
        conn.commit()
    finally:
        conn.close()
    assert new_id == 6


# (e) Damaged population / crash window: the column exists, rows hold NULL —
# a re-run (via run_migrations AND via a direct migration call) must
# converge instead of gating on column absence.
def test_modified_at_null_rows_converge_on_rerun(tmp_path):
    db = str(tmp_path / "edge_modified_at.db")
    run_migrations(db)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_hash,"
            " status, file_size, chunk_count, created_at)"
            " VALUES (1, 'p', 'n', 'h', 'indexed', 0, 0,"
            " '2024-02-02 00:00:00')"
        )
        conn.execute("UPDATE files SET modified_at = NULL")
        conn.commit()
    finally:
        conn.close()

    # Direct migration call converges...
    migrate_add_file_metadata_columns(db)
    conn = sqlite3.connect(db)
    try:
        value = conn.execute(
            "SELECT modified_at FROM files WHERE created_at ="
            " '2024-02-02 00:00:00'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert value == "2024-02-02 00:00:00"

    # ...and the full boot path repairs a damaged row again.
    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE files SET modified_at = NULL")
        conn.commit()
    finally:
        conn.close()
    run_migrations(db)
    conn = sqlite3.connect(db)
    try:
        value = conn.execute(
            "SELECT modified_at FROM files WHERE created_at ="
            " '2024-02-02 00:00:00'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert value == "2024-02-02 00:00:00"


# (f) Second rebuild site: the wiki_lint_findings rename-rebuild must also
# preserve the AUTOINCREMENT high-water (old shape = pre-json-CHECK DDL).
def test_wiki_lint_rebuild_preserves_autoincrement_high_water(tmp_path):
    db = str(tmp_path / "edge_lint_highwater.db")
    conn = sqlite3.connect(db)
    try:
        conn.execute(_OLD_LINT_DDL)
        for i in range(1, 6):
            conn.execute(
                "INSERT INTO wiki_lint_findings (vault_id, finding_type,"
                " severity, title) VALUES (1, 'stale', 'low', ?)",
                (f"finding {i}",),
            )
        conn.execute("DELETE FROM wiki_lint_findings WHERE id IN (4, 5)")
        conn.commit()
        seq = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'wiki_lint_findings'"
        ).fetchone()
        assert seq is not None and seq[0] == 5
    finally:
        conn.close()

    migrate_add_wiki_lint_findings_json_check(db)

    conn = sqlite3.connect(db)
    try:
        cursor = conn.execute(
            "INSERT INTO wiki_lint_findings (vault_id, finding_type,"
            " severity, title) VALUES (1, 'stale', 'low', 'post-rebuild')"
        )
        new_id = cursor.lastrowid
        conn.commit()
    finally:
        conn.close()
    assert new_id == 6
