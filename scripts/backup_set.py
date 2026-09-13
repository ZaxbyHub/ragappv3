"""Consistent backup sets binding SQLite, LanceDB and source/artifact files
(issue #518, Workstream E3 — legacy-12/E04).

A backup set is a directory containing:

* ``app.db.enc`` — AES-GCM (12-byte nonce || ciphertext) WAL-safe snapshot
  of ``settings.sqlite_path`` taken via the sqlite3 backup API;
* the whole ``lancedb/`` tree, with each named table's version pinned by a
  ``backup-<timestamp>`` tag BEFORE the copy (tagged versions survive later
  ``optimize()`` compaction, so a restore checks out the exact generation);
* any vault directories and the draft-room directory, copied verbatim;
* ``backup_manifest.json`` — schema ``ragapp-backup-set/1`` binding every
  artifact with sha256 digests (and per-table version tags) so restore can
  verify integrity end to end.

Use ``scripts/restore.py`` to restore and verify a set; the scheduled
operation (host cron / Windows Task Scheduler) is documented in
docs/operations.md and docs/admin-guide.md.
"""

import argparse
import hashlib
import json
import os
import secrets
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Mapping, Optional, Sequence

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT))

from backend.app.config import settings
from backend.app.services.secret_manager import SecretManager
from scripts.backup_sqlite import snapshot_sqlite

MANIFEST_NAME = "backup_manifest.json"
MANIFEST_SCHEMA = "ragapp-backup-set/1"
SQLITE_ARTIFACT = "app.db.enc"
LANCEDB_DIRNAME = "lancedb"


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
    ``backup-<timestamp>`` tag BEFORE the lancedb tree is copied, so the
    copied generation is the tagged one and restore can ``checkout(tag)``.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    provider = key_provider or _default_key_provider
    key, key_version = provider()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")

    # --- SQLite: WAL-safe snapshot, then encrypt ---
    nonce = secrets.token_bytes(12)
    plaintext = snapshot_sqlite(Path(settings.sqlite_path))
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)
    sqlite_artifact = output_dir / SQLITE_ARTIFACT
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
    if lancedb_dir is not None:
        backup_lancedb = output_dir / LANCEDB_DIRNAME
        tag = f"backup-{timestamp}"
        for name, table in (lancedb_tables or {}).items():
            table_tag = f"{tag}-{name}"
            table.create_tag(table_tag)
            tables_out.append({"table": name, "tag": table_tag})
        _copy_tree(lancedb_dir, backup_lancedb)
        items.append(
            {
                "name": LANCEDB_DIRNAME,
                "kind": "lancedb",
                "path": LANCEDB_DIRNAME,
                "tables": tables_out,
                "files": _digest_tree(backup_lancedb),
            }
        )

    # --- Vault directories ---
    # Manifest paths are data_dir-relative: vaults live at
    # <data_dir>/vaults/<id> (config.py vault_dir), so the artifact path is
    # "vaults/<id>" — a bare leaf name would restore vaults to the wrong
    # location (Copilot review on PR #589).
    for vault_dir in vault_dirs:
        name = vault_dir.name
        vault_rel = f"vaults/{name}"
        target = output_dir / vault_rel
        _copy_tree(vault_dir, target)
        items.append(
            {
                "name": name,
                "kind": "vault",
                "path": vault_rel,
                "files": _digest_tree(target),
            }
        )

    # --- Draft room ---
    if draft_room_dir is not None:
        target = output_dir / "draft-room"
        _copy_tree(draft_room_dir, target)
        items.append(
            {
                "name": "draft-room",
                "kind": "draft_room",
                "path": "draft-room",
                "files": _digest_tree(target),
            }
        )

    manifest_path = output_dir / MANIFEST_NAME
    manifest_path.write_text(
        json.dumps({"schema": MANIFEST_SCHEMA, "items": items}, indent=2),
        encoding="utf-8",
    )
    _restrict_permissions(output_dir)
    os.chmod(manifest_path, 0o600)
    return manifest_path


def main() -> None:
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
        vaults_root = Path(settings.vaults_dir)
        vault_dirs = (
            sorted(p for p in vaults_root.iterdir() if p.is_dir())
            if vaults_root.exists()
            else []
        )
    draft_room = args.draft_room_dir or Path(settings.data_dir) / "draft-room"

    # Table tagging against the LIVE store requires an async connection; for
    # the operator CLI path we tag via the sync lancedb API when available.
    tables: dict = {}
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
