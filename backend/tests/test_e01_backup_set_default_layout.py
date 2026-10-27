"""Issue #727 acceptance checks AC4-AC6 (Workstream E, PR 1) — backup sets
must reflect the DEFAULT deployment layout and the output directory's own
contents, in-process (the test_518_restore.py settings seam, so the E-01 CLI
import defect does not mask these).

RED at base (HEAD 46e99488, no fix):
- AC4: a deployment with NO draft-room directory still appends the
  draft-room manifest item with an empty files map; restore raises
  ``RestoreError("draft_room artifact missing: ...")`` on it.
- AC5: a vault directory that exists but is EMPTY digests to {} and restore
  refuses it ("binds no file digests; an unverifiable tree is not
  restorable"), instead of recreating the empty vault directory.
- AC6: re-running create_backup_set into an existing output directory uses
  ``copytree(dirs_exist_ok=True)`` and merges files the operator deleted
  since run 1 (b.txt survives in the set).

GREEN after the fix: missing/empty trees no longer produce unrestorable
sets, and a set built into an existing output either refuses (leaving the
set byte-identical) or rebuilds without the deleted file — never a merge.
"""

import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.backup_set import create_backup_set  # noqa: E402
from scripts.restore import RestoreError, restore_backup_set  # noqa: E402

from scripts import backup_set as backup_set_module  # noqa: E402

TEST_KEY = b"0123456789abcdef0123456789abcdef"  # 32 bytes


def _key_provider():
    return (TEST_KEY, "9")


def _seed_app_db(path: Path, rows: int = 3) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
        for i in range(1, rows + 1):
            conn.execute("INSERT INTO t VALUES (?, ?)", (i, f"row-{i}"))
        conn.commit()
    finally:
        conn.close()


def _sha256_tree(root: Path) -> str:
    """sha256 over the sorted (relative path, bytes) listing of the tree."""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _redirect_data_dir(monkeypatch, src: Path) -> None:
    # The test_518_restore.py _run_backup seam: the settings instance the
    # script reads is patched (never os.environ); sqlite_path is derived
    # from data_dir, so the seeded <src>/app.db is what gets snapshotted.
    monkeypatch.setattr(backup_set_module.settings, "data_dir", src)


def _recorded_restore_error(out: Path, dest: Path):
    """Restore and return the RestoreError text before the first ':'.

    None when the round trip restores cleanly.
    """
    try:
        restore_backup_set(out, dest, key_provider=_key_provider)
    except RestoreError as exc:
        return str(exc).split(":", 1)[0]
    return None


def test_set_without_draft_room_restores(tmp_path, monkeypatch):
    src = tmp_path / "src"
    (src / "vault-7" / "uploads").mkdir(parents=True)
    (src / "vault-7" / "uploads" / "file.txt").write_bytes(b"vault-original")
    _seed_app_db(src / "app.db")

    out = tmp_path / "out"
    dest = tmp_path / "dest"
    _redirect_data_dir(monkeypatch, src)
    # A deployment that has never enabled the draft room: the draft-room
    # directory simply does not exist.
    create_backup_set(
        out,
        vault_dirs=[src / "vault-7"],
        draft_room_dir=src / "draft-room",
        key_provider=_key_provider,
    )

    recorded = _recorded_restore_error(out, dest)
    assert recorded is None, (
        "E-01-AC4 DEFAULT LAYOUT NOT RESTORABLE: a set created on a "
        "deployment with no draft-room directory failed to restore with "
        f"RestoreError({recorded!r}...)"
    )


def test_set_with_empty_vault_restores(tmp_path, monkeypatch):
    src = tmp_path / "src"
    (src / "vault-empty").mkdir(parents=True)
    (src / "draft-room").mkdir(parents=True)
    (src / "draft-room" / "in.txt").write_bytes(b"draft-original")
    _seed_app_db(src / "app.db")

    out = tmp_path / "out"
    dest = tmp_path / "dest"
    _redirect_data_dir(monkeypatch, src)
    create_backup_set(
        out,
        vault_dirs=[src / "vault-empty"],
        draft_room_dir=src / "draft-room",
        key_provider=_key_provider,
    )

    recorded = _recorded_restore_error(out, dest)
    assert recorded is None, (
        "E-01-AC5 EMPTY VAULT NOT RESTORABLE: a set including an existing "
        "but empty vault directory failed to restore with "
        f"RestoreError({recorded!r}...)"
    )
    assert (dest / "vaults" / "vault-empty").exists() is True, (
        "E-01-AC5 EMPTY VAULT NOT RECREATED: restore did not recreate the "
        "empty vault directory at <dest>/vaults/vault-empty"
    )


def test_rerun_into_existing_output_does_not_merge_stale_files(
    tmp_path, monkeypatch
):
    src = tmp_path / "src"
    vault = src / "vault-7"
    vault.mkdir(parents=True)
    (vault / "a.txt").write_bytes(b"a")
    (vault / "b.txt").write_bytes(b"b")
    _seed_app_db(src / "app.db")

    out = tmp_path / "out"
    _redirect_data_dir(monkeypatch, src)
    create_backup_set(
        out, vault_dirs=[vault], draft_room_dir=None, key_provider=_key_provider
    )
    after_run1 = _sha256_tree(out)

    # The operator deletes b.txt from the source; run 2 targets the SAME
    # output directory (the CLI's default is a fixed backups/set path).
    (vault / "b.txt").unlink()

    rerun_error = None
    try:
        create_backup_set(
            out,
            vault_dirs=[vault],
            draft_room_dir=None,
            key_provider=_key_provider,
        )
    except Exception as exc:  # the fix may legitimately refuse a used output
        rerun_error = exc

    if rerun_error is not None:
        assert _sha256_tree(out) == after_run1, (
            "E-01-AC6 STALE MERGE: rerunning into the existing output "
            f"raised {type(rerun_error).__name__} but left the set "
            "changed — a refusing rerun must leave the earlier set "
            "byte-identical"
        )
    else:
        assert (out / "vaults" / "vault-7" / "b.txt").exists() is False, (
            "E-01-AC6 STALE MERGE: the rerun set still contains "
            "vaults/vault-7/b.txt, which was deleted from the source "
            "before run 2 — copytree(dirs_exist_ok=True) merged the "
            "stale file into the new set"
        )
