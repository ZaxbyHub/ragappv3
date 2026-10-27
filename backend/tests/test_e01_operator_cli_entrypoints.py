"""Issue #727 acceptance checks AC1-AC3 (Workstream E, PR 1) — the operator
backup/restore/migration CLIs must run exactly as docs/operations.md
documents them: from the repo root, with no PYTHONPATH.

RED at base (HEAD 46e99488, no fix): each documented script appends only the
repo root to sys.path and imports ``backend.app.config`` (which itself
imports ``app.*``, resolvable only with ``backend/`` on the path), or imports
``app.*`` directly — so ``python <script> --help`` exits 1 with
ModuleNotFoundError, runpy-loading ``scripts/reset_embeddings.py`` fails the
same way, and the documented backup/restore round trip dies at the import
before any backup is produced. GREEN after the fix: all five documented
CLIs answer ``--help`` with exit 0 from the repo root, reset_embeddings.py
imports cleanly without running its main block, and the documented round
trip against a default-layout DATA_DIR (seeded app.db, no draft-room dir)
completes with exit 0 and restores the seeded rows.

The subprocess checks remove PYTHONPATH from the CHILD environment dict (not
merely from the parent shell) and set DATA_DIR/AES_KEY only in the child env
— the in-process settings singleton is never mutated.
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

DOCUMENTED_CLI_SCRIPTS = [
    "scripts/restore.py",
    "scripts/backup_set.py",
    "scripts/backup_sqlite.py",
    "scripts/migrate_memories.py",
    "backend/scripts/vector_search_bypass_index_diagnostic.py",
]

# A 32-character AES key served by SecretManager (AES_KEY env) to BOTH child
# processes so the backup's AES-GCM artifact decrypts on restore.
CHILD_AES_KEY = "0123456789abcdef0123456789abcdef"

SEED_ROWS = 5


def _child_env(**overrides: object) -> dict:
    """A copy of os.environ with PYTHONPATH removed (case-insensitive).

    The documented invocation runs with no PYTHONPATH; dropping it from the
    child env dict is what makes the import-path defect observable.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() != "PYTHONPATH"
    }
    for key, value in overrides.items():
        env[key] = str(value)
    return env


def _row_count(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    finally:
        conn.close()


@pytest.mark.parametrize("script", DOCUMENTED_CLI_SCRIPTS)
def test_documented_cli_help_runs_from_repo_root(script):
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / script), "--help"],
        cwd=str(REPO_ROOT),
        env=_child_env(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        "E-01-AC1 DOCUMENTED CLI IMPORT: `python "
        f"{script} --help` from the repo root with PYTHONPATH removed "
        f"exited {proc.returncode}:\n{proc.stderr[-1500:]}"
    )


def test_reset_embeddings_imports_from_repo_root():
    # The script has no argparse (--help would RUN the reset), so the check
    # only loads it without executing the __main__ block.
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy; runpy.run_path("
            "'scripts/reset_embeddings.py', run_name='not_main')",
        ],
        cwd=str(REPO_ROOT),
        env=_child_env(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        "E-01-AC2 RESET_EMBEDDINGS IMPORT: runpy-loading "
        "scripts/reset_embeddings.py from the repo root with PYTHONPATH "
        f"removed exited {proc.returncode}:\n{proc.stderr[-1500:]}"
    )


def test_documented_backup_restore_round_trip_runs_as_written(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    source_db = data_dir / "app.db"
    conn = sqlite3.connect(str(source_db))
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
        for i in range(1, SEED_ROWS + 1):
            conn.execute("INSERT INTO t VALUES (?, ?)", (i, f"row-{i}"))
        conn.commit()
    finally:
        conn.close()
    # Default layout: a seeded app.db and NO draft-room directory — exactly
    # what a fresh deployment has when the documented cron line first runs.
    assert not (data_dir / "draft-room").exists()

    out_dir = tmp_path / "backup-set"
    dest_dir = tmp_path / "restored"
    env = _child_env(DATA_DIR=data_dir, AES_KEY=CHILD_AES_KEY)

    backup = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "backup_set.py"),
            "--output",
            str(out_dir),
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert backup.returncode == 0, (
        "E-01-AC3 DOCUMENTED BACKUP: `python scripts/backup_set.py "
        f"--output <dir>` from the repo root exited {backup.returncode}:\n"
        f"{backup.stderr[-1500:]}"
    )

    restore = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "restore.py"),
            str(out_dir),
            "--dest",
            str(dest_dir),
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert restore.returncode == 0, (
        "E-01-AC3 DOCUMENTED RESTORE: `python scripts/restore.py <dir> "
        f"--dest <dest>` from the repo root exited {restore.returncode}:\n"
        f"{restore.stderr[-1500:]}"
    )

    restored_db = dest_dir / "app.db"
    assert restored_db.exists(), "E-01-AC3 ROUND TRIP: <dest>/app.db missing"
    assert _row_count(restored_db) == _row_count(source_db), (
        "E-01-AC3 ROUND TRIP: seeded table row count in <dest>/app.db "
        f"({ _row_count(restored_db)}) differs from the source "
        f"({_row_count(source_db)})"
    )
