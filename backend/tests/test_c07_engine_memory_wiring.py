"""Engine/route memory-search wiring for issue #712 (NOT a frozen check file).

Pins the production caller pattern end-to-end at the RAGEngine level:
- the engine awaits ``MemoryStore.embed_query`` on the caller's event loop
  BEFORE dispatching the sync search to a worker thread;
- the precomputed embedding reaches ``search_memories`` via the
  ``query_embedding`` kwarg (passed even when None after a failure);
- ANY precompute failure degrades to WARNING + FTS-only instead of aborting
  the memory search (including a store double whose embed_query is not
  awaitable — the MagicMock class from test_rag_engine_memory_scope.py);
- the remember-directive path embeds only on the newly-inserted branch.
"""

import asyncio
import logging
import os
import sqlite3
import sys
import types
from unittest.mock import MagicMock, patch

# Stub optional heavy dependencies not installed in the test env.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

for _mod in ("lancedb", "pyarrow"):
    if _mod not in sys.modules:
        try:
            __import__(_mod)
        except ImportError:
            sys.modules[_mod] = types.ModuleType(_mod)

if "lancedb.index" not in sys.modules:
    _ldb_idx = types.ModuleType("lancedb.index")
    _ldb_idx.FTS = type("FTS", (), {})
    _ldb_idx.IvfPq = type("IvfPq", (), {})
    sys.modules["lancedb.index"] = _ldb_idx

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    for _sub in ("partition", "partition.auto", "chunking", "chunking.title",
                 "documents", "documents.elements"):
        sys.modules.setdefault(f"unstructured.{_sub}", types.ModuleType(f"unstructured.{_sub}"))
    sys.modules.setdefault("unstructured", types.ModuleType("unstructured"))

from app.models.database import SQLiteConnectionPool, init_db, run_migrations
from app.services import rag_engine as rag_engine_module
from app.services.memory_store import MemoryStore


class _RecordingEmbedder:
    """Deterministic embedder that records every embed_single call."""

    def __init__(self, vector=None, exc=None):
        self.calls = []
        self._vector = vector if vector is not None else [1.0, 0.0]
        self._exc = exc

    async def embed_single(self, text):
        self.calls.append(text)
        if self._exc is not None:
            raise self._exc
        return list(self._vector)

    async def embed_passage(self, text):
        # write-path embeds (embed_and_store) exercise this leg
        self.calls.append(text)
        if self._exc is not None:
            raise self._exc
        return list(self._vector)


class _RecordingStore(MemoryStore):
    """Real store that records the kwargs every search_memories call got."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.search_calls = []

    def search_memories(self, query, *args, **kwargs):
        self.search_calls.append((query, dict(kwargs)))
        return super().search_memories(query, *args, **kwargs)


from contextlib import asynccontextmanager


class _PermissiveAdmission:
    """Permissive admission recorder (518 harness pattern)."""

    async def queue_depth(self, cls):
        return 0

    @asynccontextmanager
    async def admit(self, admission_class, **kwargs):
        yield


def _make_engine(tmp_path, store, embedding_service=None):
    db_path = tmp_path / "engine-wiring.db"
    init_db(str(db_path))
    run_migrations(str(db_path))
    engine = rag_engine_module.RAGEngine(
        embedding_service=embedding_service or MagicMock(),
        vector_store=MagicMock(),
        memory_store=store,
        llm_client=MagicMock(),
        db_path=str(db_path),
    )
    return engine, db_path


async def _drive_query(engine, **kwargs):
    chunks = []
    with patch.object(
        rag_engine_module, "get_admission_controller", lambda: _PermissiveAdmission()
    ):
        async for chunk in engine.query(
            "quarterly roadmap status",
            [],
            stream=True,
            vault_id=1,
            **kwargs,
        ):
            chunks.append(chunk)
    return chunks


async def test_engine_awaits_embed_query_and_passes_precomputed_embedding(
    tmp_path, caplog
):
    embedder = _RecordingEmbedder(vector=[0.5, 0.5])
    db_file = tmp_path / "store-a.db"
    init_db(str(db_file))
    pool = SQLiteConnectionPool(str(db_file), max_size=2)
    store = _RecordingStore(pool=pool, embedding_service=embedder)
    engine, db_path = _make_engine(tmp_path, store)
    try:
        with patch.object(rag_engine_module.settings, "memory_retrieval_enabled", True):
            await _drive_query(engine)
        assert embedder.calls, "engine must await store.embed_query (embed_single)"
        assert store.search_calls, "engine must run the memory search"
        query, kwargs = store.search_calls[0]
        assert kwargs.get("query_embedding") == [0.5, 0.5], (
            "the precomputed embedding must reach the sync search via the "
            f"query_embedding kwarg; got kwargs={kwargs}"
        )
    finally:
        pool.close_all()


async def test_engine_degrades_to_fts_only_with_warning_on_embed_failure(tmp_path, caplog):
    embedder = _RecordingEmbedder(exc=RuntimeError("provider down"))
    db_file = tmp_path / "store-b.db"
    init_db(str(db_file))
    pool = SQLiteConnectionPool(str(db_file), max_size=2)
    store = _RecordingStore(pool=pool, embedding_service=embedder)
    store.add_memory("quarterly roadmap planning notes", category="task", vault_id=1)
    engine, db_path = _make_engine(tmp_path, store)
    try:
        with patch.object(rag_engine_module.settings, "memory_retrieval_enabled", True):
            with caplog.at_level(logging.WARNING, logger="app.services.memory_store"):
                await _drive_query(engine)
        assert any(
            r.levelno >= logging.WARNING and "degraded to FTS-only" in r.getMessage()
            for r in caplog.records
        ), "embed failure must surface at WARNING from the store's logger"
        assert store.search_calls, "memory search must still run (FTS-only)"
        _, kwargs = store.search_calls[0]
        assert "query_embedding" in kwargs and kwargs["query_embedding"] is None, (
            "the kwarg is still PASSED (as None) after a failed precompute so "
            "the store's never-attempted WARNING cannot misfire"
        )
    finally:
        pool.close_all()


async def test_magicmock_store_does_not_abort_memory_search(tmp_path):
    """The MagicMock-store class (test_rag_engine_memory_scope.py pattern):
    the auto-created truthy embedding_service opens the precompute gate and
    the auto-mock embed_query is not awaitable — the engine guard must
    absorb the TypeError and still run the search."""
    store = MagicMock()
    store.search_memories.return_value = []
    # A non-directive query: the auto-mock detect_memory_intent would return
    # a truthy MagicMock and route the turn into the remember-deny path.
    store.detect_memory_intent.return_value = None
    engine, db_path = _make_engine(tmp_path, store)
    try:
        with patch.object(rag_engine_module.settings, "memory_retrieval_enabled", True):
            await _drive_query(engine)
        store.search_memories.assert_called()
        _, kwargs = store.search_memories.call_args
        assert kwargs.get("query_embedding", "absent") is None, (
            "mock store receives the kwarg (as None) after the guard"
        )
    finally:
        pass


async def test_remember_directive_embeds_only_new_rows(tmp_path):
    embedder = _RecordingEmbedder()
    db_path = tmp_path / "remember-embed.db"
    init_db(str(db_path))
    run_migrations(str(db_path))
    pool = SQLiteConnectionPool(str(db_path), max_size=5)
    store = _RecordingStore(pool=pool, embedding_service=embedder)
    engine = rag_engine_module.RAGEngine(
        embedding_service=MagicMock(),
        vector_store=MagicMock(),
        memory_store=store,
        llm_client=MagicMock(),
        db_path=str(db_path),
    )
    try:
        with patch.object(
            rag_engine_module, "get_admission_controller", lambda: _PermissiveAdmission()
        ):
            for _ in range(2):  # second turn hits the idempotent branch
                chunks = []
                async for chunk in engine.query(
                    "remember that the pump code is 7",
                    [],
                    stream=True,
                    vault_id=1,
                    can_write_memory=True,
                ):
                    chunks.append(chunk)
                assert any(
                    c.get("type") == "content" and "Memory stored" in c.get("content", "")
                    for c in chunks
                )
        # embed_and_store ran exactly once (the directive's extracted
        # content), never again for the idempotent confirm of the
        # already-stored row on turn two.
        assert embedder.calls.count("the pump code is 7") == 1, embedder.calls
        check = sqlite3.connect(str(db_path))
        try:
            rows = check.execute(
                "SELECT COUNT(*) FROM memories WHERE content LIKE '%pump code is 7%'"
            ).fetchone()[0]
        finally:
            check.close()
        assert rows == 1
    finally:
        pool.close_all()


async def test_search_warning_semantics_never_attempted_vs_attempted(tmp_path, caplog):
    """Reviewer R1 probe-3 gap: the never-attempted tripwire (service
    configured + NO query_embedding kwarg -> "Memory dense search skipped"
    WARNING) and its inverse (query_embedding=None passed after a failed
    precompute -> NO second warning) are plan-named invariants and must be
    pinned, and with them the embed_query failure WARNING itself — so the
    three WARNING semantics of the sentinel design each have a direct test."""
    db_file = tmp_path / "store-warn.db"
    init_db(str(db_file))
    run_migrations(str(db_file))
    pool = SQLiteConnectionPool(str(db_file), max_size=2)
    store = MemoryStore(pool=pool, embedding_service=_RecordingEmbedder(exc=RuntimeError("down")))
    store.add_memory("quarterly roadmap planning notes", category="task", vault_id=1)
    try:
        with caplog.at_level(logging.WARNING, logger="app.services.memory_store"):
            # Attempted-and-failed precompute: embed_query warns, and the
            # search (which receives query_embedding=None) must NOT add the
            # never-attempted warning.
            query_embedding = await store.embed_query("quarterly roadmap")
            assert query_embedding is None
            results = store.search_memories(
                "quarterly roadmap", limit=5, query_embedding=None
            )
        msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("degraded to FTS-only" in m for m in msgs), msgs
        assert not any("dense search skipped" in m for m in msgs), (
            f"attempted-but-failed precompute must not trigger the "
            f"never-attempted tripwire: {msgs}"
        )
        assert len(results) == 1  # FTS still serves the row

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="app.services.memory_store"):
            # Never-attempted: service configured, kwarg absent entirely.
            results = store.search_memories("quarterly roadmap", limit=5)
        msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any(
            "Memory dense search skipped: no precomputed query embedding" in m
            for m in msgs
        ), f"the never-attempted tripwire must fire: {msgs}"
        assert len(results) == 1  # FTS still serves the row here too
    finally:
        pool.close_all()
