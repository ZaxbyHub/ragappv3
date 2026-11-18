"""A4a: end-to-end empty-revision guard tests (.

One test per route (Mark Ready, Export, Promote) driving the REAL route
handler against a REAL temporary SQLite store seeded with synthetic incident rows - a 0-character revision that was approved,
exported and promoted. No stubbed store: the guard must fire on the real data path.

Fixture shape mirrors tests/draft_room/test_draft_promote.py (pool +
db_pool + csrf_manager), which is the established pattern for driving
these routes.
"""
import sqlite3

import pytest

EMPTY_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

_SEED = [
    "INSERT INTO users (id, username, hashed_password, created_at)"
    " VALUES (7,'e2e','x','2026-11-15T00:00:00Z')",
    "INSERT INTO vaults (id, name, owner_id, created_at)"
    " VALUES (9,'e2e',7,'2026-11-15T00:00:00Z')",
    "INSERT INTO vault_members (id, vault_id, user_id, permission, granted_at,"
    " granted_by) VALUES (1, 9, 7, 'write', '2026-11-15T00:00:00Z', 7)",
    "INSERT INTO drafts (id, vault_id, created_by, title, mode, status, tier,"
    " lock_version, created_at, updated_at)"
    " VALUES (20, 9, 7, 'synthetic empty-revision draft', 'compose', 'needs_review',"
    " 'standard', 1, '2026-11-15T00:00:00Z', '2026-11-15T00:00:00Z')",
    # the audit's empty revision: 0 characters, hash of the empty string
    "INSERT INTO draft_revisions (id, draft_id, revision_no, source, content_md,"
    " content_sha256, fact_status, is_current, created_by, created_at)"
    f" VALUES (5, 20, 1, 'pipeline', '', '{EMPTY_SHA}', 'passed', 1, 7,"
    " '2026-11-15T00:00:00Z')",
]


class _Pool:
    """A one-connection-per-checkout pool over a single file DB."""

    def __init__(self, path):
        self._path = str(path)

    def get_connection(self):
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def release_connection(self, conn):
        conn.close()

    def close_all(self):
        pass


@pytest.fixture()
def world(tmp_path):
    from app.config import settings
    from app.main import app
    from app.models.database import init_db, run_migrations

    old_data = settings.data_dir
    old_enabled = settings.draft_room_enabled
    # _client() installs process-global overrides on the shared app; snapshot
    # and restore them so later suites (e.g. test_memories_auth) are not
    # served this module's fake authenticated user / CSRF bypass.
    old_overrides = dict(app.dependency_overrides)
    _missing = object()
    old_db_pool = getattr(app.state, "db_pool", _missing)
    old_csrf_manager = getattr(app.state, "csrf_manager", _missing)
    settings.data_dir = tmp_path
    settings.draft_room_enabled = True
    try:
        db_path = tmp_path / "app.db"
        init_db(str(db_path))
        run_migrations(str(db_path))
        seed = sqlite3.connect(str(db_path))
        for stmt in _SEED:
            seed.execute(stmt)
        seed.commit()
        seed.close()
        yield _Pool(db_path), db_path
    finally:
        settings.data_dir = old_data
        settings.draft_room_enabled = old_enabled
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old_overrides)
        for name, old in (("db_pool", old_db_pool), ("csrf_manager", old_csrf_manager)):
            if old is _missing:
                if hasattr(app.state, name):
                    delattr(app.state, name)
            else:
                setattr(app.state, name, old)


def _client(pool):
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    from app.api.deps import (
        get_background_processor,
        get_current_active_user,
        get_db,
        get_draft_job_processor,
    )
    from app.main import app
    from app.security import CSRFManager, csrf_protect

    def _db():
        conn = pool.get_connection()
        try:
            yield conn
        finally:
            pool.release_connection(conn)

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_active_user] = lambda: {
        "id": 7,
        "username": "e2e",
    }
    app.dependency_overrides[csrf_protect] = lambda: "test-csrf"
    app.dependency_overrides[get_background_processor] = lambda: MagicMock()
    app.dependency_overrides[get_draft_job_processor] = lambda: MagicMock()
    app.state.db_pool = pool
    app.state.csrf_manager = CSRFManager(redis_url="", ttl=900)
    return TestClient(app)


def test_ready_rejects_the_empty_revision_end_to_end(world):
    pool, db_path = world
    resp = _client(pool).post(
        "/api/draft-room/drafts/20/revisions/5/ready",
        json={"lock_version": 1},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "revision_empty"
    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT status FROM drafts WHERE id=20").fetchone()
    conn.close()
    assert row[0] != "ready", "the draft must not be marked ready"


def test_export_rejects_the_empty_revision_end_to_end(world):
    pool, db_path = world
    resp = _client(pool).post("/api/draft-room/drafts/20/revisions/5/export")
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "revision_empty"
    assert not resp.text.lstrip().startswith("#"), "no markdown body may be returned"


def test_promote_rejects_the_empty_revision_end_to_end(world):
    pool, db_path = world
    resp = _client(pool).post(
        "/api/draft-room/drafts/20/promote",
        json={"source_type": "revision", "source_id": 5, "title": "e2e"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "revision_empty"
    conn = sqlite3.connect(str(db_path))
    n = conn.execute("SELECT COUNT(*) FROM draft_promotions").fetchone()[0]
    conn.close()
    assert n == 0, "nothing may be promoted"
