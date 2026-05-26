"""Regression tests for vault-scoped document permission behavior."""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from _db_pool import SimpleConnectionPool
from fastapi.testclient import TestClient

from app.api.deps import get_current_active_user, get_db, get_vector_store
from app.main import app
from app.models.database import init_db, run_migrations


@pytest.fixture()
def permission_client():
    temp_dir = tempfile.mkdtemp()
    db_path = str(Path(temp_dir) / "app.db")
    init_db(db_path)
    run_migrations(db_path)
    pool = SimpleConnectionPool(db_path)

    vector_store = MagicMock()
    vector_store.db = MagicMock()
    vector_store.db.table_names = AsyncMock(return_value=["chunks"])
    vector_store.db.open_table = AsyncMock(return_value=MagicMock())
    vector_store.delete_by_file = AsyncMock(return_value=1)

    def override_get_db():
        conn = pool.get_connection()
        try:
            yield conn
        finally:
            pool.release_connection(conn)

    current_user = {"id": 2, "username": "admin", "role": "admin", "is_active": True}

    async def override_user():
        return current_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_active_user] = override_user
    app.dependency_overrides[get_vector_store] = lambda: vector_store

    conn = pool.get_connection()
    try:
        conn.execute(
            "INSERT INTO users (id, username, hashed_password, role) VALUES (?, ?, ?, ?)",
            (2, "admin", "fake", "admin"),
        )
        conn.execute(
            "INSERT INTO users (id, username, hashed_password, role) VALUES (?, ?, ?, ?)",
            (3, "member", "fake", "member"),
        )
        conn.execute(
            "INSERT INTO vaults (id, name, description, visibility) VALUES (?, ?, ?, ?)",
            (1, "Default Vault", "", "private"),
        )
        conn.execute(
            "INSERT INTO vaults (id, name, description, visibility) VALUES (?, ?, ?, ?)",
            (2, "Admin Vault", "", "private"),
        )
        conn.execute(
            "INSERT INTO vaults (id, name, description, visibility) VALUES (?, ?, ?, ?)",
            (3, "Other Vault", "", "private"),
        )
        conn.execute(
            "INSERT INTO vaults (id, name, description, visibility) VALUES (?, ?, ?, ?)",
            (4, "Public Vault", "", "public"),
        )
        conn.execute(
            "INSERT INTO vault_members (vault_id, user_id, permission) VALUES (?, ?, ?)",
            (2, 2, "admin"),
        )
        conn.execute(
            "INSERT INTO vault_members (vault_id, user_id, permission) VALUES (?, ?, ?)",
            (1, 3, "write"),
        )
        conn.execute(
            "INSERT INTO files (id, file_name, file_path, file_size, status, vault_id) VALUES (?, ?, ?, ?, ?, ?)",
            (1, "allowed.txt", "/tmp/allowed.txt", 1, "indexed", 2),
        )
        conn.execute(
            "INSERT INTO files (id, file_name, file_path, file_size, status, vault_id) VALUES (?, ?, ?, ?, ?, ?)",
            (2, "blocked.txt", "/tmp/blocked.txt", 1, "indexed", 3),
        )
        conn.execute(
            "INSERT INTO files (id, file_name, file_path, file_size, status, vault_id) VALUES (?, ?, ?, ?, ?, ?)",
            (3, "default.txt", "/tmp/default.txt", 1, "indexed", 1),
        )
        conn.commit()
    finally:
        pool.release_connection(conn)

    client = TestClient(app)
    yield client, pool, vector_store, current_user

    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_active_user, None)
    app.dependency_overrides.pop(get_vector_store, None)
    pool.close_all()
    shutil.rmtree(temp_dir, ignore_errors=True)


def _file_exists(pool: SimpleConnectionPool, file_id: int) -> bool:
    conn = pool.get_connection()
    try:
        row = conn.execute("SELECT id FROM files WHERE id = ?", (file_id,)).fetchone()
        return row is not None
    finally:
        pool.release_connection(conn)


def test_app_admin_with_vault_admin_can_delete_single_document(permission_client):
    client, pool, vector_store, _ = permission_client

    with patch("app.services.wiki_store.WikiStore.mark_claims_stale_by_file") as stale:
        response = client.delete("/api/documents/1")

    assert response.status_code == 200
    assert not _file_exists(pool, 1)
    vector_store.delete_by_file.assert_awaited_once_with("1")
    stale.assert_called_once_with(1, 2)


def test_app_admin_without_vault_admin_cannot_delete_single_document(permission_client):
    client, pool, vector_store, _ = permission_client

    response = client.delete("/api/documents/2")

    assert response.status_code == 403
    assert _file_exists(pool, 2)
    vector_store.delete_by_file.assert_not_awaited()


def test_batch_delete_is_per_file_vault_admin_scoped(permission_client):
    client, pool, vector_store, _ = permission_client

    with patch("app.services.wiki_store.WikiStore.mark_claims_stale_by_file") as stale:
        response = client.post(
            "/api/documents/batch",
            json={"file_ids": ["1", "2", "not-an-id"]},
        )

    assert response.status_code == 200
    assert response.json() == {"deleted_count": 1, "failed_ids": ["2", "not-an-id"]}
    assert not _file_exists(pool, 1)
    assert _file_exists(pool, 2)
    vector_store.delete_by_file.assert_awaited_once_with("1")
    stale.assert_called_once_with(1, 2)


def test_write_only_default_vault_user_cannot_delete(permission_client):
    client, pool, vector_store, current_user = permission_client
    current_user.update({"id": 3, "username": "member", "role": "member"})

    response = client.delete("/api/documents/3")

    assert response.status_code == 403
    assert _file_exists(pool, 3)
    vector_store.delete_by_file.assert_not_awaited()


def test_vault_list_includes_effective_permission_and_public_read(permission_client):
    client, _, _, current_user = permission_client
    current_user.update({"id": 3, "username": "member", "role": "member"})

    # Regular users list via /vaults/accessible; /vaults is admin-only.
    response = client.get("/api/vaults/accessible")

    assert response.status_code == 200
    vaults = {vault["id"]: vault for vault in response.json()["vaults"]}
    assert vaults[1]["current_user_permission"] == "write"
    assert vaults[4]["current_user_permission"] == "read"
    assert 2 not in vaults
    assert 3 not in vaults


def test_vault_create_response_returns_creator_admin_permission(permission_client):
    client, _, _, current_user = permission_client
    current_user.update({"id": 3, "username": "member", "role": "member"})

    response = client.post("/api/vaults", json={"name": "Created By Member"})

    assert response.status_code == 201
    assert response.json()["current_user_permission"] == "admin"
