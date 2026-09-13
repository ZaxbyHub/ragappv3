"""Create AES-GCM encrypted backups of the SQLite database.

The copy primitive is WAL-safe (issue #518, legacy-12/C08/E04): the database
is copied via ``sqlite3.Connection.backup()`` into an in-memory snapshot, so
committed transactions that still live in the ``-wal`` file (a live app, or a
write held open during backup) are captured — a raw ``read_bytes()`` of the
main file is neither complete nor consistent under WAL.
"""

import argparse
import secrets
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT))

from backend.app.config import settings
from backend.app.services.secret_manager import SecretManager


def snapshot_sqlite(sqlite_path: Path) -> bytes:
    """Return a WAL-consistent snapshot of the database via the sqlite3
    backup API (works while the app holds the DB open, including an open
    write transaction elsewhere)."""
    source = sqlite3.connect(str(sqlite_path))
    try:
        dest = sqlite3.connect(":memory:")
        try:
            source.backup(dest)
            return dest.serialize()
        finally:
            dest.close()
    finally:
        source.close()


def backup_sqlite(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    key, version = SecretManager().get_aes_key()
    sqlite_path = Path(settings.sqlite_path)
    nonce = secrets.token_bytes(12)
    aesgcm = AESGCM(key)
    data = snapshot_sqlite(sqlite_path)
    ciphertext = aesgcm.encrypt(nonce, data, None)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    backup_path = output_dir / f"db_{timestamp}_k{version}.enc"
    with open(backup_path, "wb") as fout:
        fout.write(nonce)
        fout.write(ciphertext)
    return backup_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Encrypt SQLite backup using AES-GCM")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("backups"),
        help="Directory where encrypted backups are stored",
    )
    args = parser.parse_args()

    backup_path = backup_sqlite(args.output)
    print(f"Backup written to {backup_path}")


if __name__ == "__main__":
    main()
