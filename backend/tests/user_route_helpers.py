"""Shared helpers for user-route style backend tests.

The route-test database is built from the PRODUCTION schema (init_db +
run_migrations) instead of a hand-copied TEST_SCHEMA, so fixture drift from the
real database shape is impossible by construction (issue #744 AC1/AC2): the
test DB carries every production table (including security_audit_log and
user_sessions, which the audit-emission and session-revocation assertions in
test_users_routes.py read back) and declares no vaults foreign keys production
does not have. The production schema is built ONCE per pytest session into a
template file and copied per test — init_db + run_migrations costs ~1.5s, far
too much to repeat per test (~200 consumers).

Hermeticity: run_migrations transitively touches the filesystem through the
settings-derived paths (settings.vaults_dir / settings.sqlite_path are read-only
properties of settings.data_dir — app/config.py), so the template build pins
settings.data_dir to a private temp dir and restores it in a finally block;
init_db/run_migrations always receive the explicit absolute template path.
"""

import shutil
import sqlite3
import tempfile
import threading
from pathlib import Path
from typing import Optional

from app.services.auth_service import (
    compute_client_fingerprint,
    create_access_token,
    hash_password,
)

_TEMPLATE_LOCK = threading.Lock()
_TEMPLATE_DIR: Optional[Path] = None
_TEMPLATE_DB: Optional[Path] = None


def _build_template_db() -> Path:
    """Build the production schema once per session under a private temp dir.

    settings.data_dir is pinned for the duration of the build (vault path
    migrations read settings-derived paths) and restored even on failure.
    """
    from app.config import settings
    from app.models.database import init_db, run_migrations

    template_dir = Path(tempfile.mkdtemp(prefix="route-schema-template-"))
    template_db = template_dir / "template.db"

    previous_data_dir = settings.data_dir
    try:
        settings.data_dir = template_dir  # type: ignore[assignment]
        init_db(str(template_db))
        run_migrations(str(template_db))
    finally:
        settings.data_dir = previous_data_dir

    # init_db/run_migrations connections are closed by their own finally
    # blocks; verify no WAL sidecar survives into the copies we hand out.
    for sidecar in (template_dir / "template.db-wal", template_dir / "template.db-shm"):
        sidecar.unlink(missing_ok=True)
    return template_db


def _template_path() -> Path:
    global _TEMPLATE_DIR, _TEMPLATE_DB
    with _TEMPLATE_LOCK:
        if _TEMPLATE_DB is None or not _TEMPLATE_DB.exists():
            _TEMPLATE_DIR = Path(tempfile.mkdtemp(prefix="route-schema-cache-"))
            _TEMPLATE_DB = _TEMPLATE_DIR / "template.db"
            shutil.copyfile(_build_template_db(), _TEMPLATE_DB)
        return _TEMPLATE_DB


def reset_route_schema_template() -> None:
    """Drop the session-cached template (used by the isolation tests)."""
    global _TEMPLATE_DIR, _TEMPLATE_DB
    with _TEMPLATE_LOCK:
        if _TEMPLATE_DIR is not None:
            shutil.rmtree(_TEMPLATE_DIR, ignore_errors=True)
        _TEMPLATE_DIR = None
        _TEMPLATE_DB = None


def setup_test_db(
    db_path: str,
    *,
    include_default_vault: bool = False,
    include_locked_until_index: bool = False,
) -> sqlite3.Connection:
    """Set up a user-route test database from the production schema.

    Copies the session-cached init_db+run_migrations template to db_path and
    connects with the same pragmas the route tests rely on (WAL, enforced
    foreign keys). The copy is a plain file copy: each test owns an
    independent writable database (proven by test_h07_schema_template_isolation).
    """
    shutil.copyfile(_template_path(), db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")

    if include_default_vault:
        conn.execute(
            "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (1, 'Default', 'Default vault')"
        )
    if include_locked_until_index:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_locked_until ON users(locked_until)"
        )

    conn.commit()
    return conn


def create_user(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    role: str,
    full_name: str = "",
    is_active: int = 1,
    must_change_password: int = 0,
) -> int:
    """Create a test user and return its ID."""
    hashed = hash_password(password)
    cursor = conn.execute(
        """INSERT INTO users (username, hashed_password, full_name, role, is_active, must_change_password)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (username, hashed, full_name, role, is_active, must_change_password),
    )
    conn.commit()
    return cursor.lastrowid


def get_token(user_id: int, username: str, role: str) -> str:
    """Generate a JWT token for a test user."""
    return create_access_token(
        user_id,
        username,
        role,
        client_fingerprint=compute_client_fingerprint(""),
    )
