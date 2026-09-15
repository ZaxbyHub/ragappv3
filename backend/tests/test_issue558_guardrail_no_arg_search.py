"""Issue #558 C13 guardrail: no zero-argument ``.search()`` calls in backend/app.

lancedb 0.36's ``AsyncTable.search()`` requires a query argument: the
no-argument auto path raises ``UnboundLocalError`` inside lancedb and returns
a coroutine whose omission from ``await`` produced the swallowed
``RuntimeWarning`` and the silent 50-row fallback degradation fixed in
issue #558 (the parent-window startup check chained ``.where``/``.limit``
onto the coroutine object on every boot).

This source contract fails on the original defect site and passes on the
fixed ``table.query()`` builder shape. It is deliberately keyed to the
zero-argument signature: keyword-only app-method calls
(``vector_store.search(embedding=..., ...)`` at search.py:184 and
vector_store.py:1551/1559) and the ``re``-pattern ``.search(text)`` family
all pass arguments and are correctly outside the ban (census verified at
plan time and by the Phase 4.2 sweep).
"""

import ast
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1] / "app"


def _zero_arg_search_calls():
    hits = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "search"
                and not node.args
                and not node.keywords
            ):
                hits.append(f"{path.relative_to(APP_ROOT.parent).as_posix()}:{node.lineno}")
    return hits


def test_no_zero_argument_search_calls():
    hits = _zero_arg_search_calls()
    assert hits == [], (
        "Zero-argument .search() calls are banned: lancedb 0.36's "
        "AsyncTable.search() requires a query argument (the no-argument path "
        "raises UnboundLocalError internally, and forgetting the await on its "
        "coroutine degrades every call to a silent fallback — issue #558 C13). "
        f"Offending sites: {hits}. Build the query from the synchronous "
        "table.query() builder instead."
    )
