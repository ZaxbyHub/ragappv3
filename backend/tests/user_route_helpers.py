"""Shared helpers for user-route style backend tests."""

import sqlite3

from backend.tests.schema_constants import TEST_SCHEMA

from app.services.auth_service import (
    compute_client_fingerprint,
    create_access_token,
    hash_password,
)


def setup_test_db(
    db_path: str,
    *,
    include_default_vault: bool = False,
    include_locked_until_index: bool = False,
) -> sqlite3.Connection:
    """Set up a user-route test database with the shared schema."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(TEST_SCHEMA)

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
