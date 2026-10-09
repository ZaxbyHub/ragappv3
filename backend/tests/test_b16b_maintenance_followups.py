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
