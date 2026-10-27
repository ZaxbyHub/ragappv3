"""Workstream E (#727/#728 backup-restore rework) — restore commit-protocol
regression tests (05-fix-plan.md, Selected Fix part 5 / Test Plan item 2):
crash journal + lock protocol, pre-swap unmanaged copy-in, the rollback
ladder after a LanceDB persistence failure, and the manual crash completions.
Every test asserts the SPECIFIED behavior; deviations are reported, not adapted around.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import lancedb  # noqa: E402
from scripts.backup_set import create_backup_set  # noqa: E402
from scripts.restore import RestoreError, restore_backup_set  # noqa: E402

from scripts import backup_set as backup_set_module  # noqa: E402
from scripts import restore as restore_module  # noqa: E402

TEST_KEY = b"0123456789abcdef0123456789abcdef"  # 32 bytes
TAGGED_ROWS = 10
POST_TAG_ROWS = 5
STALE_WAL_ROWS = 50
T1 = "t1"
T2 = "t2"


def _key_provider():
    return (TEST_KEY, "9")


def _put(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _sha256_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _seed_db(path: Path, rows: int = 5, val_prefix: str = "row") -> None:
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


def _stage_stale_wal_sidecars(dest: Path) -> None:
    """dest holds an app.db whose committed rows live only in an
    uncheckpointed -wal (the test_e02_restore_wal_safety harness shape)."""
    dest.mkdir(parents=True, exist_ok=True)
    db = dest / "app.db"
    aside = dest.parent / "aside-app.db-sidecars"
    aside.mkdir()
    writer = sqlite3.connect(str(db))
    pin = sqlite3.connect(str(db))
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE markers(marker TEXT)")
        writer.commit()
        pin.execute("BEGIN")
        pin.execute("SELECT COUNT(*) FROM markers").fetchone()
        for _ in range(STALE_WAL_ROWS):
            writer.execute("INSERT INTO markers VALUES ('post-backup-live')")
        writer.commit()
        # pin's read transaction keeps committed pages in the -wal; snapshot
        # the sidecars, close (SQLite may delete them), put them back.
        for name in ("app.db-wal", "app.db-shm"):
            if (dest / name).exists():
                shutil.copy2(dest / name, aside / name)
    finally:
        writer.close()
        pin.close()
    for saved in aside.iterdir():
        shutil.copy2(saved, dest / saved.name)


def _make_plain_set(tmp_path: Path, monkeypatch, out: Path | None = None) -> Path:
    """A set covering app.db + vaults/vault-7 + draft-room (no lancedb)."""
    src = tmp_path / "src"
    _put(src / "vault-7" / "file.txt", b"vault-original")
    _put(src / "draft-room" / "in.txt", b"draft-original")
    _seed_db(src / "app.db")
    out = out or tmp_path / "set"
    monkeypatch.setattr(backup_set_module.settings, "data_dir", src)
    create_backup_set(
        out,
        vault_dirs=[src / "vault-7"],
        draft_room_dir=src / "draft-room",
        key_provider=_key_provider,
    )
    return out


def _make_plain_dest(dest: Path) -> Path:
    _put(dest / "vaults" / "vault-7" / "dest-file.txt", b"dest-vault")
    _put(dest / "documents" / "keep.txt", b"dest-doc")
    _seed_db(dest / "app.db", rows=1, val_prefix="dest")
    return dest


def _make_two_table_set(tmp_path: Path, monkeypatch) -> Path:
    """A real-lancedb set with two tagged tables (t1, t2; TAGGED_ROWS each)."""
    src = tmp_path / "src"
    src.mkdir()
    _seed_db(src / "app.db")
    lancedb_dir = src / "lancedb"
    db = lancedb.connect(str(lancedb_dir))
    tables = {
        name: db.create_table(name, _rows(TAGGED_ROWS, name)) for name in (T1, T2)
    }
    out = tmp_path / "set"
    monkeypatch.setattr(backup_set_module.settings, "data_dir", src)
    create_backup_set(
        out, lancedb_dir=lancedb_dir, lancedb_tables=tables, key_provider=_key_provider
    )
    return out


class _FaultingCheckoutTable:
    """Duck-typed proxy whose checkout() raises (commit-window fault); other
    attributes delegate to the real restored table."""

    def __init__(self, inner):
        self._inner = inner

    def checkout(self, tag):
        raise RuntimeError(f"E-03 injected checkout failure for {tag}")

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _factory_with_checkout_fault(dest: Path, fault_on: str):
    """Wraps the real CLI factory; the named table's checkout() raises."""
    base = restore_module._cli_lancedb_factory(dest)

    def factory(name: str):
        table = base(name)
        return _FaultingCheckoutTable(table) if name == fault_on else table

    return factory


def _make_post_tag_set(tmp_path: Path, monkeypatch, table_name: str) -> Path:
    """A real-lancedb set whose tagged generation (TAGGED_ROWS) is older than
    the copied tree's latest (the tags.create proxy adds rows after tagging —
    the live-store shape), so a persisted checkout is observable through a
    fresh connection."""
    src = tmp_path / "src"
    src.mkdir()
    _seed_db(src / "app.db")
    lancedb_dir = src / "lancedb"
    real_table = lancedb.connect(str(lancedb_dir)).create_table(
        table_name, _rows(TAGGED_ROWS, table_name)
    )
    extra = _rows(POST_TAG_ROWS, "post-tag")

    def create_then_add(tag, version):
        result = real_table.tags.create(tag, version)
        real_table.add(extra)
        return result

    proxy = SimpleNamespace(
        tags=SimpleNamespace(create=create_then_add), version=real_table.version
    )
    out = tmp_path / "set"
    monkeypatch.setattr(backup_set_module.settings, "data_dir", src)
    create_backup_set(
        out, lancedb_dir=lancedb_dir, lancedb_tables={table_name: proxy}, key_provider=_key_provider
    )
    return out


def _crash_before_first_rename(out: Path, dest: Path):
    """Run a real restore that dies right after the journal was written and
    BEFORE the first rename (rename fault on the dest->aside call), then put
    back what a killed process leaves behind: the journal (with the REAL
    payload this run computed) and the lock. Returns (journal, payload)."""
    captured = {}
    real_write_journal = restore_module._write_journal

    def spy_write_journal(dest_root, payload):
        journal = real_write_journal(dest_root, payload)
        captured["journal"] = journal
        captured["payload"] = payload
        return journal

    real_rename = os.rename

    def crash_rename(src, dst):
        # dest->aside is the rename whose SOURCE is the destination root
        # itself (staging->dest and aside->dest have it as their TARGET).
        if Path(src) == dest:
            raise OSError("E-03 simulated process death before the first rename")
        return real_rename(src, dst)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(restore_module, "_write_journal", spy_write_journal)
        mp.setattr(os, "rename", crash_rename)
        # rename#1 failure is a TYPED RestoreError as of the feedback
        # round (it names the stop-the-application precondition); the
        # crash simulation then re-stages the journal + lock a killed
        # process would have left behind.
        with pytest.raises((OSError, RestoreError)):
            restore_backup_set(out, dest, key_provider=_key_provider)

    journal = captured["journal"]
    payload = captured["payload"]
    # The in-process fault handler clears the staged tree (a confirmed
    # rollback: it holds the decrypted database); a KILLED process would
    # have left it. Re-stage it faithfully from the backup set: the staged
    # tree at crash time holds the decrypted app.db plus verbatim tree
    # copies.
    staging = Path(payload["staging"])
    staging.mkdir(parents=True)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    blob = (out / "app.db.enc").read_bytes()
    plain = AESGCM(TEST_KEY).decrypt(blob[:12], blob[12:], None)
    (staging / "app.db").write_bytes(plain)
    for item in json.loads(
        (out / "backup_manifest.json").read_text(encoding="utf-8")
    )["items"]:
        if item.get("kind") == "sqlite":
            continue
        rel = Path(item["path"])
        if (out / rel).exists():
            shutil.copytree(out / rel, staging / rel)
    # The in-process fault handler deletes the journal (a confirmed rollback
    # for an OS error); a KILLED process would not have run that cleanup.
    journal.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    lock = dest.parent / f"{dest.name}.restore.lock"
    lock.write_text("pid=crash-simulation\n", encoding="utf-8")
    return journal, payload


def test_persistence_failure_rolls_back_to_pre_state(tmp_path, monkeypatch):
    out = _make_two_table_set(tmp_path, monkeypatch)

    dest = tmp_path / "dest"
    _stage_stale_wal_sidecars(dest)
    pre_hash = _sha256_tree(dest)
    assert (dest / "app.db-wal").exists(), "E-03 HARNESS: no stale -wal staged in the destination"

    factory = _factory_with_checkout_fault(dest, fault_on=T2)
    with pytest.raises(RestoreError):
        restore_backup_set(out, dest, lancedb_factory=factory, key_provider=_key_provider)

    assert not list(dest.parent.glob(f"{dest.name}.restore-journal-*.json")), "E-03 PERSISTENCE ROLLBACK: a crash journal survived a CONFIRMED rollback"
    failed = list(dest.parent.glob(f"{dest.name}.failed-*"))
    assert len(failed) == 1, f"E-03 PERSISTENCE ROLLBACK: expected one .failed-* residue, found {[p.name for p in failed]}"
    assert not (dest.parent / f"{dest.name}.restore.lock").exists(), "E-03 PERSISTENCE ROLLBACK: the restore lock survived a completed (failed) restore"
    # Ordered last so the earlier assertions still report on this path.
    now = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
    assert _sha256_tree(dest) == pre_hash, (
        f"E-03 PERSISTENCE ROLLBACK NOT BYTE-IDENTICAL: the rolled-back destination "
        f"differs from its pre-restore sha256 tree (files now: {now}) — the pre-state "
        "(including its stale -wal/-shm sidecars) must come back byte-for-byte"
    )


def test_persistence_failure_with_absent_destination_restores_absence(tmp_path, monkeypatch):
    out = _make_two_table_set(tmp_path, monkeypatch)
    dest = tmp_path / "dest"
    assert not dest.exists()

    factory = _factory_with_checkout_fault(dest, fault_on=T2)
    with pytest.raises(RestoreError) as excinfo:
        restore_backup_set(out, dest, lancedb_factory=factory, key_provider=_key_provider)

    assert not dest.exists(), "E-03 ABSENT-DEST ROLLBACK: absent pre-restore, must be absent again (removing the restored tree IS the confirmed rollback — no aside exists)"
    assert not list(dest.parent.glob(f"{dest.name}.restore-journal-*.json")), "E-03 ABSENT-DEST ROLLBACK: a crash journal survived a confirmed rollback"
    assert not (dest.parent / f"{dest.name}.restore.lock").exists(), "E-03 ABSENT-DEST ROLLBACK: the restore lock survived a completed (failed) restore"
    assert "UNCERTAIN" not in str(excinfo.value), f"E-03 ABSENT-DEST ROLLBACK: no aside exists, so removal is CONFIRMED — must not claim uncertainty: {excinfo.value}"


def test_unmanaged_content_survives_success(tmp_path, monkeypatch):
    out = _make_plain_set(tmp_path, monkeypatch)

    dest = tmp_path / "dest"
    _put(dest / "vaults" / "vault-7" / "stale.txt", b"stale-must-not-resurrect")
    _put(dest / "vaults" / "vault-8" / "keep8.txt", b"vault-eight")
    _put(dest / "vaults" / "loose.txt", b"loose-file")
    _put(dest / "documents" / "keep.txt", b"keep-doc")
    _stage_stale_wal_sidecars(dest)
    assert (dest / "app.db-wal").exists(), "E-03 HARNESS: no stale -wal staged in the destination"

    restore_backup_set(out, dest, key_provider=_key_provider)

    vault7 = dest / "vaults" / "vault-7"
    assert sorted(p.name for p in vault7.rglob("*") if p.is_file()) == ["file.txt"], (
        f"E-03 UNMANAGED PRESERVATION: the covered vault must contain exactly the "
        f"backup's content, got {sorted(p.name for p in vault7.rglob('*'))} (stale.txt resurrected?)"
    )
    assert (dest / "vaults" / "vault-7" / "file.txt").read_bytes() == b"vault-original"
    assert (dest / "vaults" / "vault-8" / "keep8.txt").read_bytes() == b"vault-eight", "E-03 UNMANAGED PRESERVATION: uncovered sibling vault-8 was dropped"
    assert (dest / "documents" / "keep.txt").read_bytes() == b"keep-doc", "E-03 UNMANAGED PRESERVATION: unmanaged documents/ was dropped"
    assert (dest / "vaults" / "loose.txt").read_bytes() == b"loose-file", "E-03 UNMANAGED PRESERVATION: a loose file under the partial vaults/ tree was dropped"
    assert not (dest / "app.db-wal").exists(), "E-03 UNMANAGED PRESERVATION: a stale app.db-wal survived into the restored destination"
    assert not (dest / "app.db-shm").exists(), "E-03 UNMANAGED PRESERVATION: a stale app.db-shm survived into the restored destination"


def test_unmanaged_copy_fault_leaves_dest_untouched(tmp_path, monkeypatch):
    out = _make_plain_set(tmp_path, monkeypatch)
    dest = _make_plain_dest(tmp_path / "dest")
    pre_hash = _sha256_tree(dest)

    real_copytree = shutil.copytree

    def faulting_copytree(src, dst, *args, **kwargs):
        # The unmanaged copy-in's documents/ subtree (matched on the final
        # path component so a basetemp name can never trigger the fault).
        if Path(dst).name == "documents":
            raise OSError("E-03 injected unmanaged copy fault")
        return real_copytree(src, dst, *args, **kwargs)

    monkeypatch.setattr(shutil, "copytree", faulting_copytree)
    with pytest.raises(OSError):
        restore_backup_set(out, dest, key_provider=_key_provider)

    assert _sha256_tree(dest) == pre_hash, (
        "E-03 UNMANAGED COPY FAULT: the destination changed — the unmanaged copy-in "
        "must run pre-swap with the destination untouched"
    )
    assert not list(dest.parent.glob(f"{dest.name}.restore-journal-*.json"))
    assert not (dest.parent / f"{dest.name}.restore.lock").exists()
    assert not list(dest.parent.glob(f"{dest.name}.staging-*")), "E-03 UNMANAGED COPY FAULT: staging residue survived the fault"


def test_leftover_journal_refuses(tmp_path, monkeypatch):
    out = _make_plain_set(tmp_path, monkeypatch)
    dest = _make_plain_dest(tmp_path / "dest")
    pre_hash = _sha256_tree(dest)
    journal = dest.parent / f"{dest.name}.restore-journal-deadbeef.json"
    journal.write_text("{}", encoding="utf-8")

    with pytest.raises(RestoreError) as excinfo:
        restore_backup_set(out, dest, key_provider=_key_provider)

    assert "restore-journal-deadbeef.json" in str(excinfo.value), (
        f"E-03 LEFTOVER JOURNAL: the refusal must name the journal, got: {excinfo.value}"
    )
    assert _sha256_tree(dest) == pre_hash
    assert not (dest.parent / f"{dest.name}.restore.lock").exists(), "E-03 LEFTOVER JOURNAL: the refusal must happen before the lock is created"


def test_lock_present_refuses(tmp_path, monkeypatch):
    out = _make_plain_set(tmp_path, monkeypatch)
    dest = _make_plain_dest(tmp_path / "dest")
    pre_hash = _sha256_tree(dest)
    lock = dest.parent / f"{dest.name}.restore.lock"
    lock.write_text("pid=other\n", encoding="utf-8")

    with pytest.raises(RestoreError) as excinfo:
        restore_backup_set(out, dest, key_provider=_key_provider)

    assert f"{dest.name}.restore.lock" in str(excinfo.value), (
        f"E-03 LOCK PRESENT: the refusal must name the lock, got: {excinfo.value}"
    )
    assert _sha256_tree(dest) == pre_hash
    assert lock.exists(), "E-03 LOCK PRESENT: the foreign lock was removed"


def test_pre_first_rename_crash_recovery_both_directions(tmp_path, monkeypatch):
    out = _make_post_tag_set(tmp_path, monkeypatch, T1)

    # (a) roll-forward: finish the two renames, then persist the tagged
    # generations; a FRESH connection must serve the tagged generation.
    dest = tmp_path / "dest-forward"
    _seed_db(dest / "app.db", rows=1, val_prefix="forward")
    _put(dest / "unmanaged.txt", b"forward-unmanaged")
    journal, payload = _crash_before_first_rename(out, dest)
    aside = Path(payload["aside"])
    staging = Path(payload["staging"])

    os.rename(dest, aside)
    os.rename(staging, dest)
    manifest = json.loads((out / "backup_manifest.json").read_text(encoding="utf-8"))
    for item in manifest["items"]:
        if item.get("kind") != "lancedb":
            continue
        for entry in item["tables"]:
            table = lancedb.connect(str(dest / "lancedb")).open_table(entry["table"])
            table.checkout(entry["tag"])
            table.restore()
    fresh = lancedb.connect(str(dest / "lancedb")).open_table(T1)
    assert fresh.count_rows() == TAGGED_ROWS, (
        f"E-03 CRASH ROLL-FORWARD: after the manual completion (two renames + "
        f"checkout/restore per manifest table) a fresh connection reports "
        f"{fresh.count_rows()} rows, not the {TAGGED_ROWS}-row tagged generation"
    )
    shutil.rmtree(aside)
    journal.unlink()
    (dest.parent / f"{dest.name}.restore.lock").unlink()

    # (b) roll-back: the ORIGINAL destination is still intact; deleting
    # staging/journal/lock completes the recovery.
    dest_b = tmp_path / "dest-back"
    _seed_db(dest_b / "app.db", rows=2, val_prefix="back")
    _put(dest_b / "unmanaged.txt", b"back-unmanaged")
    pre_b = _sha256_tree(dest_b)
    journal_b, payload_b = _crash_before_first_rename(out, dest_b)

    assert _sha256_tree(dest_b) == pre_b, "E-03 CRASH ROLL-BACK: the original destination was touched before the first rename"
    shutil.rmtree(Path(payload_b["staging"]))
    journal_b.unlink()
    (dest_b.parent / f"{dest_b.name}.restore.lock").unlink()
    assert _sha256_tree(dest_b) == pre_b, "E-03 CRASH ROLL-BACK: the original destination is not byte-for-byte intact after the roll-back"
    assert not list(dest_b.parent.glob(f"{dest_b.name}.restore-journal-*.json"))


def test_nested_set_refused(tmp_path, monkeypatch):
    dest = tmp_path / "dest"
    dest.mkdir()
    _put(dest / "existing.txt", b"dest-existing")
    out = _make_plain_set(tmp_path, monkeypatch, out=dest / "set")
    pre_hash = _sha256_tree(dest)

    with pytest.raises(RestoreError) as excinfo:
        restore_backup_set(out, dest, key_provider=_key_provider)

    assert "inside the destination" in str(excinfo.value), (
        f"E-03 NESTED SET: the refusal must say the set lies inside the destination, got: {excinfo.value}"
    )
    assert _sha256_tree(dest) == pre_hash


def test_dest_symlink_refused(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    _put(real / "f.txt", b"real-content")
    dest = tmp_path / "dest-link"
    try:
        os.symlink(real, dest, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks need privileges on this host: {exc}")

    out = _make_plain_set(tmp_path, monkeypatch)

    with pytest.raises(RestoreError) as excinfo:
        restore_backup_set(out, dest, key_provider=_key_provider)

    assert "symlink" in str(excinfo.value), (
        f"E-03 DEST SYMLINK: the refusal must name the symlink, got: {excinfo.value}"
    )
    assert dest.is_symlink()


def test_two_fault_uncertain_retains_journal(tmp_path, monkeypatch):
    out = _make_plain_set(tmp_path, monkeypatch)
    dest = _make_plain_dest(tmp_path / "dest")

    real_rename = os.rename
    state = {"aside_done": False, "aside": None}

    def two_fault_rename(src, dst):
        if Path(src) == dest:
            # First rename (dest -> aside): succeeds.
            state["aside"] = Path(dst)
            result = real_rename(src, dst)
            state["aside_done"] = True
            return result
        if state["aside_done"] and Path(dst) == dest:
            # Second (staging -> dest) AND third (aside -> dest rollback)
            # renames both fail: a deliberate two-fault UNCERTAIN outcome.
            raise OSError("E-03 injected two-fault rename failure")
        return real_rename(src, dst)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "rename", two_fault_rename)
        # The plan's UNCERTAIN contract: a typed RestoreError naming the
        # aside directory and the manual completion steps (not the raw
        # injected OSError).
        with pytest.raises(RestoreError) as excinfo:
            restore_backup_set(out, dest, key_provider=_key_provider)

    aside_name = state["aside"].name if state["aside"] else ""
    try:
        assert list(dest.parent.glob(f"{dest.name}.restore-journal-*.json")), "E-03 UNCERTAIN RETENTION: the crash journal was not retained after a two-fault (UNCERTAIN) outcome"
        assert (dest.parent / f"{dest.name}.restore.lock").exists(), (
            "E-03 UNCERTAIN RETENTION: the restore lock was not retained after a "
            "two-fault (UNCERTAIN) outcome — per the fix plan the journal AND the "
            "lock are kept so no second restore starts while the state is unresolved"
        )
        assert aside_name in str(excinfo.value), (
            f"E-03 UNCERTAIN NAMING: the raised error must name the aside directory "
            f"({aside_name}) holding the intact pre-state and the manual completion "
            f"steps, got: {excinfo.value}"
        )
    finally:
        # Test hygiene: complete the recovery manually so nothing leaks.
        for path in dest.parent.glob(f"{dest.name}.restore-journal-*.json"):
            path.unlink()
        (dest.parent / f"{dest.name}.restore.lock").unlink(missing_ok=True)
        if state["aside"] is not None and state["aside"].exists():
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            os.rename(state["aside"], dest)
        for staging in dest.parent.glob(f"{dest.name}.staging-*"):
            shutil.rmtree(staging, ignore_errors=True)
