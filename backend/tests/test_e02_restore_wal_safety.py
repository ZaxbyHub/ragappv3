"""Issue #728 acceptance checks AC3-AC4 (Workstream E, PR 2) — restoring a
database must not leave the destination's stale SQLite WAL to be replayed
over the restored bytes.

RED at base (HEAD 46e99488, no fix) for AC3: restore_backup_set overwrites
``dest/app.db`` with raw bytes (``write_bytes``) and never touches the
``-wal``/``-shm`` siblings, so the first open after restore replays the
previous database's committed-but-uncheckpointed WAL — audit probe t1-28-s-p4
recorded ('post-backup-live', 50) surviving the restore.

AC4 (legacy scripts/migrate_memories.py decrypt_backup): the issue was
authored against af4914d6, where both branches raw-copied over the target;
at this base the #705 staged replace already unlinks the sidecars, so this
check guards the WAL-safety property (it must stay GREEN through the E-02
rework, which must not regress it).

GREEN after the fix: a fresh SQLite open of the restored/target database
shows none of the stale WAL's rows.
"""

import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.backup_set import create_backup_set  # noqa: E402
from scripts.restore import restore_backup_set  # noqa: E402

from scripts import backup_set as backup_set_module  # noqa: E402

TEST_KEY = b"0123456789abcdef0123456789abcdef"  # 32 bytes
STALE_WAL_ROWS = 50


def _key_provider():
    return (TEST_KEY, "9")


def _seed_source_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE markers(marker TEXT)")
        conn.execute("INSERT INTO markers VALUES ('backup_marker')")
        conn.commit()
    finally:
        conn.close()


def _live_rows(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM markers WHERE marker = 'post-backup-live'"
        ).fetchone()[0]
    finally:
        conn.close()


def _prepare_stale_wal_dest(dest: Path, rows: int = STALE_WAL_ROWS) -> None:
    """Give ``dest`` an app.db whose committed rows live only in an
    uncheckpointed ``-wal``, then leave that WAL in place: a read
    transaction pins the WAL while the sidecars are copied aside, every
    connection is closed (SQLite may checkpoint/delete them), and the saved
    uncheckpointed copies are put back."""
    dest.mkdir(parents=True, exist_ok=True)
    db = dest / "app.db"
    aside_wal = dest.parent / "aside-app.db-wal"
    aside_shm = dest.parent / "aside-app.db-shm"

    writer = sqlite3.connect(str(db))
    pin = sqlite3.connect(str(db))
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE markers(marker TEXT)")
        writer.commit()

        # The open read transaction keeps committed pages out of the main
        # file (a live app / a write held open).
        pin.execute("BEGIN")
        pin.execute("SELECT COUNT(*) FROM markers").fetchone()

        for _ in range(rows):
            writer.execute("INSERT INTO markers VALUES ('post-backup-live')")
        writer.commit()

        shutil.copy2(dest / "app.db-wal", aside_wal)
        shm = dest / "app.db-shm"
        if shm.exists():
            shutil.copy2(shm, aside_shm)
    finally:
        writer.close()
        pin.close()

    # Put the uncheckpointed sidecars back: dest now holds a stale -wal
    # (and -shm) next to app.db, exactly what a crashed live app leaves.
    shutil.copy2(aside_wal, dest / "app.db-wal")
    if aside_shm.exists():
        shutil.copy2(aside_shm, dest / "app.db-shm")


def test_stale_wal_is_not_replayed_over_restored_db(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    _seed_source_db(src / "app.db")

    out = tmp_path / "set"
    monkeypatch.setattr(backup_set_module.settings, "data_dir", src)
    create_backup_set(out, key_provider=_key_provider)

    dest = tmp_path / "dest"
    _prepare_stale_wal_dest(dest)
    assert (dest / "app.db-wal").exists(), (
        "E-02-AC3 HARNESS: no stale -wal was staged in the destination"
    )

    restore_backup_set(out, dest, key_provider=_key_provider)

    live = _live_rows(dest / "app.db")
    assert live == 0, (
        "E-02-AC3 STALE WAL REPLAYED: a fresh open of <dest>/app.db after "
        f"restore reports {live} 'post-backup-live' rows from the stale "
        "app.db-wal — restore overwrote the main file's bytes but left "
        "the -wal/-shm siblings in place, so SQLite replays the previous "
        "database over the restored one"
    )


def test_legacy_decrypt_backup_is_wal_safe(tmp_path):
    # A plain (unencrypted) SQLite backup file: migrate_memories.decrypt_backup
    # takes its copy-file branch for these.
    backup_path = tmp_path / "plain-backup.db"
    _seed_source_db(backup_path)
    assert backup_path.read_bytes()[:16] == b"SQLite format 3\x00"

    dest = tmp_path / "dest"
    _prepare_stale_wal_dest(dest)
    target = dest / "app.db"

    # Import under a controlled DATA_DIR so the module's import-time
    # os.environ.setdefault cannot leak the repo data path into this process.
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("DATA_DIR", str(tmp_path))
        from scripts import migrate_memories

        migrate_memories.decrypt_backup(backup_path, target)

    live = _live_rows(target)
    assert live == 0, (
        "E-02-AC4 LEGACY DECRYPT NOT WAL-SAFE: after "
        "scripts/migrate_memories.py decrypt_backup(backup_path, "
        f"target_path) a fresh open of the target reports {live} "
        "'post-backup-live' rows from the stale -wal — the restore must "
        "not leave the target's WAL sidecars to replay the old database"
    )
