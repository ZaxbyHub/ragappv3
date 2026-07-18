"""RAG-level regression test for issue #404 (critic R13).

Proves the chat-time memory leak is closed end-to-end at the engine seam:

* ``include_global=False`` (non-admin caller) → ``search_memories`` is called
  with ``include_global=False`` and global memories do NOT surface in the
  retrieved candidate set. Pre-fix: the engine called ``search_memories``
  with no flag and global memories leaked into the chat prompt.
* ``include_global=True`` (admin caller) → global memories are eligible.
* ``can_write_memory=False`` → a detected "remember ..." directive does NOT
  call ``add_memory``; the user gets a feedback chunk instead. Pre-fix: the
  directive was always persisted.
* ``can_write_memory=True`` → the directive IS persisted.

These tests exercise ``RAGEngine.query`` directly with the heavy downstream
dependencies (embedding service, vector store, LLM client) mocked, so the
memory-intent + memory-retrieve branches are driven deterministically
without a live model.
"""

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "backend")

from app.services.rag_engine import RAGEngine


def _run(coro):
    """Drive an async generator to completion, collecting yielded chunks.

    Swallows RAGEngineError raised mid-pipeline — the tests in this module
    only assert on what the engine *called* before the pipeline reached a
    step that needs a live LLM/embedder, not on a clean end-of-stream.
    """
    chunks = []

    async def drain():
        try:
            async for c in coro:
                chunks.append(c)
        except Exception:
            # Expected: the mocked downstream deps can't complete the full
            # pipeline. We only care about whether the memory branches were
            # reached with the right flags.
            pass

    asyncio.run(drain())
    return chunks


def _make_engine():
    """RAGEngine with downstream deps patched at construction.

    Returns the engine with async-friendly stubs wired onto its instance
    attributes so the memory-intent and memory-retrieve branches run.
    """
    with (
        patch("app.services.rag_engine.EmbeddingService"),
        patch("app.services.rag_engine.VectorStore"),
        patch("app.services.rag_engine.MemoryStore"),
        patch("app.services.rag_engine.LLMClient"),
    ):
        eng = RAGEngine()

    # Stub the embedding service with async coroutines returning a fixed
    # vector so the pipeline can pass the embed step and reach memory
    # retrieval. The vector dimension is arbitrary — the mocked vector_store
    # returns no results either way.
    _VEC = [0.1] * 8

    async def _embed_passage(text):
        return _VEC

    async def _embed_single(text):
        return _VEC

    eng.embedding_service.embed_passage = _embed_passage
    eng.embedding_service.embed_single = _embed_single

    # Memory store: real callables so we can observe what the engine called.
    eng.memory_store = MagicMock()
    eng.memory_store.search_memories = MagicMock(return_value=[])
    eng.memory_store.detect_memory_intent = MagicMock(return_value=None)
    eng.memory_store.add_memory = MagicMock(return_value=None)
    return eng


class TestCanWriteMemoryFlag:
    """These run early in query() (before embedding) — the cleanest proof of
    the R12 fix: the directive branch honors can_write_memory."""

    def test_blocked_write_does_not_persist(self):
        engine = _make_engine()
        engine.memory_store.detect_memory_intent = MagicMock(
            return_value="the key is under the mat"
        )
        chunks = _run(
            engine.query(
                "remember that the key is under the mat",
                [],
                stream=False,
                vault_id=2,
                require_vault=True,
                can_write_memory=False,
            )
        )
        engine.memory_store.add_memory.assert_not_called()
        contents = [c.get("content") for c in chunks if c.get("type") == "content"]
        assert any("can't save memories" in c.lower() for c in contents), (
            f"Expected a feedback chunk when write is blocked; got: {contents}"
        )

    def test_allowed_write_persists(self):
        engine = _make_engine()
        engine.memory_store.detect_memory_intent = MagicMock(
            return_value="the key is under the mat"
        )
        engine.memory_store.add_memory = MagicMock(
            return_value=MagicMock(content="the key is under the mat")
        )
        _run(
            engine.query(
                "remember that the key is under the mat",
                [],
                stream=False,
                vault_id=2,
                require_vault=True,
                can_write_memory=True,
            )
        )
        engine.memory_store.add_memory.assert_called_once()
        _, kwargs = engine.memory_store.add_memory.call_args
        assert kwargs.get("vault_id") == 2


class TestIncludeGlobalFlag:
    """These reach memory retrieval (past embedding). The mocked vector store
    returns no results and the LLM is mocked, so the pipeline can't fully
    complete — but ``search_memories`` IS invoked with the threaded flag,
    which is what we assert. _run swallows the downstream error."""

    def test_non_admin_excludes_global_memories(self):
        engine = _make_engine()
        chunks = _run(
            engine.query(
                "hello",
                [],
                stream=False,
                vault_id=2,
                require_vault=True,
                include_global=False,
            )
        )
        engine.memory_store.search_memories.assert_called()
        _, kwargs = engine.memory_store.search_memories.call_args
        assert kwargs.get("include_global") is False, (
            "Non-admin (include_global=False) was not honored at search_memories: "
            f"{kwargs}"
        )

    def test_admin_includes_global_memories(self):
        engine = _make_engine()
        _run(
            engine.query(
                "hello",
                [],
                stream=False,
                vault_id=2,
                require_vault=False,
                include_global=True,
            )
        )
        engine.memory_store.search_memories.assert_called()
        _, kwargs = engine.memory_store.search_memories.call_args
        assert kwargs.get("include_global") is True

    def test_default_include_global_is_false_fail_closed(self):
        """A caller that omits include_global must NOT leak globals.

        The default is fail-closed (issue #404 reviewer finding). A future
        caller that forgets the flag cannot accidentally expose global
        memories to a non-admin context."""
        engine = _make_engine()
        _run(
            engine.query(
                "hello",
                [],
                stream=False,
                vault_id=2,
                require_vault=True,
                # include_global intentionally OMITTED — must default to False
            )
        )
        engine.memory_store.search_memories.assert_called()
        _, kwargs = engine.memory_store.search_memories.call_args
        assert kwargs.get("include_global") is False, (
            "include_global default is not fail-closed; a caller that omits "
            f"the flag would leak globals: {kwargs}"
        )

    def test_default_can_write_memory_is_false_fail_closed(self):
        """A caller that omits can_write_memory must NOT persist a memory.

        The default is fail-closed (issue #404 reviewer finding)."""
        engine = _make_engine()
        engine.memory_store.detect_memory_intent = MagicMock(
            return_value="the key is under the mat"
        )
        _run(
            engine.query(
                "remember that the key is under the mat",
                [],
                stream=False,
                vault_id=2,
                require_vault=True,
                # can_write_memory intentionally OMITTED — must default to False
            )
        )
        engine.memory_store.add_memory.assert_not_called()


class TestPromoteMemoryDefaultFailClosed:
    """promote_memory's is_admin default must be fail-closed (reviewer finding)."""

    def test_default_is_admin_false_blocks_global_promotion(self):
        """A caller that omits is_admin cannot promote a global memory."""
        import sqlite3
        import tempfile
        from pathlib import Path

        from app.services.wiki_compiler import WikiCompiler
        from app.services.wiki_store import WikiStore

        # Build an in-memory DB with a global memory.
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute(
            "CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT, "
            "vault_id INTEGER, category TEXT, tags TEXT, source TEXT)"
        )
        db.execute(
            "INSERT INTO memories (id, content, vault_id) VALUES (1, 'global secret', NULL)"
        )
        db.commit()

        store = WikiStore(db)
        compiler = WikiCompiler(db, store)
        with pytest.raises(PermissionError):
            # is_admin intentionally OMITTED — must default to False and block
            # promotion of the global (vault_id IS NULL) memory.
            compiler.promote_memory(memory_id=1, vault_id=2)


