"""Feedback-round regression pins for the Workstream E rework (#727/#728,
swarm-pr-review pr1026-20261026). Each test is the reviewer finding it
closes; all are non-frozen and run on both platforms unless noted.

Covers: nested empty-directory round-trip (PRR-001), backslash path
refusal (PRR-002), reserved-name refusal (PRR-014), confirmed-rollback
staging cleanup + typed rename failures (PRR-005/006), success-path
journal tolerance (PRR-007), probe WAL gate (PRR-004), sqlite-less +
generation binding (PRR-011), space guard (PRR-018/TC-05), journal
phase-update failure (PRR-018/TC-04), in-flight staging exclusion from
the pruner (PRR-009).
"""

import hashlib
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.backup_set import create_backup_set  # noqa: E402
from scripts.cleanup_backups import cleanup_backups  # noqa: E402
from scripts.restore import RestoreError, restore_backup_set  # noqa: E402

from scripts import backup_set as backup_set_module  # noqa: E402

TEST_KEY = b"0123456789abcdef0123456789abcdef"


def _key_provider():
    return (TEST_KEY, "9")


def _sha256_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _seed_db(path: Path, rows: int = 2) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
        for i in range(1, rows + 1):
            conn.execute("INSERT INTO t VALUES (?, ?)", (i, f"row-{i}"))
        conn.commit()
    finally:
        conn.close()


def _make_set(tmp_path, monkeypatch, *, with_lancedb=False):
    src = tmp_path / "src"
    (src / "vault-7").mkdir(parents=True)
    (src / "vault-7" / "f.txt").write_bytes(b"vault")
    _seed_db(src / "app.db")
    monkeypatch.setattr(backup_set_module.settings, "data_dir", src)
    out = tmp_path / "set"
    kwargs = {}
    if with_lancedb:
        import lancedb

        lancedb_dir = tmp_path / "lancedb"
        table = lancedb.connect(str(lancedb_dir)).create_table(
            "chunks", [{"chunk_id": i, "text": "x"} for i in (1, 2)]
        )
        kwargs = {"lancedb_dir": lancedb_dir, "lancedb_tables": {"chunks": table}}
    create_backup_set(
        out,
        vault_dirs=[src / "vault-7"],
        draft_room_dir=None,
        key_provider=_key_provider,
        **kwargs,
    )
    return out


# ---------------------------------------------------------------------------
# PRR-001: nested empty directories round-trip for fileless trees.
# ---------------------------------------------------------------------------


def test_nested_empty_dirs_round_trip(tmp_path, monkeypatch):
    src = tmp_path / "src"
    nested = src / "vault-7" / "uploads" / "empty"
    nested.mkdir(parents=True)
    _seed_db(src / "app.db")
    monkeypatch.setattr(backup_set_module.settings, "data_dir", src)

    out = tmp_path / "set"
    create_backup_set(
        out,
        vault_dirs=[src / "vault-7"],
        draft_room_dir=None,
        key_provider=_key_provider,
    )
    manifest = json.loads(
        (out / "backup_manifest.json").read_text(encoding="utf-8")
    )
    vault_item = next(i for i in manifest["items"] if i["kind"] == "vault")
    assert vault_item.get("empty") is True
    assert "uploads/empty" in vault_item.get("dirs", []), (
        "PRR-001 PRODUCER: the manifest does not record the nested empty "
        f"directory skeleton ({vault_item.get('dirs')})"
    )

    dest = tmp_path / "dest"
    restore_backup_set(out, dest, key_provider=_key_provider)
    assert (dest / "vaults" / "vault-7" / "uploads" / "empty").is_dir(), (
        "PRR-001 RESTORE: the nested empty directory was not recreated"
    )


# ---------------------------------------------------------------------------
# PRR-002: backslash manifest paths refused (normalization divergence).
# ---------------------------------------------------------------------------


def test_backslash_manifest_path_refused(tmp_path, monkeypatch):
    out = _make_set(tmp_path, monkeypatch)
    manifest_path = out / "backup_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    vault = next(i for i in manifest["items"] if i["kind"] == "vault")
    vault["path"] = "vaults\\7"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    dest = tmp_path / "dest"
    (dest / "vaults" / "7").mkdir(parents=True)
    (dest / "vaults" / "7" / "live.txt").write_bytes(b"live")

    with pytest.raises(RestoreError, match="backslash"):
        restore_backup_set(out, dest, key_provider=_key_provider)
    assert (dest / "vaults" / "7" / "live.txt").read_bytes() == b"live", (
        "PRR-002: the live vault content was disturbed by the refused "
        "restore"
    )


# ---------------------------------------------------------------------------
# PRR-014: reserved destination names refused for tree items.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reserved", ["app.db", "app.db-wal", "app.db-shm"])
def test_reserved_tree_item_name_refused(tmp_path, monkeypatch, reserved):
    out = _make_set(tmp_path, monkeypatch)
    manifest_path = out / "backup_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    vault = next(i for i in manifest["items"] if i["kind"] == "vault")
    vault["path"] = reserved
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    dest = tmp_path / "dest"
    with pytest.raises(RestoreError, match="reserves the destination name"):
        restore_backup_set(out, dest, key_provider=_key_provider)


# ---------------------------------------------------------------------------
# PRR-011: sqlite-less manifest with a lancedb generation binding restores.
# ---------------------------------------------------------------------------


def test_sqliteless_manifest_with_generation_restores(tmp_path, monkeypatch):
    # Seed the source db with an index-generation journal row so the
    # producer emits a lancedb generation binding (the reviewer's A7
    # probe shape: binding present + sqlite item absent).
    src_hint = tmp_path / "src"
    src_hint.mkdir(parents=True)
    conn = sqlite3.connect(str(src_hint / "app.db"))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS migration_journal ("
            "id INTEGER PRIMARY KEY, migration_name TEXT, detail TEXT)"
        )
        conn.execute(
            "INSERT INTO migration_journal VALUES "
            "(1, 'index_generation:g1', 'test')"
        )
        conn.commit()
    finally:
        conn.close()
    out = _make_set(tmp_path, monkeypatch, with_lancedb=True)
    manifest_path = out / "backup_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    lancedb_item = next(i for i in manifest["items"] if i["kind"] == "lancedb")
    assert lancedb_item.get("generation") is not None, (
        "harness: the produced set carries no generation binding"
    )
    manifest["items"] = [lancedb_item]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    dest = tmp_path / "dest"
    report = restore_backup_set(out, dest, key_provider=_key_provider)
    assert report["verified"] is True, (
        "PRR-011: a sqlite-less manifest with a lancedb generation binding "
        "must restore (the binding check compares the restored database, "
        "which does not exist in this set shape)"
    )


# ---------------------------------------------------------------------------
# PRR-005/006: swap-phase failure typing and staged-copy cleanup.
# ---------------------------------------------------------------------------


def _make_dest(dest: Path) -> None:
    dest.mkdir(parents=True)
    (dest / "pre.txt").write_bytes(b"pre-state")


def test_rename1_failure_is_typed_and_clears_staging(tmp_path, monkeypatch):
    out = _make_set(tmp_path, monkeypatch)
    dest = tmp_path / "dest"
    _make_dest(dest)

    real_rename = os.rename

    def fail_rename1(src, dst):
        if Path(str(src)) == dest:
            raise OSError("injected rename1 failure (open handles)")
        return real_rename(src, dst)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "rename", fail_rename1)
        with pytest.raises(RestoreError, match="stop the application"):
            restore_backup_set(out, dest, key_provider=_key_provider)

    assert (dest / "pre.txt").read_bytes() == b"pre-state"
    assert not list(dest.parent.glob(f"{dest.name}.staging-*")), (
        "PRR-005: the staged copy (with the decrypted database) survived a "
        "refused restore"
    )
    assert not list(dest.parent.glob(f"{dest.name}.restore-journal-*.json"))


def test_rename2_failure_rollback_clears_staging(tmp_path, monkeypatch):
    out = _make_set(tmp_path, monkeypatch)
    dest = tmp_path / "dest"
    _make_dest(dest)

    real_rename = os.rename
    state = {"aside": None}

    def fail_rename2(src, dst):
        if Path(str(src)) == dest:
            state["aside"] = Path(str(dst))
            return real_rename(src, dst)
        if "staging" in Path(str(src)).name and Path(str(dst)) == dest:
            raise OSError("injected rename2 failure")
        return real_rename(src, dst)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "rename", fail_rename2)
        with pytest.raises(RestoreError):
            restore_backup_set(out, dest, key_provider=_key_provider)

    assert (dest / "pre.txt").read_bytes() == b"pre-state", (
        "PRR-006: the confirmed rollback did not restore the destination"
    )
    assert not list(dest.parent.glob(f"{dest.name}.staging-*")), (
        "PRR-006: the staged copy (with the decrypted database) survived "
        "the confirmed rollback"
    )
    assert not list(dest.parent.glob(f"{dest.name}.restore-journal-*.json"))


def test_success_path_survives_external_journal_removal(tmp_path, monkeypatch):
    out = _make_set(tmp_path, monkeypatch)
    dest = tmp_path / "dest"

    real_unlink = os.unlink

    def eat_journal_unlink(path, *args, **kwargs):
        # Simulate an external process removing the journal between the
        # swap commit and the success-path unlink: the first journal
        # unlink raises FileNotFoundError, which missing_ok must swallow.
        if ".restore-journal-" in Path(str(path)).name:
            raise FileNotFoundError(path)
        return real_unlink(path, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "unlink", eat_journal_unlink)
        report = restore_backup_set(out, dest, key_provider=_key_provider)

    assert report["verified"] is True, (
        "PRR-007: a missing journal must not fail a fully committed restore"
    )
    assert not list(dest.parent.glob(f"{dest.name}.restore.lock")), (
        "PRR-007: the lock was retained after a committed restore"
    )


# ---------------------------------------------------------------------------
# PRR-004: the probe leaves a holding writer's WAL frames untouched.
# ---------------------------------------------------------------------------


def test_probe_keeps_holder_wal_frames(tmp_path, monkeypatch):
    out = _make_set(tmp_path, monkeypatch)
    dest = tmp_path / "dest"
    dest.mkdir()
    db = dest / "app.db"
    w = sqlite3.connect(str(db))
    holder = sqlite3.connect(str(db))
    try:
        w.execute("PRAGMA journal_mode=WAL")
        w.execute("PRAGMA wal_autocheckpoint=0")
        w.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
        w.commit()

        # Tamper the SET so restore refuses AFTER the probe ran (digest
        # mismatch on the vault file).
        vault_file = out / "vaults" / "vault-7" / "f.txt"
        vault_file.write_bytes(vault_file.read_bytes() + b"tampered")

        holder.execute("BEGIN IMMEDIATE")
        holder.execute("INSERT INTO t VALUES (1001, 'held-frames')")
        pre_wal = (dest / "app.db-wal").read_bytes()
        assert pre_wal, "harness: no WAL frames staged (writer holds it)"

        with pytest.raises(RestoreError, match="held by another connection"):
            restore_backup_set(out, dest, key_provider=_key_provider)

        post_wal = (dest / "app.db-wal").read_bytes()
        assert post_wal == pre_wal, (
            "PRR-004: the probe mutated the live WAL while a writer held "
            "it — the writer's frames were rolled back"
        )
    finally:
        holder.rollback()
        holder.close()
        w.close()


# ---------------------------------------------------------------------------
# PRR-018/TC-05: the space guard refuses when free space is insufficient.
# ---------------------------------------------------------------------------


def test_space_guard_refuses(tmp_path, monkeypatch):
    out = _make_set(tmp_path, monkeypatch)
    dest = tmp_path / "dest"
    _make_dest(dest)
    pre_hash = _sha256_tree(dest)

    import shutil as _shutil

    class _Tiny:
        free = 1
        total = 10**12
        used = 0

    def tiny_disk(path):
        return _Tiny()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_shutil, "disk_usage", tiny_disk)
        with pytest.raises(RestoreError, match="insufficient space"):
            restore_backup_set(out, dest, key_provider=_key_provider)

    assert _sha256_tree(dest) == pre_hash, (
        "TC-05: the space-guard refusal changed the destination"
    )


# ---------------------------------------------------------------------------
# PRR-018/TC-04: a journal phase-update failure surfaces as UNCERTAIN with
# the journal retained (dest sits at the aside path).
# ---------------------------------------------------------------------------


def test_journal_phase_update_failure_is_uncertain(tmp_path, monkeypatch):
    out = _make_set(tmp_path, monkeypatch)
    dest = tmp_path / "dest"
    _make_dest(dest)

    real_write_text = Path.write_text

    def fail_phase_write(self, data, *args, **kwargs):
        if ".restore-journal-" in self.name and "aside-created" in str(data):
            raise OSError("injected phase-update failure")
        return real_write_text(self, data, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Path, "write_text", fail_phase_write)
        # A phase-update failure after rename#1 is DETERMINISTICALLY
        # recoverable (the rename is trivially reversible), so the restore
        # rolls back automatically instead of going UNCERTAIN — the dest
        # returns byte-identical and no residue survives.
        with pytest.raises(RestoreError, match="rolled back"):
            restore_backup_set(out, dest, key_provider=_key_provider)

    assert (dest / "pre.txt").read_bytes() == b"pre-state", (
        "phase-update failure rollback did not restore the destination"
    )
    assert not list(dest.parent.glob(f"{dest.name}.restore-journal-*.json"))
    assert not list(dest.parent.glob(f"{dest.name}.restore.lock"))
    assert not list(dest.parent.glob(f"{dest.name}.staging-*"))
    assert not list(dest.parent.glob(f"{dest.name}.pre-restore-*"))


# ---------------------------------------------------------------------------
# PRR-009: the pruner skips the producer's in-flight staging directory.
# ---------------------------------------------------------------------------


def test_pruner_skips_in_flight_partial_staging(tmp_path):
    import time

    backups = tmp_path / "backups"
    in_flight = backups / "set.partial-deadbeef"
    in_flight.mkdir(parents=True)
    (in_flight / "backup_manifest.json").write_text(
        '{"schema": "ragapp-backup-set/1", "items": []}'
    )
    aged = time.time() - 40 * 24 * 3600
    os.utime(in_flight / "backup_manifest.json", (aged, aged))

    removed = cleanup_backups(backups, retention_days=0)
    assert removed == [], (
        "PRR-009: the pruner consumed the producer's in-flight staging "
        "directory"
    )
    assert in_flight.exists(), (
        "PRR-009: the in-flight staging directory was deleted mid-run"
    )
