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
import logging
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Callable, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT))

from backend.app.config import settings
from backend.app.services.secret_manager import SecretManager
from scripts.backup_set import _restrict_permissions

logger = logging.getLogger(__name__)

MANIFEST_NAME = "backup_manifest.json"

# SQLite identifiers cannot be bound as parameters; every interpolated table
# name in this module is validated against this pattern first.
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class RestoreError(Exception):
    """A backup set failed verification or could not be restored."""


def _contained(base: Path, candidate: Path) -> bool:
    """True when ``candidate`` resolves inside ``base`` (no ``..`` escapes,
    no absolute-path hijacks — swarm review F-005)."""
    try:
        candidate.resolve().relative_to(base.resolve())
    except ValueError:
        return False
    return True


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
            if not _IDENTIFIER_RE.fullmatch(table):
                # Belt-and-braces: sqlite_master names come from the
                # decrypted backup, not user input, but identifiers cannot
                # be bound parameters — validate before interpolation.
                raise RestoreError(
                    f"unexpected table name in restored db: {table!r}"
                )
            total += conn.execute(
                f'SELECT COUNT(*) FROM "{table}"'  # nosec B608 — identifier validated against _IDENTIFIER_RE above; no values interpolated
            ).fetchone()[0]
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
        # Path containment (swarm review F-005): every manifest path must
        # stay inside the backup set on the source side and inside the
        # destination root on the restore side — a crafted manifest must
        # never rmtree/copytree outside either root.
        item_path = str(item.get("path", ""))
        if not item_path:
            raise RestoreError(f"manifest item {name!r} has no path")
        if not _contained(backup_dir, backup_dir / item_path):
            raise RestoreError(
                f"manifest path escapes the backup set: {item_path!r}"
            )
        if not _contained(dest_root, dest_root / item_path):
            raise RestoreError(
                f"manifest path escapes the destination root: {item_path!r}"
            )

        if kind == "sqlite":
            artifact = backup_dir / item_path
            if not artifact.exists():
                raise RestoreError(f"sqlite artifact missing: {artifact}")
            if _sha256_file(artifact) != item.get("sha256"):
                raise RestoreError(
                    f"sqlite artifact digest mismatch: {artifact}"
                )
            blob = artifact.read_bytes()
            nonce, ciphertext = blob[:12], blob[12:]
            key, key_version = provider()
            try:
                plain = AESGCM(key).decrypt(nonce, ciphertext, None)
            except Exception as exc:
                hint = ""
                manifest_version = item.get("key_version")
                if manifest_version and str(manifest_version) != str(
                    key_version
                ):
                    hint = (
                        " (manifest was encrypted under key version "
                        f"{manifest_version!r}; the current provider serves "
                        f"{key_version!r})"
                    )
                raise RestoreError(
                    f"sqlite artifact failed to decrypt: {exc}{hint}"
                ) from exc
            dest_db = dest_root / "app.db"
            dest_db.write_bytes(plain)
            os.chmod(dest_db, 0o600)
            sqlite_rows = _count_sqlite_rows(dest_db)

        elif kind in ("lancedb", "vault", "draft_room"):
            src_dir = backup_dir / item_path
            if not src_dir.exists():
                raise RestoreError(f"{kind} artifact missing: {src_dir}")
            expected_files = item.get("files") or {}
            if not expected_files:
                # An empty/missing files map would "verify" zero digests and
                # still report success — that falsifies the module contract
                # (swarm review F-005).
                raise RestoreError(
                    f"{kind} artifact {item_path!r} binds no file digests; "
                    "an unverifiable tree is not restorable"
                )
            for rel, digest in expected_files.items():
                candidate = src_dir / rel
                if not _contained(src_dir, candidate):
                    raise RestoreError(
                        f"manifest file escapes the artifact tree: {rel!r}"
                    )
                if not candidate.exists():
                    raise RestoreError(
                        f"manifest file missing from backup set: {rel}"
                    )
                if _sha256_file(candidate) != digest:
                    raise RestoreError(
                        f"artifact digest mismatch: {item_path}/{rel}"
                    )
            dest_dir = dest_root / item_path
            dest_dir.parent.mkdir(parents=True, exist_ok=True)
            if dest_dir.exists():
                shutil.rmtree(dest_dir)
            shutil.copytree(src_dir, dest_dir)
            _restrict_permissions(dest_dir)
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

    _verify_generation_binding(manifest, dest_root)

    return {
        "verified": verified and all(entry["ok"] for entry in items_out),
        "items": items_out,
        "sqlite_rows": sqlite_rows,
    }


def _verify_generation_binding(manifest: dict, dest_root: Path) -> None:
    """Cross-check the C1 authoritative generation (#518 G6).

    When a lancedb manifest item carries a ``generation`` binding (the row
    the backup read FROM THE SNAPSHOT's migration_journal — the LATEST one
    at snapshot time), the restored SQLite's LATEST index-generation row
    must be that same id. Comparing against MAX(id) (PR #595 review F-003)
    is what makes the check non-tautological: presence-of-id alone could
    never fail on an honest set, because the restored DB is byte-identical
    to the snapshot the binding came from. A restored DB NEWER than the
    binding (an interleaved-write set that paired a newer SQLite with an
    older vector tree) fails HERE with both ids named. A DB older than the
    binding fails the row lookup below. This remains a structural sanity
    check, not tamper-proofing: a fully doctored set could rewrite both
    sides. Legacy sets without the field restore with a WARNING.
    """
    binding = None
    for item in manifest.get("items", []):
        if item.get("kind") == "lancedb":
            binding = item.get("generation")
            break
    if binding is None:
        logger.warning(
            "manifest carries no generation binding (legacy set or fresh "
            "install with no journal row); restore proceeds without the "
            "C1 coherence check"
        )
        return
    raw_id = binding.get("id")
    try:
        bound_id = int(raw_id)
    except (TypeError, ValueError):
        raise RestoreError(
            f"generation binding has a malformed id: {raw_id!r}"
        ) from None
    dest_db = dest_root / "app.db"
    if not dest_db.exists():
        raise RestoreError(
            "generation binding present but the sqlite artifact was not "
            "restored alongside the lancedb tree"
        )
    conn = sqlite3.connect(str(dest_db))
    try:
        row = conn.execute(
            "SELECT MAX(id) FROM migration_journal"
            " WHERE migration_name LIKE 'index_generation:%'"
        ).fetchone()
        bound_row = conn.execute(
            "SELECT id FROM migration_journal WHERE id = ?", (bound_id,)
        ).fetchone()
    finally:
        conn.close()
    if bound_row is None:
        raise RestoreError(
            "generation binding mismatch: the manifest binds "
            f"migration_journal id {bound_id} "
            f"({binding.get('name')!r}) but the restored sqlite does not "
            "contain that row — this set pairs a sqlite snapshot with an "
            "inconsistent vector tree; do not use it"
        )
    latest = int(row[0]) if row and row[0] is not None else None
    if latest is not None and latest > bound_id:
        raise RestoreError(
            "generation binding mismatch: the manifest binds generation "
            f"{bound_id} but the restored sqlite's latest index generation "
            f"is {latest} — this set pairs a NEWER sqlite snapshot with an "
            "OLDER vector tree; do not use it"
        )


def _cli_lancedb_factory(
    dest_root: Path,
) -> Callable[[str], object]:
    """Open the RESTORED lancedb tree lazily and return a table factory.

    The operator CLI must actually perform the documented tag checkout
    (swarm review NEW-LANCEDB-UNWIRED): the module docstring,
    docs/operations.md and the admin guide all promise
    ``checkout(tag)`` on restore, which never ran when main() omitted the
    factory. Connection is deferred to the first call so it happens AFTER
    the lancedb tree has been restored into ``dest_root``.
    """
    import lancedb

    state: dict = {}

    def factory(name: str):
        if "db" not in state:
            state["db"] = lancedb.connect(str(dest_root / "lancedb"))
        return state["db"].open_table(name)

    return factory


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

    try:
        factory = _cli_lancedb_factory(dest)
    except ImportError:
        raise SystemExit(
            "restore requires the lancedb package to check out tagged "
            "table versions; install the backend requirements first"
        )
    report = restore_backup_set(
        args.backup_dir, dest, lancedb_factory=factory
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
