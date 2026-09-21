"""AST guard for issue #645 (AC2): no on-loop pooled checkouts anywhere in
backend/app — routes included (a strict superset of the #592 guard, which is
scoped to ``backend/app/api/routes/`` and only matches ``.get_connection()``).

Both defect predicates of the issue's census
(``repro/census645_frozen.py``) are enforced:

* A-class: any ``<receiver>.get_connection()`` call whose INNERMOST enclosing
  callable is an ``async def`` (any receiver — the sync checkout blocks its
  caller for up to ``max_wait_attempts`` x ``CHECKOUT_WAIT_SECONDS`` and
  always runs ``_validate_connection`` on the calling thread, i.e. on the
  event loop).
* B-class: any ``<receiver>.connection()`` call whose receiver source
  contains ``pool`` and whose innermost enclosing callable is an ``async
  def`` (the sync context manager performs the same blocking checkout
  internally).

Barrier rule (the issue's own safe-by-construction definition, inherited
from the #592 guard): a call is a violation ONLY when the INNERMOST
enclosing callable is an ``async def``. An intervening sync ``def`` or
``lambda`` is a barrier — that callable is what gets handed to a worker
thread (the repo's ``asyncio.to_thread`` shape), so the checkout runs off
the loop.

The converted surfaces — ``await pool.get_connection_async()`` and
``async with pool.connection_async()`` — do NOT trip the matcher: attribute
equality is exact (``get_connection_async`` != ``get_connection`` and
``connection_async`` != ``connection``), never a prefix match.

Pure source inspection (mirrors the precedent in
backend/tests/test_issue592_no_on_loop_checkout.py): no app imports, no
asyncio, no database — the module under test is never loaded.
"""

import ast
import glob
import os
import unittest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_TESTS_DIR, os.pardir))
_APP_DIR = os.path.join(_BACKEND_ROOT, "app")
_ROUTES_REL_PREFIX = "api/routes/"

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


def _is_on_loop_checkout(func_node, receiver_text):
    """True when an Attribute call node is one of the two violation shapes.

    Attribute names are compared for EQUALITY, so the #645-converted async
    surfaces (`get_connection_async`, `connection_async`) never match.
    """
    if func_node.attr == "get_connection":
        return True
    return func_node.attr == "connection" and "pool" in receiver_text


def _violations_in_source(source, label):
    """Return [(label, lineno, kind, receiver, async_func_name)] for every
    A/B-class pooled-checkout call whose innermost enclosing callable is an
    `async def` (sync def / lambda are barriers; module level is safe).
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
            and stack
            and stack[-1][0] == "async"
            and _is_on_loop_checkout(node.func, _receiver_text(node))
        ):
            kind = (
                "get_connection"
                if node.func.attr == "get_connection"
                else "connection-cm"
            )
            violations.append(
                (label, node.lineno, kind, _receiver_text(node), stack[-1][1])
            )
        for child in ast.iter_child_nodes(node):
            walk(child)

    walk(tree)
    return violations


def find_app_violations(app_dir, *, include_routes=True):
    """AST-scan every backend/app/**/*.py file.

    Returns (sorted_file_list, violations) where each violation is
    (rel_path_from_backend_root, lineno, kind, receiver, async_func_name).
    """
    files = sorted(glob.glob(os.path.join(app_dir, "**", "*.py"), recursive=True))
    violations = []
    for path in files:
        with open(path, "r", encoding="utf-8") as fh:
            source = fh.read()
        rel = os.path.relpath(path, _BACKEND_ROOT).replace(os.sep, "/")
        if not include_routes and rel.startswith(_ROUTES_REL_PREFIX):
            continue
        violations.extend(_violations_in_source(source, rel))
    return files, violations


class TestIssue645NoOnLoopCheckout(unittest.TestCase):
    """AC2: zero on-loop pooled checkouts lexically inside async defs in
    backend/app (routes included)."""

    def test_repo_wide_no_on_loop_pooled_checkouts(self):
        files, violations = find_app_violations(_APP_DIR, include_routes=True)
        self.assertTrue(os.path.isdir(_APP_DIR), f"app dir not found: {_APP_DIR}")
        self.assertTrue(files, f"no source files found under {_APP_DIR}")

        if violations:
            for rel, lineno, kind, recv, func in violations:
                print(f"{rel}:{lineno}: {recv}.{kind} inside async def {func}")
            preview = ", ".join(f"{v[0]}:{v[1]}" for v in violations[:8])
            self.fail(
                f"{_SENTINEL_FAIL} — {len(violations)} violation(s) of AC2: "
                f"on-loop pooled checkout (<name>.get_connection() or "
                f"pool-receiver .connection()) lexically inside async def in "
                f"backend/app/ (first: {preview}). Move the checkout off the "
                f"event loop (await pool.get_connection_async() / async with "
                f"pool.connection_async(), or a nested sync def/lambda "
                f"dispatched via asyncio.to_thread)."
            )

        print(
            f"{_SENTINEL_PASS} — 0 violations "
            f"({len(files)} source files scanned under backend/app/)"
        )

    def test_census_matches_issue645_checkpoint(self):
        """Linkage to the frozen C1 census: outside routes/ the base tree
        recorded 43 on-loop checkouts (14 direct `.get_connection()` + 29
        pool-receiver `.connection()` CM sites — see
        .agents/issue-traces/645-on-loop-pooled-checkouts-off-event-loop/
        repro/C1.base.log, SUMMARY line). This test runs the SAME walker over
        backend/app EXCLUDING api/routes and asserts those counts are now
        zero, so the outside-routes population the issue's table names cannot
        regress independently of the (stricter) repo-wide guard above.
        """
        files, violations = find_app_violations(_APP_DIR, include_routes=False)
        self.assertTrue(files, f"no source files found under {_APP_DIR}")
        if violations:
            for rel, lineno, kind, recv, func in violations:
                print(f"{rel}:{lineno}: {recv}.{kind} inside async def {func}")
            preview = ", ".join(f"{v[0]}:{v[1]}" for v in violations[:8])
            self.fail(
                f"{_SENTINEL_FAIL} — the C1 outside-routes census population "
                f"(base: 14 direct + 29 CM = 43) regressed: "
                f"{len(violations)} violation(s) outside api/routes/ "
                f"(first: {preview})."
            )
        print(
            f"{_SENTINEL_PASS} — C1 outside-routes census is 0/0 "
            f"(base was direct-get_connection=14 pool-connection-cm=29)"
        )

    def test_connection_async_is_not_a_violation(self):
        """The #645-converted async surfaces must NOT trip the matcher.

        Attribute equality (never prefix): `get_connection_async` is not
        `get_connection`, and `connection_async` is not `connection` —
        including when the receiver is a pool.
        """
        cases = [
            # await pool.get_connection_async() inside async def: clean.
            (
                "async def h():\n"
                "    conn = await pool.get_connection_async()\n"
                "    return conn\n",
                [],
            ),
            # async with pool.connection_async() inside async def: clean.
            (
                "async def h():\n"
                "    async with db_pool.connection_async() as conn:\n"
                "        return conn.execute('SELECT 1')\n",
                [],
            ),
            # Chained receiver still only matches the exact sync attrs.
            (
                "async def h():\n"
                "    async with self.processor.pool.connection_async() as c:\n"
                "        return c\n",
                [],
            ),
        ]
        for source, expected in cases:
            got = _violations_in_source(source, "<case>")
            got_trimmed = [(v[1], v[2], v[3], v[4]) for v in got]
            self.assertEqual(
                got_trimmed,
                expected,
                f"async surface wrongly flagged for source:\n{source}\n"
                f"got {got_trimmed}",
            )

    def test_barrier_semantics(self):
        """The walker flags only innermost-async frames (no test theater).

        Direct sync calls inside async defs are violations; a sync def or a
        lambda between the call and the async def is a barrier (the repo's
        asyncio.to_thread dispatch shape); module level is safe.
        """
        cases = [
            # (source, expected [(lineno, kind, receiver, func_name)])
            # A-class direct: violation.
            (
                "async def h():\n"
                "    conn = pool.get_connection()\n",
                [(2, "get_connection", "pool", "h")],
            ),
            # B-class CM: violation (receiver mentions pool).
            (
                "async def h():\n"
                "    with db_pool.connection() as conn:\n"
                "        return conn\n",
                [(2, "connection-cm", "db_pool", "h")],
            ),
            # Nested sync def dispatched via to_thread: barrier (safe).
            (
                "async def h():\n"
                "    def _write():\n"
                "        return pool.get_connection()\n"
                "    return await asyncio.to_thread(_write)\n",
                [],
            ),
            # Lambda dispatched via to_thread: barrier (safe).
            (
                "async def h():\n"
                "    return await asyncio.to_thread("
                "lambda: pool.get_connection())\n",
                [],
            ),
            # Nested async def: the innermost async frame is reported.
            (
                "async def outer():\n"
                "    async def inner():\n"
                "        return pool.get_connection()\n"
                "    return await inner()\n",
                [(3, "get_connection", "pool", "inner")],
            ),
            # Sync def at module level: safe (off-loop population).
            (
                "def h():\n    return pool.get_connection()\n",
                [],
            ),
            # Module level: safe.
            (
                "pool.get_connection()\n",
                [],
            ),
            # Non-pool receiver .connection() is NOT a B-class hit.
            (
                "async def h():\n"
                "    with engine.connection() as c:\n"
                "        return c\n",
                [],
            ),
        ]
        for source, expected in cases:
            got = _violations_in_source(source, "<case>")
            got_trimmed = [(v[1], v[2], v[3], v[4]) for v in got]
            self.assertEqual(
                got_trimmed,
                expected,
                f"unexpected walker result for source:\n{source}\ngot {got_trimmed}",
            )

    def test_seeded_violation_is_caught(self):
        """Self-probe: the instrument itself detects the defect class.

        An in-memory source with `.get_connection()` (and a pool-receiver
        `.connection()`) inside an async def MUST be flagged — the guards
        above can never pass vacuously.
        """
        seeded = (
            "async def leaky_handler():\n"
            "    conn = db_pool.get_connection()\n"
            "    try:\n"
            "        return conn.execute('SELECT 1')\n"
            "    finally:\n"
            "        db_pool.release_connection(conn)\n"
            "\n"
            "async def leaky_cm_handler():\n"
            "    with self.processor.pool.connection() as conn:\n"
            "        return conn\n"
        )
        got = _violations_in_source(seeded, "<seeded>")
        got_trimmed = [(v[1], v[2], v[3], v[4]) for v in got]
        self.assertEqual(
            got_trimmed,
            [
                (2, "get_connection", "db_pool", "leaky_handler"),
                (9, "connection-cm", "self.processor.pool", "leaky_cm_handler"),
            ],
            f"seeded violation was not detected; got {got_trimmed}",
        )


if __name__ == "__main__":
    unittest.main()
