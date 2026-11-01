"""Consistent backup sets binding SQLite, LanceDB and source/artifact files
(issue #518, Workstream E3 — legacy-12/E04; producer hardening #727).

A backup set is a directory containing:

* ``app.db.enc`` — AES-GCM (12-byte nonce || ciphertext) WAL-safe snapshot
  of ``settings.sqlite_path`` taken via the sqlite3 backup API;
* the whole ``lancedb/`` tree, with each named table's version pinned by a
  ``backup-<timestamp>-<token>`` tag BEFORE the copy (tagged versions
  survive later ``optimize()`` compaction, so a restore checks out the
  exact generation; the token keeps same-second sets from colliding);
* any vault directories and the draft-room directory, copied verbatim —
  a source that exists but holds no files is recorded with an explicit
  ``"empty": true`` marker instead of an unrestorable empty files map, and
  a source that does not exist is simply not part of the set (#727,
  T1-28-K-02);
* ``backup_manifest.json`` — schema ``ragapp-backup-set/1`` binding every
  artifact with sha256 digests (and per-table version tags) so restore can
  verify integrity end to end.

The set is BUILT under a sibling staging directory and renamed into place
only when complete; a non-empty existing output directory is refused
before anything is snapshotted or tagged, so re-running into a used
directory can never merge stale files into a new set (#727, T1-28-S2-07).
If a run fails after tags were created, the tags it created are deleted
(best effort); when that rollback itself fails the staging directory is
KEPT as evidence and the remaining live tags are named in the error log.

Use ``scripts/restore.py`` to restore and verify a set; the scheduled
operation (host cron / Windows Task Scheduler) is documented in
docs/operations.md and docs/admin-guide.md.
"""

import argparse
import logging
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Mapping, Optional, Sequence

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT))
# backend.app.config imports ``app.*`` absolutely (config.py L13), which
# only resolves with backend/ itself on the path (issue #727 / T1-28-K-01).
sys.path.append(str(ROOT / "backend"))

from backend.app.config import settings
from backend.app.services.secret_manager import SecretManager
from scripts.backup_sqlite import snapshot_sqlite

logger = logging.getLogger(__name__)

MANIFEST_NAME = "backup_manifest.json"
MANIFEST_SCHEMA = "ragapp-backup-set/1"
SQLITE_ARTIFACT = "app.db.enc"
LANCEDB_DIRNAME = "lancedb"


class BackupError(Exception):
    """A backup set could not be created (refusal or failure)."""


def _default_key_provider() -> tuple:
    key, version = SecretManager().get_aes_key()
    return (key, str(version))


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest_tree(root: Path) -> dict:
    files: dict = {}
    if not root.exists():
        return files
    for path in sorted(root.rglob("*")):
        if path.is_file():
            files[path.relative_to(root).as_posix()] = _sha256_bytes(
                path.read_bytes()
            )
    return files


def _copy_tree(src: Path, dst: Path) -> None:
    if src.exists():
        shutil.copytree(src, dst, dirs_exist_ok=True)


def _restrict_permissions(root: Path) -> None:
    """Tighten a written tree to owner-only (0700 dirs / 0600 files).

    Backup/restore trees contain user documents and a decrypted database;
    the default umask on shared hosts would leave them group/world readable
    (swarm review, LOW hardening).
    """
    if not root.exists():
        return
    os.chmod(root, 0o700)
    for path in root.rglob("*"):
        try:
            os.chmod(path, 0o700 if path.is_dir() else 0o600)
        except OSError:  # pragma: no cover — read-only mount/locked file
            pass


def _delete_tag(table: object, tag: str) -> None:
    """Best-effort tag deletion across real lancedb and test fakes."""
    tags = getattr(table, "tags", None)
    delete = getattr(tags, "delete", None) if tags is not None else None
    if delete is not None:
        delete(tag)
        return
    raise BackupError(f"table object has no tags.delete: {table!r}")


def _rollback_tags(created_tags: Sequence[tuple]) -> List[str]:
    """Delete the tags this run created; return the ones that survived."""
    remaining: List[str] = []
    for table, tag in created_tags:
        try:
            _delete_tag(table, tag)
        except Exception as exc:  # noqa: BLE001 — rollback is best effort
            remaining.append(tag)
            logger.warning(
                "could not release created backup tag %r after a failed "
                "run: %s",
                tag,
                exc,
            )
    return remaining


def _snapshot_generation_binding(snapshot_bytes: bytes) -> Optional[dict]:
    """Latest authoritative index-generation row FROM THE SNAPSHOT BYTES.

    Writes the plaintext snapshot to a private temp file and reads it
    READ-ONLY (``Connection.deserialize`` proved unreliable for large
    snapshots on this sqlite build), selecting the highest-id
    ``index_generation:%`` row from ``migration_journal`` — the C1
    authoritative generation at snapshot time (issue #518: bind the
    manifest to the generation actually frozen in the set, never the live
    DB's newer rows). Returns None when the snapshot has no such row
    (fresh installs); readers treat absence as legacy.
    """
    import tempfile

    fd = -1
    tmp_path = None
    fd, tmp_path = tempfile.mkstemp(suffix=".snap.db")
    try:
        # Write fully (short writes truncate the snapshot silently) and
        # guarantee the fd closes even on failure (PR #595 review F-004).
        view = memoryview(snapshot_bytes)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.close(fd)
        fd = -1
        conn = sqlite3.connect(f"file:{tmp_path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT id, migration_name, detail FROM migration_journal"
                " WHERE migration_name LIKE 'index_generation:%'"
                " ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        logger.warning(
            "generation binding unreadable from snapshot; manifest will"
            " carry no generation field",
            exc_info=True,
        )
        return None
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    if row is None:
        return None
    return {"id": int(row[0]), "name": str(row[1]), "detail": str(row[2] or "")}


def _tree_item(
    name: str, kind: str, rel_path: str, files: dict, source: Path
) -> dict:
    """A tree manifest item; an existing-but-fileless source is recorded
    with an explicit ``empty`` marker plus its directory skeleton, so
    restore recreates the tree's nested empty directories instead of only
    the root (#727, T1-28-K-02; swarm review pr1026 PRR-001)."""
    item = {
        "name": name,
        "kind": kind,
        "path": rel_path,
        "files": files,
    }
    if not files:
        item["empty"] = True
        item["dirs"] = sorted(
            path.relative_to(source).as_posix()
            for path in source.rglob("*")
            if path.is_dir()
        )
    return item


def _write_set(
    staging: Path,
    *,
    lancedb_dir: Optional[Path],
    lancedb_tables: Optional[Mapping[str, object]],
    vault_dirs: Sequence[Path],
    draft_room_dir: Optional[Path],
    key_provider: Optional[Callable[[], tuple]],
    created_tags: List[tuple],
) -> None:
    """Write the complete backup set into ``staging`` (manifest last)."""
    staging.mkdir(parents=True, exist_ok=True)
    provider = key_provider or _default_key_provider
    key, key_version = provider()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    # Same-second runs of the same table must not collide on the tag name
    # (#727, T1-28-S2-16): the timestamp stays human-readable, the token
    # carries the uniqueness. ``datetime`` is read at call time so the
    # frozen same-second check can pin it.
    run_token = secrets.token_hex(4)

    # --- SQLite: WAL-safe snapshot, then encrypt ---
    nonce = secrets.token_bytes(12)
    plaintext = snapshot_sqlite(Path(settings.sqlite_path))
    # E3 closure (#518, G6): read the authoritative generation journal row
    # FROM THE SNAPSHOT BYTES (not the live DB), so the manifest binds the
    # generation that is actually frozen inside this encrypted artifact.
    generation_binding = _snapshot_generation_binding(plaintext)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)
    sqlite_artifact = staging / SQLITE_ARTIFACT
    sqlite_artifact.write_bytes(nonce + ciphertext)
    # Owner-only: the encrypted snapshot still binds the deployment's data
    # shape; keep the backup set itself restrictive.
    os.chmod(sqlite_artifact, 0o600)
    items: List[dict] = [
        {
            "name": "app.db",
            "kind": "sqlite",
            "path": SQLITE_ARTIFACT,
            "sha256": _sha256_bytes(sqlite_artifact.read_bytes()),
            "key_version": key_version,
        }
    ]

    # --- LanceDB: tag table versions BEFORE copying, then copy the tree ---
    tables_out: List[dict] = []
    if lancedb_dir is not None and not Path(lancedb_dir).exists():
        logger.info(
            "lancedb directory %s does not exist; the set will carry no "
            "lancedb item",
            lancedb_dir,
        )
    elif lancedb_dir is not None:
        backup_lancedb = staging / LANCEDB_DIRNAME
        tag = f"backup-{timestamp}-{run_token}"
        for name, table in (lancedb_tables or {}).items():
            table_tag = f"{tag}-{name}"
            # Real lancedb (>= 0.3x) manages tags through ``table.tags``;
            # ``create_tag`` remains supported for the frozen test fakes
            # and any older table objects (G5: the real path is exercised
            # by test_518_restore_drill_real.py).
            create_tag = getattr(table, "create_tag", None)
            if create_tag is not None:
                create_tag(table_tag)
            else:
                table.tags.create(table_tag, int(table.version))
            created_tags.append((table, table_tag))
            tables_out.append({"table": name, "tag": table_tag})
            logger.info("tagged lancedb table %s as %s", name, table_tag)
        _copy_tree(Path(lancedb_dir), backup_lancedb)
        lancedb_files = _digest_tree(backup_lancedb)
        # Manifest binding (#518): the lancedb item records the C1
        # authoritative generation (migration_journal row id + its SQLite
        # rowid) read from the SNAPSHOT, so restore can verify the restored
        # journal matches the vector tree this set froze.
        items.append(
            {
                "name": LANCEDB_DIRNAME,
                "kind": "lancedb",
                "path": LANCEDB_DIRNAME,
                "tables": tables_out,
                "generation": generation_binding,
                "files": lancedb_files,
                **({"empty": True} if not lancedb_files else {}),
            }
        )

    # --- Vault directories ---
    # Manifest paths are data_dir-relative: vaults live at
    # <data_dir>/vaults/<id> (config.py vault_dir), so the artifact path is
    # "vaults/<id>" — a bare leaf name would restore vaults to the wrong
    # location (Copilot review on PR #589). A vault directory that does
    # not exist is skipped, not recorded empty (#727, T1-28-K-02).
    for vault_dir in vault_dirs:
        if not Path(vault_dir).exists():
            logger.info(
                "vault directory %s does not exist; skipping it",
                vault_dir,
            )
            continue
        name = vault_dir.name
        vault_rel = f"vaults/{name}"
        target = staging / vault_rel
        _copy_tree(vault_dir, target)
        items.append(
            _tree_item(
                name, "vault", vault_rel, _digest_tree(target), vault_dir
            )
        )

    # --- Draft room ---
    # A deployment that never enabled the draft room has no directory; the
    # item is skipped entirely so restore never demands a missing artifact
    # (#727, T1-28-K-02).
    if draft_room_dir is not None and Path(draft_room_dir).exists():
        target = staging / "draft-room"
        _copy_tree(draft_room_dir, target)
        items.append(
            _tree_item(
                "draft-room",
                "draft_room",
                "draft-room",
                _digest_tree(target),
                draft_room_dir,
            )
        )
    elif draft_room_dir is not None:
        logger.info(
            "draft-room directory %s does not exist; the set will carry no "
            "draft-room item",
            draft_room_dir,
        )

    manifest_path = staging / MANIFEST_NAME
    manifest_path.write_text(
        json.dumps({"schema": MANIFEST_SCHEMA, "items": items}, indent=2),
        encoding="utf-8",
    )
    _restrict_permissions(staging)
    os.chmod(manifest_path, 0o600)


def create_backup_set(
    output_dir: Path,
    *,
    lancedb_dir: Optional[Path] = None,
    lancedb_tables: Optional[Mapping[str, object]] = None,
    vault_dirs: Sequence[Path] = (),
    draft_room_dir: Optional[Path] = None,
    key_provider: Optional[Callable[[], tuple]] = None,
) -> Path:
    """Create a backup set and return the manifest path.

    ``lancedb_tables`` maps table name -> live table object exposing
    ``create_tag(tag)``; each table is version-tagged with a
    ``backup-<timestamp>-<token>`` tag BEFORE the lancedb tree is copied,
    so the copied generation is the tagged one and restore can
    ``checkout(tag)``.

    The set is built under ``<output>.partial-<token>`` and renamed into
    place when complete. A NON-EMPTY existing output directory is refused
    up front (before the snapshot or any tag exists), so a re-run can
    never merge stale files into a previous set; an existing EMPTY output
    directory (e.g. a wrapper script's pre-created folder) is replaced.
    """
    output_dir = Path(output_dir)
    if output_dir.exists():
        if not output_dir.is_dir():
            raise BackupError(
                f"output path exists and is not a directory: {output_dir}"
            )
        if any(output_dir.iterdir()):
            raise BackupError(
                f"output directory is not empty: {output_dir} — a backup "
                "set must be written to a fresh directory; refusing to "
                "merge into an existing set"
            )
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = output_dir.with_name(f"{output_dir.name}.partial-{secrets.token_hex(4)}")
    if staging.exists():
        shutil.rmtree(staging)
    created_tags: List[tuple] = []

    def _fail_after_created_tags() -> None:
        """Roll back the tags this run created; keep staging as evidence
        when that rollback itself fails (#728 review finding: the commit
        seam must not silently orphan tags)."""
        remaining = _rollback_tags(created_tags)
        if remaining:
            logger.error(
                "backup failed and tag rollback also failed: staging "
                "evidence KEPT at %s; remove the live tags manually: %s",
                staging,
                sorted(remaining),
            )
        else:
            shutil.rmtree(staging, ignore_errors=True)

    try:
        _write_set(
            staging,
            lancedb_dir=lancedb_dir,
            lancedb_tables=lancedb_tables,
            vault_dirs=vault_dirs,
            draft_room_dir=draft_room_dir,
            key_provider=key_provider,
            created_tags=created_tags,
        )
    except BaseException:
        _fail_after_created_tags()
        raise
    # Commit: replace an existing (checked-empty) output directory, then
    # rename the completed staging tree into place. This seam is inside
    # the rollback envelope too: a commit-phase failure (rmdir/replace
    # error — an open handle on the pre-created output dir, an AV lock)
    # must release the created tags rather than orphan them.
    try:
        if output_dir.exists():
            output_dir.rmdir()
        os.replace(staging, output_dir)
    except BaseException:
        _fail_after_created_tags()
        raise
    return output_dir / MANIFEST_NAME


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s %(message)s"
    )
    parser = argparse.ArgumentParser(
        description="Create a consistent backup set (SQLite + LanceDB + files)"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("backups") / "set",
        help="Directory the backup set is written to",
    )
    parser.add_argument(
        "--lancedb-dir",
        type=Path,
        default=None,
        help="LanceDB directory to include (defaults to settings.lancedb_path)",
    )
    parser.add_argument(
        "--vault-dirs",
        type=Path,
        nargs="*",
        default=None,
        help="Vault directories to include (defaults to settings.vaults_dir children)",
    )
    parser.add_argument(
        "--draft-room-dir",
        type=Path,
        default=None,
        help="Draft-room input directory (defaults to <data_dir>/draft-room)",
    )
    args = parser.parse_args()

    lancedb_dir = args.lancedb_dir or Path(settings.lancedb_path)
    if args.vault_dirs is not None:
        vault_dirs: List[Path] = list(args.vault_dirs)
    else:
        # Discovery must be side-effect free (#727): settings.vaults_dir
        # MKDIRs on read, which would modify the live tree merely by
        # backing it up. Derive the same path without the property.
        vaults_root = Path(settings.data_dir) / "vaults"
        vault_dirs = (
            sorted(p for p in vaults_root.iterdir() if p.is_dir())
            if vaults_root.exists()
            else []
        )
    draft_room = args.draft_room_dir or Path(settings.data_dir) / "draft-room"

    # Table tagging against the LIVE store requires an async connection; for
    # the operator CLI path we tag via the sync lancedb API when available.
    # lancedb.connect() CREATES a missing directory, so only connect when
    # the store already exists (#727: backups must not modify live data
    # beyond creating tags).
    tables: dict = {}
    if lancedb_dir.exists():
        try:
            import lancedb

            db = lancedb.connect(str(lancedb_dir))
            for name in db.table_names():
                tables[name] = db.open_table(name)
        except Exception as exc:  # noqa: BLE001 — tagging is best effort from CLI
            print(f"warning: could not open lancedb tables for tagging: {exc}")

    manifest = create_backup_set(
        args.output,
        lancedb_dir=lancedb_dir,
        lancedb_tables=tables,
        vault_dirs=vault_dirs,
        draft_room_dir=draft_room,
    )
    print(f"Backup set written to {manifest}")


if __name__ == "__main__":
    main()
