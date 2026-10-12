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
    conn = None
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
        # conn is bound only after run_migrations succeeds; guard so a schema
        # failure surfaces its own traceback instead of an UnboundLocalError.
        if conn is not None:
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
        # Return the searched chunks back so the retrieved evidence survives
        # into the message build (returning [] silently drops every chunk and
        # would let the done-chunk assertions pass without a real query path).
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
    # as test_agentic_control_parity.py). The mutation block sits INSIDE the
    # try so an exception mid-loop still restores everything already written.
    snapshot = {}

    def _set(attr, value):
        # Fail loudly if a setting is renamed: silently skipping it would leave
        # the feature ENABLED and quietly exercise a different engine branch.
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
        ):
            _set(attr, False)
        # The agentic early-return path bypasses the resolver entirely (it is a
        # known out-of-scope boundary), so neutralise it or this test would fail
        # for an unrelated reason on a flag-enabled deployment.
        _set("agentic_rag_enabled", False)
        _set("retrieval_top_k", 5)
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

        # _get_indexed_file_ids/_check_supersession read settings.sqlite_path
        # (the developer's ./data/app.db), NOT the injected db_path, which would
        # make this test's outcome depend on the local machine. Stub both seams
        # and assert a real chunk reaches the done payload below.
        async def _no_supersession_chunks(sources):
            return None

        engine._check_supersession = _no_supersession_chunks
        # Must return the fake chunk's file id, not an empty set: an empty
        # result makes the atomic visibility filter drop every chunk.
        engine._get_indexed_file_ids = lambda vault_id: {"file1"}

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
    # The retrieved chunk must reach the payload, so the assertions above can
    # only pass if the whole query path actually ran.
    assert done[0].get("sources"), "no sources reached the done chunk"


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


# ---------------------------------------------------------------------------
# 5. Feedback-round coverage (swarm-pr-feedback on PR #883)
#    Each block below kills a mutant that survived the pre-feedback suite.
# ---------------------------------------------------------------------------


def test_all_vaults_resolves_active_content_not_just_label(fresh_db, store):
    """All-Vaults resolution must serve the ACTIVE version's CONTENT.

    Kills the mutant that returns the right label but wrong (or no) content
    for ``vault_id=None``; the label-only frozen check cannot see that.
    """
    store.create_version(
        "c03-active", "C03-ACTIVE-PROMPT-MARKER all-vaults content", activate=True
    )

    engine = _make_engine(fresh_db)
    content, _ab_eid, _ab_var, p_version = engine._resolve_prompt_with_ab_sync(
        vault_id=None, user_id=None
    )

    assert p_version == "c03-active"
    assert content == "C03-ACTIVE-PROMPT-MARKER all-vaults content"


def test_inactive_newer_version_does_not_shadow_active_older(fresh_db, store):
    """Only the is_active row is served, and the choice is re-read per query.

    Kills two mutants: ignoring ``is_active`` (which would serve the newest
    inactive row) and caching the first resolution (which would miss a
    re-activation between two calls on the same long-lived engine).
    """
    store.create_version("c03-active", "C03-ACTIVE-PROMPT-MARKER original", activate=True)
    store.create_version("c03-newer-inactive", "C03-SHOULD-NOT-BE-SERVED")

    engine = _make_engine(fresh_db)

    content, _ab_eid, _ab_var, p_version = engine._resolve_prompt_with_ab_sync(
        vault_id=None, user_id=None
    )
    assert p_version == "c03-active"
    assert content == "C03-ACTIVE-PROMPT-MARKER original"
    assert "C03-SHOULD-NOT-BE-SERVED" not in content

    # Admin activates the newer version: the next query must pick it up with
    # no engine restart (resolution is per-query, never cached).
    store.activate("c03-newer-inactive")
    content2, _ab_eid2, _ab_var2, p_version2 = engine._resolve_prompt_with_ab_sync(
        vault_id=None, user_id=None
    )
    assert p_version2 == "c03-newer-inactive"
    assert content2 == "C03-SHOULD-NOT-BE-SERVED"


def test_orgless_vault_ignores_other_orgs_override(fresh_db, store):
    """Org-less resolution never consults another org's override.

    Kills the mutant that resolves an override regardless of the vault's org
    (e.g. resolving an org override for an org the query does not belong to).
    """
    store.create_version("c03-active", "C03-ACTIVE-PROMPT-MARKER global", activate=True)
    store.create_version("c03-other-org", "OTHER-ORG-PRIVATE-PROMPT")
    store.set_org_override(org_id=10, version="c03-other-org")
    _insert_orgless_vault(fresh_db, 9303)

    engine = _make_engine(fresh_db)
    content, _ab_eid, _ab_var, p_version = engine._resolve_prompt_with_ab_sync(
        vault_id=9303, user_id=None
    )

    assert p_version == "c03-active"
    assert content == "C03-ACTIVE-PROMPT-MARKER global"
    assert "OTHER-ORG-PRIVATE-PROMPT" not in content


def test_resolver_fails_open_when_db_is_unusable(fresh_db, store, caplog):
    """A DB failure degrades to (None, None) and logs the cause — never raises.

    Kills the mutant that re-raises out of the exception handler: prompt
    resolution must never fail a query. Uses a DB file with no schema so the
    prompt_versions lookup genuinely fails.
    """
    import logging

    from app.services.rag_engine import RAGEngine

    empty_fd, empty_path = tempfile.mkstemp(suffix=".db")
    os.close(empty_fd)
    try:
        engine = RAGEngine(
            db_path=empty_path,
            embedding_service=_FakeEmbSvc(),
            vector_store=_FakeVec([]),
            memory_store=_FakeMem(),
            llm_client=_CapturingLLM(),
        )
        with caplog.at_level(logging.WARNING):
            content, ab_eid, ab_var, p_version = engine._resolve_prompt_with_ab_sync(
                vault_id=None, user_id=None
            )
        assert (content, ab_eid, ab_var, p_version) == (None, None, None, None)
        assert any("Could not resolve effective prompt" in r.message for r in caplog.records)
    finally:
        os.unlink(empty_path)
