"""AST guard for issue #592 (AC1): no `.get_connection()` on the event loop.

`SQLiteConnectionPool.get_connection()` blocks its caller for up to
`max_wait_attempts` x 5 s when the pool is exhausted, and always runs
`_validate_connection`'s `SELECT 1` + PRAGMA on the calling thread. Any
`<name>.get_connection()` call (e.g. `pool.get_connection()`,
`db_pool.get_connection()`) that executes lexically inside an `async def`
route handler or helper in `backend/app/api/routes/` therefore lands that
block on the asyncio event loop and freezes every concurrent request.

Barrier rule (the issue's own safe-by-construction definition): a call is a
violation ONLY when the INNERMOST enclosing callable is an `async def`. An
intervening sync `def` or `lambda` is a barrier — that callable is what gets
handed to a worker thread (the repo's `asyncio.to_thread` shape), so the
checkout runs off the loop. Nested `async def` inside `async def`: the
innermost async frame counts.

Pure source inspection (mirrors the precedent in
backend/tests/test_deps_auth_to_thread.py): no app imports, no asyncio, no
database — the module under test is never loaded.
"""

import ast
import glob
import os
import unittest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_TESTS_DIR, os.pardir))
_ROUTES_DIR = os.path.join(_BACKEND_ROOT, "app", "api", "routes")

# Sentinel strings the trace manifest greps for.
_SENTINEL_PASS = "AST-GUARD: PASS"
_SENTINEL_FAIL = "AST-GUARD: FAIL"

_CALLABLE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _frame_kind_and_name(node):
    """Classify a callable node as ("async"|"sync"|"lambda", name)."""
    if isinstance(node, ast.AsyncFunctionDef):
        return "async", node.name
    if isinstance(node, ast.Lambda):
        return "lambda", "<lambda>"
    return "sync", node.name


def _receiver_text(call_node):
    """Source text of the `<name>` in `<name>.get_connection()`."""
    try:
        return ast.unparse(call_node.func.value)
    except Exception:
        return "<expr>"


def _violations_in_source(source, label):
    """Return [(label, lineno, receiver, async_func_name)] for every
    `<name>.get_connection()` Call node whose innermost enclosing callable is
    an `async def` (sync def / lambda are barriers; module level is safe).
    """
    violations = []
    tree = ast.parse(source, filename=label)
    stack = []  # innermost-last (kind, name) of enclosing callables

    def walk(node):
        if isinstance(node, _CALLABLE_NODES):
            kind, name = _frame_kind_and_name(node)
            stack.append((kind, name))
            for child in ast.iter_child_nodes(node):
                walk(child)
            stack.pop()
            return
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get_connection"
            and stack
            and stack[-1][0] == "async"
        ):
            violations.append((label, node.lineno, _receiver_text(node), stack[-1][1]))
        for child in ast.iter_child_nodes(node):
            walk(child)

    walk(tree)
    return violations


def find_routes_violations(routes_dir):
    """AST-scan every backend/app/api/routes/*.py file.

    Returns (sorted_file_list, violations) where each violation is
    (rel_path_from_backend_root, lineno, receiver, async_func_name).
    """
    files = sorted(glob.glob(os.path.join(routes_dir, "*.py")))
    violations = []
    for path in files:
        with open(path, "r", encoding="utf-8") as fh:
            source = fh.read()
        rel = os.path.relpath(path, _BACKEND_ROOT).replace(os.sep, "/")
        violations.extend(_violations_in_source(source, rel))
    return files, violations


class TestIssue592NoOnLoopCheckout(unittest.TestCase):
    """AC1: zero `.get_connection()` calls lexically inside async defs in routes."""

    def test_no_get_connection_call_inside_async_def_in_routes(self):
        files, violations = find_routes_violations(_ROUTES_DIR)
        self.assertTrue(
            os.path.isdir(_ROUTES_DIR), f"routes dir not found: {_ROUTES_DIR}"
        )
        self.assertTrue(files, f"no route files found under {_ROUTES_DIR}")

        if violations:
            for rel, lineno, recv, func in violations:
                print(f"{rel}:{lineno}: {recv}.get_connection() inside async def {func}")
            preview = ", ".join(f"{v[0]}:{v[1]}" for v in violations[:8])
            self.fail(
                f"{_SENTINEL_FAIL} — {len(violations)} violation(s) of AC1: "
                f"<name>.get_connection() lexically inside async def in "
                f"backend/app/api/routes/ (first: {preview}). Move the checkout "
                f"off the event loop (nested sync def/lambda dispatched via "
                f"asyncio.to_thread, or the sync get_db dependency)."
            )

        print(
            f"{_SENTINEL_PASS} — 0 violations "
            f"({len(files)} route files scanned under app/api/routes/)"
        )

    def test_barrier_semantics_of_the_walker(self):
        """The walker flags only innermost-async frames (no test theater).

        Pins the safe-by-construction rules so the guard above cannot silently
        drift: direct call in async def -> violation; sync def or lambda
        between the call and the async def -> barrier (safe); nested async def
        -> the innermost async frame is reported.
        """
        cases = [
            # (source, expected [(lineno, receiver, func_name)])
            (
                "async def h():\n    pool = get_pool(p)\n"
                "    conn = pool.get_connection()\n",
                [(3, "pool", "h")],
            ),
            (
                "async def h():\n"
                "    conn = db_pool.get_connection()\n",
                [(2, "db_pool", "h")],
            ),
            (
                "async def h():\n"
                "    def _write():\n"
                "        return pool.get_connection()\n"
                "    return await asyncio.to_thread(_write)\n",
                [],
            ),
            (
                "async def h():\n"
                "    return await asyncio.to_thread("
                "lambda: pool.get_connection())\n",
                [],
            ),
            (
                "async def outer():\n"
                "    async def inner():\n"
                "        return pool.get_connection()\n"
                "    return await inner()\n",
                [(3, "pool", "inner")],
            ),
            (
                "def h():\n    return pool.get_connection()\n",
                [],
            ),
            (
                "pool.get_connection()\n",
                [],
            ),
        ]
        for source, expected in cases:
            got = _violations_in_source(source, "<case>")
            # Drop the label column for comparison.
            got_trimmed = [(v[1], v[2], v[3]) for v in got]
            self.assertEqual(
                got_trimmed,
                expected,
                f"unexpected walker result for source:\n{source}\ngot {got_trimmed}",
            )


if __name__ == "__main__":
    unittest.main()
