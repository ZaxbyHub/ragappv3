"""Phase 4.2 guardrail + FTS semantics pins for issue #712 (NOT a frozen
check file).

Guardrail: MemoryStore must contain ZERO fresh-event-loop bridges
(``asyncio.run`` calls / ``self._run_coro_sync`` calls) anywhere in the
module — the frozen C1 check covers the search path one hop deep; this
census bans the mechanism module-wide.

SCOPE (stated precisely, per review C30): the census matches exactly the
two spellings ``asyncio.run(...)`` and ``self._run_coro_sync(...)`` as
CALL nodes anywhere in ``backend/app/services/memory_store.py``. It does
not claim to police other event-loop constructions (an alias, a from-import,
or ``new_event_loop().run_until_complete`` would not match) and it does not
scan any other file. Demonstrated RED at base
0a43a0a00fe64c13ed246145407fa09862fd2c79 with three call sites
(memory_store.py:577 add_memory, :630 update_memory_content, :950
search_memories).

FTS pins: the two DISCLOSED narrowing classes of the tokenizer swap
(operator words neutralized; underscore-joined single-token queries) and
the broadening cap note.
"""

import ast
import os
import sys
import types
from pathlib import Path

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

from app.models.database import SQLiteConnectionPool, init_db
from app.services.memory_store import MemoryStore


def test_memory_store_module_has_no_fresh_event_loop_bridges():
    """Zero asyncio.run / self._run_coro_sync CALL nodes module-wide.

    Mutation-demonstrated: reverting any of the three removed call sites
    (or introducing a new one anywhere in the module) fails this census.
    """
    source_path = (
        Path(__file__).resolve().parents[1] / "app" / "services" / "memory_store.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "asyncio"
            and func.attr == "run"
        ):
            hits.append(node.lineno)
        elif (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "self"
            and func.attr == "_run_coro_sync"
        ):
            hits.append(node.lineno)
    assert hits == [], (
        "MemoryStore must never bridge sync->async with a fresh event loop "
        f"(asyncio.run/_run_coro_sync found at lines {hits}); await "
        "embed_query/embed_and_store on the caller's loop instead (issue #712)"
    )


def _store_with_rows(tmp_path):
    """Build a real MemoryStore under pytest's tmp_path so nothing leaks."""
    db_path = Path(str(tmp_path)) / "census.db"
    init_db(str(db_path))
    pool = SQLiteConnectionPool(str(db_path), max_size=2)
    store = MemoryStore(pool=pool)
    store.add_memory("Justice Sakyi is the AFOMIS Chief", category="facts")
    store.add_memory("choose the chief or the deputy for review", category="facts")
    store.add_memory("the foo_bar integration endpoint", category="doc")
    return store, pool


def test_fts_operator_words_are_neutralized_not_live_operators(tmp_path):
    """Disclosed narrowing class (a): 'chief OR deputy' previously leaked the
    bare OR into FTS5 as a live operator (matching chief-only rows). The
    shared tokenizer neutralizes it to a literal prefix term, so the query
    now ANDs chief + or + deputy and the chief-only row no longer matches."""
    store, pool = _store_with_rows(tmp_path)
    try:
        results = store.search_memories("chief OR deputy", limit=5)
        contents = [r.content for r in results]
        # The row carrying all three literal terms (chief, or, deputy) matches.
        assert any("choose the chief or the deputy" in c for c in contents), (
            f"row with every literal term must match; got {contents}"
        )
        assert not any("AFOMIS Chief" in c for c in contents), (
            "chief-only row must not match the AND-of-literal-prefix-terms "
            f"query anymore (the old code leaked OR as a live FTS5 operator); "
            f"got {contents}"
        )
    finally:
        pool.close_all()


def test_fts_underscore_query_is_one_token(tmp_path):
    """Disclosed narrowing class (b): 'foo_bar' is a single \\w+ token (not
    two AND terms), so it matches only rows carrying the literal
    underscore-joined token."""
    store, pool = _store_with_rows(tmp_path)
    try:
        results = store.search_memories("foo_bar", limit=5)
        contents = [r.content for r in results]
        assert contents == ["the foo_bar integration endpoint"], (
            f"underscore query must match only the foo_bar row; got {contents}"
        )
    finally:
        pool.close_all()


def test_fts_stopword_heavy_query_not_decapitated(tmp_path):
    """Cap-semantics broadening pin: a query whose first 8 raw tokens are
    stop words still reaches the content tokens (filter precedes the cap)."""
    store, pool = _store_with_rows(tmp_path)
    try:
        results = store.search_memories(
            "who is the what is the who are the afomis chief", limit=5
        )
        assert any("AFOMIS" in r.content for r in results), (
            "stop-word-heavy question must still match; the raw token cap "
            "must apply AFTER stop-word removal"
        )
    finally:
        pool.close_all()
