"""Route-level coverage for the issue #712 memory wiring (PR #931 feedback,
PRR-003).

The PR's engine-side wiring had tests (``test_c07_engine_memory_wiring.py``),
but the memories ROUTE half did not: reverting both
``api/routes/memories.py`` hunks — the ``_perform_memory_search`` precompute +
kwarg gate, and ``create_memory``'s embed-after-insert — left every test green,
so the route could silently degrade to FTS-only (exactly the class this PR
exists to remove) with no signal.

Not frozen / not a manifest path — added in the PR-feedback round.
"""

import logging
import os
import sys
import types
from types import SimpleNamespace
from typing import List

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

from app.api.routes import memories as memories_routes
from app.models.database import SQLiteConnectionPool, init_db
from app.services.memory_store import MemoryStore


class _RecordingEmbedder:
    def __init__(self, exc=None, falsy=False):
        self.calls: List[str] = []
        self._exc = exc
        self._falsy = falsy
        self.embedding_service = self  # the store/callers gate on this attribute

    async def embed_single(self, text):
        self.calls.append(text)
        if self._exc is not None:
            raise self._exc
        return [] if self._falsy else [1.0, 0.0]

    async def embed_passage(self, text):
        self.calls.append(text)
        if self._exc is not None:
            raise self._exc
        return [1.0, 0.0]

    async def effective_embedding_model(self):
        return "test-model"


class _RecordingStore(MemoryStore):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.search_calls = []

    def search_memories(self, query, *args, **kwargs):
        self.search_calls.append((query, dict(kwargs)))
        return super().search_memories(query, *args, **kwargs)


def _store(tmp_path, embedder):
    db_path = tmp_path / "route.db"
    init_db(str(db_path))
    pool = SQLiteConnectionPool(str(db_path), max_size=2)
    return _RecordingStore(pool=pool, embedding_service=embedder), pool


async def test_route_search_precomputes_and_passes_the_query_embedding(tmp_path):
    """_perform_memory_search must await the embedding on its own loop and
    hand it to the sync search — the route half of the AC1 wiring."""
    embedder = _RecordingEmbedder()
    store, pool = _store(tmp_path, embedder)
    store.add_memory("quarterly roadmap planning notes", category="task", vault_id=1)
    try:
        rows = await memories_routes._perform_memory_search(
            store, "quarterly roadmap", limit=5, vault_id=1
        )
        assert embedder.calls == ["quarterly roadmap"], (
            f"the route must precompute the query embedding once: {embedder.calls}"
        )
        assert store.search_calls, "the route must run the memory search"
        _query, kwargs = store.search_calls[0]
        assert kwargs.get("query_embedding") == [1.0, 0.0], (
            "the precomputed embedding must reach the sync search via the "
            f"query_embedding kwarg; got {kwargs}"
        )
        assert kwargs.get("vault_id") == 1
        assert kwargs.get("include_global") is False
        assert rows
    finally:
        pool.close_all()


async def test_route_search_degrades_to_fts_with_warning_when_precompute_fails(
    tmp_path, caplog
):
    """A precompute failure must log a WARNING and still serve the search —
    never abort the request (the route's degrade contract)."""
    embedder = _RecordingEmbedder(exc=RuntimeError("provider down"))
    store, pool = _store(tmp_path, embedder)
    store.add_memory("quarterly roadmap planning notes", category="task", vault_id=1)
    try:
        with caplog.at_level(logging.WARNING, logger="app.services.memory_store"):
            rows = await memories_routes._perform_memory_search(
                store, "quarterly roadmap", limit=5, vault_id=1
            )
        assert any(
            r.levelno >= logging.WARNING and "degraded to FTS-only" in r.getMessage()
            for r in caplog.records
        ), "a failed precompute must surface at WARNING"
        assert store.search_calls, "the search must still run (FTS-only)"
        _query, kwargs = store.search_calls[0]
        assert "query_embedding" in kwargs and kwargs["query_embedding"] is None, (
            "the kwarg is still PASSED (as None) after a failed precompute"
        )
        assert rows, "FTS must still serve the row"
    finally:
        pool.close_all()


async def test_route_search_omits_kwarg_when_no_embedding_service(tmp_path):
    """A store with no embedding service must receive NO new kwarg — plain
    test doubles and third-party stores keep working unchanged."""
    db_path = tmp_path / "nosvc.db"
    init_db(str(db_path))
    pool = SQLiteConnectionPool(str(db_path), max_size=2)
    store = _RecordingStore(pool=pool, embedding_service=None)
    store.add_memory("quarterly roadmap planning notes", category="task", vault_id=1)
    try:
        rows = await memories_routes._perform_memory_search(
            store, "quarterly roadmap", limit=5, vault_id=1
        )
        _query, kwargs = store.search_calls[0]
        assert "query_embedding" not in kwargs, (
            f"a store without an embedding service must get no new kwarg: {kwargs}"
        )
        assert rows
    finally:
        pool.close_all()


async def test_falsy_embedding_is_observed_at_warning(tmp_path, caplog):
    """PRR-002 / C19: a provider that returns an empty vector WITHOUT raising
    used to degrade silently, because both the store log and the callers'
    ``query_embedding=None`` stay quiet on that path."""
    embedder = _RecordingEmbedder(falsy=True)
    store, pool = _store(tmp_path, embedder)
    store.add_memory("quarterly roadmap planning notes", category="task", vault_id=1)
    try:
        with caplog.at_level(logging.WARNING, logger="app.services.memory_store"):
            query_embedding = await store.embed_query("quarterly roadmap")
            rows = await memories_routes._perform_memory_search(
                store, "quarterly roadmap", limit=5, vault_id=1
            )
        assert query_embedding is None
        assert any(
            "returned empty" in r.getMessage() for r in caplog.records
        ), f"the falsy-embedding path must log at WARNING; got {[r.getMessage() for r in caplog.records]}"
        assert rows, "FTS must still serve the row"
    finally:
        pool.close_all()


class _StubStore:
    """Minimal store double recording the create route's embed follow-up."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.embedded = []
        self.added = []
        self.embedding_service = object()  # route gates on its presence

    def add_memory(self, content, **kwargs):
        self.added.append(content)
        return SimpleNamespace(id=42, content=content, category=None,
                               tags=None, source=None, importance=0.5,
                               expires_at=None, created_at=None,
                               updated_at=None, vault_id=kwargs.get("vault_id"))

    async def embed_and_store(self, memory_id, content):
        self.embedded.append((memory_id, content))
        return self.outcome


class _Recorder(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.NOTSET)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


async def _call_create(store, level=logging.WARNING):
    """Drive create_memory directly with stubbed deps (no HTTP layer).

    Returns the list of WARNING+ messages the route emitted.
    """
    from app.api.routes import memories as routes

    async def _evaluate(_user, _kind, _vault_id, _action):
        return True

    # slowapi's rate-limit decorator requires a real starlette Request.
    from starlette.requests import Request as _StarletteRequest

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/memories",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 12345),
        "query_string": b"",
    }
    recorder = _Recorder()
    target = logging.getLogger("app.api.routes.memories")
    target.addHandler(recorder)
    try:
        await routes.create_memory(
            request=_StarletteRequest(scope),
            body=routes.MemoryCreateRequest(content="roadmap notes",
                                            vault_id=1),
            memory_store=store,
            user={"id": 1, "role": "member"},
            evaluate=_evaluate,
            _csrf_token="t",
            conn=None,
        )
    finally:
        target.removeHandler(recorder)
    return recorder.messages


async def test_create_route_embeds_after_insert(tmp_path):
    """PRR-003 residual: create_memory's embed-after-insert must be pinned
    BEHAVIORALLY. The previous source-string pin could be satisfied by the
    UPDATE route's identical warning string, so deleting only this block
    left every test green."""
    store = _StubStore(outcome="stored")
    await _call_create(store)
    assert store.added == ["roadmap notes"], "the row insert must still happen"
    assert store.embedded == [(42, "roadmap notes")], (
        "create_memory must embed the new row on the caller's loop; the "
        f"follow-up did not run (embedded={store.embedded})"
    )


async def test_create_route_warns_when_the_embed_does_not_store(tmp_path):
    store = _StubStore(outcome="failed")
    messages = await _call_create(store)
    assert any(
        "without embedding" in m and "id=42" in m and "outcome=failed" in m
        for m in messages
    ), f"a failed embed must warn with the row id; got {messages}"


async def test_create_route_is_quiet_on_a_stored_embed(tmp_path):
    store = _StubStore(outcome="stored")
    messages = await _call_create(store)
    assert not any(
        "without embedding" in m for m in messages
    ), f"a stored embed must not warn; got {messages}"


if __name__ == "__main__":
    raise SystemExit(
        "run this module with pytest — its tests are async and use tmp_path"
    )
