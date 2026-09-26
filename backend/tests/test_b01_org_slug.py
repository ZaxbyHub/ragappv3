"""Acceptance checks (issue #690, defect 3): organization slug generation.

Pins the desired post-fix behavior: non-ASCII-only organization names must
receive a usable non-empty slug, and a second such organization must be
creatable (the fresh schema's UNIQUE(slug) constraint must not be hit by two
empty slugs). The HTTP tests build the database with the REAL migration
pipeline (``run_migrations``), not the minimal test schema.
"""

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.auth import router as auth_router
from app.api.routes.organizations import router as organizations_router
from app.models.database import (
    init_db,
    migrate_add_org_slug_column,
    run_migrations,
)
from app.services.auth_service import (
    compute_client_fingerprint,
    create_access_token,
    hash_password,
)


@pytest.fixture(autouse=True)
def setup_db(monkeypatch, tmp_path):
    """Build the database with the real migrations and seed one admin user."""
    # Clear pool cache BEFORE setting up the new database.
    from app.models.database import _pool_cache, _pool_cache_lock

    with _pool_cache_lock:
        for _path, pool in list(_pool_cache.items()):
            pool.close_all()
        _pool_cache.clear()

    db_path = str(tmp_path / "app.db")

    # Patch settings first so settings.vaults_dir points at the (absent) temp
    # tree and the vault-path step of run_migrations is a no-op.
    monkeypatch.setattr("app.config.settings.data_dir", tmp_path)
    monkeypatch.setattr(
        "app.config.settings.jwt_secret_key",
        "test-secret-key-for-testing-only-min-32-chars!!",
    )
    monkeypatch.setattr("app.config.settings.users_enabled", True)

    run_migrations(db_path)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    pw = hash_password("testpass")
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, full_name, role,"
        " is_active) VALUES (?, ?, ?, ?, ?, 1)",
        (2, "admin1", pw, "Admin One", "admin"),
    )
    conn.commit()
    conn.close()

    yield db_path

    with _pool_cache_lock:
        if db_path in _pool_cache:
            _pool_cache[db_path].close_all()
            del _pool_cache[db_path]


def admin_token():
    return create_access_token(
        2, "admin1", "admin", client_fingerprint=compute_client_fingerprint("")
    )


def admin_headers():
    return {"Authorization": f"Bearer {admin_token()}"}


@pytest.fixture
def client():
    """Create test client with the auth and organizations routers."""
    app = FastAPI()
    app.include_router(auth_router, prefix="/api")
    app.include_router(organizations_router, prefix="/api")
    tc = TestClient(app)
    # Override default User-Agent so fingerprint validation matches token
    tc.headers["user-agent"] = ""
    return tc


def test_non_ascii_org_name_gets_non_empty_slug(client):
    """AC5: an org whose name has no ASCII letters still gets a real slug."""
    response = client.post(
        "/api/organizations",
        json={"name": "東京", "description": "Non-ASCII org"},
        headers=admin_headers(),
    )
    assert response.status_code == 200
    assert response.json()["slug"] != ""


def test_second_non_ascii_org_is_created(client):
    """AC6: two non-ASCII org names must not collide on an empty slug."""
    resp1 = client.post(
        "/api/organizations",
        json={"name": "東京", "description": "First"},
        headers=admin_headers(),
    )
    assert resp1.status_code == 200

    resp2 = client.post(
        "/api/organizations",
        json={"name": "Москва", "description": "Second"},
        headers=admin_headers(),
    )
    assert resp2.status_code == 200


def _try_two_empty_slugs(db_path: str) -> str:
    """Insert two organizations with empty slugs; report the outcome."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO organizations (name, description, slug, created_by)"
            " VALUES (?, ?, ?, ?)",
            ("Org One", "", "", 1),
        )
        conn.commit()
        try:
            conn.execute(
                "INSERT INTO organizations (name, description, slug, created_by)"
                " VALUES (?, ?, ?, ?)",
                ("Org Two", "", "", 1),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            return "IntegrityError"
        return "accepted"
    finally:
        conn.close()


def test_slug_uniqueness_same_on_fresh_and_migrated_schema(tmp_path):
    """AC7: slug uniqueness must behave identically on fresh and legacy DBs."""
    fresh_db = str(tmp_path / "fresh.db")
    init_db(fresh_db)
    fresh_outcome = _try_two_empty_slugs(fresh_db)

    migrated_db = str(tmp_path / "migrated.db")
    conn = sqlite3.connect(migrated_db)
    conn.execute(
        """
        CREATE TABLE organizations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            description TEXT DEFAULT '',
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()
    migrate_add_org_slug_column(migrated_db)
    migrated_outcome = _try_two_empty_slugs(migrated_db)

    assert fresh_outcome == migrated_outcome
