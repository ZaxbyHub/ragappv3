"""Migration helper that toggles maintenance, backups, and schema updates."""

import argparse
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT))

# Set up basic environment for migrations
os.environ.setdefault("DATA_DIR", str(ROOT / "data"))

from backend.app.config import settings
from backend.app.models.database import (
    _SYSTEM_FLAGS_DDL,
    SQLiteConnectionPool,
    init_db,
    run_migrations,
)
from backend.app.services.maintenance import MaintenanceService

# Import optional dependencies
SecretManager = None
HAS_SECRET_MANAGER = False
try:
    from backend.app.services.secret_manager import SecretManager as _SecretManager
    SecretManager = _SecretManager
    HAS_SECRET_MANAGER = True
except ImportError:
    pass

# Import backup function if available
backup_sqlite = None
HAS_BACKUP_MODULE = False
try:
    from scripts.backup_sqlite import backup_sqlite as _backup_sqlite
    backup_sqlite = _backup_sqlite
    HAS_BACKUP_MODULE = True
except ImportError:
    pass


def backup_sqlite_fallback(output_dir: Path) -> Path:
    """Fallback backup function if backup_sqlite module is not available.

    WAL-safe copy (issue #518, legacy-12/C08): the raw ``shutil.copy2`` of
    the main file was replaced by a sqlite3 backup-API snapshot — committed
    transactions still living in the -wal file are captured even while the
    app holds the database open.
    """
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup_path = output_dir / f"app_backup_{timestamp}.db"
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    source = Path(settings.sqlite_path)
    if source.exists():
        src_conn = sqlite3.connect(str(source))
        try:
            dst_conn = sqlite3.connect(str(backup_path))
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
        finally:
            src_conn.close()
        print(f"Created backup at {backup_path}")
    return backup_path


def _stage_and_replace(target_path: Path, fill) -> None:
    """Stage a restore copy and atomically replace ``target_path``.

    Issue #705 review (PRR-001/C01): a restore that truncates the live file
    in place is torn by any mid-copy failure, and — worse — leaves the
    target's SQLite sidecars (``-wal``/``-shm``/``-journal``) behind, so the
    next connection replays the PRE-restore WAL over the restored file and
    silently resurrects the old database. The staged file makes the copy
    crash-safe, the sidecars are removed only after staging succeeded, and
    ``os.replace`` swaps the restored file in atomically. ``copyfile`` plus
    an explicit writable mode keeps a read-only backup's mode/attribute from
    propagating to the live database (``copy2``'s ``copystat`` would).
    """
    staged = target_path.with_name(target_path.name + ".restore-tmp")
    try:
        fill(staged)
        os.chmod(staged, 0o644)
        for suffix in ("-wal", "-shm", "-journal"):
            try:
                target_path.with_name(target_path.name + suffix).unlink()
            except FileNotFoundError:
                pass
        os.replace(staged, target_path)
    except BaseException:
        try:
            staged.unlink()
        except FileNotFoundError:
            pass
        raise


def decrypt_backup(backup_path: Path, target_path: Path | None = None) -> Path:
    """
    Decrypt and restore a backup file.
    
    Supports both encrypted backups (with AESGCM) and plain SQLite backups.
    
    Args:
        backup_path: Path to the backup file
        target_path: Where to restore the database (defaults to settings.sqlite_path)
    
    Returns:
        Path to the restored database
    """
    if target_path is None:
        target_path = Path(settings.sqlite_path)
    
    # Ensure target directory exists
    target_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Check if this is an encrypted backup (starts with nonce-like data)
    data = backup_path.read_bytes()
    
    # Simple heuristic: if file starts with SQLite header, it's not encrypted
    if data[:16] == b'SQLite format 3\x00':
        # Plain SQLite backup - staged copy + atomic replace
        _stage_and_replace(target_path, lambda staged: shutil.copyfile(backup_path, staged))
        print(f"Restored plain SQLite backup to {target_path}")
        return target_path
    
    # Try to decrypt using AESGCM if available
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        
        if not HAS_SECRET_MANAGER or SecretManager is None:
            raise RuntimeError("SecretManager not available for decryption")

        secret_manager = SecretManager()  # type: ignore
        key, key_version = secret_manager.get_aes_key()
        
        nonce = data[:12]
        ciphertext = data[12:]
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, None)
        _stage_and_replace(target_path, lambda staged: staged.write_bytes(plaintext))
        print(f"Restored encrypted backup using key {key_version}")
        return target_path
    except ImportError:
        raise RuntimeError(
            "Backup appears to be encrypted but cryptography library is not available. "
            "Install it with: pip install cryptography"
        )


def _bootstrap_system_flags(pool: SQLiteConnectionPool) -> None:
    """CREATE the system_flags table if a legacy database predates it.

    MaintenanceService construction INSERTs into system_flags immediately
    (issue #705): on a legacy DB whose schema predates the table, the service
    would raise "no such table" before the maintenance flag — and the
    pre-migration backup — could ever be taken. The DDL constant is the app's
    own degraded-boot double-definition pattern (models/database.py), so no
    schema is duplicated here.
    """
    conn = pool.get_connection()
    try:
        conn.execute(_SYSTEM_FLAGS_DDL)
        conn.commit()
    finally:
        pool.release_connection(conn)


def _force_clear_maintenance(sqlite_path: str) -> bool:
    """Clear maintenance mode on a connection opened AFTER a restore.

    The pre-migration backup is taken while maintenance is enabled, so a
    restored backup carries maintenance=1 and would boot the app straight
    into maintenance mode (issue #705). The clear handles restored databases
    in any state (no system_flags table, no maintenance row, row stuck at 1)
    and bumps the flag row's version/reason so the change is auditable.

    Returns True when the clear committed AND a ``PRAGMA quick_check`` on the
    restored database passes; False otherwise. The caller must treat False
    as a failed rollback verification, not print an unconditional success.
    """
    try:
        conn = sqlite3.connect(sqlite_path, timeout=30)
        try:
            conn.execute(_SYSTEM_FLAGS_DDL)
            conn.execute(
                "INSERT OR IGNORE INTO system_flags(name, value, version, reason)"
                " VALUES ('maintenance', 0, 0, '')"
            )
            conn.execute(
                "UPDATE system_flags"
                " SET value = 0, reason = 'rollback maintenance clear',"
                " version = version + 1"
                " WHERE name = 'maintenance'"
            )
            conn.commit()
            check = conn.execute("PRAGMA quick_check").fetchone()
            return bool(check) and check[0] == "ok"
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(
            f"WARNING: could not clear maintenance mode after rollback ({exc})."
            " The restored database may boot the application in maintenance"
            " mode — clear it manually (system_flags name='maintenance' -> 0)."
        )
        return False


def migrate(rollback: bool, backup: Path | None, retention: int) -> None:
    """
    Run database migrations with maintenance mode and backups.

    Args:
        rollback: If True, restore from backup instead of migrating
        backup: Path to backup file for rollback
        retention: Number of days to retain backups (not yet implemented)
    """
    # Ensure database directory exists
    Path(settings.sqlite_path).parent.mkdir(parents=True, exist_ok=True)

    maintenance_pool = SQLiteConnectionPool(str(settings.sqlite_path), max_size=2)
    try:
        # Legacy databases may predate system_flags; bootstrap it before the
        # MaintenanceService constructor touches it (issue #705).
        _bootstrap_system_flags(maintenance_pool)

        # Initialize maintenance service
        maintenance = MaintenanceService(maintenance_pool)

        if rollback:
            if not backup:
                raise SystemExit("Rollback requires --backup")
            if not backup.exists():
                raise SystemExit(f"Backup file not found: {backup}")

            print(
                "WARNING: stop the application before rolling back. The"
                " restore replaces the database file; a live writer can"
                " corrupt or resurrect pre-restore content."
            )
            # Release every pooled handle BEFORE the restore so no script
            # connection writes through a replaced database file (issue
            # #705). The restore itself is staged and atomic and removes the
            # target's stale WAL sidecars (see _stage_and_replace). This
            # branch deliberately never enables maintenance — a decrypt_backup
            # failure must not strand maintenance=1 — and returns before the
            # migration try/finally below, whose finally must never run
            # set_flag on the closed pool.
            maintenance_pool.close_all()
            decrypt_backup(backup)
            if _force_clear_maintenance(str(settings.sqlite_path)):
                print(f"Rollback completed successfully from {backup}")
            else:
                print(
                    f"Rollback restored {backup}, but the maintenance flag"
                    " could not be cleared or the restored database failed"
                    " its integrity check — set system_flags"
                    " name='maintenance' to 0 manually (and restore from the"
                    " backup again if integrity failed) before starting the"
                    " app."
                )
                raise SystemExit(1)
            return

        # Enable maintenance mode during migration — before the backup and
        # before init_db's ALTERs, so the whole mutating window is covered.
        maintenance.set_flag(True, "migration in progress")
        try:
            # Create backup BEFORE init_db (issue #705): init_db's ALTER loop
            # and executescript mutate the schema, and --rollback must restore
            # the true pre-migration state, not a post-init one.
            backup_dir = Path("backups")
            backup_dir.mkdir(parents=True, exist_ok=True)

            if HAS_BACKUP_MODULE and backup_sqlite is not None:
                backup_path = backup_sqlite(backup_dir)  # type: ignore
            else:
                backup_path = backup_sqlite_fallback(backup_dir)

            print(f"Backup created: {backup_path}")

            # Initialize database schema AFTER the backup
            init_db(str(settings.sqlite_path))

            # Run migrations
            run_migrations(str(settings.sqlite_path))
            print("Migrations completed successfully")

        except Exception as e:
            print(f"Migration failed: {e}")
            raise
        finally:
            # Always disable maintenance mode
            maintenance.set_flag(False, "migration complete")
    finally:
        maintenance_pool.close_all()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run migrations with maintenance mode + backups"
    )
    parser.add_argument(
        "--rollback",
        action="store_true",
        help="Restore from backup"
    )
    parser.add_argument(
        "--backup",
        type=Path,
        help="Backup file to restore when rolling back"
    )
    parser.add_argument(
        "--retention",
        type=int,
        default=30,
        help="Retention days for backups"
    )
    parser.add_argument(
        "--init-only",
        action="store_true",
        help="Only initialize schema without running migrations"
    )
    args = parser.parse_args()
    
    if args.init_only:
        # Just initialize the database schema
        Path(settings.sqlite_path).parent.mkdir(parents=True, exist_ok=True)
        init_db(str(settings.sqlite_path))
        print(f"Database initialized at {settings.sqlite_path}")
        return
    
    migrate(args.rollback, args.backup, args.retention)


if __name__ == "__main__":
    main()
