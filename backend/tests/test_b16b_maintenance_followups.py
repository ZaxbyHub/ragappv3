"""Issue #705 follow-ups beyond the frozen acceptance set (plan-critic test-gap items).

- A REAL rollback restores a backup that carries maintenance=1 (the backup is
  taken after the flag is enabled) and must end with maintenance cleared.
- The widened reset_embeddings predicate heals the rows the PRE-fix script
  stranded (status='pending' with phase NULL, invisible to the startup sweep)
  while leaving 'processing' rows to the app's own startup sweeps.
- Recurrence guardrail (issue #705 defect class C8): operator scripts must
  never filter the files table by status='indexed' alone.
"""

import re
import shutil
import sqlite3
import subprocess
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.models.database import run_migrations  # noqa: E402
from scripts import migrate_memories, reset_embeddings  # noqa: E402


def _maintenance_value(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM system_flags WHERE name = 'maintenance'"
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def test_rollback_clears_maintenance_carried_by_real_backup(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    db_path = scratch / "app.db"

    monkeypatch.setattr(migrate_memories.settings, "data_dir", scratch)
    monkeypatch.setattr(migrate_memories, "backup_sqlite", None)
    monkeypatch.setattr(migrate_memories, "HAS_BACKUP_MODULE", False)
    monkeypatch.setattr(migrate_memories, "run_migrations", lambda _path: None)
    monkeypatch.chdir(scratch)

    # A full migrate run: the flag is enabled BEFORE the backup is taken, so
    # the produced backup carries maintenance=1.
    migrate_memories.migrate(rollback=False, backup=None, retention=30)
    assert _maintenance_value(db_path) == 0  # flag cleared after the run

    backups = sorted((scratch / "backups").glob("app_backup_*.db"))
    assert backups, "expected a backup from the migrate run"
    backup_conn = sqlite3.connect(backups[0])
    try:
        carried = backup_conn.execute(
            "SELECT value FROM system_flags WHERE name = 'maintenance'"
        ).fetchone()[0]
    finally:
        backup_conn.close()
    assert int(carried) == 1, "the pre-migration backup must carry maintenance=1"

    # Rollback with the REAL decrypt_backup (plain-SQLite copy path) restores
    # the backup over the database; the post-restore clear must flip the
    # carried flag back to 0.
    migrate_memories.migrate(rollback=True, backup=backups[0], retention=30)
    assert _maintenance_value(db_path) == 0


def test_reset_embeddings_heals_stranded_pending_and_skips_processing(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "app.db"
    run_migrations(str(db_path))
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """INSERT INTO files (vault_id, file_path, file_name, file_hash,
               file_size, chunk_count, status, phase)
               VALUES (1, 'a.pdf', 'a.pdf', 'h1', 10, 2, 'indexed', NULL)"""
        )
        conn.execute(
            """INSERT INTO files (vault_id, file_path, file_name, file_hash,
               file_size, chunk_count, status, phase)
               VALUES (1, 'stranded.pdf', 'stranded.pdf', 'h2', 10, 0,
                       'pending', NULL)"""
        )
        conn.execute(
            """INSERT INTO files (vault_id, file_path, file_name, file_hash,
               file_size, chunk_count, status, phase)
               VALUES (1, 'live.pdf', 'live.pdf', 'h3', 10, 1,
                       'processing', 'parsing')"""
        )
        conn.commit()
    finally:
        conn.close()

    lancedb_dir = tmp_path / "lancedb"
    lancedb_dir.mkdir()
    monkeypatch.setattr(
        reset_embeddings,
        "settings",
        types.SimpleNamespace(sqlite_path=db_path, lancedb_path=lancedb_dir),
    )

    reset_embeddings.reset_all_embeddings()

    conn = sqlite3.connect(db_path)
    try:
        rows = {
            name: (status, phase)
            for name, status, phase in conn.execute(
                "SELECT file_name, status, phase FROM files"
            )
        }
    finally:
        conn.close()

    # The stranded pending row (phase NULL — invisible to the startup sweep's
    # status='pending' AND phase='queued' predicate) is healed to the
    # queue-entry signal.
    assert rows["stranded.pdf"] == ("pending", "queued")
    assert rows["a.pdf"] == ("pending", "queued")
    # Processing rows are deliberately left to the app's own age-gated
    # startup sweeps (blanket-resetting a live parse lease is out of scope).
    assert rows["live.pdf"] == ("processing", "parsing")


# ---------------------------------------------------------------------------
# Recurrence guardrail (issue #705 defect class C8): operator scripts must not
# filter the files table by status='indexed' alone — the app's status model
# (database.py files CHECK) treats 'partial' as live/searchable (issue #513)
# and pending/processing rows own in-flight vectors. A line-level census is
# the guard rung: any WHERE/UPDATE touching files with a bare indexed-equality
# status filter is a recurrence of T1-28-S-09/T1-28-S-11. The pattern also
# catches the double-quoted and single-element-IN spellings of the same
# filter (feedback round E08); it deliberately does NOT match multi-element
# IN-lists, which are the fixed form.
# ---------------------------------------------------------------------------

_INDEXED_ONLY_STATUS_FILTER = re.compile(
    r"status\s*=\s*['\"]indexed['\"]|status\s+IN\s*\(\s*'indexed'\s*\)"
)


def test_census_regex_catches_known_bad_spelling_forms():
    bad = [
        "WHERE status = 'indexed'",
        'status = "indexed"',
        "WHERE status IN ('indexed')",
        "status  =  'indexed'",
        "status='indexed' AND x = 1",
    ]
    good = [
        "WHERE status IN ('indexed', 'partial')",
        "status IN ('indexed','partial','pending')",
        "status = 'pending'",
        "WHERE status = 'processing'",
    ]
    for line in bad:
        assert _INDEXED_ONLY_STATUS_FILTER.search(line), line
    for line in good:
        assert not _INDEXED_ONLY_STATUS_FILTER.search(line), line


def test_no_indexed_only_status_filters_in_operator_scripts():
    hits = []
    root = Path(__file__).resolve().parents[2]
    for rel in ("scripts", "backend/scripts"):
        for path in sorted((root / rel).glob("*.py")):
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if _INDEXED_ONLY_STATUS_FILTER.search(line):
                    hits.append(f"{rel}/{path.name}:{lineno}")
    assert hits == [], (
        "operator script(s) filter the files table by status='indexed' alone "
        "(the 'partial' status owns live rows — issue #705 class C8): "
        + ", ".join(hits)
    )


# ---------------------------------------------------------------------------
# Implementation-review round-1 coverage gaps (mutation probes P-b / P-c):
# each of the two reconcile mechanisms — the report live-set widening and the
# pre-delete re-check — is individually sufficient for the frozen checks, so
# each needs a test that bites when THAT mechanism alone is reverted.
# ---------------------------------------------------------------------------

import importlib.util  # noqa: E402
import json  # noqa: E402

RECONCILE_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "reconcile_lancedb_sqlite.py"
)
RECONCILE_SPEC = importlib.util.spec_from_file_location(
    "reconcile_lancedb_sqlite_b16b", RECONCILE_PATH
)
reconcile = importlib.util.module_from_spec(RECONCILE_SPEC)
sys.modules[RECONCILE_SPEC.name] = reconcile
RECONCILE_SPEC.loader.exec_module(reconcile)


class _Arrow:
    def __init__(self, rows):
        self._rows = rows

    def to_pylist(self):
        return list(self._rows)


class _Table:
    def __init__(self, rows):
        self.rows = list(rows)

    async def to_arrow(self):
        return _Arrow(self.rows)

    async def count_rows(self, filter_expr=None):
        if not filter_expr:
            return len(self.rows)
        return sum(1 for row in self.rows if self._matches(row, filter_expr))

    async def delete(self, filter_expr):
        self.rows = [row for row in self.rows if not self._matches(row, filter_expr)]

    def _matches(self, row, filter_expr):
        if filter_expr.startswith("file_id IN ("):
            raw = filter_expr.removeprefix("file_id IN (").removesuffix(")")
            values = {v.strip().strip("'") for v in raw.split(",")}
            return row["file_id"] in values
        raise AssertionError(f"unexpected filter: {filter_expr}")


def _sqlite_db(db_path, rows):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE files (
            id INTEGER PRIMARY KEY, vault_id INTEGER NOT NULL,
            file_name TEXT NOT NULL, chunk_count INTEGER DEFAULT 0,
            status TEXT NOT NULL)"""
    )
    conn.executemany(
        "INSERT INTO files (id, vault_id, file_name, chunk_count, status)"
        " VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def test_partial_file_is_live_in_report_snapshot(tmp_path, monkeypatch, capsys):
    """Probe P-b pin: a 'partial' file with LanceDB rows is report-live.

    Bites when _load_indexed_files is reverted to WHERE status='indexed':
    the partial file would leave the live snapshot (totals 2 -> 1), appear in
    the orphan key, and drop out of the per-vault live counter.
    """
    db_path = tmp_path / "app.db"
    _sqlite_db(
        db_path,
        [(10, 1, "a.txt", 1, "indexed"), (20, 1, "p.txt", 1, "partial")],
    )
    table = _Table(
        [
            {"id": "c10", "file_id": "10", "vault_id": "1", "chunk_scale": "default"},
            {"id": "c20", "file_id": "20", "vault_id": "1", "chunk_scale": "default"},
        ]
    )

    async def open_chunks_table(lancedb_path):
        return table

    monkeypatch.setattr(reconcile, "_open_chunks_table", open_chunks_table)
    exit_code = reconcile.main(
        [
            "--sqlite-path", str(db_path),
            "--lancedb-path", str(tmp_path / "lancedb"),
            "--delete-orphan-file-ids", "--confirm",
        ]
    )
    report = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert report["totals"]["sqlite_indexed_files"] == 2
    assert report["sqlite_indexed_files_by_vault_id"] == {"1": 2}
    assert "20" not in report["file_ids_present_in_lancedb_but_not_sqlite_indexed"]
    assert [row["file_id"] for row in table.rows] == ["10", "20"]


def test_recheck_drops_candidate_protected_after_plan_build(
    tmp_path, monkeypatch, capsys
):
    """Probe P-c pin: the pre-delete re-check drops a candidate whose SQLite
    row appears (protected) only AFTER the plan-build filter ran.

    The seam wraps _planned_delete_counts — invoked between the plan-build
    protection filter and the re-check — and inserts the candidate row at
    'processing' from inside it. If the re-check block is removed (or moved
    after apply_cleanup), file 30's row is deleted and this test fails.
    """
    db_path = tmp_path / "app.db"
    _sqlite_db(db_path, [(10, 1, "a.txt", 1, "indexed")])
    table = _Table(
        [
            {"id": "c10", "file_id": "10", "vault_id": "1", "chunk_scale": "default"},
            {"id": "c30", "file_id": "30", "vault_id": "1", "chunk_scale": "default"},
        ]
    )

    async def open_chunks_table(lancedb_path):
        return table

    original_counts = reconcile._planned_delete_counts

    def counts_with_late_ingest(report, plan):
        # File 30 appears in SQLite only now — after the report snapshot AND
        # after the plan-build protection filter, before the re-check.
        # INSERT OR IGNORE: the seam is invoked a second time inside the
        # re-check block itself, where the late row is already present.
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO files (id, vault_id, file_name, chunk_count, status)"
                " VALUES (30, 1, 'late.txt', 1, 'processing')"
            )
            conn.commit()
        finally:
            conn.close()
        return original_counts(report, plan)

    monkeypatch.setattr(reconcile, "_open_chunks_table", open_chunks_table)
    monkeypatch.setattr(reconcile, "_planned_delete_counts", counts_with_late_ingest)
    exit_code = reconcile.main(
        [
            "--sqlite-path", str(db_path),
            "--lancedb-path", str(tmp_path / "lancedb"),
            "--delete-orphan-file-ids", "--confirm",
        ]
    )
    report = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert report["cleanup_plan"]["recheck_dropped_orphan_ids"] == ["30"]
    assert [row["file_id"] for row in table.rows] == ["10", "30"]


# ---------------------------------------------------------------------------
# Feedback round (PR #870 swarm-pr-review pr870-20261009 + out-of-band review):
# coverage and hardening pins below the frozen acceptance set.
# ---------------------------------------------------------------------------


def test_env_bool_tokens_match_pydantic_bool(tmp_path, monkeypatch):
    """PRR-005: the script's env-bool parser must agree with the app's
    pydantic-settings bool coercion, or 'auto' resolves opposite to the
    config it mirrors (the T1-28-K-06 class via a spelling)."""
    from pydantic import TypeAdapter

    coerce = TypeAdapter(bool)
    for token in ("1", "true", "yes", "on", "t", "y", "TRUE", "True", "On", "Y"):
        monkeypatch.setenv("PRR005_TOKEN", token)
        assert reconcile._env_bool("PRR005_TOKEN", False) is coerce.validate_python(
            token
        ), token
    for token in ("0", "false", "no", "off", "f", "n", "FALSE", "No"):
        monkeypatch.setenv("PRR005_TOKEN", token)
        assert reconcile._env_bool("PRR005_TOKEN", True) is coerce.validate_python(
            token
        ), token


def test_env_true_aliases_keep_stale_multiscale_noop(tmp_path, monkeypatch, capsys):
    """E06: the env-set branch of the 'auto' resolver is behaviorally pinned —
    an app-true alias must keep --delete-stale-multiscale from deleting the
    live multi-scale rows."""
    for index, alias in enumerate(("t", "1", "T", "TRUE")):
        work = tmp_path / f"alias-{index}"
        work.mkdir()
        db_path = work / "app.db"
        _sqlite_db(db_path, [(1, 1, "a.txt", 2, "indexed")])
        table = _Table(
            [{"id": "c1", "file_id": "1", "vault_id": "1", "chunk_scale": "768"}]
        )

        async def open_chunks_table(lancedb_path):
            return table

        monkeypatch.setenv("MULTI_SCALE_INDEXING_ENABLED", alias)
        monkeypatch.setattr(reconcile, "_open_chunks_table", open_chunks_table)
        exit_code = reconcile.main(
            [
                "--sqlite-path", str(db_path),
                "--lancedb-path", str(work / "lancedb"),
                "--delete-stale-multiscale", "--confirm",
            ]
        )
        report = json.loads(capsys.readouterr().out)
        assert exit_code == 0, alias
        assert report["stale_multiscale_rows_when_disabled"]["row_count"] == 0, alias
        assert [row["file_id"] for row in table.rows] == ["1"], alias


def test_wipe_empties_lancedb_directory(tmp_path, monkeypatch, capsys):
    """E02: a successful reset leaves the LanceDB directory present and
    empty (the wipe actually happened)."""
    db_path = tmp_path / "app.db"
    run_migrations(str(db_path))
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """INSERT INTO files (vault_id, file_path, file_name, file_hash,
               file_size, chunk_count, status, phase)
               VALUES (1, 'a.pdf', 'a.pdf', 'h1', 10, 2, 'indexed', 'queued')"""
        )
        conn.commit()
    finally:
        conn.close()
    lancedb_dir = tmp_path / "lancedb"
    lancedb_dir.mkdir()
    (lancedb_dir / "chunks.lance").write_text("vectors", encoding="utf-8")
    monkeypatch.setattr(
        reset_embeddings,
        "settings",
        types.SimpleNamespace(sqlite_path=db_path, lancedb_path=lancedb_dir),
    )

    reset_embeddings.reset_all_embeddings()

    assert lancedb_dir.is_dir()
    assert list(lancedb_dir.iterdir()) == []


def test_reset_boundary_error_cancelled_and_column_values(tmp_path, monkeypatch):
    """E03: 'error'/'cancelled' rows are untouched (status AND columns), and
    reset rows land on the full queue-entry signal."""
    db_path = tmp_path / "app.db"
    run_migrations(str(db_path))
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """INSERT INTO files (vault_id, file_path, file_name, file_hash,
               file_size, chunk_count, status, phase, partial_embeddings,
               chunks_failed, error_message)
               VALUES (1, 'a.pdf', 'a.pdf', 'h1', 10, 2, 'indexed', 'queued',
                       1, 1, 'boom')"""
        )
        conn.execute(
            """INSERT INTO files (vault_id, file_path, file_name, file_hash,
               file_size, chunk_count, status, phase, partial_embeddings,
               chunks_failed, error_message)
               VALUES (1, 'e.pdf', 'e.pdf', 'h2', 10, 3, 'error', NULL,
                       1, 3, 'kaput')"""
        )
        conn.execute(
            """INSERT INTO files (vault_id, file_path, file_name, file_hash,
               file_size, chunk_count, status, phase)
               VALUES (1, 'x.pdf', 'x.pdf', 'h3', 10, 1, 'cancelled', NULL)"""
        )
        conn.commit()
    finally:
        conn.close()
    lancedb_dir = tmp_path / "lancedb"
    lancedb_dir.mkdir()
    monkeypatch.setattr(
        reset_embeddings,
        "settings",
        types.SimpleNamespace(sqlite_path=db_path, lancedb_path=lancedb_dir),
    )

    reset_embeddings.reset_all_embeddings()

    conn = sqlite3.connect(db_path)
    try:
        rows = {
            name: tuple(row)
            for name, *row in conn.execute(
                "SELECT file_name, status, phase, chunk_count,"
                " partial_embeddings, chunks_failed, error_message FROM files"
            )
        }
    finally:
        conn.close()
    # Terminal rows keep every column: the reset must not touch them.
    assert rows["e.pdf"] == ("error", None, 3, 1, 3, "kaput")
    assert rows["x.pdf"] == ("cancelled", None, 1, 0, 0, None)
    # Reset rows land on the queue-entry signal with clean metadata.
    assert rows["a.pdf"] == ("pending", "queued", 0, 0, 0, None)


def test_all_protected_plan_dry_run_exits_zero(tmp_path, monkeypatch, capsys):
    """E04: an all-protected candidate set is a zero-effect plan — exit 0
    without --confirm, with the protection visible in the report. The
    candidate must be 'pending' (a 'partial' file is live and never an
    orphan candidate)."""
    db_path = tmp_path / "app.db"
    _sqlite_db(db_path, [(10, 1, "p.txt", 1, "pending")])
    table = _Table(
        [{"id": "c10", "file_id": "10", "vault_id": "1", "chunk_scale": "default"}]
    )

    async def open_chunks_table(lancedb_path):
        return table

    monkeypatch.setattr(reconcile, "_open_chunks_table", open_chunks_table)
    exit_code = reconcile.main(
        [
            "--sqlite-path", str(db_path),
            "--lancedb-path", str(tmp_path / "lancedb"),
            "--delete-orphan-file-ids",
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["cleanup_plan"]["requires_confirm"] is False
    assert report["cleanup_plan"]["planned_delete_counts"] == {}
    assert report["cleanup_plan"]["plan_protected_orphan_ids"] == ["10"]
    assert [row["file_id"] for row in table.rows] == ["10"]


@pytest.mark.parametrize("flipped_status", ["indexed", "partial"])
def test_recheck_catches_status_flip_to_protected_mid_run(
    tmp_path, monkeypatch, capsys, flipped_status
):
    """E10: a candidate whose status flips INTO a protected value after the
    first SQLite read must survive — this bites a mutant that drops
    'indexed' or 'partial' from DELETE_PROTECTED_STATUSES."""
    db_path = tmp_path / "app.db"
    _sqlite_db(db_path, [(1, 1, "a.txt", 1, "indexed"), (40, 1, "f.txt", 0, "processing")])
    table = _Table(
        [
            {"id": "c1", "file_id": "1", "vault_id": "1", "chunk_scale": "default"},
            {"id": "c40", "file_id": "40", "vault_id": "1", "chunk_scale": "default"},
        ]
    )

    async def open_chunks_table(lancedb_path):
        # File 40 finishes its ingest (processing -> protected status) after
        # reconcile read SQLite but before the cleanup layer runs.
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE files SET status = ? WHERE id = 40", (flipped_status,)
            )
            conn.commit()
        finally:
            conn.close()
        return table

    monkeypatch.setattr(reconcile, "_open_chunks_table", open_chunks_table)
    exit_code = reconcile.main(
        [
            "--sqlite-path", str(db_path),
            "--lancedb-path", str(tmp_path / "lancedb"),
            "--delete-orphan-file-ids", "--confirm",
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["cleanup_plan"]["plan_protected_orphan_ids"] == ["40"]
    assert report["cleanup_plan"]["planned_delete_counts"] == {}
    assert [row["file_id"] for row in table.rows] == ["1", "40"]


def test_vault_delete_skipped_while_vault_ingesting(tmp_path, monkeypatch, capsys):
    """PRR-002: --delete-orphan-vault refuses to run against a vault that
    still owns a mid-ingest ('processing') file; the skip is visible in the
    report and no rows are deleted."""
    db_path = tmp_path / "app.db"
    _sqlite_db(db_path, [(7, 7, "busy.txt", 1, "processing")])
    table = _Table(
        [{"id": "c7", "file_id": "7", "vault_id": "7", "chunk_scale": "default"}]
    )

    async def open_chunks_table(lancedb_path):
        return table

    monkeypatch.setattr(reconcile, "_open_chunks_table", open_chunks_table)
    exit_code = reconcile.main(
        [
            "--sqlite-path", str(db_path),
            "--lancedb-path", str(tmp_path / "lancedb"),
            "--delete-orphan-vault", "7", "--confirm",
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["cleanup_plan"]["vault_delete_skipped_in_flight"] is True
    assert report["cleanup_plan"]["planned_delete_counts"] == {}
    assert [row["file_id"] for row in table.rows] == ["7"]


def test_rollback_restore_survives_crash_sidecars(tmp_path):
    """PRR-001 regression: a crash-style stale WAL pair (committed frames in
    -wal, writer never closed) beside the live database must not resurrect
    the pre-restore content over the restored backup."""
    crash_script = (
        "import sqlite3, sys, os\n"
        "c = sqlite3.connect(sys.argv[1])\n"
        "c.execute('PRAGMA journal_mode=WAL')\n"
        "c.execute('CREATE TABLE resurrected (v TEXT)')\n"
        "c.execute(\"INSERT INTO resurrected VALUES ('OLD-CONTENT')\")\n"
        "c.commit()\n"
        "os._exit(0)\n"
    )
    stale_dir = tmp_path / "stale"
    stale_dir.mkdir()
    stale_db = stale_dir / "app.db"
    subprocess.run([sys.executable, "-c", crash_script, str(stale_db)], check=True)
    assert (stale_dir / "app.db-wal").exists(), "expected crash-style sidecars"

    live_db = tmp_path / "app.db"
    shutil.copyfile(stale_db, live_db)
    shutil.copyfile(stale_dir / "app.db-wal", str(live_db) + "-wal")

    backup_db = tmp_path / "backup.db"
    bconn = sqlite3.connect(backup_db)
    try:
        bconn.execute("CREATE TABLE restored (v TEXT)")
        bconn.execute("INSERT INTO restored VALUES ('NEW-CONTENT')")
        bconn.commit()
    finally:
        bconn.close()

    migrate_memories.decrypt_backup(backup_db, live_db)
    assert migrate_memories._force_clear_maintenance(str(live_db)) is True

    conn = sqlite3.connect(live_db)
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        restored_rows = conn.execute("SELECT v FROM restored").fetchall()
    finally:
        conn.close()
    assert restored_rows == [("NEW-CONTENT",)]
    assert "resurrected" not in tables
