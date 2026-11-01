"""Frozen checks for issue #712 (C07) — memory bridge event-loop behavior.

AC1: ``MemoryStore.search_memories`` (and, one hop deeper, the bodies of
every ``self.<method>`` it calls) must not bridge async embedding calls
with ``asyncio.run`` / ``self._run_coro_sync`` — spinning a fresh event
loop per search is the stall vector this issue fixes.

AC2: when the dense-embedding service fails on the memory search path, the
fallback to FTS-only must be logged at WARNING (not silently at DEBUG) so
operators can observe the degraded retrieval mode. Amended per the issue's
CHECK_WRONG note to capture the caller-side embed helper
(``MemoryStore.embed_query``) that the fix moves the embedding into.
"""

import ast
import inspect
import logging
import os
import sys
import types
from pathlib import Path

# Stub optional heavy dependencies not installed in the test env.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

for _mod in ('lancedb', 'pyarrow'):
    if _mod not in sys.modules:
        try:
            __import__(_mod)
        except ImportError:
            _stub = types.ModuleType(_mod)
            sys.modules[_mod] = _stub

# lancedb.index must exist before vector_store.py is imported
if 'lancedb.index' not in sys.modules:
    _ldb_idx = types.ModuleType('lancedb.index')
    _ldb_idx.FTS = type('FTS', (), {})
    _ldb_idx.IvfPq = type('IvfPq', (), {})
    sys.modules['lancedb.index'] = _ldb_idx

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    for _sub in ('partition', 'partition.auto', 'chunking', 'chunking.title',
                 'documents', 'documents.elements'):
        sys.modules.setdefault(f'unstructured.{_sub}', types.ModuleType(f'unstructured.{_sub}'))
    sys.modules.setdefault('unstructured', types.ModuleType('unstructured'))

from app.models.database import SQLiteConnectionPool, init_db
from app.services.memory_store import MemoryStore


def test_search_memories_does_not_spin_a_fresh_event_loop():
    """AC1: no asyncio.run / self._run_coro_sync in search_memories' body
    or in the bodies of the MemoryStore methods it calls (one hop)."""
    source_path = (
        Path(__file__).resolve().parents[1] / "app" / "services" / "memory_store.py"
    )
    tree = ast.parse(
        source_path.read_text(encoding="utf-8"), filename=str(source_path)
    )

    # Map every method name defined on any class to its FunctionDef, and
    # locate the search_memories definition.
    methods = {}
    search_def = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                methods.setdefault(item.name, item)
                if item.name == "search_memories":
                    search_def = item
    assert search_def is not None, "search_memories not found in memory_store.py"

    def _bridge_call_count(func_def):
        """Count asyncio.run(...) / self._run_coro_sync(...) Call nodes."""
        hits = 0
        for node in ast.walk(func_def):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "asyncio"
                and func.attr == "run"
            ):
                hits += 1
            elif (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "self"
                and func.attr == "_run_coro_sync"
            ):
                hits += 1
        return hits

    # Collect self.<method>(...) calls made directly inside search_memories
    # that resolve to methods defined on the class (one hop deeper). The
    # bridge method's own implementation is not descended into: its body is
    # ``asyncio.run`` by definition, so walking it would double-count the
    # very call site (search_memories -> self._run_coro_sync) this check
    # targets. Helpers like _fts_search / _dense_search ARE swept.
    called = set()
    for node in ast.walk(search_def):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
            and node.func.attr in methods
            and node.func.attr not in ("search_memories", "_run_coro_sync")
        ):
            called.add(node.func.attr)

    count = _bridge_call_count(search_def)
    for name in sorted(called):
        count += _bridge_call_count(methods[name])

    assert count == 0


class _Raising:
    """Embedding-service double whose query embed always fails.

    Deliberately a plain class: no embed_passage / embed_passage_with_label,
    so add_memory's best-effort embed is skipped via the exception path and
    the memory row is still inserted.
    """

    async def embed_single(self, text):
        raise RuntimeError("embed down")


class _RecordHandler(logging.Handler):
    """Collect every record emitted by a logger while attached."""

    def __init__(self):
        super().__init__(level=logging.NOTSET)
        self.records = []

    def emit(self, record):
        self.records.append(record)


async def test_dense_embedding_failure_is_logged_at_warning(tmp_path):
    """AC2 (amended per the issue's own CHECK_WRONG note: "If the fix moves
    query embedding into RAGEngine, the check author re-targets the capture
    at that caller"): the query-embed step of the memory search path —
    ``MemoryStore.embed_query``, the helper the RAG engine awaits on its own
    loop before dispatching ``search_memories`` — must surface a failing
    dense embed at WARNING, and the store must still serve FTS results."""
    db_path = tmp_path / "test.db"
    init_db(str(db_path))
    pool = SQLiteConnectionPool(str(db_path), max_size=2)
    store = MemoryStore(pool=pool, embedding_service=_Raising())
    store.add_memory(content="quarterly roadmap planning notes", category="task")

    handler = _RecordHandler()
    log = logging.getLogger("app.services.memory_store")
    log.addHandler(handler)
    try:
        query_embedding = await store.embed_query("quarterly roadmap")
        result = store.search_memories("quarterly roadmap", limit=5)
        if inspect.isawaitable(result):
            result = await result
    finally:
        log.removeHandler(handler)
        pool.close_all()

    warned = any(r.levelno >= logging.WARNING for r in handler.records)
    assert warned is True
    assert query_embedding is None
    assert len(result) == 1
