"""Migration helper that toggles maintenance, backups, and schema updates."""

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT))

# Set up basic environment for migrations
os.environ.setdefault("DATA_DIR", str(ROOT / "data"))

from backend.app.config import settings
from backend.app.models.database import SQLiteConnectionPool, init_db, run_migrations
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
    """Fallback backup function if backup_sqlite module is not available."""
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup_path = output_dir / f"app_backup_{timestamp}.db"
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    source = Path(settings.sqlite_path)
    if source.exists():
        shutil.copy2(source, backup_path)
        print(f"Created backup at {backup_path}")
    return backup_path


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
        # Plain SQLite backup - just copy
        shutil.copy2(backup_path, target_path)
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
        target_path.write_bytes(plaintext)
        print(f"Restored encrypted backup using key {key_version}")
        return target_path
    except ImportError:
        raise RuntimeError(
            "Backup appears to be encrypted but cryptography library is not available. "
            "Install it with: pip install cryptography"
        )


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
    
    # Initialize database schema first
    init_db(str(settings.sqlite_path))
    
    maintenance_pool = SQLiteConnectionPool(str(settings.sqlite_path), max_size=2)
    try:
        # Initialize maintenance service
        maintenance = MaintenanceService(maintenance_pool)

        if rollback:
            if not backup:
                raise SystemExit("Rollback requires --backup")
            if not backup.exists():
                raise SystemExit(f"Backup file not found: {backup}")

            decrypt_backup(backup)
            print(f"Rollback completed successfully from {backup}")
            return

        # Enable maintenance mode during migration
        maintenance.set_flag(True, "migration in progress")
        try:
            # Create backup before migration
            backup_dir = Path("backups")
            backup_dir.mkdir(parents=True, exist_ok=True)

            if HAS_BACKUP_MODULE and backup_sqlite is not None:
                backup_path = backup_sqlite(backup_dir)  # type: ignore
            else:
                backup_path = backup_sqlite_fallback(backup_dir)

            print(f"Backup created: {backup_path}")

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
