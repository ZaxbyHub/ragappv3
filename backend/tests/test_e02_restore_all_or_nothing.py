"""Issue #728 acceptance checks AC1, AC2, AC6 (Workstream E, PR 2) — restore
must be all-or-nothing: a destination is either byte-identical to its
pre-restore state or fully restored, never a mix. In-process (the
test_518_restore.py settings seam), so the E-01 CLI import fix is not a
prerequisite.

RED at base (HEAD 46e99488, no fix):
- AC1: manifest items are verified and written one at a time into the
  destination, so a tampered LAST tree item raises RestoreError only after
  app.db was overwritten and earlier trees were rmtree'd/replaced.
- AC2: the audit spike measured torn_HEAD = 19/22 — an OSError injected at
  most write/copy/rename/delete steps leaves a half-restored mix.
- AC6: there is no held-database precondition; restore proceeds to
  write_bytes on a destination another connection holds under BEGIN
  IMMEDIATE (on Windows a raw PermissionError escapes mid-restore; on POSIX
  the restore runs to completion under the holder).

GREEN after the fix: verification happens in staging before anything is
swapped (a late failure leaves the destination byte-identical), every
single-fault outcome is either the pre-restore tree or the clean-restore
tree, and a held destination DB is refused with RestoreError before
anything is written.
"""

import hashlib
import os
import pathlib
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.backup_set import create_backup_set  # noqa: E402
from scripts.restore import RestoreError, restore_backup_set  # noqa: E402

from scripts import backup_set as backup_set_module  # noqa: E402

TEST_KEY = b"0123456789abcdef0123456789abcdef"  # 32 bytes

SOURCE_ROWS = 5


def _key_provider():
    return (TEST_KEY, "9")


def _seed_db(path: Path, rows: int, val_prefix: str = "row") -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
        for i in range(1, rows + 1):
            conn.execute(
                "INSERT INTO t VALUES (?, ?)", (i, f"{val_prefix}-{i}")
            )
        conn.commit()
    finally:
        conn.close()


def _make_source_tree(root: Path) -> Path:
    src = root / "src"
    (src / "vault-7" / "uploads").mkdir(parents=True)
    (src / "vault-7" / "uploads" / "file.txt").write_bytes(b"vault-original")
    (src / "draft-room").mkdir(parents=True)
    (src / "draft-room" / "in.txt").write_bytes(b"draft-original")
    _seed_db(src / "app.db", SOURCE_ROWS)
    return src


def _run_backup(src: Path, out: Path, monkeypatch) -> Path:
    monkeypatch.setattr(backup_set_module.settings, "data_dir", src)
    return create_backup_set(
        out,
        vault_dirs=[src / "vault-7"],
        draft_room_dir=src / "draft-room",
        key_provider=_key_provider,
    )


def _make_dest(dest: Path) -> Path:
    """A populated destination whose contents differ from the backup."""
    (dest / "vaults" / "vault-7" / "uploads").mkdir(parents=True)
    (dest / "vaults" / "vault-7" / "uploads" / "dest-file.txt").write_bytes(
        b"dest-vault"
    )
    (dest / "draft-room").mkdir(parents=True)
    (dest / "draft-room" / "in.txt").write_bytes(b"dest-draft")
    _seed_db(dest / "app.db", 1, val_prefix="dest")
    return dest


def _sha256_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


class _StepTracker:
    """Counts write/copy/rename/delete steps; raises OSError on the k-th."""

    def __init__(self, fail_at):
        self.count = 0
        self.fail_at = fail_at

    def hit(self):
        self.count += 1
        if self.fail_at is not None and self.count == self.fail_at:
            raise OSError(f"E-02-AC2 injected fault at step {self.count}")


def _install_step_patches(monkeypatch, tracker: _StepTracker) -> None:
    """Count (and optionally fault) shutil.copytree/rmtree/copy2,
    Path.write_bytes, os.replace and os.rename during a restore."""

    def wrap_moduleattr(module, name):
        original = getattr(module, name)

        def wrapper(*args, **kwargs):
            tracker.hit()
            return original(*args, **kwargs)

        monkeypatch.setattr(module, name, wrapper)

    for name in ("copytree", "rmtree", "copy2"):
        wrap_moduleattr(shutil, name)
    for name in ("replace", "rename"):
        wrap_moduleattr(os, name)

    original_write = pathlib.Path.write_bytes

    def write_bytes_wrapper(self, data):
        tracker.hit()
        return original_write(self, data)

    monkeypatch.setattr(pathlib.Path, "write_bytes", write_bytes_wrapper)


def test_late_verification_failure_leaves_dest_untouched(tmp_path, monkeypatch):
    src = _make_source_tree(tmp_path)
    out = tmp_path / "set"
    _run_backup(src, out, monkeypatch)

    # Flip one byte in a file of the LAST tree item (draft-room): the digest
    # verification failure happens only after earlier items were applied.
    tampered = out / "draft-room" / "in.txt"
    data = bytearray(tampered.read_bytes())
    data[0] ^= 0xFF
    tampered.write_bytes(bytes(data))

    dest = _make_dest(tmp_path / "dest")
    pre_hash = _sha256_tree(dest)

    with pytest.raises(RestoreError):
        restore_backup_set(out, dest, key_provider=_key_provider)

    assert _sha256_tree(dest) == pre_hash, (
        "E-02-AC1 TORN RESTORE: restore failed verification on the last "
        "manifest item but the destination differs from its pre-restore "
        "sha256 tree — app.db was overwritten and earlier trees replaced "
        "before the tampered item was checked (items are verified and "
        "written one at a time)"
    )


def test_fault_at_any_step_leaves_no_torn_state(tmp_path, monkeypatch):
    src = _make_source_tree(tmp_path)
    out = tmp_path / "set"
    _run_backup(src, out, monkeypatch)

    # Control run: count the restore's write/copy/rename/delete steps (N)
    # and record the clean-restore tree.
    tracker = _StepTracker(fail_at=None)
    mp = pytest.MonkeyPatch()
    clean_dest = _make_dest(tmp_path / "dest-clean")
    _install_step_patches(mp, tracker)
    try:
        restore_backup_set(out, clean_dest, key_provider=_key_provider)
    finally:
        mp.undo()
    n_steps = tracker.count
    assert n_steps >= 1, (
        "E-02-AC2 HARNESS: a clean restore performed no counted filesystem "
        "steps; the fault matrix cannot discriminate"
    )
    clean_hash = _sha256_tree(clean_dest)
    pre_hash = _sha256_tree(_make_dest(tmp_path / "dest-pre"))

    torn_steps = []
    for k in range(1, n_steps + 1):
        dest = _make_dest(tmp_path / f"dest-{k}")
        assert _sha256_tree(dest) == pre_hash  # harness: identical fixtures
        mp = pytest.MonkeyPatch()
        _install_step_patches(mp, _StepTracker(fail_at=k))
        try:
            restore_backup_set(out, dest, key_provider=_key_provider)
        except Exception:
            pass  # the fault surfaces as OSError (or whatever wraps it)
        finally:
            mp.undo()
        post = _sha256_tree(dest)
        if post != pre_hash and post != clean_hash:
            torn_steps.append(k)

    torn = len(torn_steps)
    assert torn == 0, (
        f"E-02-AC2 TORN STATES: {torn}/{n_steps} single-fault restore(s) "
        f"(steps {torn_steps}) left the destination neither byte-identical "
        "to its pre-restore state nor fully restored — the in-place, "
        "item-by-item apply mixes old and new state"
    )


def test_restore_refuses_while_destination_db_is_held(tmp_path, monkeypatch):
    src = _make_source_tree(tmp_path)
    out = tmp_path / "set"
    _run_backup(src, out, monkeypatch)

    dest = _make_dest(tmp_path / "dest")

    # A running app holds the destination database under an active write
    # transaction. AMEND (CHECK_WRONG, trace 727-728): the pre-restore
    # baseline is taken AFTER the holder's UPDATE — the holder's own
    # rollback journal (app.db-journal) appears on disk with that write and
    # stays until ROLLBACK, so a baseline taken before the hold can never
    # match, regardless of what restore does. The refusal oracle is
    # unchanged: restore must write nothing while the hold is active.
    holder = sqlite3.connect(str(dest / "app.db"))
    try:
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("UPDATE t SET val = 'held' WHERE id = 1")
        pre_hash = _sha256_tree(dest)

        raised_name = None
        try:
            restore_backup_set(out, dest, key_provider=_key_provider)
        except Exception as exc:
            raised_name = type(exc).__name__

        assert raised_name == "RestoreError", (
            "E-02-AC6 HELD DESTINATION DB: restore raised "
            f"{raised_name!r} instead of refusing with RestoreError while "
            "another connection held dest/app.db under BEGIN IMMEDIATE — "
            "there is no held-database precondition"
        )
        assert _sha256_tree(dest) == pre_hash, (
            "E-02-AC6 HELD DESTINATION DB: the refused restore left the "
            "destination changed — it must write nothing"
        )
    finally:
        holder.rollback()
        holder.close()
