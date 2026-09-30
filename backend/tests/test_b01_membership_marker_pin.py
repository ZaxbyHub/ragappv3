"""Regression pin (issue #690, S03-SK-01): durable membership removal.

Non-frozen companion to frozen checks C1/C12: C1 pins the migration's own
handled-marker path (raw-SQL revocation); C12 pins that the DELETE route
still deletes. This pin covers the route-tombstone leg the redesign added:

- removing a user's only membership on a NON-Default vault through the
  DELETE route must durably block the Default-vault orphan backfill
  (AC1's text is vault-unqualified);
- the route stays schema-safe on databases without a system_flags table
  (TEST_SCHEMA has none; the tombstone helper creates it in-transaction).
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest
from backend.tests.schema_constants import TEST_SCHEMA
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.auth import router as auth_router
from app.api.routes.vault_members import router as vault_members_router
from app.models.database import migrate_assign_orphan_users_to_default_vault
from app.services.auth_service import (
    compute_client_fingerprint,
    create_access_token,
    hash_password,
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    temp_dir = Path(tempfile.mkdtemp())
    db_path = str(temp_dir / "app.db")

    from app.models.database import _pool_cache, _pool_cache_lock

    with _pool_cache_lock:
        for path, pool in list(_pool_cache.items()):
            pool.close_all()
        _pool_cache.clear()

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(TEST_SCHEMA)
    conn.commit()

    pw = hash_password("testpass")
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, full_name, role, is_active)"
        " VALUES (1, 'superadmin', ?, 'Super Admin', 'superadmin', 1)",
        (pw,),
    )
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, full_name, role, is_active)"
        " VALUES (2, 'admin1', ?, 'Admin One', 'admin', 1)",
        (pw,),
    )
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, full_name, role, is_active)"
        " VALUES (3, 'member1', ?, 'Member One', 'member', 1)",
        (pw,),
    )
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, full_name, role, is_active)"
        " VALUES (4, 'member2', ?, 'Member Two', 'member', 1)",
        (pw,),
    )
    # Vault 1 'Test Vault' is NOT the Default vault; admin1 administers it.
    conn.execute("INSERT INTO vaults (id, name) VALUES (1, 'Test Vault')")
    conn.execute(
        "INSERT INTO vault_members (vault_id, user_id, permission, granted_by)"
        " VALUES (1, 2, 'admin', 1)"
    )
    # member1's only membership is the non-Default vault 1.
    conn.execute(
        "INSERT INTO vault_members (vault_id, user_id, permission, granted_by)"
        " VALUES (1, 3, 'read', 1)"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr("app.config.settings.data_dir", temp_dir)
    monkeypatch.setattr(
        "app.config.settings.jwt_secret_key",
        "test-secret-key-for-testing-only-min-32-chars!!",
    )
    monkeypatch.setattr("app.config.settings.users_enabled", True)

    yield db_path

    with _pool_cache_lock:
        if db_path in _pool_cache:
            _pool_cache[db_path].close_all()
            del _pool_cache[db_path]

    import shutil

    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(auth_router, prefix="/api")
    app.include_router(vault_members_router, prefix="/api")
    tc = TestClient(app)
    tc.headers["user-agent"] = ""
    return tc


def _member_row_count(db_path: str, user_id: int) -> int:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM vault_members WHERE user_id = ?", (user_id,)
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def test_nondefault_route_removal_blocks_default_backfill(env, client):
    db_path = env
    token = create_access_token(
        1, "superadmin", "superadmin", client_fingerprint=compute_client_fingerprint("")
    )

    response = client.delete(
        "/api/vaults/1/members/3", headers={"Authorization": f"Bearer {token}"}
    )
    # Also proves the route is schema-safe without a system_flags table.
    assert response.status_code == 200
    assert response.json()["message"] == "Member removed"
    assert _member_row_count(db_path, 3) == 0

    migrate_assign_orphan_users_to_default_vault(db_path)

    # The removed membership must stay removed: no Default-vault grant for a
    # user whose only membership was removed through the app, on any vault.
    assert _member_row_count(db_path, 3) == 0


def test_backfill_still_grants_unmarked_orphans(env, client):
    """The tombstone must not turn the backfill into a blanket no-op.

    member2 (user 4) never held a membership and was never removed, so the
    one-time backfill still grants Default-vault read exactly once.
    """
    db_path = env
    migrate_assign_orphan_users_to_default_vault(db_path)
    assert _member_row_count(db_path, 4) == 1
    migrate_assign_orphan_users_to_default_vault(db_path)
    assert _member_row_count(db_path, 4) == 1
