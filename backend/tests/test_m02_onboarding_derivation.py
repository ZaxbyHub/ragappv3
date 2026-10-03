"""Issue #782 (UI-ENH-07 stage 2): derivation census for the onboarding
milestone GET — the derive-at-read contract's write-path-drift guardrail.

Each of the three DERIVED flags must trace to exactly one source table: the
census deletes/inserts one row in each source and asserts ONLY the matching
flag moves. If a future change starts writing milestone flags from upload or
chat code paths (the issue's rejected Candidate B), a flag that no longer
follows its source row fails here.
"""

from __future__ import annotations

import sqlite3

import pytest
from backend.tests.schema_constants import TEST_SCHEMA
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.auth import router as auth_router
from app.api.routes.onboarding import router as onboarding_router
from app.services.auth_service import (
    compute_client_fingerprint,
    create_access_token,
    hash_password,
)

_WIDENED_FILES_DDL = """
CREATE TABLE files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_id INTEGER NOT NULL,
    file_path TEXT NOT NULL,
    file_name TEXT NOT NULL,
    file_hash TEXT,
    file_size INTEGER NOT NULL DEFAULT 0,
    file_type TEXT,
    chunk_count INTEGER DEFAULT 0,
    chunks_failed INTEGER NOT NULL DEFAULT 0,
    partial_embeddings INTEGER NOT NULL DEFAULT 0,
    status TEXT DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'indexed', 'partial', 'error')),
    error_message TEXT,
    source TEXT DEFAULT 'upload',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    processed_at TIMESTAMP,
    modified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

_DERIVATION_SUPPLEMENT = """
CREATE TABLE IF NOT EXISTS user_onboarding_state (
    user_id INTEGER PRIMARY KEY,
    citation_opened_at TEXT,
    checklist_dismissed_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS chat_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_id INTEGER NOT NULL,
    user_id INTEGER,
    title TEXT,
    forked_from_session_id INTEGER,
    fork_message_index INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (vault_id) REFERENCES vaults(id)
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content TEXT NOT NULL,
    sources TEXT,
    memories TEXT,
    mode TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_session_id ON chat_messages(session_id);
"""

CSRF_TEST_POLICY = "naive"


@pytest.fixture()
def seeded_db(tmp_path):
    """A fresh DB with user 1 owning NOTHING (all flags false)."""
    db_path = str(tmp_path / "app.db")

    from app.models.database import _pool_cache, _pool_cache_lock

    with _pool_cache_lock:
        for path, pool in list(_pool_cache.items()):
            pool.close_all()
        _pool_cache.clear()

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(TEST_SCHEMA)
    conn.executescript(_DERIVATION_SUPPLEMENT)
    # TEST_SCHEMA files CHECK predates the widened status enum (issue
    # #513): recreate with the production CHECK so the strict-vs-partial
    # derivation census can insert a partial row (migrate_widen_files_status
    # covers real databases; this is a test-schema convenience).
    conn.execute("DROP TABLE IF EXISTS files")
    conn.executescript(_WIDENED_FILES_DDL)
    pw = hash_password("testpass")
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) "
        "VALUES (1, 'census', ?, 'Census User', 'member', 1)",
        (pw,),
    )
    conn.commit()
    conn.close()
    yield db_path

    with _pool_cache_lock:
        if db_path in _pool_cache:
            _pool_cache[db_path].close_all()
            del _pool_cache[db_path]


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(auth_router, prefix="/api")
    app.include_router(onboarding_router, prefix="/api")
    tc = TestClient(app)
    tc.headers["user-agent"] = ""
    return tc


def _headers() -> dict:
    return {
        "Authorization": "Bearer "
        + create_access_token(
            1, "census", "member", client_fingerprint=compute_client_fingerprint("")
        )
    }


def _flags(client: TestClient) -> dict:
    resp = client.get("/api/onboarding/milestones", headers=_headers())
    assert resp.status_code == 200, resp.text
    return resp.json()


def _sql(db_path: str, statement: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(statement)
        conn.commit()
    finally:
        conn.close()


class TestDerivationCensus:
    def test_vault_created_follows_the_vault_row(self, seeded_db, monkeypatch) -> None:
        import pathlib

        monkeypatch.setattr(
            "app.config.settings.data_dir", pathlib.Path(seeded_db).parent
        )
        monkeypatch.setattr(
            "app.config.settings.jwt_secret_key",
            "test-secret-key-for-testing-only-min-32-chars!!",
        )
        monkeypatch.setattr("app.config.settings.users_enabled", True)
        client = _client()

        before = _flags(client)
        assert before["vault_created"] is False

        _sql(
            seeded_db,
            "INSERT INTO vaults (id, name, owner_id) VALUES (1, 'V', 1)",
        )
        after = _flags(client)
        assert after["vault_created"] is True
        # Only the vault flag moved — the other three stayed false.
        assert after["upload_indexed"] is False
        assert after["first_question_asked"] is False
        assert after["first_citation_opened"] is False

    def test_upload_indexed_follows_the_file_row(self, seeded_db, monkeypatch) -> None:
        import pathlib

        monkeypatch.setattr(
            "app.config.settings.data_dir", pathlib.Path(seeded_db).parent
        )
        monkeypatch.setattr(
            "app.config.settings.jwt_secret_key",
            "test-secret-key-for-testing-only-min-32-chars!!",
        )
        monkeypatch.setattr("app.config.settings.users_enabled", True)
        _sql(
            seeded_db,
            "INSERT INTO vaults (id, name, owner_id) VALUES (1, 'V', 1)",
        )
        client = _client()

        assert _flags(client)["upload_indexed"] is False

        _sql(
            seeded_db,
            "INSERT INTO files (vault_id, file_path, file_name, status) "
            "VALUES (1, 'uploads/a.pdf', 'a.pdf', 'pending')",
        )
        assert _flags(client)["upload_indexed"] is False

        # The strict contract: only status='indexed' flips it (a
        # scan/sync-path partial lands 'indexed' + partial_embeddings and
        # counts; the upload/reindex path's 'partial' does not).
        _sql(seeded_db, "UPDATE files SET status = 'indexed' WHERE file_name = 'a.pdf'")
        flipped = _flags(client)
        assert flipped["upload_indexed"] is True
        assert flipped["first_question_asked"] is False
        assert flipped["first_citation_opened"] is False

    def test_first_question_follows_the_chat_row(self, seeded_db, monkeypatch) -> None:
        import pathlib

        monkeypatch.setattr(
            "app.config.settings.data_dir", pathlib.Path(seeded_db).parent
        )
        monkeypatch.setattr(
            "app.config.settings.jwt_secret_key",
            "test-secret-key-for-testing-only-min-32-chars!!",
        )
        monkeypatch.setattr("app.config.settings.users_enabled", True)
        _sql(
            seeded_db,
            "INSERT INTO vaults (id, name, owner_id) VALUES (1, 'V', 1)",
        )
        client = _client()

        assert _flags(client)["first_question_asked"] is False

        # A NULL-user session (system/admin lineage) must NOT flip the flag.
        _sql(
            seeded_db,
            "INSERT INTO chat_sessions (id, vault_id, user_id, title) "
            "VALUES (1, 1, NULL, 'system')",
        )
        _sql(
            seeded_db,
            "INSERT INTO chat_messages (session_id, role, content) "
            "VALUES (1, 'user', 'who owns this?')",
        )
        assert _flags(client)["first_question_asked"] is False

        # The user's own session with a user-role message flips it.
        _sql(
            seeded_db,
            "INSERT INTO chat_sessions (id, vault_id, user_id, title) "
            "VALUES (2, 1, 1, 'mine')",
        )
        _sql(
            seeded_db,
            "INSERT INTO chat_messages (session_id, role, content) "
            "VALUES (2, 'user', 'what is covered?')",
        )
        assert _flags(client)["first_question_asked"] is True

    def test_partial_status_does_not_flip_upload_indexed(
        self, seeded_db, monkeypatch
    ) -> None:
        import pathlib

        monkeypatch.setattr(
            "app.config.settings.data_dir", pathlib.Path(seeded_db).parent
        )
        monkeypatch.setattr(
            "app.config.settings.jwt_secret_key",
            "test-secret-key-for-testing-only-min-32-chars!!",
        )
        monkeypatch.setattr("app.config.settings.users_enabled", True)
        _sql(
            seeded_db,
            "INSERT INTO vaults (id, name, owner_id) VALUES (1, 'V', 1)",
        )
        client = _client()

        _sql(
            seeded_db,
            "INSERT INTO files (vault_id, file_path, file_name, status) "
            "VALUES (1, 'uploads/b.pdf', 'b.pdf', 'partial')",
        )
        flags = _flags(client)
        assert flags["upload_indexed"] is False
        assert flags["show_checklist"] is True
