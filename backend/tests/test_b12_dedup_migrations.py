"""Issue #701 (Workstream B PR 12) — destructive rebuild and dedup migrations
stop losing or duplicating rows.

Behavioral pins for the audit findings:

- T1-05-K2-03 (AC2): the ``wiki_relations`` dedup must not collapse distinct
  rows that differ only in ``object_text`` while sharing a NULL
  ``object_entity_id`` — SQL ``GROUP BY`` folds NULLs into one group, but the
  ``UNIQUE`` index the dedup prepares for treats NULLs as distinct.
- T1-05-S-02 (AC3): the ``wiki_claims`` dedup must re-point, not
  cascade-delete, ``wiki_relations.claim_id`` rows attached to losing twins.
- T1-05-S-03 (AC4): ``migrate_add_wiki_claims_normalized_text`` must backfill
  on re-run when the column exists but rows hold NULL (crash-after-ALTER
  window), matching the ``chat_messages.seq`` recovery pattern.
- T1-05-S-03 (AC5): ``migrate_add_org_slug_column`` must backfill NULL slugs
  on re-run (exercised by the #690 every-invocation repair pass).
- T1-06-S-02 (AC6): rename-copy rebuilds must preserve the AUTOINCREMENT
  high-water mark so ids of rows deleted before the migration are never
  reissued.
- T1-05-S-09 (AC7): a legacy ``files`` upgrade via ``run_migrations`` must
  backfill ``modified_at`` from ``created_at``.

Plus the AC1 companion: the full rename-rebuild swap on a legacy NOT NULL
``draft_claims`` with a ``draft_claim_sources`` child must land with the
child's FK still targeting the canonical table (the CRITICAL
``legacy_alter_table=ON`` leg) and the span columns nullable.
"""
import sqlite3

from app.models.database import (
    migrate_add_org_slug_column,
    migrate_add_wiki_claims_normalized_text,
    migrate_add_wiki_claims_unique_claim_text,
    migrate_add_wiki_relations_unique,
    migrate_relax_draft_claims_span_not_null,
    run_migrations,
)

# ---------------------------------------------------------------------------
# Shared minimal DDL (TestClaimsUniqueDedupRemapsEvidence idiom, but scoped
# to this NEW module so Workstream H PR 7's fixture-count check (#744 AC8)
# is untouched).
# ---------------------------------------------------------------------------

_MIN_CLAIMS_DDL = """
CREATE TABLE wiki_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_id INTEGER NOT NULL,
    claim_text TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'document'
)
"""

_MIN_RELATIONS_DDL = """
CREATE TABLE wiki_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_id INTEGER NOT NULL,
    subject_entity_id INTEGER,
    predicate TEXT NOT NULL,
    object_entity_id INTEGER,
    object_text TEXT,
    claim_id INTEGER REFERENCES wiki_claims(id) ON DELETE CASCADE,
    confidence REAL DEFAULT 0.0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
    resolved_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    resolved_at TIMESTAMP,
    resolution_note TEXT,
    UNIQUE(revision_id, ordinal)
)
"""

_DRAFT_CLAIM_SOURCES_DDL = """
CREATE TABLE draft_claim_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER NOT NULL REFERENCES draft_claims(id) ON DELETE CASCADE,
    source_kind TEXT NOT NULL DEFAULT 'document',
    quote TEXT
)
"""


def _seed_legacy_draft_room(db):
    """users + draft_revisions + legacy NOT NULL draft_claims + child rows."""
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")
        conn.execute("INSERT INTO users (id, username) VALUES (1, 'u')")
        conn.execute(
            """
            CREATE TABLE draft_revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                draft_id INTEGER NOT NULL,
                revision_no INTEGER NOT NULL,
                source TEXT NOT NULL,
                content_md TEXT NOT NULL,
                content_sha256 TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO draft_revisions (id, draft_id, revision_no, source,"
            " content_md, content_sha256) VALUES (1, 1, 1, 'manual', 'x', 'h')"
        )
        conn.execute(_LEGACY_DRAFT_CLAIMS_DDL)
        conn.execute(_DRAFT_CLAIM_SOURCES_DDL)
        for ordinal in range(1, 6):
            conn.execute(
                "INSERT INTO draft_claims (revision_id, ordinal, claim_text,"
                " claim_sha256, span_start, span_end, claim_type, status,"
                " severity) VALUES (1, ?, ?, ?, 0, 1, 'factual',"
                " 'supported', 'info')",
                (ordinal, f"claim {ordinal}", f"sha-{ordinal}"),
            )
        conn.execute(
            "INSERT INTO draft_claim_sources (claim_id, source_kind, quote)"
            " VALUES (1, 'document', 'q1')"
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# AC2 — T1-05-K2-03: NULL-object relations survive the triple dedup
# ---------------------------------------------------------------------------


def test_relations_dedup_keeps_distinct_null_object_rows(tmp_path):
    db = str(tmp_path / "ac2_relations.db")
    run_migrations(db)

    conn = sqlite3.connect(db)
    try:
        # Re-arm the migration by removing its completion marker; the two
        # NULL-object rows below are legal under both the table-level UNIQUE
        # and idx_wiki_relations_unique_triple (NULLs are distinct).
        conn.execute("DROP INDEX IF EXISTS idx_wiki_relations_unique_triple")
        conn.execute(
            "INSERT INTO wiki_entities (vault_id, canonical_name)"
            " VALUES (1, 'Germany')"
        )
        conn.execute(
            "INSERT INTO wiki_relations (vault_id, subject_entity_id,"
            " predicate, object_entity_id, object_text)"
            " VALUES (1, 1, 'capital_of', NULL, 'Berlin')"
        )
        conn.execute(
            "INSERT INTO wiki_relations (vault_id, subject_entity_id,"
            " predicate, object_entity_id, object_text)"
            " VALUES (1, 1, 'capital_of', NULL, 'Munich')"
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
    assert count == 2, (
        "distinct free-text relations that share subject/predicate but differ "
        "in object_text must survive the dedup (the UNIQUE index treats NULL "
        "object_entity_id values as distinct)"
    )


# ---------------------------------------------------------------------------
# AC3 — T1-05-S-02: claims dedup re-points relations on the losing twin
# ---------------------------------------------------------------------------


def test_claims_dedup_preserves_relations_on_losing_twin(tmp_path):
    db = str(tmp_path / "ac3_claims.db")
    conn = sqlite3.connect(db)
    try:
        conn.execute(_MIN_CLAIMS_DDL)
        conn.execute(
            "INSERT INTO wiki_claims (id, vault_id, claim_text)"
            " VALUES (1, 7, 'dup')"
        )
        conn.execute(
            "INSERT INTO wiki_claims (id, vault_id, claim_text)"
            " VALUES (2, 7, 'dup')"
        )
        conn.execute(_MIN_RELATIONS_DDL)
        conn.execute(
            "INSERT INTO wiki_relations (vault_id, subject_entity_id,"
            " predicate, object_text, claim_id)"
            " VALUES (7, 1, 'relates_to', 'note', 1)"
        )
        conn.commit()
    finally:
        conn.close()

    migrate_add_wiki_claims_unique_claim_text(db)

    conn = sqlite3.connect(db)
    try:
        survivor_relations = conn.execute(
            "SELECT COUNT(*) FROM wiki_relations WHERE claim_id = 2"
        ).fetchone()[0]
    finally:
        conn.close()
    assert survivor_relations == 1, (
        "a relation on the losing duplicate must be re-pointed at the "
        "surviving claim, not cascade-deleted by the dedup DELETE"
    )


# ---------------------------------------------------------------------------
# AC4 — T1-05-S-03: normalized_text backfills on re-run (crash-after-ALTER)
# ---------------------------------------------------------------------------


def test_normalized_text_rerun_backfills_null_rows(tmp_path):
    db = str(tmp_path / "ac4_normalized.db")
    run_migrations(db)

    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO wiki_claims (vault_id, claim_text, source_type)"
            " VALUES (7, 'Sky is blue', 'document')"
        )
        conn.commit()
    finally:
        conn.close()

    # Simulate the crash-after-ALTER state: the column exists, a row holds
    # NULL. A re-run must converge to fully backfilled (chat_messages.seq
    # pattern) instead of gating solely on column absence.
    migrate_add_wiki_claims_normalized_text(db)

    conn = sqlite3.connect(db)
    try:
        value = conn.execute(
            "SELECT normalized_text FROM wiki_claims WHERE claim_text = 'Sky is blue'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert value == "sky is blue"


# ---------------------------------------------------------------------------
# AC5 — T1-05-S-03: organizations.slug backfills on re-run
# ---------------------------------------------------------------------------


def test_org_slug_rerun_backfills_null_rows(tmp_path):
    db = str(tmp_path / "ac5_orgslug.db")
    run_migrations(db)

    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO organizations (name, slug) VALUES ('Acme Corp', NULL)"
        )
        conn.commit()
    finally:
        conn.close()

    migrate_add_org_slug_column(db)

    conn = sqlite3.connect(db)
    try:
        value = conn.execute(
            "SELECT slug FROM organizations WHERE name = 'Acme Corp'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert value == "acme-corp"


# ---------------------------------------------------------------------------
# AC6 — T1-06-S-02: rebuild preserves the AUTOINCREMENT high-water mark
# ---------------------------------------------------------------------------


def test_draft_claims_rebuild_preserves_autoincrement_high_water(tmp_path):
    db = str(tmp_path / "ac6_highwater.db")
    _seed_legacy_draft_room(db)

    conn = sqlite3.connect(db)
    try:
        conn.execute("DELETE FROM draft_claims WHERE id IN (4, 5)")
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
            " VALUES (1, 10, 'post-migration claim', 'factual', 'supported',"
            " 'info')"
        )
        new_id = cursor.lastrowid
        conn.commit()
    finally:
        conn.close()
    assert new_id == 6, (
        "ids of rows deleted before the rename-rebuild must never be "
        "reissued: sqlite_sequence held 5 before the swap, so the next id "
        "must be 6"
    )


# ---------------------------------------------------------------------------
# AC7 — T1-05-S-09: legacy files upgrade backfills modified_at
# ---------------------------------------------------------------------------


def test_legacy_files_modified_at_is_backfilled(tmp_path):
    db = str(tmp_path / "ac7_files.db")
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
            " chunk_count, created_at) VALUES (1, 'p', 'n', 'h', 'indexed', 0,"
            " '2024-01-01 00:00:00')"
        )
        conn.commit()
    finally:
        conn.close()

    run_migrations(db)

    conn = sqlite3.connect(db)
    try:
        value = conn.execute(
            "SELECT modified_at FROM files WHERE id = 1"
        ).fetchone()[0]
    finally:
        conn.close()
    assert value == "2024-01-01 00:00:00"


# ---------------------------------------------------------------------------
# AC1 companion — the CRITICAL legacy_alter_table=ON swap, end to end
# ---------------------------------------------------------------------------


def test_relax_draft_claims_legacy_rebuild_swaps_cleanly(tmp_path):
    """Legacy NOT NULL draft_claims + draft_claim_sources child.

    After the swap: the child's FK must still target the canonical table
    (the exact failure mode legacy_alter_table=OFF would produce), the span
    columns must be nullable, every claim and child row must survive, and
    PRAGMA foreign_key_check must come back clean.
    """
    db = str(tmp_path / "ac1_legacy_swap.db")
    _seed_legacy_draft_room(db)

    migrate_relax_draft_claims_span_not_null(db)

    conn = sqlite3.connect(db)
    try:
        fk_targets = {
            row[2] for row in conn.execute(
                "PRAGMA foreign_key_list(draft_claim_sources)"
            ).fetchall()
        }
        assert "draft_claims" in fk_targets, (
            "draft_claim_sources.claim_id must reference the canonical "
            "draft_claims table after the rename-rebuild"
        )
        assert "draft_claims_old" not in {
            row[2] for row in conn.execute(
                "PRAGMA foreign_key_list(draft_claim_sources)"
            ).fetchall()
        }
        notnull = {
            row[1]: int(row[3])
            for row in conn.execute("PRAGMA table_info(draft_claims)").fetchall()
        }
        assert all(
            notnull.get(col) == 0
            for col in ("claim_sha256", "span_start", "span_end")
        ), "the rebuild must land the relaxed (nullable) span shape"
        claims = conn.execute(
            "SELECT COUNT(*) FROM draft_claims"
        ).fetchone()[0]
        sources = conn.execute(
            "SELECT COUNT(*) FROM draft_claim_sources"
        ).fetchone()[0]
        assert claims == 5
        assert sources == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        leftover = conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'draft_claims_old'"
        ).fetchone()
        assert leftover is None
    finally:
        conn.close()
