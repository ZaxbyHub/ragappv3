"""Issue-trace 782-firstrun-checklist-vaultgate-kms-wiki — acceptance checks
C3/C4/C5 (AC3/AC4/AC5).

Onboarding-milestone routes over per-user server state. The route path
``/api/onboarding/milestones`` and the module name ``app.api.routes.onboarding``
ARE the contract — at base the module does not exist, so this file fails
collection with ``ModuleNotFoundError`` (the pre-fix RED signature).

Fixture pattern mirrors backend/tests/test_vault_members_routes.py:
setup_db autouse fixture with TEST_SCHEMA from backend/tests/schema_constants.py,
seeded users with hashed_password, token helpers, auth_headers, a TestClient
with the user-agent "" override, and the _pool_cache clear.

Route-behavior testing: the per-user onboarding state table is created here
with CREATE TABLE IF NOT EXISTS (the real migration is tested separately by
the fix's own suite).
"""

CSRF_TEST_POLICY = "naive"

import sqlite3
import tempfile
from pathlib import Path

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

# Local supplement: the tables the onboarding routes read/write beyond
# TEST_SCHEMA. user_onboarding_state is the fix's per-user state table; the
# chat tables mirror the production DDL (app/models/database.py:860-886) the
# first_question_asked derivation reads.
_ONBOARDING_SUPPLEMENT = """
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


@pytest.fixture(autouse=True)
def setup_db(monkeypatch):
    """Set up test database with schema and seed data (per test, fresh DB)."""
    temp_dir = tempfile.mkdtemp()
    db_path = str(Path(temp_dir) / "app.db")

    # Clear pool cache BEFORE setting up new database
    from app.models.database import _pool_cache, _pool_cache_lock

    with _pool_cache_lock:
        for path, pool in list(_pool_cache.items()):
            pool.close_all()
        _pool_cache.clear()

    # Initialize schema manually with valid SQL
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(TEST_SCHEMA)
    conn.executescript(_ONBOARDING_SUPPLEMENT)
    conn.commit()
    conn.close()

    # Patch settings
    monkeypatch.setattr("app.config.settings.data_dir", Path(temp_dir))
    monkeypatch.setattr(
        "app.config.settings.jwt_secret_key",
        "test-secret-key-for-testing-only-min-32-chars!!",
    )
    monkeypatch.setattr("app.config.settings.users_enabled", True)

    # Seed test users: A (id 1) owns the walkthrough state, B (id 2) is a
    # bare user with no onboarding activity.
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    pw = hash_password("testpass")
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
        (1, "alice", pw, "Alice One", "member"),
    )
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
        (2, "bob", pw, "Bob Two", "member"),
    )
    # A vault owned by user A -> vault_created for A.
    conn.execute(
        "INSERT INTO vaults (id, name, owner_id) VALUES (1, 'Onboarding Vault', 1)"
    )
    # One file row with status 'indexed' in that vault -> upload_indexed for A.
    # NOT NULL columns of files (TEST_SCHEMA DDL): vault_id, file_path,
    # file_name (file_size/chunks_failed have defaults).
    conn.execute(
        "INSERT INTO files (vault_id, file_path, file_name, status) VALUES (?, ?, ?, ?)",
        (1, "uploads/handbook.pdf", "handbook.pdf", "indexed"),
    )
    # One chat session (user_id = A) with a role='user' message ->
    # first_question_asked for A.
    conn.execute(
        "INSERT INTO chat_sessions (id, vault_id, user_id, title) VALUES (?, ?, ?, ?)",
        (1, 1, 1, "First questions"),
    )
    conn.execute(
        "INSERT INTO chat_messages (session_id, role, content) VALUES (?, ?, ?)",
        (1, "user", "What is covered in the handbook?"),
    )
    conn.commit()
    conn.close()

    yield db_path

    # Cleanup
    with _pool_cache_lock:
        if db_path in _pool_cache:
            _pool_cache[db_path].close_all()
            del _pool_cache[db_path]

    import shutil

    shutil.rmtree(temp_dir, ignore_errors=True)


def user_a_token():
    return create_access_token(1, "alice", "member",
                            client_fingerprint=compute_client_fingerprint(""))


def user_b_token():
    return create_access_token(2, "bob", "member",
                            client_fingerprint=compute_client_fingerprint(""))


def auth_headers(token_fn):
    return {"Authorization": f"Bearer {token_fn()}"}


def make_client() -> TestClient:
    """Fresh TestClient mounting the auth + onboarding routers (the app
    contract: prefix /api, onboarding router defines the /onboarding/... paths)."""
    app = FastAPI()
    app.include_router(auth_router, prefix="/api")
    app.include_router(onboarding_router, prefix="/api")
    tc = TestClient(app)
    # Override default User-Agent so fingerprint validation matches token
    tc.headers["user-agent"] = ""
    return tc


def milestones_of(payload: dict) -> dict:
    """Tolerant extractor: the four milestone flags live either at the top
    level of the GET response or under a 'milestones' key."""
    if isinstance(payload, dict) and isinstance(payload.get("milestones"), dict):
        return payload["milestones"]
    return payload


def show_checklist_of(payload: dict) -> bool:
    m = milestones_of(payload)
    if isinstance(m, dict) and "show_checklist" in m:
        return bool(m["show_checklist"])
    return bool(payload.get("show_checklist"))


class TestOnboardingMilestones:
    """GET /api/onboarding/milestones derives its flags from server state."""

    def test_milestones_derive_from_server_state(self):
        client = make_client()

        # A opens a citation (the AC4 write) — the fourth milestone flips.
        resp = client.post(
            "/api/onboarding/milestones/citation-opened",
            headers=auth_headers(user_a_token),
        )
        assert resp.status_code == 200, resp.text

        resp = client.get(
            "/api/onboarding/milestones", headers=auth_headers(user_a_token)
        )
        assert resp.status_code == 200, resp.text
        m = milestones_of(resp.json())
        assert m["vault_created"] is True
        assert m["upload_indexed"] is True
        assert m["first_question_asked"] is True
        assert m["first_citation_opened"] is True

    def test_citation_open_is_recorded_per_user(self):
        # Fresh client per user: per-client memory cannot satisfy this — the
        # state must persist per-user in the DB.
        client_a = make_client()
        resp = client_a.post(
            "/api/onboarding/milestones/citation-opened",
            headers=auth_headers(user_a_token),
        )
        assert resp.status_code == 200, resp.text

        a_payload = client_a.get(
            "/api/onboarding/milestones", headers=auth_headers(user_a_token)
        ).json()
        assert milestones_of(a_payload)["first_citation_opened"] is True

        client_b = make_client()
        b_payload = client_b.get(
            "/api/onboarding/milestones", headers=auth_headers(user_b_token)
        ).json()
        assert milestones_of(b_payload)["first_citation_opened"] is False

    def test_dismissed_or_complete_checklist_stays_hidden(self):
        # (a) A user with incomplete milestones dismisses -> hidden.
        client_b = make_client()
        resp = client_b.post(
            "/api/onboarding/milestones/dismiss",
            headers=auth_headers(user_b_token),
        )
        assert resp.status_code == 200, resp.text
        b_payload = client_b.get(
            "/api/onboarding/milestones", headers=auth_headers(user_b_token)
        ).json()
        assert show_checklist_of(b_payload) is False

        # (b) A user with all four milestones complete (seeded vault/file/
        # chat plus the citation-opened write) -> hidden WITHOUT dismissing.
        client_a = make_client()
        resp = client_a.post(
            "/api/onboarding/milestones/citation-opened",
            headers=auth_headers(user_a_token),
        )
        assert resp.status_code == 200, resp.text
        a_payload = client_a.get(
            "/api/onboarding/milestones", headers=auth_headers(user_a_token)
        ).json()
        m = milestones_of(a_payload)
        assert m["vault_created"] is True
        assert m["upload_indexed"] is True
        assert m["first_question_asked"] is True
        assert m["first_citation_opened"] is True
        assert show_checklist_of(a_payload) is False
