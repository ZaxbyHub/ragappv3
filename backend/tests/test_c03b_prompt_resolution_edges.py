"""C03 supplementary edge coverage for the org-less prompt resolution fix (#708).

Non-frozen companion to test_c03_orgless_prompt.py (the frozen acceptance
checks). Pins the disclosed behavior deltas and the interactions the frozen
checks do not reach:

1. A MISSING vault row (deleted vault) resolves the global active version —
   topologically "no vault", same tier as an All-Vaults query.
2. With no active version anywhere, the resolver still returns (None, None)
   and the builder default survives (the fallback must not invent content).
3. End-to-end: engine.query() for an org-less vault serves the active
   version's content in the LLM system message and reports its label on the
   done chunk (execution-pins the rag_engine.py query-path wiring that the
   frozen C3 check mirrors).
4. An active A/B experiment still overrides the global-active fallback for
   org-less vaults when a subject key exists (user_id given).
"""

import asyncio
import os
import sqlite3
import tempfile

import pytest

from app.models.database import run_migrations
from app.services.prompt_store import PromptVersionStore


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
    return PromptVersionStore(fresh_db)


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
        return []

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
    """Records the message lists every completion seam receives."""

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


def _make_engine(fresh_db, vector_store=None, llm_client=None):
    from app.services.rag_engine import RAGEngine

    db_path = fresh_db.execute("PRAGMA database_list").fetchone()[2]
    return RAGEngine(
        db_path=db_path,
        embedding_service=_FakeEmbSvc(),
        vector_store=vector_store or _FakeVec([]),
        memory_store=_FakeMem(),
        llm_client=llm_client or _CapturingLLM(),
    )


def _insert_orgless_vault(fresh_db, vault_id):
    fresh_db.execute(
        "INSERT OR IGNORE INTO vaults (id, org_id, name, created_at) VALUES (?, ?, ?, ?)",
        (vault_id, None, f"C03b Orgless Vault {vault_id}", "2024-01-01T00:00:00Z"),
    )
    fresh_db.commit()


# ---------------------------------------------------------------------------
# 1. Missing vault row (disclosed delta, critic R1 required item 1)
# ---------------------------------------------------------------------------


def test_missing_vault_row_resolves_global_active(fresh_db, store):
    """A query whose vault row no longer exists resolves the global active version.

    Pre-fix the missing row returned (None, None); with the fix a deleted
    vault's chat still serves the admin-activated version rather than silently
    reverting to the built-in default.
    """
    store.create_version("c03-active", "c03 global active content", activate=True)

    engine = _make_engine(fresh_db)
    # 9777 does not exist in vaults.
    content, ab_eid, ab_var, p_version = engine._resolve_prompt_with_ab_sync(
        vault_id=9777, user_id=None
    )

    assert p_version == "c03-active"
    assert content == "c03 global active content"
    assert ab_eid is None and ab_var is None


# ---------------------------------------------------------------------------
# 2. Negative case: no active version anywhere
# ---------------------------------------------------------------------------


def test_no_active_version_falls_back_to_builtin_default(fresh_db, store):
    """With nothing activated and no override, resolution is (None, None) and
    the builder's built-in default survives."""
    engine = _make_engine(fresh_db)

    content, ab_eid, ab_var, p_version = engine._resolve_prompt_with_ab_sync(
        vault_id=None, user_id=None
    )

    assert content is None
    assert p_version is None
    assert ab_eid is None and ab_var is None

    messages = engine.prompt_builder.build_messages(
        "q", [], [], [], None, system_prompt_override=content
    )
    assert messages[0]["content"].startswith("You are KnowledgeVault")


# ---------------------------------------------------------------------------
# 3. End-to-end query() wiring pin
# ---------------------------------------------------------------------------


def test_query_orgless_serves_active_prompt_end_to_end(fresh_db, store):
    """engine.query() against an org-less vault serves the active version.

    Captures the real message list handed to the LLM and the done chunk, so a
    regression that stopped passing system_prompt_override inside query()
    fails here even though the resolver-level checks stay green.
    """
    from app.config import settings

    store.create_version(
        "c03-active",
        "C03-ACTIVE-PROMPT-MARKER c03 global active content",
        activate=True,
    )
    _insert_orgless_vault(fresh_db, 9301)

    # settings is a process-wide singleton: restore every write (same pattern
    # as test_agentic_control_parity.py).
    snapshot = {}

    def _set(attr, value):
        if hasattr(settings, attr):
            snapshot[attr] = getattr(settings, attr)
            setattr(settings, attr, value)

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
    ):
        _set(attr, False)
    _set("retrieval_top_k", 5)
    try:
        llm = _CapturingLLM(response="answer [S1]")
        engine = _make_engine(
            fresh_db,
            vector_store=_FakeVec(
                [
                    {
                        "text": "chunk one",
                        "file_id": "file1",
                        "vault_id": 9301,
                        "metadata": {"source_file": "doc.md"},
                        "score": 0.9,
                    }
                ]
            ),
            llm_client=llm,
        )

        async def _no_supersession(file_ids):
            return []

        engine._check_supersession_file_ids = _no_supersession

        chunks = []

        async def _collect():
            async for chunk in engine.query(
                "question", [], stream=False, vault_id=9301
            ):
                chunks.append(chunk)

        asyncio.run(_collect())
    finally:
        for attr, value in snapshot.items():
            setattr(settings, attr, value)

    assert llm.calls, "LLM completion seam was never reached"
    system_contents = [m[0]["content"] for m in llm.calls if m and m[0].get("role") == "system"]
    assert system_contents, "no system message captured"
    assert any("C03-ACTIVE-PROMPT-MARKER" in c for c in system_contents)

    done = [c for c in chunks if c.get("type") == "done"]
    assert len(done) == 1
    assert done[0].get("prompt_version") == "c03-active"


# ---------------------------------------------------------------------------
# 4. A/B-over-global-active interaction for org-less vaults
# ---------------------------------------------------------------------------


def test_orgless_ab_variant_overrides_global_active(fresh_db, store):
    """An active A/B experiment still wins over the global-active fallback
    for an org-less vault when a subject key exists (user_id given)."""
    from app.services.ab_testing import ABTestingService

    store.create_version("c03-active", "C03-ACTIVE-PROMPT-MARKER global", activate=True)
    store.create_version("c03b_ctrl", "c03b control content")
    store.create_version("c03b_chlg", "c03b challenger content")
    _insert_orgless_vault(fresh_db, 9302)

    ab_service = ABTestingService(fresh_db)
    exp = ab_service.create_experiment("c03b_exp", "c03b_ctrl", "c03b_chlg", split_pct=100)

    engine = _make_engine(fresh_db)
    content, ab_eid, ab_var, p_version = engine._resolve_prompt_with_ab_sync(
        vault_id=9302, user_id=1
    )

    assert ab_eid == exp.id
    assert ab_var == "challenger"
    assert content == "c03b challenger content"
    assert p_version == "c03b_chlg"
