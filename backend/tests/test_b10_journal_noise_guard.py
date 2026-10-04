"""Issue #699 (Workstream B PR 10) — journal-noise and signal guard family.

Pins the mechanisms the frozen checks in test_b10_migration_journal_signals.py
assert end-to-end:

(i)   a clean second boot writes ZERO migration_journal rows;
(ii)  latest_outcomes_with_signal: signal-in-window vs buried-signal vs empty;
(iii) the run_migrations user_sessions dedup journals its deleted-row count
      inside the delete's transaction;
(iv)  the claims dedup remaps wiki_relations.claim_id (nothing cascade-deleted)
      and completes under an unrelated FK orphan;
(v)   rename-only crash recovery for the previously-untested trio
      (files_old / document_reindex_jobs_old / draft_claims_old) plus
      stale-backup-when-complete disposition for curator, lint, and
      claim-sources — dispositioned with zero journal noise;
(vi)  AST guardrail: every executed ``PRAGMA foreign_key_check`` in
      backend/app carries a table argument.
"""
import ast
import sqlite3

import pytest

from app.models.database import (
    migrate_add_curator_claim_support,
    migrate_add_document_near_dups,
    migrate_add_wiki_claims_unique_claim_text,
    migrate_add_wiki_lint_findings_json_check,
    migrate_relax_draft_claims_span_not_null,
    migrate_widen_document_reindex_jobs_status,
    migrate_widen_files_status,
    migrate_widen_wiki_claim_sources_source_kind,
    run_migrations,
)
from app.models.migration_journal import (
    latest_outcomes_with_signal,
    record_migration_outcome,
)


def _journal_rows(path, where="1=1", params=()):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            f"SELECT phase, outcome FROM migration_journal WHERE {where}"
            " ORDER BY id",
            params,
        ).fetchall()
    except sqlite3.OperationalError:
        return []  # journal table never created: nothing was ever written
    finally:
        conn.close()


def _count(path, table):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# (i) clean boots journal nothing
# ---------------------------------------------------------------------------


def test_clean_second_boot_writes_zero_journal_rows(tmp_path):
    db = str(tmp_path / "a.db")
    run_migrations(db)
    after_first = _journal_rows(db)
    assert after_first, "first boot must journal its real migration work"

    run_migrations(db)
    after_second = _journal_rows(db)
    assert after_second == after_first


def test_near_dups_fresh_db_is_journal_silent(tmp_path):
    from app.models.database import init_db

    db = str(tmp_path / "b.db")
    init_db(db)  # _BASE_SCHEMA already carries embedding_model
    migrate_add_document_near_dups(db)
    assert _journal_rows(db, "migration_name = 'migrate_add_document_near_dups'") == []


# ---------------------------------------------------------------------------
# (ii) latest_outcomes_with_signal branches
# ---------------------------------------------------------------------------


def _journal_db(tmp_path, rows):
    db = str(tmp_path / "j.db")
    conn = sqlite3.connect(db)
    from app.models.migration_journal import MIGRATION_JOURNAL_DDL

    conn.execute(MIGRATION_JOURNAL_DDL)
    for phase, outcome in rows:
        record_migration_outcome(
            conn, migration_name="m_test", phase=phase, outcome=outcome
        )
    conn.commit()
    conn.close()
    return db


def test_latest_outcomes_with_signal_signal_in_window_returns_none(tmp_path):
    db = _journal_db(
        tmp_path,
        [("start", "ok"), ("failed", "error"), ("succeeded", "ok")],
    )
    recent, signal = latest_outcomes_with_signal(db, limit=3)
    assert [r["phase"] for r in recent] == ["succeeded", "failed", "start"]
    assert signal is None


def test_latest_outcomes_with_signal_ignores_resolved_failure(tmp_path):
    """Out-of-band review F-A: a failed row superseded by a later succeeded
    row of the SAME migration is resolved — no boot may re-warn about it."""
    db = _journal_db(
        tmp_path,
        [("failed", "error")] + [("succeeded", "ok")] * 4,
    )
    recent, signal = latest_outcomes_with_signal(db, limit=3)
    assert [r["phase"] for r in recent] == ["succeeded", "succeeded", "succeeded"]
    assert signal is None


def test_latest_outcomes_with_signal_surfaces_buried_failure(tmp_path):
    """The failure is buried past the window AND unresolved (no later
    succeeded row for its own migration — the other rows belong to a
    different migration) — it must surface."""
    db = str(tmp_path / "b.db")
    conn = sqlite3.connect(db)
    from app.models.migration_journal import MIGRATION_JOURNAL_DDL

    conn.execute(MIGRATION_JOURNAL_DDL)
    record_migration_outcome(
        conn, migration_name="m_dead", phase="failed", outcome="error"
    )
    for _ in range(4):
        record_migration_outcome(
            conn, migration_name="m_other", phase="succeeded", outcome="ok"
        )
    conn.commit()
    conn.close()
    recent, signal = latest_outcomes_with_signal(db, limit=3)
    assert [r["phase"] for r in recent] == ["succeeded", "succeeded", "succeeded"]
    assert signal is not None
    assert signal["phase"] == "failed"
    assert signal["migration_name"] == "m_dead"


def test_latest_outcomes_with_signal_empty_journal(tmp_path):
    recent, signal = latest_outcomes_with_signal(str(tmp_path / "empty.db"), limit=3)
    assert recent == []
    assert signal is None


# ---------------------------------------------------------------------------
# (iii) user_sessions dedup journals its count
# ---------------------------------------------------------------------------


def test_user_sessions_dedup_journals_deleted_count_in_transaction(tmp_path):
    db = str(tmp_path / "c.db")
    run_migrations(db)
    conn = sqlite3.connect(db)
    try:
        conn.execute("DROP INDEX idx_user_sessions_refresh_hash")
        conn.execute(
            "INSERT INTO user_sessions (user_id, refresh_token_hash, expires_at)"
            " VALUES (1, 'dup-hash', '2030-01-01 00:00:00')"
        )
        conn.execute(
            "INSERT INTO user_sessions (user_id, refresh_token_hash, expires_at)"
            " VALUES (1, 'dup-hash', '2030-01-01 00:00:00')"
        )
        conn.commit()
    finally:
        conn.close()

    run_migrations(db)

    rows = _journal_rows(
        db, "migration_name = 'run_migrations_user_sessions_refresh_hash_unique'"
    )
    # The schema ships a non-unique index, so the FIRST boot may journal a
    # count=0 rebuild row too; the seeded run must journal exactly one
    # count-bearing terminal row.
    assert rows[-1] == ("succeeded", "ok")
    detail = sqlite3.connect(db).execute(
        "SELECT detail FROM migration_journal"
        " WHERE migration_name = 'run_migrations_user_sessions_refresh_hash_unique'"
        " ORDER BY id"
    ).fetchall()[-1][0]
    assert "1" in detail
    assert _count(db, "user_sessions") == 1


# ---------------------------------------------------------------------------
# (iv) claims dedup remaps wiki_relations (no cascade destruction)
# ---------------------------------------------------------------------------

_CLAIMS_DDL = """
CREATE TABLE wiki_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_id INTEGER NOT NULL,
    claim_text TEXT NOT NULL,
    source_type TEXT NOT NULL
)
"""


def _claims_fixture(db):
    conn = sqlite3.connect(db)
    conn.execute(_CLAIMS_DDL)
    conn.execute(
        "CREATE TABLE wiki_claim_sources ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " claim_id INTEGER NOT NULL REFERENCES wiki_claims(id) ON DELETE CASCADE,"
        " source_kind TEXT NOT NULL DEFAULT 'document')"
    )
    conn.execute(
        "CREATE TABLE wiki_relations ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " claim_id INTEGER REFERENCES wiki_claims(id) ON DELETE CASCADE,"
        " subject_entity_id INTEGER, predicate TEXT, object_entity_id INTEGER)"
    )
    conn.execute(
        "INSERT INTO wiki_claims (vault_id, claim_text, source_type)"
        " VALUES (7, 'Claim one text', 'document')"
    )
    conn.execute(
        "INSERT INTO wiki_claims (vault_id, claim_text, source_type)"
        " VALUES (7, 'Claim one text', 'document')"
    )
    conn.execute("INSERT INTO wiki_relations (claim_id) VALUES (1)")
    conn.execute("INSERT INTO wiki_relations (claim_id) VALUES (2)")
    conn.execute("INSERT INTO wiki_claim_sources (claim_id) VALUES (1)")
    conn.execute("INSERT INTO wiki_claim_sources (claim_id) VALUES (2)")
    # Unrelated pre-existing FK violation: must NOT block the dedup (#699 AC4).
    conn.execute("CREATE TABLE tags (id INTEGER PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE document_tags ("
        "file_id INTEGER, tag_id INTEGER REFERENCES tags(id))"
    )
    conn.execute("INSERT INTO document_tags VALUES (1, 99)")
    conn.commit()
    conn.close()


def test_claims_dedup_remaps_wiki_relations_and_ignores_unrelated_orphan(tmp_path):
    db = str(tmp_path / "d.db")
    _claims_fixture(db)

    migrate_add_wiki_claims_unique_claim_text(db)  # must not raise

    conn = sqlite3.connect(db)
    try:
        relations = conn.execute(
            "SELECT claim_id FROM wiki_relations ORDER BY id"
        ).fetchall()
        assert relations == [(2,), (2,), ], "both relation rows survive on the survivor claim"
        sources = conn.execute(
            "SELECT claim_id FROM wiki_claim_sources ORDER BY id"
        ).fetchall()
        assert sources == [(2,), (2,)]
        claims = conn.execute("SELECT id FROM wiki_claims").fetchall()
        assert claims == [(2,)]
    finally:
        conn.close()
    conn = sqlite3.connect(db)
    try:
        detail = conn.execute(
            "SELECT detail FROM migration_journal"
            " WHERE migration_name = 'migrate_add_wiki_claims_unique_claim_text'"
            " AND detail LIKE 'deduplicated%' ORDER BY id"
        ).fetchall()[-1][0]
    finally:
        conn.close()
    # F-E: the journaled count must equal the rows actually deleted (1 of 2).
    assert detail == "deduplicated wiki_claims rows deleted: 1"
    phases = _journal_rows(
        db, "migration_name = 'migrate_add_wiki_claims_unique_claim_text'"
        " AND phase != 'start'"
    )
    assert phases[-1] == ("succeeded", "ok")


# ---------------------------------------------------------------------------
# (v) recovery states: the previously-untested trio + stale-backup drops
# ---------------------------------------------------------------------------


def test_widen_files_status_rename_only_recovery_journals_recovered(tmp_path):
    db = str(tmp_path / "e.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE files_old ("
        "id INTEGER PRIMARY KEY, status TEXT"
        " CHECK (status IN ('pending','processing','indexed','partial','error')),"
        " partial_embeddings INTEGER)"
    )
    conn.execute("INSERT INTO files_old VALUES (1, 'partial', 1)")
    conn.commit()
    conn.close()

    migrate_widen_files_status(db)

    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name='files_old'"
        ).fetchone() is None
        assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
    finally:
        conn.close()
    # Rename-back recovery is signal; the already-widened probe then returns
    # silently, so exactly the recovery rows were written.
    phases = _journal_rows(db, "migration_name = 'migrate_widen_files_status'")
    assert phases == [
        ("recovered", "recovered_from_backup"),
        ("succeeded", "rebuilt"),
    ]


def test_widen_document_reindex_rename_only_recovery_journals_recovered(tmp_path):
    db = str(tmp_path / "f.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE document_reindex_jobs_old ("
        "id INTEGER PRIMARY KEY, status TEXT"
        " CHECK (status IN ('pending','running','completed','failed',"
        "'cancelled','interrupted')))"
    )
    conn.commit()
    conn.close()

    migrate_widen_document_reindex_jobs_status(db)

    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name='document_reindex_jobs_old'"
        ).fetchone() is None
    finally:
        conn.close()
    phases = _journal_rows(
        db, "migration_name = 'migrate_widen_document_reindex_jobs_status'"
    )
    assert phases == [
        ("recovered", "recovered_from_backup"),
        ("succeeded", "rebuilt"),
    ]


def test_relax_draft_claims_rename_only_recovery_is_dispositioned(tmp_path):
    db = str(tmp_path / "g.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE draft_claims_old ("
        "id INTEGER PRIMARY KEY, revision_id INTEGER NOT NULL, ordinal INTEGER,"
        " claim_text TEXT NOT NULL, claim_sha256 TEXT, span_start INTEGER,"
        " span_end INTEGER)"
    )
    conn.execute(
        "INSERT INTO draft_claims_old VALUES (1, 1, 0, 'claim', NULL, NULL, NULL)"
    )
    conn.commit()
    conn.close()

    migrate_relax_draft_claims_span_not_null(db)

    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name='draft_claims_old'"
        ).fetchone() is None
        assert conn.execute("SELECT COUNT(*) FROM draft_claims").fetchone()[0] == 1
    finally:
        conn.close()
    # relax_draft_claims has no separate recovered phase (its recovery falls
    # through to the already-relaxed probe); the disposition is journal-silent.
    assert _journal_rows(
        db, "migration_name = 'migrate_relax_draft_claims_span_not_null'"
    ) == []


def _stale_backup_db(tmp_path, canonical_ddl, backup_ddl, seed):
    db = str(tmp_path / "h.db")
    conn = sqlite3.connect(db)
    conn.execute(canonical_ddl)
    conn.execute(backup_ddl)
    seed(conn)
    conn.commit()
    conn.close()
    return db


def test_curator_stale_backup_beside_complete_table_is_silent(tmp_path):
    db = _stale_backup_db(
        tmp_path,
        "CREATE TABLE wiki_claims (id INTEGER PRIMARY KEY, claim_text TEXT,"
        " created_by_kind TEXT)",
        "CREATE TABLE wiki_claims_old (id INTEGER PRIMARY KEY, claim_text TEXT)",
        lambda conn: (
            conn.execute("INSERT INTO wiki_claims VALUES (1, 'a', 'llm')"),
            conn.execute("INSERT INTO wiki_claims_old VALUES (1, 'a')"),
        ),
    )
    migrate_add_curator_claim_support(db)
    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name='wiki_claims_old'"
        ).fetchone() is None
    finally:
        conn.close()
    assert _journal_rows(
        db, "migration_name = 'migrate_add_curator_claim_support'"
    ) == []


def test_lint_stale_backup_beside_complete_table_is_silent(tmp_path):
    db = _stale_backup_db(
        tmp_path,
        "CREATE TABLE wiki_lint_findings (id INTEGER PRIMARY KEY,"
        " related_page_ids_json TEXT NOT NULL DEFAULT '[]'"
        " CHECK (json_type(related_page_ids_json) = 'array'),"
        " related_claim_ids_json TEXT NOT NULL DEFAULT '[]'"
        " CHECK (json_type(related_claim_ids_json) = 'array'))",
        "CREATE TABLE _wiki_lint_findings_old (id INTEGER PRIMARY KEY)",
        lambda conn: conn.execute("INSERT INTO wiki_lint_findings (id)"
                                  " VALUES (1)"),
    )
    migrate_add_wiki_lint_findings_json_check(db)
    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT name FROM sqlite_master"
            " WHERE name='_wiki_lint_findings_old'"
        ).fetchone() is None
    finally:
        conn.close()
    assert _journal_rows(
        db, "migration_name = 'migrate_add_wiki_lint_findings_json_check'"
    ) == []


def test_claim_sources_stale_backup_beside_complete_table_is_silent(tmp_path):
    db = _stale_backup_db(
        tmp_path,
        "CREATE TABLE wiki_claim_sources (id INTEGER PRIMARY KEY,"
        " claim_id INTEGER NOT NULL REFERENCES wiki_claims(id) ON DELETE CASCADE,"
        " source_kind TEXT NOT NULL CHECK (source_kind IN ("
        "'document','memory','chat_message','manual','wiki')))",
        "CREATE TABLE wiki_claim_sources_old (id INTEGER PRIMARY KEY,"
        " source_kind TEXT)",
        lambda conn: (
            conn.execute("CREATE TABLE wiki_claims (id INTEGER PRIMARY KEY,"
                         " claim_text TEXT)"),
            conn.execute("INSERT INTO wiki_claims (id) VALUES (1)"),
            conn.execute("INSERT INTO wiki_claim_sources (id, claim_id,"
                         " source_kind) VALUES (1, 1, 'document')"),
            conn.execute("INSERT INTO wiki_claim_sources_old (id)"
                         " VALUES (1)"),
        ),
    )
    migrate_widen_wiki_claim_sources_source_kind(db)
    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT name FROM sqlite_master"
            " WHERE name='wiki_claim_sources_old'"
        ).fetchone() is None
    finally:
        conn.close()
    assert _journal_rows(
        db, "migration_name = 'migrate_widen_wiki_claim_sources_source_kind'"
    ) == []


# ---------------------------------------------------------------------------
# (vii) F-C: BEGIN IMMEDIATE in a journaled migration must sit inside a try
# whose except journals an outcome — the frozen C1 pins one site; this pins
# the class.
# ---------------------------------------------------------------------------


def test_begin_immediate_in_journaled_migrations_is_inside_journaling_try():
    """Out-of-band review F-C: every ``BEGIN IMMEDIATE`` executed inside a
    journal-writing migration must be enclosed by a try whose except calls
    ``record_migration_outcome`` (so a lock error at the transaction open
    cannot strand an unjournaled or start-only attempt). The two
    non-journaled BEGIN sites (_widen_files_hash_vault_unique_index,
    migrate_add_draft_room_promotions) are out of scope by definition."""
    import ast
    import pathlib

    db_path = (
        pathlib.Path(__file__).resolve().parents[1] / "app" / "models" / "database.py"
    )
    tree = ast.parse(db_path.read_text(encoding="utf-8"))
    offenders = []
    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
        fn_nodes = list(ast.walk(fn))
        if not any(
            isinstance(c.func, ast.Name) and c.func.id == "record_migration_outcome"
            for c in fn_nodes
            if isinstance(c, ast.Call)
        ):
            continue
        begins = [
            c
            for c in fn_nodes
            if isinstance(c, ast.Call)
            and isinstance(c.func, ast.Attribute)
            and c.func.attr == "execute"
            and c.args
            and isinstance(c.args[0], ast.Constant)
            and isinstance(c.args[0].value, str)
            and c.args[0].value.strip() == "BEGIN IMMEDIATE"
        ]
        for begin in begins:
            guarded = False
            for try_node in fn_nodes:
                if not (isinstance(try_node, ast.Try) and try_node.handlers):
                    continue
                if not any(sub is begin for sub in ast.walk(try_node)):
                    continue
                journals = any(
                    isinstance(c.func, ast.Name)
                    and c.func.id == "record_migration_outcome"
                    for handler in try_node.handlers
                    for c in ast.walk(handler)
                    if isinstance(c, ast.Call)
                )
                if journals:
                    guarded = True
                    break
            if not guarded:
                offenders.append(f"{fn.name}:{begin.lineno}")
    assert offenders == []


# ---------------------------------------------------------------------------
# (viii) F-E/PRR-007: exact-count + rolled-back-delete coverage for the
# previously unpinned destructive-dedup writers.
# ---------------------------------------------------------------------------


def test_wiki_relations_unique_dedup_journals_exact_count(tmp_path):
    from app.models.database import migrate_add_wiki_relations_unique

    db = str(tmp_path / "wr.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE wiki_relations ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " claim_id INTEGER, subject_entity_id INTEGER, predicate TEXT,"
        " object_entity_id INTEGER)"
    )
    for _ in range(3):
        conn.execute(
            "INSERT INTO wiki_relations (claim_id, subject_entity_id,"
            " predicate, object_entity_id) VALUES (1, 5, 'p', 6)"
        )
    conn.execute(
        "INSERT INTO wiki_relations (claim_id, subject_entity_id,"
        " predicate, object_entity_id) VALUES (1, 7, 'q', 8)"
    )
    conn.commit()
    conn.close()

    migrate_add_wiki_relations_unique(db)

    conn = sqlite3.connect(db)
    try:
        remaining = conn.execute("SELECT COUNT(*) FROM wiki_relations").fetchone()[0]
        detail = conn.execute(
            "SELECT detail FROM migration_journal"
            " WHERE migration_name = 'migrate_add_wiki_relations_unique'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert remaining == 2
    # F-E: the journaled count must equal the rows actually deleted (3 -> 2).
    assert detail == "deduplicated wiki_relations rows deleted: 2"


def test_wiki_relations_unique_failed_delete_journals_failed_and_rolls_back(tmp_path):
    """PRR-001 regression pin: a failing destructive attempt journals exactly
    one terminal failed row and persists no journal record of the delete."""
    from app.models.database import migrate_add_wiki_relations_unique

    db = str(tmp_path / "wrf.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE wiki_relations ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " claim_id INTEGER, subject_entity_id INTEGER, predicate TEXT,"
        " object_entity_id INTEGER)"
    )
    for _ in range(2):
        conn.execute(
            "INSERT INTO wiki_relations (claim_id, subject_entity_id,"
            " predicate, object_entity_id) VALUES (1, 5, 'p', 6)"
        )
    conn.execute(
        "CREATE TRIGGER wr_block_delete BEFORE DELETE ON wiki_relations"
        " BEGIN SELECT RAISE(ABORT, 'injected dedup failure'); END"
    )
    conn.commit()
    conn.close()

    with pytest.raises(sqlite3.IntegrityError, match="injected dedup failure"):
        migrate_add_wiki_relations_unique(db)

    rows = _journal_rows(db, "migration_name = 'migrate_add_wiki_relations_unique'")
    assert rows == [("failed", "error")]
    assert _count(db, "wiki_relations") == 2  # delete rolled back


def test_user_sessions_branch_b_dedup_journals_exact_count(tmp_path):
    """PRR-007: the non-unique-index branch (shipped by _BASE_SCHEMA) with
    real duplicates — previously untested — journals the exact deleted
    count and collapses the duplicates."""
    from app.models.database import init_db

    db = str(tmp_path / "usb.db")
    init_db(db)  # ships NON-unique idx_user_sessions_refresh_hash
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO user_sessions (user_id, refresh_token_hash, expires_at)"
        " VALUES (1, 'branch-b-hash', '2030-01-01 00:00:00')"
    )
    conn.execute(
        "INSERT INTO user_sessions (user_id, refresh_token_hash, expires_at)"
        " VALUES (2, 'branch-b-hash', '2030-01-01 00:00:00')"
    )
    conn.commit()
    conn.close()

    run_migrations(db)

    conn = sqlite3.connect(db)
    try:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM user_sessions WHERE refresh_token_hash ="
            " 'branch-b-hash'"
        ).fetchone()[0]
        details = conn.execute(
            "SELECT detail FROM migration_journal"
            " WHERE migration_name = 'run_migrations_user_sessions_refresh_hash_unique'"
            " ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    assert remaining == 1
    # F-E exact count on branch B (the elif path, never exercised before).
    assert details[-1][0] == "deduplicated user_sessions rows deleted: 1"


# ---------------------------------------------------------------------------
# (vi) guardrail: every executed foreign_key_check is table-scoped
# ---------------------------------------------------------------------------


def test_every_foreign_key_check_in_backend_app_is_table_scoped():
    """#699 AC4 guardrail: an unscoped ``PRAGMA foreign_key_check`` sweeps the
    whole database, so an unrelated pre-existing orphan can block any
    migration that runs it. Every string EXECUTED via ``.execute(...)`` in
    backend/app that issues the pragma must name a table (f-string
    occurrences must still carry the parenthesised argument). Error-message
    and docstring strings that merely mention the pragma are out of scope.
    """
    import pathlib

    def _arg_text(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            return "".join(
                v.value if isinstance(v, ast.Constant) else "{}"
                for v in node.values
            )
        return None

    app_root = pathlib.Path(__file__).resolve().parents[1] / "app"
    offenders = []
    for py_file in app_root.rglob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "execute"):
                continue
            if not node.args:
                continue
            text = _arg_text(node.args[0])
            if text and "PRAGMA foreign_key_check" in text:
                if "PRAGMA foreign_key_check(" not in text:
                    offenders.append(f"{py_file.name}: line {node.lineno}")
    assert offenders == []
