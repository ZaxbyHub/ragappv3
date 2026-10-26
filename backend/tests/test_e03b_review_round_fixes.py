"""Review-round regression pins for the Workstream E backup/restore rework
(#727/#728, implementation-review round 1 findings). Each test is the
falsifier the reviewer named for a finding; they stay non-frozen but are
load-bearing: the frozen checks' fixtures could not see these classes
(delete-mode destinations, commit-seam faults, junctions, degenerate
manifests).
"""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.backup_set import create_backup_set  # noqa: E402
from scripts.restore import RestoreError, restore_backup_set  # noqa: E402

from scripts import backup_set as backup_set_module  # noqa: E402

TEST_KEY = b"0123456789abcdef0123456789abcdef"


def _key_provider():
    return (TEST_KEY, "9")


def _sha256_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        try:
            data = path.read_bytes()
        except PermissionError:
            # OS-locked shared-memory sidecars (a held -shm on Windows)
            # cannot be read by anyone; hash their identity instead.
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0<locked>\0")
            continue
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seed_db(path: Path, rows: int = 2) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
        for i in range(1, rows + 1):
            conn.execute("INSERT INTO t VALUES (?, ?)", (i, f"row-{i}"))
        conn.commit()
    finally:
        conn.close()


def _make_set(tmp_path: Path, monkeypatch) -> Path:
    src = tmp_path / "src"
    (src / "vault-7").mkdir(parents=True)
    (src / "vault-7" / "f.txt").write_bytes(b"vault")
    _seed_db(src / "app.db")
    monkeypatch.setattr(backup_set_module.settings, "data_dir", src)
    out = tmp_path / "set"
    create_backup_set(
        out,
        vault_dirs=[src / "vault-7"],
        draft_room_dir=None,
        key_provider=_key_provider,
    )
    return out


def _make_wal_dest(dest: Path, *, uncheckpointed: bool = False) -> None:
    """A WAL-mode destination; with ``uncheckpointed`` the last 50
    committed rows live ONLY in the -wal (a true-crash state).

    The byte-level true-crash state is built by SNAPSHOTTING app.db,
    -wal and -shm while the connections are still open (the -wal then
    holds the committed frames), letting the closes checkpoint, and
    restoring the snapshotted bytes — so rows-in-app.db-alone is
    strictly fewer than rows-with-wal (implementation-review round 2:
    the earlier fixture checkpointed everything at close and its
    byte-identity pin passed at the defective commit)."""
    dest.mkdir(parents=True, exist_ok=True)
    db = dest / "app.db"
    aside = dest.parent / "wal-aside"
    aside.mkdir(exist_ok=True)
    writer = sqlite3.connect(str(db))
    pin = sqlite3.connect(str(db))
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
        for i in range(1, 3):
            writer.execute("INSERT INTO t VALUES (?, 'seeded')", (i,))
        writer.commit()
        writer.execute("BEGIN IMMEDIATE")
        for i in range(101, 151):
            writer.execute(
                "INSERT INTO t VALUES (?, 'post-backup-live')", (i,)
            )
        writer.commit()
        if uncheckpointed:
            # Snapshot the exact on-disk bytes WHILE the wal still holds
            # the last 50 committed frames.
            pin.execute("BEGIN")
            pin.execute("SELECT COUNT(*) FROM t").fetchone()
            for name in ("app.db", "app.db-wal", "app.db-shm"):
                side = dest / name
                if side.exists():
                    shutil.copy2(side, aside / name)
    finally:
        writer.close()
        pin.close()
    if uncheckpointed:
        # Close checkpointed the wal into app.db; restore the crash
        # state byte-for-byte. The sanity check is BYTE-LEVEL: opening
        # the database at all would replay the wal and destroy the very
        # state under test.
        for name in ("app.db", "app.db-wal", "app.db-shm"):
            saved = aside / name
            if saved.exists():
                shutil.copy2(saved, dest / name)
        assert (dest / "app.db").read_bytes() == (aside / "app.db").read_bytes(), (
            "fixture failed to restore the pre-checkpoint app.db bytes"
        )
        assert (dest / "app.db-wal").stat().st_size > 0, (
            "fixture failed to restore a non-empty wal"
        )


# ---------------------------------------------------------------------------
# Finding 1 (Critical): WAL-mode held destination — typed refusal,
# byte-identity, no probe residue.
# ---------------------------------------------------------------------------


def test_wal_held_destination_typed_refusal_byte_identical(
    tmp_path, monkeypatch
):
    out = _make_set(tmp_path, monkeypatch)
    dest = tmp_path / "dest"
    _make_wal_dest(dest, uncheckpointed=True)

    holder = sqlite3.connect(str(dest / "app.db"))
    try:
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("UPDATE t SET val = 'held' WHERE id = 1")
        pre_hash = _sha256_tree(dest)

        with pytest.raises(RestoreError, match="held by another connection"):
            restore_backup_set(out, dest, key_provider=_key_provider)

        assert _sha256_tree(dest) == pre_hash, (
            "E-03b WAL-HELD: the refused restore changed the destination "
            "tree (probe residue or checkpoint mutation)"
        )
        assert not list(dest.parent.glob("*.probe-keep-*")), (
            "E-03b WAL-HELD: probe keep files left behind outside the "
            "destination"
        )
    finally:
        holder.rollback()
        holder.close()


# ---------------------------------------------------------------------------
# Finding 2: true-crash WAL destination — a later refusal leaves the
# destination byte-identical INCLUDING app.db (no checkpoint mutation).
# ---------------------------------------------------------------------------


def test_true_crash_wal_refusal_leaves_app_db_bytes_unchanged(
    tmp_path, monkeypatch
):
    out = _make_set(tmp_path, monkeypatch)
    # Tamper the set so a LATER verification failure refuses the restore
    # (after the probe ran).
    vault_file = out / "vaults" / "vault-7" / "f.txt"
    vault_file.write_bytes(b"tampered")

    dest = tmp_path / "dest"
    _make_wal_dest(dest, uncheckpointed=True)
    pre_hash = _sha256_tree(dest)
    pre_db_hash = _sha256_file(dest / "app.db")
    assert (dest / "app.db-wal").exists()

    with pytest.raises(RestoreError, match="digest mismatch"):
        restore_backup_set(out, dest, key_provider=_key_provider)

    assert _sha256_file(dest / "app.db") == pre_db_hash, (
        "E-03b TRUE-CRASH WAL: the probe's close checkpointed the WAL "
        "into app.db — a refused restore must leave the bytes untouched"
    )
    assert _sha256_tree(dest) == pre_hash, (
        "E-03b TRUE-CRASH WAL: the refused restore changed the "
        "destination tree"
    )
    assert (dest / "app.db-wal").exists(), (
        "E-03b TRUE-CRASH WAL: the -wal sidecar vanished without a "
        "successful restore"
    )


# ---------------------------------------------------------------------------
# Finding 3: corrupt destination database — the restore proceeds (the
# pre-change behavior; a corrupt db cannot be held).
# ---------------------------------------------------------------------------


def test_corrupt_destination_db_restored_over(tmp_path, monkeypatch):
    out = _make_set(tmp_path, monkeypatch)
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "app.db").write_bytes(b"definitely not a sqlite database" * 8)

    report = restore_backup_set(out, dest, key_provider=_key_provider)
    assert report["verified"] is True
    header = (dest / "app.db").read_bytes()[:16]
    assert header == b"SQLite format 3\x00", (
        "E-03b CORRUPT DEST: the restored destination database is not a "
        "valid SQLite file"
    )


# ---------------------------------------------------------------------------
# Finding 4: producer commit-phase failure rolls the created tags back.
# ---------------------------------------------------------------------------


def test_producer_commit_failure_rolls_back_tags(tmp_path, monkeypatch):
    src = tmp_path / "src"
    (src / "vault-7").mkdir(parents=True)
    (src / "vault-7" / "f.txt").write_bytes(b"vault")
    _seed_db(src / "app.db")
    monkeypatch.setattr(backup_set_module.settings, "data_dir", src)

    import lancedb

    lancedb_dir = tmp_path / "lancedb"
    table = lancedb.connect(str(lancedb_dir)).create_table(
        "chunks", [{"chunk_id": i, "text": f"row-{i}"} for i in (1, 2)]
    )
    out = tmp_path / "cs-out"

    real_replace = os.replace

    def failing_replace(src_path, dst_path):
        if str(dst_path).endswith("cs-out"):
            raise OSError("E-03b injected commit-seam failure")
        return real_replace(src_path, dst_path)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "replace", failing_replace)
        with pytest.raises(OSError, match="commit-seam"):
            create_backup_set(
                out,
                lancedb_dir=lancedb_dir,
                lancedb_tables={"chunks": table},
                vault_dirs=[],
                key_provider=_key_provider,
            )

    backup_tags = [
        name
        for name in table.tags.list()
        if isinstance(name, str) and name.startswith("backup-")
    ]
    assert backup_tags == [], (
        "E-03b COMMIT SEAM: the failed run leaked live backup tags "
        f"{backup_tags} — the S2-04 optimize-blockage class"
    )


# ---------------------------------------------------------------------------
# Finding 5: journal records aside-created; an async exception between
# the first rename and the flag store leaves UNCERTAIN evidence.
# ---------------------------------------------------------------------------


def test_journal_records_aside_created_and_async_kill_is_uncertain(
    tmp_path, monkeypatch
):
    out = _make_set(tmp_path, monkeypatch)
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "pre.txt").write_bytes(b"pre-state")

    real_rename = os.rename

    def kill_after_first_rename(src, dst):
        result = real_rename(src, dst)
        if Path(str(src)) == dest:
            # The first swap rename just completed; an async exception
            # (KeyboardInterrupt) lands BEFORE the flag store.
            raise KeyboardInterrupt("E-03b async kill window")
        return result

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "rename", kill_after_first_rename)
        with pytest.raises(RestoreError, match="UNCERTAIN"):
            restore_backup_set(out, dest, key_provider=_key_provider)

    journals = list(dest.parent.glob(f"{dest.name}.restore-journal-*.json"))
    assert journals, (
        "E-03b ASYNC KILL: the journal was deleted while the original "
        "destination sat at the aside path — recovery evidence lost"
    )
    payload = json.loads(journals[0].read_text(encoding="utf-8"))
    aside = Path(payload["aside"])
    assert aside.exists(), (
        "journal names an aside that is not on disk — the journal's "
        "recorded aside path IS the recovery signal (disk truth; the "
        "phase field is a best-effort hint, an async exception can land "
        "between the rename and the phase write)"
    )
    assert (dest.parent / f"{dest.name}.restore.lock").exists(), (
        "E-03b ASYNC KILL: the lock was released on an UNCERTAIN outcome"
    )
    # Hygiene: complete the recovery so nothing leaks.
    journals[0].unlink()
    (dest.parent / f"{dest.name}.restore.lock").unlink()
    os.rename(aside, dest)
    for staging in dest.parent.glob(f"{dest.name}.staging-*"):
        shutil.rmtree(staging, ignore_errors=True)


def test_two_fault_journal_phase_is_aside_created(tmp_path, monkeypatch):
    out = _make_set(tmp_path, monkeypatch)
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "pre.txt").write_bytes(b"pre-state")

    real_rename = os.rename
    state = {"aside": None}

    def two_fault_rename(src, dst):
        if Path(str(src)) == dest:
            state["aside"] = Path(str(dst))
            return real_rename(src, dst)
        if state["aside"] is not None and Path(str(dst)) == dest:
            raise OSError("E-03b injected two-fault rename failure")
        return real_rename(src, dst)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "rename", two_fault_rename)
        with pytest.raises(RestoreError, match="UNCERTAIN"):
            restore_backup_set(out, dest, key_provider=_key_provider)

    journal = next(dest.parent.glob(f"{dest.name}.restore-journal-*.json"))
    payload = json.loads(journal.read_text(encoding="utf-8"))
    assert payload["phase"] == "aside-created", (
        f"E-03b TWO-FAULT PHASE: journal phase is {payload['phase']!r}, "
        "expected 'aside-created' (the documented recovery steps for this "
        "state depend on it)"
    )
    assert state["aside"].name in journal.read_text(encoding="utf-8")
    # Hygiene.
    journal.unlink()
    (dest.parent / f"{dest.name}.restore.lock").unlink()
    if state["aside"].exists():
        os.rename(state["aside"], dest)
    for staging in dest.parent.glob(f"{dest.name}.staging-*"):
        shutil.rmtree(staging, ignore_errors=True)


# ---------------------------------------------------------------------------
# Finding 6: junction nested inside an uncovered subtree is refused
# (mklink /J needs no privilege).
# ---------------------------------------------------------------------------


def _make_junction(link: Path, target: Path) -> bool:
    try:
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return link.exists()


def test_junction_nested_in_uncovered_subtree_refused(
    tmp_path, monkeypatch
):
    out = _make_set(tmp_path, monkeypatch)
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "documents").mkdir()
    (dest / "documents" / "keep.txt").write_bytes(b"keep")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"outside content")
    link = dest / "documents" / "junction-nested"
    if not _make_junction(link, outside):
        pytest.skip("mklink /J unavailable on this host")

    pre_hash = _sha256_tree(dest)
    with pytest.raises(RestoreError, match="junction"):
        restore_backup_set(out, dest, key_provider=_key_provider)
    assert _sha256_tree(dest) == pre_hash, (
        "E-03b NESTED JUNCTION: the refused restore changed the "
        "destination"
    )


# ---------------------------------------------------------------------------
# Finding 7: pruner skips a junction candidate with a logged error and
# the sweep continues.
# ---------------------------------------------------------------------------


def test_pruner_skips_junction_candidate_and_continues(tmp_path):
    from scripts.cleanup_backups import cleanup_backups

    outside = tmp_path / "outside-set"
    outside.mkdir()
    (outside / "backup_manifest.json").write_text(
        '{"schema": "ragapp-backup-set/1", "items": []}'
    )
    import time

    backups = tmp_path / "backups"
    backups.mkdir()
    link = backups / "linked-set"
    if not _make_junction(link, outside):
        pytest.skip("mklink /J unavailable on this host")
    real_set = backups / "real-set"
    real_set.mkdir()
    (real_set / "backup_manifest.json").write_text(
        '{"schema": "ragapp-backup-set/1", "items": []}'
    )
    aged = time.time() - 40 * 24 * 3600
    for path in (real_set, real_set / "backup_manifest.json"):
        os.utime(path, (aged, aged))
    os.utime(
        outside / "backup_manifest.json", (aged, aged)
    )

    import logging as _logging

    root_logger = _logging.getLogger("scripts.cleanup_backups")
    captured = []

    class _Capture(_logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    handler = _Capture()
    root_logger.addHandler(handler)
    try:
        removed = cleanup_backups(backups, retention_days=0)
    finally:
        root_logger.removeHandler(handler)
    assert real_set in removed, (
        "E-03b JUNCTION SWEEP: the real expired set was not pruned after "
        "the junction candidate"
    )
    assert link.exists(), "the junction itself must never be removed"
    assert outside.exists(), "the junction target must never be touched"
    assert any(
        "symlink or junction" in message and str(link) in message
        for message in captured
    ), (
        "E-03b JUNCTION SWEEP: the plan-mandated logged skip was not "
        f"emitted (captured: {captured})"
    )


# ---------------------------------------------------------------------------
# Finding 8: a manifest without a sqlite item preserves dest/app.db.
# ---------------------------------------------------------------------------


def test_sqliteless_manifest_preserves_dest_app_db(tmp_path, monkeypatch):
    out = _make_set(tmp_path, monkeypatch)
    manifest_path = out / "backup_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["items"] = [
        i for i in manifest["items"] if i.get("kind") != "sqlite"
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    dest = tmp_path / "dest"
    dest.mkdir()
    _seed_db(dest / "app.db")
    pre_hash = _sha256_file(dest / "app.db")

    report = restore_backup_set(out, dest, key_provider=_key_provider)
    assert report["verified"] is True
    assert (dest / "app.db").exists(), (
        "E-03b SQLITELESS MANIFEST: dest/app.db was silently dropped"
    )
    assert _sha256_file(dest / "app.db") == pre_hash, (
        "E-03b SQLITELESS MANIFEST: dest/app.db changed although the set "
        "does not cover it"
    )


# ---------------------------------------------------------------------------
# Finding 9: restore into a fresh nested destination (missing parent).
# ---------------------------------------------------------------------------


def test_restore_into_fresh_nested_destination(tmp_path, monkeypatch):
    out = _make_set(tmp_path, monkeypatch)
    dest = tmp_path / "brand-new-parent" / "data"

    report = restore_backup_set(out, dest, key_provider=_key_provider)
    assert report["verified"] is True
    assert (dest / "app.db").exists(), (
        "E-03b NESTED DEST: the restore into a destination whose parent "
        "did not exist failed to land"
    )


# ---------------------------------------------------------------------------
# Nit: lock/journal creation failures raise typed RestoreError.
# ---------------------------------------------------------------------------


def test_lock_creation_failure_is_typed(tmp_path, monkeypatch):
    out = _make_set(tmp_path, monkeypatch)
    dest = tmp_path / "dest"
    dest.mkdir()

    real_open = os.open

    def failing_open(path, flags, *args, **kwargs):
        if str(path).endswith(".restore.lock"):
            raise OSError("E-03b injected lock-creation failure")
        return real_open(path, flags, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "open", failing_open)
        with pytest.raises(RestoreError, match="lock"):
            restore_backup_set(out, dest, key_provider=_key_provider)


def test_journal_creation_failure_is_typed(tmp_path, monkeypatch):
    out = _make_set(tmp_path, monkeypatch)
    dest = tmp_path / "dest"
    dest.mkdir()

    real_write_text = Path.write_text

    def failing_write_text(self, data, *args, **kwargs):
        if ".restore-journal-" in self.name:
            raise OSError("E-03b injected journal-creation failure")
        return real_write_text(self, data, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Path, "write_text", failing_write_text)
        with pytest.raises(RestoreError, match="crash journal"):
            restore_backup_set(out, dest, key_provider=_key_provider)
    # The lock must be released on this failure path.
    assert not (dest.parent / f"{dest.name}.restore.lock").exists()
    assert not list(dest.parent.glob(f"{dest.name}.restore-journal-*.json"))
    # A journal write failure happens after staging was built but before
    # any rename: the destination is untouched.
    assert dest.exists() and not any(dest.iterdir())


# ---------------------------------------------------------------------------
# Round 2, finding 3: the remaining plan-listed link tests.
# ---------------------------------------------------------------------------


def test_symlink_in_uncovered_tree_preserved_as_link(tmp_path, monkeypatch):
    out = _make_set(tmp_path, monkeypatch)
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "documents").mkdir()
    (dest / "documents" / "keep.txt").write_bytes(b"keep")
    target = tmp_path / "link-target.txt"
    target.write_bytes(b"target content")
    link = dest / "documents" / "a-symlink"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks need privileges on this host: {exc}")

    restore_backup_set(out, dest, key_provider=_key_provider)
    staged_link = dest / "documents" / "a-symlink"
    assert staged_link.is_symlink(), (
        "E-03b SYMLINK-AS-LINK: the unmanaged symlink was not preserved "
        "as a link"
    )
    assert os.readlink(staged_link) == os.readlink(link), (
        "E-03b SYMLINK-AS-LINK: the preserved link points somewhere else"
    )
    assert target.read_bytes() == b"target content", (
        "E-03b SYMLINK-AS-LINK: the link target was read or mutated"
    )
    assert (dest / "documents" / "keep.txt").read_bytes() == b"keep"


def test_nested_sidecar_named_file_in_uncovered_tree_preserved(
    tmp_path, monkeypatch
):
    out = _make_set(tmp_path, monkeypatch)
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "documents").mkdir()
    nested_wal = dest / "documents" / "app.db-wal"
    nested_wal.write_bytes(b"ordinary unmanaged content, not a sidecar")
    pre_hash = _sha256_file(nested_wal)

    restore_backup_set(out, dest, key_provider=_key_provider)
    assert nested_wal.exists(), (
        "E-03b NESTED SIDECAR NAME: a file named app.db-wal inside an "
        "uncovered subtree was dropped"
    )
    assert _sha256_file(nested_wal) == pre_hash, (
        "E-03b NESTED SIDECAR NAME: the uncovered-subtree file changed"
    )


# ---------------------------------------------------------------------------
# Round 2, finding 2 pin: a sqlite-less manifest preserves the destination
# database TOGETHER WITH its live -wal (no silent row loss).
# ---------------------------------------------------------------------------


def test_sqliteless_manifest_preserves_uncheckpointed_rows(
    tmp_path, monkeypatch
):
    out = _make_set(tmp_path, monkeypatch)
    manifest_path = out / "backup_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["items"] = [
        i for i in manifest["items"] if i.get("kind") != "sqlite"
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    dest = tmp_path / "dest"
    _make_wal_dest(dest, uncheckpointed=True)

    def _rows_with_wal():
        conn = sqlite3.connect(str(dest / "app.db"))
        try:
            return conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        finally:
            conn.close()

    # Fixture sanity is byte-level inside _make_wal_dest (opening the
    # database here would replay the wal and destroy the crash state).
    # The 2 seeded rows are in app.db alone; the 50 committed rows live
    # only in the wal.
    pre_rows = _rows_with_wal()
    assert pre_rows == 52, (
        f"E-03b FIXTURE: expected 52 rows with the wal, got {pre_rows}"
    )
    # Rebuild once more: the count check above absorbed the wal.
    shutil.rmtree(dest)
    _make_wal_dest(dest, uncheckpointed=True)
    pre_db_bytes = (dest / "app.db").read_bytes()

    report = restore_backup_set(out, dest, key_provider=_key_provider)
    assert report["verified"] is True
    # The wal assertion must run BEFORE any sqlite open on the restored
    # tree: opening (and closing) the database checkpoints and deletes
    # the wal — the preservation oracle would destroy its own subject.
    assert (dest / "app.db-wal").exists(), (
        "E-03b SQLITELESS WAL LOSS: the live -wal was dropped although "
        "the database it journals was preserved"
    )
    assert (dest / "app.db").read_bytes() == pre_db_bytes, (
        "E-03b SQLITELESS WAL LOSS: app.db bytes changed although the "
        "set does not cover the database"
    )
    post_rows = _rows_with_wal()
    assert post_rows == pre_rows, (
        f"E-03b SQLITELESS WAL LOSS: {pre_rows - post_rows} committed "
        "rows vanished although the set does not cover the database"
    )


# ---------------------------------------------------------------------------
# Round 2 question: orphan reconciliation aborts when a linked candidate
# was skipped (its manifest cannot be counted).
# ---------------------------------------------------------------------------


def test_orphan_reconciliation_aborts_when_link_skipped(tmp_path):
    import lancedb
    from scripts.cleanup_backups import cleanup_backups

    from scripts import cleanup_backups as cleanup_module

    outside = tmp_path / "outside-set"
    outside.mkdir()
    (outside / "backup_manifest.json").write_text(
        '{"schema": "ragapp-backup-set/1", "items": []}'
    )
    backups = tmp_path / "backups"
    backups.mkdir()
    link = backups / "linked-set"
    if not _make_junction(link, outside):
        pytest.skip("mklink /J unavailable on this host")

    lancedb_dir = tmp_path / "lancedb"
    table = lancedb.connect(str(lancedb_dir)).create_table(
        "chunks", [{"chunk_id": 1, "text": "row-1"}]
    )
    table.tags.create("backup-orphan-candidate", int(table.version))

    import unittest.mock as _mock

    with _mock.patch.object(
        cleanup_module, "_live_lancedb_dir", return_value=lancedb_dir
    ), _mock.patch.object(
        cleanup_module,
        "_release_unreferenced_tags",
        wraps=cleanup_module._release_unreferenced_tags,
    ) as spy:
        cleanup_backups(backups, retention_days=0, release_orphan_tags=True)

    assert spy.call_count == 0, (
        "E-03b CENSUS LINK: orphan reconciliation ran although a linked "
        "candidate's manifest could not be counted"
    )
    names = list(table.tags.list())
    assert "backup-orphan-candidate" in names, (
        "E-03b CENSUS LINK: a tag was released (or attempted) although "
        "reconciliation should have aborted"
    )
