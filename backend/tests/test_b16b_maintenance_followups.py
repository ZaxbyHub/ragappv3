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
import sqlite3
import sys
import types
from pathlib import Path

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
# status filter is a recurrence of T1-28-S-09/T1-28-S-11.
# ---------------------------------------------------------------------------


def test_no_indexed_only_status_filters_in_operator_scripts():
    pattern = re.compile(r"status\s*=\s*'indexed'")
    hits = []
    root = Path(__file__).resolve().parents[2]
    for rel in ("scripts", "backend/scripts"):
        for path in sorted((root / rel).glob("*.py")):
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if pattern.search(line):
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
