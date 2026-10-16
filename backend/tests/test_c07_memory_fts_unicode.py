"""Frozen checks for issue #712 (C07) — FTS tokenization of non-ASCII text.

AC4: a CJK query must recall a memory whose content contains that CJK
text via the FTS lexical arm.

AC5: an accented-Latin query ("café") must recall the memory containing
the accented token. The ASCII-only ``[a-zA-Z0-9]+`` tokenizer drops the
whole CJK query (and truncates "café" to "caf"), silently yielding zero
FTS tokens and no lexical recall at all.
"""

import inspect
import os
import sys
import types

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


def _make_store(tmp_path):
    """Pool + MemoryStore with no embedding service (FTS-only arm)."""
    db_path = tmp_path / "test.db"
    init_db(str(db_path))
    pool = SQLiteConnectionPool(str(db_path), max_size=2)
    store = MemoryStore(pool=pool)
    return pool, store


async def test_cjk_query_recalls_memory_via_fts(tmp_path):
    """AC4: CJK query text must reach the FTS index."""
    pool, store = _make_store(tmp_path)
    try:
        store.add_memory(
            content="北京 office opening schedule for the Q3 launch",
            category="plan",
        )
        result = store.search_memories("北京")
        if inspect.isawaitable(result):
            result = await result
    finally:
        pool.close_all()
    assert len(result) == 1


async def test_accented_query_recalls_memory_via_fts(tmp_path):
    """AC5: accented-Latin query text must reach the FTS index."""
    pool, store = _make_store(tmp_path)
    try:
        store.add_memory(
            content="Meeting at the café on Friday to review the design",
            category="task",
        )
        result = store.search_memories("café")
        if inspect.isawaitable(result):
            result = await result
    finally:
        pool.close_all()
    assert len(result) == 1
