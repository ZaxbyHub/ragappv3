"""Wire-level proof that an org-less vault's chat response carries the
activated prompt version (issue #708, PR #883 feedback round).

The frozen acceptance checks prove the RESOLVER returns the active label and
content; this module proves that value survives the route mapping
(``ChatResponse.prompt_version``) with a REAL RAGEngine behind the chat router
— every other route test stubs the engine and hardcodes that key, so the
newly-populated topology was unproven on the wire.

CSRF_TEST_POLICY = "naive"  # the module tests chat, not the CSRF gate
"""

import os
import sqlite3
import tempfile
from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

import pytest

from app.models.database import run_migrations
from app.services.prompt_store import PromptVersionStore


@pytest.fixture
def fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = None
    try:
        run_migrations(path)
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO organizations (id, name, slug, created_at) VALUES (?, ?, ?, ?)",
            (10, "Test Org", "test-org", "2024-01-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO vaults (id, org_id, name, created_at) VALUES (?, ?, ?, ?)",
            (9401, None, "Wire Orgless Vault", "2024-01-01T00:00:00Z"),
        )
        conn.commit()
        yield conn
    finally:
        if conn is not None:
            conn.close()
        os.unlink(path)


class _FakeEmbSvc:
    async def embed_single(self, t):
        return [0.1, 0.2]

    async def embed_passage(self, t):
        return [0.1, 0.2]


class _FakeVec:
    def __init__(self, results):
        self._results = results

    async def search(self, embedding, limit=10, filter_expr=None, vault_id=None, query_text=None, hybrid=False, **kwargs):
        results = self._results
        if vault_id is not None:
            target = str(vault_id)
            results = [r for r in results if str(r.get("vault_id")) == target]
        return results[:limit]

    async def get_chunks_by_uid(self, chunk_uids):
        return list(self._results)

    def get_fts_exceptions(self):
        return 0


class _FakeMem:
    def __init__(self):
        self.added = []

    def detect_memory_intent(self, text):
        return None

    def find_memory_by_content(self, content, source, vault_id):
        return None

    def add_memory(self, content, category=None, tags=None, source=None, vault_id=None):
        self.added.append(content)
        return None

    def search_memories(self, query, limit=5, vault_id=None, include_global=False):
        return []


class _CapturingLLM:
    def __init__(self, response="ok [S1]"):
        self.response = response
        self.calls = []

    async def chat_completion(self, messages, **kwargs):
        self.calls.append([dict(m) for m in messages])
        return self.response

    async def chat_completion_stream(self, messages, **kwargs):
        self.calls.append([dict(m) for m in messages])
        for token in (self.response,):
            yield token


def test_chat_response_carries_prompt_version_for_orgless_vault(fresh_db):
    """POST /api/chat against an org-less vault returns the active version label.

    Uses the real RAGEngine (db_path = the migrated temp DB) so the whole
    chain runs: resolver → build_messages → done chunk → ChatResponse.
    """
    from app.services.rag_engine import RAGEngine

    store = PromptVersionStore(fresh_db)
    store.create_version(
        "c03-wire-active",
        "C03-WIRE-PROMPT-MARKER wire content",
        activate=True,
    )
    db_path = fresh_db.execute("PRAGMA database_list").fetchone()[2]

    engine = RAGEngine(
        db_path=db_path,
        embedding_service=_FakeEmbSvc(),
        vector_store=_FakeVec(
            [
                {
                    "text": "chunk one",
                    "file_id": "file1",
                    "vault_id": 9401,
                    "metadata": {"source_file": "doc.md"},
                    "score": 0.9,
                }
            ]
        ),
        memory_store=_FakeMem(),
        llm_client=_CapturingLLM(),
    )

    # Pin the query to the deterministic standard pipeline: no agentic early
    # return (a known out-of-scope boundary) and no optional enrichment stages.
    from app.config import settings

    snapshot = {}

    def _set(attr, value):
        # Fail loudly if a setting is renamed (silent skipping would leave the
        # feature ENABLED and quietly change what this test exercises).
        assert hasattr(settings, attr), f"settings.{attr} no longer exists"
        snapshot[attr] = getattr(settings, attr)
        setattr(settings, attr, value)

    try:
        for attr in (
            "hybrid_search_enabled",
            "context_distillation_enabled",
            "retrieval_evaluation_enabled",
            "query_transformation_enabled",
            "reranking_enabled",
            "wiki_enabled",
            "parent_retrieval_enabled",
            "anchor_best_chunk",
            "context_distillation_synthesis_enabled",
            "instant_skip_retrieval_evaluation",
            "instant_skip_query_transformation",
            "agentic_rag_enabled",
        ):
            _set(attr, False)
        _drive_route_and_assert(engine)
    finally:
        for attr, value in snapshot.items():
            setattr(settings, attr, value)


def _drive_route_and_assert(engine):
    """Stub the DB seams, drive POST /api/chat, assert the wire payload."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.deps import get_db, get_rag_engine, get_vector_store
    from app.api.routes import chat as chat_module
    from app.api.routes.chat import get_current_active_user

    async def _no_supersession_chunks(sources):
        return None

    engine._check_supersession = _no_supersession_chunks
    # Non-empty file id set: an empty result makes the visibility filter drop
    # every retrieved chunk.
    engine._get_indexed_file_ids = lambda vault_id: {"file1"}

    async def _no_supersession_ids(file_ids):
        return []

    engine._check_supersession_file_ids = _no_supersession_ids

    ready_vs = MagicMock()
    ready_vs._ready = True
    fake_conn = MagicMock()
    fake_conn.in_transaction = False

    app = FastAPI()
    app.include_router(chat_module.router, prefix="/api")
    app.dependency_overrides[get_rag_engine] = lambda: engine
    app.dependency_overrides[get_current_active_user] = lambda: {
        "id": 1, "username": "u", "email": "u@e", "role": "admin",
    }
    app.dependency_overrides[get_vector_store] = lambda: ready_vs
    app.dependency_overrides[get_db] = lambda: fake_conn

    client = TestClient(app)
    try:
        with patch.object(
            chat_module, "get_admission_controller", lambda: _PassthroughController()
        ):
            response = client.post(
                "/api/chat",
                json={"message": "question", "history": [], "vault_id": 9401},
            )
        assert response.status_code == 200, response.text[:400]
        payload = response.json()
        # The label the admin activated must reach the client for an org-less
        # vault (before #708 this was always None on this topology).
        assert payload.get("prompt_version") == "c03-wire-active", payload
        # And the content must actually reach the LLM system message.
        system_contents = [
            m[0]["content"]
            for m in engine.llm_client.calls
            if m and m[0].get("role") == "system"
        ]
        assert any("C03-WIRE-PROMPT-MARKER" in c for c in system_contents)
    finally:
        app.dependency_overrides.clear()


class _PassthroughController:
    """Admission controller stub: admit immediately (shape mirrors chat.py)."""

    def __init__(self):
        self.calls = []

    async def queue_depth(self, cls):
        return 0

    @asynccontextmanager
    async def admit(self, admission_class, **kwargs):
        self.calls.append((admission_class, kwargs))
        yield
