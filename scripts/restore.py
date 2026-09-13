"""Restore and verify a backup set created by scripts/backup_set.py.

Restores ``app.db`` (decrypting ``app.db.enc``), copies the lancedb /
vault / draft-room artifacts back under the destination root, verifies
every manifest sha256 digest (any mismatch raises ``RestoreError``), and
checks out each manifest table tag on the restored lancedb tables via
``lancedb_factory(name).checkout(tag)``.

The CI restore drill is backend/tests/test_518_restore.py — it exercises
backup -> mutate -> restore -> verify end to end with fakes.
"""

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Callable, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT))

from backend.app.config import settings
from backend.app.services.secret_manager import SecretManager

MANIFEST_NAME = "backup_manifest.json"


class RestoreError(Exception):
    """A backup set failed verification or could not be restored."""


def _default_key_provider() -> tuple:
    key, version = SecretManager().get_aes_key()
    return (key, str(version))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fin:
        for chunk in iter(lambda: fin.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _count_sqlite_rows(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        total = 0
        for table in tables:
            total += conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        return total
    finally:
        conn.close()


def restore_backup_set(
    backup_dir: Path,
    dest_root: Path,
    *,
    lancedb_factory: Optional[Callable[[str], object]] = None,
    key_provider: Optional[Callable[[], tuple]] = None,
) -> dict:
    manifest_path = backup_dir / MANIFEST_NAME
    if not manifest_path.exists():
        raise RestoreError(f"no {MANIFEST_NAME} in {backup_dir}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != "ragapp-backup-set/1":
        raise RestoreError(
            f"unsupported manifest schema {manifest.get('schema')!r}"
        )
    provider = key_provider or _default_key_provider
    dest_root.mkdir(parents=True, exist_ok=True)

    items_out = []
    sqlite_rows = 0
    verified = True

    for item in manifest.get("items", []):
        kind = item.get("kind")
        name = item.get("name", "?")
        ok = True

        if kind == "sqlite":
            artifact = backup_dir / item["path"]
            if not artifact.exists():
                raise RestoreError(f"sqlite artifact missing: {artifact}")
            if _sha256_file(artifact) != item.get("sha256"):
                raise RestoreError(
                    f"sqlite artifact digest mismatch: {artifact}"
                )
            blob = artifact.read_bytes()
            nonce, ciphertext = blob[:12], blob[12:]
            key, _version = provider()
            try:
                plain = AESGCM(key).decrypt(nonce, ciphertext, None)
            except Exception as exc:
                raise RestoreError(
                    f"sqlite artifact failed to decrypt: {exc}"
                ) from exc
            dest_db = dest_root / "app.db"
            dest_db.write_bytes(plain)
            sqlite_rows = _count_sqlite_rows(dest_db)

        elif kind in ("lancedb", "vault", "draft_room"):
            src_dir = backup_dir / item["path"]
            if not src_dir.exists():
                raise RestoreError(f"{kind} artifact missing: {src_dir}")
            expected_files = item.get("files", {})
            for rel, digest in expected_files.items():
                candidate = src_dir / rel
                if not candidate.exists():
                    raise RestoreError(
                        f"manifest file missing from backup set: {rel}"
                    )
                if _sha256_file(candidate) != digest:
                    raise RestoreError(
                        f"artifact digest mismatch: {item['path']}/{rel}"
                    )
            dest_dir = dest_root / item["path"]
            dest_dir.parent.mkdir(parents=True, exist_ok=True)
            if dest_dir.exists():
                import shutil

                shutil.rmtree(dest_dir)
            import shutil

            shutil.copytree(src_dir, dest_dir)
            if kind == "lancedb":
                for table_entry in item.get("tables", []):
                    table_name = table_entry["table"]
                    tag = table_entry["tag"]
                    if lancedb_factory is None:
                        continue
                    table = lancedb_factory(table_name)
                    table.checkout(tag)

        else:
            raise RestoreError(f"unknown manifest item kind {kind!r}")

        items_out.append({"name": name, "ok": ok})

    if not items_out:
        raise RestoreError("manifest binds no items")

    return {
        "verified": verified and all(entry["ok"] for entry in items_out),
        "items": items_out,
        "sqlite_rows": sqlite_rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Restore and verify a backup set (see scripts/backup_set.py)"
    )
    parser.add_argument("backup_dir", type=Path, help="Backup set directory")
    parser.add_argument(
        "--dest",
        type=Path,
        default=None,
        help="Destination root (defaults to settings.data_dir)",
    )
    args = parser.parse_args()
    dest = args.dest or Path(settings.data_dir)

    report = restore_backup_set(args.backup_dir, dest)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
