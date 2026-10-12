"""C03 acceptance checks: global active prompt version ignored for org-less targets.

Root cause under test (pre-fix RED state):
- ``RAGEngine._resolve_effective_prompt_sync`` returns ``(None, None)`` when
  ``vault_id`` is None (All-Vaults chat) and when the vault row is missing or
  its ``org_id`` is NULL (org-less vault).
- Therefore an admin-activated GLOBAL prompt version (``prompt_versions`` row
  with ``is_active=1``) never reaches queries that target an org-less vault or
  an All-Vaults chat: the resolved version label is None and the built message
  falls back to the builder's built-in default instead of the active content.

These tests must FAIL at the current tree for exactly that reason:
- tests 1/2 fail with ``assert None == 'c03-active'``
- test 3 fails with ``assert False is True``
"""

import os
import sqlite3
import tempfile

import pytest

from app.models.database import run_migrations
from app.services.prompt_store import PromptVersionStore

# ---------------------------------------------------------------------------
# Fixtures (mirrors test_prompt_ab_variants.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_db():
    """Create a temporary DB with the full schema including A/B tables."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        run_migrations(path)
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        # Seed test org so FK constraints on prompt tables pass.
        conn.execute(
            "INSERT INTO organizations (id, name, slug, created_at) "
            "VALUES (?, ?, ?, ?)",
            (10, "Test Org", "test-org", "2024-01-01T00:00:00Z"),
        )
        conn.commit()
        yield conn
    finally:
        conn.close()
        os.unlink(path)


@pytest.fixture
def store(fresh_db):
    """Return a PromptVersionStore backed by the fresh DB."""
    return PromptVersionStore(fresh_db)


# ---------------------------------------------------------------------------
# Harness helpers
# ---------------------------------------------------------------------------


class _FakeEmbSvc:
    def embed_single(self, t):
        return []

    def embed_passage(self, t):
        return []


class _FakeVec:
    pass


class _FakeMem:
    pass


class _FakeLLM:
    pass


def _make_engine(fresh_db):
    """Build a RAGEngine on the fresh DB with all-fake services (no network)."""
    from app.services.rag_engine import RAGEngine

    db_path = fresh_db.execute("PRAGMA database_list").fetchone()[2]
    return RAGEngine(
        db_path=db_path,
        embedding_service=_FakeEmbSvc(),
        vector_store=_FakeVec(),
        memory_store=_FakeMem(),
        llm_client=_FakeLLM(),
    )


# ---------------------------------------------------------------------------
# Acceptance checks
# ---------------------------------------------------------------------------


def test_orgless_vault_resolves_global_active_version(fresh_db, store):
    """A global active prompt version must resolve for an org-less vault.

    An admin activated version "c03-active" globally (is_active=1); the chat
    targets a vault whose org_id is NULL. The resolver must still surface the
    global active version label.
    """
    store.create_version("c03-active", "c03 global active content", activate=True)

    # Org-less vault (org_id NULL), unique id to avoid fixture collisions.
    fresh_db.execute(
        "INSERT OR IGNORE INTO vaults (id, org_id, name, created_at) VALUES (?, ?, ?, ?)",
        (9201, None, "C03 Orgless Vault 9201", "2024-01-01T00:00:00Z"),
    )
    fresh_db.commit()

    engine = _make_engine(fresh_db)
    content, ab_eid, ab_var, p_version = engine._resolve_prompt_with_ab_sync(
        vault_id=9201, user_id=None
    )

    assert p_version == "c03-active"


def test_all_vaults_query_resolves_global_active_version(fresh_db, store):
    """A global active prompt version must resolve for an All-Vaults chat.

    An All-Vaults chat passes vault_id=None; the resolver must still surface
    the globally active version label instead of None.
    """
    store.create_version("c03-active", "c03 global active content", activate=True)

    engine = _make_engine(fresh_db)
    content, ab_eid, ab_var, p_version = engine._resolve_prompt_with_ab_sync(
        vault_id=None, user_id=None
    )

    assert p_version == "c03-active"


def test_orgless_vault_system_message_carries_active_content(fresh_db, store):
    """The built system message must carry the active version's content.

    Mirrors the query path (rag_engine.py ~L1986-2007): resolve the effective
    prompt, then pass it to prompt_builder.build_messages as
    system_prompt_override. The unique marker in the active version's content
    must appear in the system message (messages[0]).
    """
    store.create_version(
        "c03-active",
        "C03-ACTIVE-PROMPT-MARKER c03 global active content",
        activate=True,
    )

    fresh_db.execute(
        "INSERT OR IGNORE INTO vaults (id, org_id, name, created_at) VALUES (?, ?, ?, ?)",
        (9201, None, "C03 Orgless Vault 9201", "2024-01-01T00:00:00Z"),
    )
    fresh_db.commit()

    engine = _make_engine(fresh_db)
    content, *_ = engine._resolve_prompt_with_ab_sync(vault_id=9201, user_id=None)

    messages = engine.prompt_builder.build_messages(
        "q", [], [], [], None, system_prompt_override=content
    )
    found = "C03-ACTIVE-PROMPT-MARKER" in messages[0]["content"]
    assert found is True
