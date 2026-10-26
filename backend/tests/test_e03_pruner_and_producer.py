"""Workstream E (#727/#728 backup-restore rework) — pruner and producer
regression tests (05-fix-plan.md, Selected Fix parts 3-4 / Test Plan item 2):
the documented pruner CLI end to end from the repo root (set pruning + live
tag release + optimizable store), its guards (symlink skip, malformed
manifest skip, negative retention), opt-in orphan-tag reconciliation
(fail-closed census, (table, tag) keying), and the producer's staging build
(tag rollback, evidence retention on rollback failure, empty-output
replacement, same-second unique file names).
"""

import fnmatch
import gc
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import lancedb  # noqa: E402
from scripts.backup_set import BackupError, create_backup_set  # noqa: E402
from scripts.cleanup_backups import cleanup_backups  # noqa: E402

from scripts import backup_set as backup_set_module  # noqa: E402
from scripts import backup_sqlite as backup_sqlite_module  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_KEY = b"0123456789abcdef0123456789abcdef"  # 32 bytes
CHILD_AES_KEY = "0123456789abcdef0123456789abcdef"
DAY = 24 * 3600
FROZEN_NOW = datetime(2027, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
TABLE = "chunks"


def _key_provider():
    return (TEST_KEY, "9")


def _seed_db(path: Path, rows: int = 3, val_prefix: str = "row") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
        for i in range(1, rows + 1):
            conn.execute("INSERT INTO t VALUES (?, ?)", (i, f"{val_prefix}-{i}"))
        conn.commit()
    finally:
        conn.close()


def _rows(n: int, label: str) -> list:
    return [{"chunk_id": i, "text": f"{label}-{i}"} for i in range(1, n + 1)]


def _tag_names(table) -> list:
    listed = table.tags.list()
    return list(listed.keys()) if isinstance(listed, dict) else list(listed)


def _age_tree(root: Path, age_s: float) -> None:
    aged = time.time() - age_s
    paths = [root, *root.rglob("*")] if root.is_dir() else [root]
    for path in paths:
        os.utime(path, (aged, aged))


def _future_tree(root: Path) -> None:
    """A future mtime keeps a set OUTSIDE every retention window (even
    retention 0, under which any past mtime is already expired)."""
    aged = time.time() + 3600
    paths = [root, *root.rglob("*")] if root.is_dir() else [root]
    for path in paths:
        os.utime(path, (aged, aged))


def _write_manifest_set(set_dir: Path, table_tags: list) -> Path:
    """A hand-written set directory binding the given (table, tag) pairs."""
    set_dir.mkdir(parents=True, exist_ok=True)
    item = {
        "name": "lancedb",
        "kind": "lancedb",
        "path": "lancedb",
        "tables": [
            {"table": table, "tag": tag} for table, tag in table_tags
        ],
    }
    manifest = set_dir / "backup_manifest.json"
    manifest.write_text(
        json.dumps({"schema": "ragapp-backup-set/1", "items": [item]}),
        encoding="utf-8",
    )
    return manifest


def _make_table(lancedb_dir: Path, name: str = TABLE):
    return lancedb.connect(str(lancedb_dir)).create_table(name, _rows(2, name))


_SYMLINK_DIRS = None


def _dir_symlinks_supported() -> bool:
    """Directory symlinks need admin/Developer Mode on Windows; probe once."""
    global _SYMLINK_DIRS  # noqa: PLW0603 — module-level cache
    if _SYMLINK_DIRS is None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            link = Path(tmp) / "link"
            try:
                os.symlink(target, link, target_is_directory=True)
                _SYMLINK_DIRS = bool(link.is_symlink())
            except (OSError, NotImplementedError):
                _SYMLINK_DIRS = False
    return _SYMLINK_DIRS


def test_pruner_cli_runs_from_repo_root(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    _seed_db(data / "app.db")
    lancedb_dir = data / "lancedb"
    table = _make_table(lancedb_dir)
    backups = tmp_path / "backups"
    backups.mkdir()
    set_dir = backups / "set-1"

    monkeypatch.setattr(backup_set_module.settings, "data_dir", data)
    create_backup_set(
        set_dir,
        lancedb_dir=lancedb_dir,
        lancedb_tables={TABLE: table},
        key_provider=_key_provider,
    )
    # Writes past the tagged generation, so newer versions exist on the live
    # table when the retention window closes (optimize must stay possible).
    table.add(_rows(3, "post-backup"))
    del table
    gc.collect()  # release parent-process handles before the child locks it
    _age_tree(set_dir, 60)

    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() != "PYTHONPATH"
    }
    env["DATA_DIR"] = str(data)
    env["AES_KEY"] = CHILD_AES_KEY
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/cleanup_backups.py",
            "--directory",
            str(backups),
            "--retention",
            "0",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, (
        "E-03 PRUNER CLI: `python scripts/cleanup_backups.py --directory "
        f"<dir> --retention 0` from the repo root (no PYTHONPATH) exited "
        f"{proc.returncode}:\n{proc.stderr[-1500:]}"
    )
    assert not set_dir.exists(), (
        "E-03 PRUNER CLI: the expired set directory survived the documented "
        "CLI run"
    )

    fresh = lancedb.connect(str(lancedb_dir)).open_table(TABLE)
    leftover = [n for n in _tag_names(fresh) if n.startswith("backup-")]
    assert leftover == [], (
        f"E-03 PRUNER CLI: the pruned set's live tags survived ({leftover})"
    )
    # With the tag released the periodic optimize runs clean (a pinned tag
    # raises "Cleanup error: tagged version(s) marked for cleanup").
    fresh.optimize(cleanup_older_than=timedelta(0))


@pytest.mark.skipif(
    not _dir_symlinks_supported(),
    reason="directory symlinks require privileges on this Windows host",
)
def test_pruner_skips_symlinked_set(tmp_path, monkeypatch):
    monkeypatch.setattr(backup_set_module.settings, "data_dir", tmp_path / "data")
    backups = tmp_path / "backups"
    backups.mkdir()
    target = tmp_path / "real-set-elsewhere"
    manifest = _write_manifest_set(target, [(TABLE, "backup-sym")])
    _age_tree(target, 40 * DAY)
    link = backups / "set-link"
    os.symlink(target, link, target_is_directory=True)

    removed = cleanup_backups(backups, retention_days=0)

    assert link.is_symlink(), (
        "E-03 PRUNER SYMLINK: the symlinked set candidate was replaced or "
        "removed — a set candidate that is a symlink must be SKIPPED"
    )
    assert manifest.exists(), (
        "E-03 PRUNER SYMLINK: the symlink TARGET was pruned through the link"
    )
    assert removed == []


def test_pruner_malformed_manifest_keeps_set(tmp_path, monkeypatch):
    monkeypatch.setattr(backup_set_module.settings, "data_dir", tmp_path / "data")
    backups = tmp_path / "backups"
    backups.mkdir()
    set_dir = backups / "set-old"
    set_dir.mkdir()
    manifest = set_dir / "backup_manifest.json"
    manifest.write_text("{corrupt json", encoding="utf-8")
    _age_tree(set_dir, 40 * DAY)
    legacy = backups / "db_20200101_000000_k1.enc"
    legacy.write_bytes(b"legacy-backup")
    _age_tree(legacy, 40 * DAY)

    cleanup_backups(backups, retention_days=0)

    assert set_dir.exists(), (
        "E-03 PRUNER MALFORMED MANIFEST: the expired set with a corrupt "
        "manifest was deleted — a set whose tags cannot be known must be "
        "KEPT (fail closed)"
    )
    assert not legacy.exists(), (
        "E-03 PRUNER MALFORMED MANIFEST: legacy db_*_k*.enc pruning was "
        "affected by the malformed set"
    )


def test_pruner_negative_retention_raises(tmp_path):
    with pytest.raises(ValueError):
        cleanup_backups(tmp_path, retention_days=-1)


def test_orphan_reconciliation_fail_closed(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    lancedb_dir = data / "lancedb"
    table = _make_table(lancedb_dir)
    table.tags.create("backup-tag-A", 1)
    table.tags.create("backup-orphan-live", 1)
    monkeypatch.setattr(backup_set_module.settings, "data_dir", data)

    backups = tmp_path / "backups"
    backups.mkdir()
    remaining = backups / "set-remaining"
    remaining.mkdir()
    corrupt = remaining / "backup_manifest.json"
    corrupt.write_text("{corrupt json", encoding="utf-8")
    _future_tree(remaining)
    expired = backups / "set-expired"
    _write_manifest_set(expired, [(TABLE, "backup-tag-A")])
    _age_tree(expired, 60)

    cleanup_backups(backups, retention_days=0, release_orphan_tags=True)

    assert not expired.exists(), (
        "E-03 ORPHAN CENSUS: the expired VALID set was not pruned — the "
        "reconciliation failure must not affect ordinary set handling"
    )
    assert remaining.exists()
    tags = _tag_names(table)
    assert "backup-orphan-live" in tags, (
        "E-03 ORPHAN CENSUS NOT FAIL-CLOSED: an unreferenced live tag was "
        "deleted even though a remaining set's manifest could not be read — "
        "the whole reconciliation phase must abort"
    )
    assert "backup-tag-A" not in tags, (
        "E-03 ORPHAN CENSUS: the pruned set's tag was not released"
    )


def test_orphan_releases_unreferenced_tags(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    lancedb_dir = data / "lancedb"
    table = _make_table(lancedb_dir)
    table.tags.create("backup-tag-A", 1)
    table.tags.create("backup-orphan-B", 1)
    monkeypatch.setattr(backup_set_module.settings, "data_dir", data)

    backups = tmp_path / "backups"
    backups.mkdir()
    remaining = backups / "set-remaining"
    _write_manifest_set(remaining, [(TABLE, "backup-tag-A")])
    _future_tree(remaining)

    cleanup_backups(backups, retention_days=0, release_orphan_tags=False)
    tags_off = _tag_names(table)
    assert "backup-orphan-B" in tags_off, (
        "E-03 ORPHAN OPT-IN: unreferenced tags were deleted with the flag OFF"
    )
    assert "backup-tag-A" in tags_off

    cleanup_backups(backups, retention_days=0, release_orphan_tags=True)
    tags_on = _tag_names(table)
    assert "backup-orphan-B" not in tags_on, (
        "E-03 ORPHAN RELEASE: the unreferenced live tag survived the opt-in "
        "reconciliation"
    )
    assert "backup-tag-A" in tags_on, (
        "E-03 ORPHAN RELEASE: a REFERENCED live tag was deleted"
    )


def test_same_tag_name_two_tables_keyed(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    lancedb_dir = data / "lancedb"
    t1 = _make_table(lancedb_dir, "t1")
    t2 = _make_table(lancedb_dir, "t2")
    t1.tags.create("backup-x", 1)
    t2.tags.create("backup-x", 1)
    monkeypatch.setattr(backup_set_module.settings, "data_dir", data)

    backups = tmp_path / "backups"
    backups.mkdir()
    remaining = backups / "set-remaining"
    _write_manifest_set(remaining, [("t1", "backup-x")])
    _future_tree(remaining)

    cleanup_backups(backups, retention_days=0, release_orphan_tags=True)

    assert "backup-x" in _tag_names(t1), (
        "E-03 TAG KEYING: the REFERENCED (t1, backup-x) tag was deleted"
    )
    assert "backup-x" not in _tag_names(t2), (
        "E-03 TAG KEYING: the unreferenced (t2, backup-x) tag survived — "
        "references are keyed (table, tag); the same tag name on two tables "
        "must never be conflated"
    )


def test_producer_tag_rollback_failure_keeps_staging(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    _seed_db(data / "app.db")
    lancedb_dir = data / "lancedb"
    table = _make_table(lancedb_dir)
    monkeypatch.setattr(backup_set_module.settings, "data_dir", data)

    # (a) a copy fault AFTER tags were created rolls the tags back
    # automatically and removes the staging tree.
    def faulting_copytree(src, dst, *args, **kwargs):
        raise OSError("E-03 injected copy fault after tagging")

    monkeypatch.setattr(shutil, "copytree", faulting_copytree)
    with pytest.raises(OSError):
        create_backup_set(
            tmp_path / "out-a",
            lancedb_dir=lancedb_dir,
            lancedb_tables={TABLE: table},
            key_provider=_key_provider,
        )
    assert [n for n in _tag_names(table) if n.startswith("backup-")] == [], (
        "E-03 PRODUCER TAG ROLLBACK: the tags this run created survived the "
        "failed backup"
    )
    assert not list(tmp_path.glob("out-a.partial-*")), (
        "E-03 PRODUCER TAG ROLLBACK: staging survived a rollback that "
        "succeeded (evidence retention applies only when rollback FAILS)"
    )

    # (b) when the rollback itself fails, the staging directory is KEPT as
    # evidence and the original error propagates.
    def broken_delete(table_obj, tag):
        raise BackupError("E-03 injected tag-rollback failure")

    monkeypatch.setattr(backup_set_module, "_delete_tag", broken_delete)
    with pytest.raises(OSError):
        create_backup_set(
            tmp_path / "out-b",
            lancedb_dir=lancedb_dir,
            lancedb_tables={TABLE: table},
            key_provider=_key_provider,
        )
    partials = list(tmp_path.glob("out-b.partial-*"))
    assert len(partials) == 1, (
        "E-03 PRODUCER EVIDENCE RETENTION: a failed tag rollback must KEEP "
        f"the staging directory, found {[p.name for p in partials]}"
    )
    live = [n for n in _tag_names(table) if n.startswith("backup-")]
    assert len(live) == 1, (
        "E-03 PRODUCER EVIDENCE RETENTION: the tag that could not be "
        "released must stay visible for manual removal"
    )


def test_precreated_empty_output_replaced(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    _seed_db(src / "app.db")
    out = tmp_path / "out"
    out.mkdir()  # the admin-guide wrapper's pre-created (empty) directory
    monkeypatch.setattr(backup_set_module.settings, "data_dir", src)

    manifest = create_backup_set(out, key_provider=_key_provider)

    assert (out / "backup_manifest.json").exists(), (
        "E-03 EMPTY OUTPUT: the backup did not complete into the pre-created "
        "empty output directory"
    )
    assert manifest.exists()


class _FrozenDatetime(datetime):
    """datetime subclass whose now() always returns FROZEN_NOW, so two runs
    land in the same wall-clock second (the e01 frozen-C7 pattern)."""

    @classmethod
    def now(cls, tz=None):
        return FROZEN_NOW


def test_backup_sqlite_names_unique_same_second(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    _seed_db(src / "app.db")
    out = tmp_path / "backups"
    monkeypatch.setattr(backup_set_module.settings, "data_dir", src)
    monkeypatch.setenv("AES_KEY", CHILD_AES_KEY)
    monkeypatch.setattr(backup_sqlite_module, "datetime", _FrozenDatetime)

    first = backup_sqlite_module.backup_sqlite(out)
    second = backup_sqlite_module.backup_sqlite(out)

    assert first != second, (
        f"E-03 SAME-SECOND NAMES: two backup_sqlite runs in one wall-clock "
        f"second produced the same file ({first.name})"
    )
    names = sorted(p.name for p in out.iterdir())
    assert len(names) == 2
    assert all(fnmatch.fnmatch(name, "db_*_k*.enc") for name in names), (
        f"E-03 SAME-SECOND NAMES: the uniquified names must still match the "
        f"pruner's legacy glob db_*_k*.enc, got {names}"
    )


def test_migrate_fallback_names_unique_same_second(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    _seed_db(src / "app.db")
    out = tmp_path / "backups"
    # Import under a controlled DATA_DIR so the module's import-time
    # os.environ.setdefault cannot leak the repo data path into this process.
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("DATA_DIR", str(tmp_path))
        from scripts import migrate_memories as migrate_module  # noqa: F401
    monkeypatch.setattr(migrate_module.settings, "data_dir", src)
    # The fallback reads time.strftime at call time; freeze it so both runs
    # land in the same wall-clock second.
    monkeypatch.setattr(
        migrate_module.time, "strftime", lambda fmt: "20270101_120000"
    )

    first = migrate_module.backup_sqlite_fallback(out)
    second = migrate_module.backup_sqlite_fallback(out)

    assert first.exists() and second.exists()
    assert first != second, (
        f"E-03 SAME-SECOND NAMES: two migrate_memories fallback runs in one "
        f"wall-clock second produced the same file ({first.name})"
    )
    assert sorted(p.name for p in out.iterdir()) == sorted(
        [first.name, second.name]
    )
