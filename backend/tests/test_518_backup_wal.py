"""Issue #518 acceptance check C5a / AC5 (WAL half) — the SQLite backup
primitive must capture committed-but-uncheckpointed WAL transactions.

Behavior spec (frozen): ``scripts/backup_sqlite.py::backup_sqlite`` (or its
WAL-safe replacement behind the same import) must produce an encrypted
artifact whose decrypted copy contains EVERY transaction that was committed
at backup time, even when those transactions live only in ``<db>-wal``
because an open read transaction pins the WAL (a live app / a write held
open during backup). The copy primitive must be WAL-safe
(``sqlite3.Connection.backup()`` or ``VACUUM INTO``), not a raw
``read_bytes()`` of the main file.

This test FAILS on current master: the raw copy contains no table at all
(reproduced by `.agents/issue-traces/518-e3-admission-telemetry-backup/
repro/repro_backup_wal.py`).
"""

import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import backup_sqlite as backup_module  # noqa: E402

TEST_KEY = b"0123456789abcdef0123456789abcdef"  # 32 bytes
ROW_COUNT = 50


def _count_rows(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    finally:
        conn.close()


def test_backup_captures_committed_wal_rows_while_txn_open():
    with tempfile.TemporaryDirectory() as tmpname:
        tmp = Path(tmpname)
        db = tmp / "app.db"
        out_dir = tmp / "backups"

        writer = sqlite3.connect(db)
        pin = sqlite3.connect(db)
        try:
            writer.execute("PRAGMA journal_mode=WAL;")
            writer.execute("PRAGMA wal_autocheckpoint=0;")
            writer.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT);")
            writer.commit()

            # Open a read transaction FIRST so committed pages cannot be
            # checkpointed back into the main file — models a live app or a
            # write held open during the backup.
            pin.execute("BEGIN;")
            pin.execute("SELECT COUNT(*) FROM t").fetchone()

            for i in range(1, ROW_COUNT + 1):
                writer.execute(
                    "INSERT INTO t (id, val) VALUES (?, ?)", (i, f"row-{i}")
                )
            writer.commit()  # committed — resident in app.db-wal only

            wal = tmp / "app.db-wal"
            wal_size = wal.stat().st_size if wal.exists() else 0
            source_rows = _count_rows(db)
            assert source_rows == ROW_COUNT
            assert wal_size > 0, (
                "518-C5A HARNESS: WAL is empty — the pinning read txn did "
                "not prevent checkpointing; test cannot discriminate"
            )

            # Script-module seams: settings instance the script reads and the
            # SecretManager name it calls (AES key fixed for the test).
            monkeypatch = pytest.MonkeyPatch()
            try:
                monkeypatch.setattr(backup_module.settings, "data_dir", tmp)
                monkeypatch.setattr(
                    backup_module,
                    "SecretManager",
                    lambda: SimpleNamespace(
                        get_aes_key=lambda: (TEST_KEY, "9")
                    ),
                )

                backup_path = backup_module.backup_sqlite(out_dir)
            finally:
                monkeypatch.undo()

            assert backup_path.exists() and backup_path.stat().st_size > 0, (
                "518-C5A HARNESS: backup artifact missing/empty"
            )

            # Restore side: decrypt the artifact and verify EVERY committed
            # row is present in the restored copy.
            blob = backup_path.read_bytes()
            nonce, ciphertext = blob[:12], blob[12:]
            plain = AESGCM(TEST_KEY).decrypt(nonce, ciphertext, None)
            restored = out_dir / "restored.db"
            restored.write_bytes(plain)

            try:
                restored_rows = _count_rows(restored)
            except sqlite3.DatabaseError as exc:
                pytest.fail(
                    "518-C5A WAL BACKUP LOSES COMMITTED ROWS: restored copy "
                    f"is not a usable database ({type(exc).__name__}: {exc}) "
                    "— committed-but-uncheckpointed WAL transactions were "
                    "not captured (raw read_bytes() of the main file)"
                )
            assert restored_rows == source_rows, (
                "518-C5A WAL BACKUP LOSES COMMITTED ROWS: restored copy has "
                f"{restored_rows} rows, source had {source_rows} committed "
                "rows while the WAL was pinned — backup must be WAL-safe "
                "(sqlite3 backup() or VACUUM INTO), never a raw file copy"
            )
        finally:
            pin.close()
            writer.close()
