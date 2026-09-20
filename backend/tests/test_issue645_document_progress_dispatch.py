"""AST dispatch guard for issue #645 (AC12 / plan round-2 item 4): the
``document_progress`` helpers are native ``async def`` (their pooled checkout
runs off the event loop via ``get_connection_async``), and EVERY reference to
them anywhere in ``backend/app`` must be a directly-awaited call inside an
async def.

The guarded defect classes (each pins a real miss shape):

* ``await asyncio.to_thread(set_phase, ...)`` — the pre-#645 comma-argument
  dispatch (the draft_promotion.py:766 class of miss): once the helper is
  async, to_thread would schedule the coroutine FUNCTION on a worker thread
  without ever awaiting it — a silent no-op write flagged here as a
  violation, never an allowed shape.
* a bare helper reference passed as an argument (executor.submit,
  partial(set_phase, ...), create_task(set_phase(...)) without the call,
  decorators): same silent-drop family.
* a helper call inside a sync def, or not directly awaited, or at module
  level: drops the write or drags the (formerly blocking / now suspending)
  checkout into the wrong context.

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
_HELPER_MODULE_REL = "services/document_progress.py"

_HELPER_NAMES = frozenset({"set_phase", "clear_progress", "set_wiki_pending"})

# Sentinel strings the trace manifest greps for.
_SENTINEL_PASS = "DISPATCH-GUARD: PASS"
_SENTINEL_FAIL = "DISPATCH-GUARD: FAIL"

_CALLABLE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)

# Module-level calls to these dispatch helpers receive a dedicated message.
_TO_THREAD_ATTRS = frozenset({"to_thread", "run_in_executor"})


def _frame_kind_and_name(node):
    if isinstance(node, ast.AsyncFunctionDef):
        return "async", node.name
    if isinstance(node, ast.Lambda):
        return "lambda", "<lambda>"
    return "sync", node.name


def _is_to_thread_call(node):
    """True for `asyncio.to_thread(...)`-style dispatch calls."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return False
    return node.func.attr in _TO_THREAD_ATTRS


def _violations_in_source(source, label):
    """Return (violations, legal_awaited_calls, sync_def_calls).

    A violation is (label, lineno, kind, detail) for every reference to a
    helper name that is NOT a directly-awaited call inside an async def:
      - "to_thread-dispatch": bare helper name passed to asyncio.to_thread /
        run_in_executor (forbidden even awaited — the helper is async).
      - "bare-reference": helper name used as a value (argument, decorator,
        assignment) anywhere other than the called-function slot.
      - "unawaited-call": helper called but not directly awaited (e.g. handed
        to create_task as a call result).
      - "sync-def-call": helper called inside a sync def.
      - "module-level-call": helper called outside any callable.
    """
    violations = []
    legal_awaited_calls = 0
    sync_def_calls = 0
    tree = ast.parse(source, filename=label)
    stack = []  # innermost-last (kind, name)

    def walk(node, from_await, in_to_thread):
        nonlocal legal_awaited_calls, sync_def_calls
        if isinstance(node, _CALLABLE_NODES):
            kind, name = _frame_kind_and_name(node)
            stack.append((kind, name))
            for child in ast.iter_child_nodes(node):
                walk(child, False, False)
            stack.pop()
            return

        if isinstance(node, ast.Await):
            for child in ast.iter_child_nodes(node):
                walk(child, True, False)
            return

        if _is_to_thread_call(node):
            for child in node.args + node.keywords:
                walk(child, from_await, True)
            # The func attribute chain is not a helper reference; skip it.
            return

        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in _HELPER_NAMES
        ):
            helper = node.func.id
            where = f"in {stack[-1][0]} def {stack[-1][1]}" if stack else "at module level"
            if not stack:
                violations.append(
                    (label, node.lineno, "module-level-call",
                     f"{helper}(...) called at module level {where}")
                )
            elif stack[-1][0] == "sync":
                sync_def_calls += 1
                violations.append(
                    (label, node.lineno, "sync-def-call",
                     f"{helper}(...) called inside sync def {stack[-1][1]} "
                     f"(the async helper's coroutine would be dropped)")
                )
            elif stack[-1][0] == "lambda":
                violations.append(
                    (label, node.lineno, "sync-def-call",
                     f"{helper}(...) called inside a lambda {where}")
                )
            elif not from_await:
                violations.append(
                    (label, node.lineno, "unawaited-call",
                     f"{helper}(...) is not directly awaited {where}")
                )
            else:
                legal_awaited_calls += 1
            # Recurse into the call's own args/keywords for nested refs,
            # skipping the consumed func Name.
            for child in node.args + node.keywords:
                walk(child, False, in_to_thread)
            return

        if isinstance(node, ast.Name) and node.id in _HELPER_NAMES:
            if in_to_thread:
                violations.append(
                    (label, node.lineno, "to_thread-dispatch",
                     f"bare {node.id} passed to asyncio.to_thread/run_in_executor "
                     f"(the helper is async def — the coroutine is never awaited)")
                )
            else:
                violations.append(
                    (label, node.lineno, "bare-reference",
                     f"bare reference to {node.id} used as a value — must be "
                     f"`await {node.id}(...)` inside an async def")
                )
            return

        for child in ast.iter_child_nodes(node):
            walk(child, False, in_to_thread)

    walk(tree, False, False)
    return violations, legal_awaited_calls, sync_def_calls


def find_app_dispatch_violations(app_dir):
    """AST-scan every backend/app/**/*.py file for helper dispatch
    violations. Returns (sorted_file_list, violations, total_legal_calls)."""
    files = sorted(glob.glob(os.path.join(app_dir, "**", "*.py"), recursive=True))
    all_violations = []
    legal = 0
    for path in files:
        with open(path, "r", encoding="utf-8") as fh:
            source = fh.read()
        rel = os.path.relpath(path, _BACKEND_ROOT).replace(os.sep, "/")
        violations, legal_calls, _sync = _violations_in_source(source, rel)
        all_violations.extend(violations)
        legal += legal_calls
    return files, all_violations, legal


class TestIssue645DocumentProgressDispatch(unittest.TestCase):
    """AC12: async helpers, awaited everywhere, dispatched nowhere else."""

    maxDiff = None

    # -- Requirement 1: the helpers themselves are async with a bounded
    #    checkout + try/finally release. --------------------------------

    def _helper_module_tree(self):
        path = os.path.join(_APP_DIR, *_HELPER_MODULE_REL.split("/"))
        self.assertTrue(os.path.isfile(path), f"helper module missing: {path}")
        with open(path, "r", encoding="utf-8") as fh:
            return path, ast.parse(fh.read(), filename=path)

    def test_helpers_are_async_def_with_awaited_checkout_in_try_finally(self):
        path, tree = self._helper_module_tree()
        functions = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in _HELPER_NAMES
        }
        self.assertEqual(
            set(functions), set(_HELPER_NAMES),
            f"expected exactly the three helpers in {path}",
        )
        for name, node in functions.items():
            self.assertIsInstance(
                node, ast.AsyncFunctionDef,
                f"{name} must be `async def` (its pooled checkout runs off "
                f"the event loop via get_connection_async, issue #645)",
            )
            # An awaited get_connection_async checkout lexically inside a
            # try block (the outer best-effort try), plus a finally block
            # releasing the connection (the inner try/finally).
            try_nodes = [t for t in ast.walk(node) if isinstance(t, ast.Try)]
            awaited_checkouts = [
                inner
                for inner in ast.walk(node)
                if isinstance(inner, ast.Await)
                and isinstance(inner.value, ast.Call)
                and isinstance(inner.value.func, ast.Attribute)
                and inner.value.func.attr == "get_connection_async"
            ]
            releases_in_finally = [
                call
                for t in try_nodes
                for stmt in t.finalbody
                for call in ast.walk(stmt)
                if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "release_connection"
            ]
            checkout_inside_try = any(
                isinstance(sub, ast.Await)
                and isinstance(sub.value, ast.Call)
                and isinstance(sub.value.func, ast.Attribute)
                and sub.value.func.attr == "get_connection_async"
                for t in try_nodes
                for region in (t.body, t.orelse, t.handlers, t.finalbody)
                for stmt in region
                for sub in ast.walk(stmt)
            )
            self.assertTrue(
                awaited_checkouts,
                f"{name} must await pool.get_connection_async()",
            )
            self.assertTrue(
                checkout_inside_try,
                f"{name}'s awaited checkout must sit inside a try block",
            )
            self.assertTrue(
                releases_in_finally,
                f"{name} must release the connection in a try's finally",
            )

    # -- Requirement 2 + 3: repo-wide dispatch audit. ----------------------

    def test_repo_wide_helper_dispatch_is_always_directly_awaited(self):
        files, violations, legal = find_app_dispatch_violations(_APP_DIR)
        self.assertTrue(os.path.isdir(_APP_DIR), f"app dir not found: {_APP_DIR}")
        self.assertTrue(files, f"no source files found under {_APP_DIR}")

        if violations:
            for rel, lineno, kind, detail in violations:
                print(f"{rel}:{lineno}: [{kind}] {detail}")
            preview = ", ".join(f"{v[0]}:{v[1]}" for v in violations[:8])
            self.fail(
                f"{_SENTINEL_FAIL} — {len(violations)} helper-dispatch "
                f"violation(s) in backend/app/ (first: {preview}). Every "
                f"set_phase/clear_progress/set_wiki_pending reference must "
                f"be `await helper(...)` inside an async def; "
                f"asyncio.to_thread dispatch and bare references are "
                f"forbidden since the helpers are async def."
            )
        # The audit must be live, not vacuous: the converted tree really has
        # awaited helper calls to find.
        self.assertGreater(
            legal, 0,
            "no awaited helper calls found — the dispatch audit matched "
            "nothing and cannot guard anything",
        )
        print(
            f"{_SENTINEL_PASS} — 0 violations, {legal} directly-awaited "
            f"helper calls across {len(files)} source files"
        )

    def test_zero_helper_calls_inside_sync_defs(self):
        files, violations, legal = find_app_dispatch_violations(_APP_DIR)
        sync_defs = [v for v in violations if v[2] == "sync-def-call"]
        self.assertEqual(
            sync_defs, [],
            f"helper call(s) inside sync defs: {sync_defs}",
        )

    # -- Instrument self-probes (no test theater). -------------------------

    def test_seeded_dispatch_shapes_are_classified(self):
        """The instrument detects every forbidden shape and accepts the one
        legal shape (mirrors the #592/#645 guards' self-probe pattern)."""
        seeded = (
            "import asyncio\n"
            "from app.services.document_progress import set_phase\n"
            "\n"
            "async def ok_caller(pool, fid):\n"
            "    await set_phase(pool, fid, phase='queued')\n"      # legal
            "\n"
            "async def to_thread_caller(pool, fid):\n"
            "    await asyncio.to_thread(set_phase, pool, fid)\n"   # forbidden
            "\n"
            "async def unawaited_caller(pool, fid):\n"
            "    set_phase(pool, fid, phase='queued')\n"            # forbidden
            "\n"
            "async def bare_ref_caller(pool, fid):\n"
            "    task = asyncio.create_task(set_phase)\n"           # forbidden (bare)
            "\n"
            "def sync_caller(pool, fid):\n"
            "    set_phase(pool, fid, phase='queued')\n"            # forbidden
        )
        violations, legal, sync = _violations_in_source(seeded, "<seeded>")
        kinds = sorted(v[2] for v in violations)
        self.assertEqual(
            kinds,
            ["bare-reference", "sync-def-call", "to_thread-dispatch", "unawaited-call"],
            f"unexpected classification: {violations}",
        )
        self.assertEqual(legal, 1, "exactly the awaited call is legal")
        self.assertEqual(sync, 1)

    def test_barrier_semantics_of_the_walker(self):
        """Imports and definitions produce no Name hits; nested async frames
        keep the awaited call legal; a lambda wrapper is a sync barrier."""
        cases = [
            # Import alone: nothing to flag.
            (
                "from app.services.document_progress import "
                "set_phase, clear_progress, set_wiki_pending\n",
                0,
            ),
            # Awaited call inside a nested async def: legal (innermost frame).
            (
                "async def outer():\n"
                "    async def inner(pool, fid):\n"
                "        await set_phase(pool, fid, phase='queued')\n"
                "    return await inner(None, 1)\n",
                0,
            ),
            # Lambda between: sync barrier — a helper call inside it is a
            # sync-def violation (the coroutine would be dropped).
            (
                "async def h(pool, fid):\n"
                "    f = lambda: set_phase(pool, fid)\n"  # noqa: E731
                "    return f\n",
                1,
            ),
        ]
        for source, expected_violations in cases:
            violations, _legal, _sync = _violations_in_source(source, "<case>")
            self.assertEqual(
                len(violations), expected_violations,
                f"unexpected walker result for source:\n{source}\n{violations}",
            )


if __name__ == "__main__":
    unittest.main()
