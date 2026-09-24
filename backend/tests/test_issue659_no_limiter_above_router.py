"""AST guard for issue #659: no `@limiter.limit` stacked above a route decorator.

slowapi's `@limiter.limit(...)` wrapper is the only place a rate-limit check
can fire, and FastAPI's route decorator (`@router.post(...)` etc.) registers
and returns the UNWRAPPED function. Python applies decorators bottom-up, so
when `@limiter.limit` is stacked ABOVE the route decorator, the router stores
the raw endpoint and the limiter wrapper is orphaned at import time — the
limit silently never executes. That is exactly the defect issue #659 fixed on
`/auth/register`, `/auth/login`, `/auth/refresh` (previously stacked limiter-
above-router; now router-outermost like change-password and the repo's other
35 correct limiter sites).

Barrier rule (the issue's own definition): within one function's
`decorator_list`, a `limiter.limit(...)` decorator positioned ABOVE any
`router.<method>(...)` decorator is a violation — regardless of what sits
between them, because the route decorator still applies last and registers the
function below the limiter. Decorators are checked per callable (module-level
functions and methods alike).

Pure source inspection (mirrors the precedent in
backend/tests/test_issue592_no_on_loop_checkout.py): no app imports, no
slowapi, no database — the module under test is never loaded.
"""

import ast
import glob
import os
import unittest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_TESTS_DIR, os.pardir))
_APP_DIR = os.path.join(_BACKEND_ROOT, "app")

# Sentinel strings the trace manifest greps for.
_SENTINEL_PASS = "AST-GUARD: PASS"
_SENTINEL_FAIL = "AST-GUARD: FAIL"

_CALLABLE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)

_ROUTE_ATTRS = {
    "get",
    "post",
    "put",
    "patch",
    "delete",
    "head",
    "options",
    "trace",
    "api_route",
    "websocket",
}


def _decorator_kind(dec):
    """Classify a decorator Call: "limiter.limit", "router.<method>", or None."""
    if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
        return None
    receiver = dec.func.value
    if not isinstance(receiver, ast.Name):
        return None
    if receiver.id == "limiter" and dec.func.attr == "limit":
        return "limiter.limit"
    if receiver.id == "router" and dec.func.attr in _ROUTE_ATTRS:
        return "router.%s" % dec.func.attr
    return None


def _violations_in_source(source, label):
    """Return [(label, lineno, func_name)] for every callable whose
    decorator_list has a `limiter.limit(...)` decorator positioned above a
    `router.<method>(...)` decorator.
    """
    violations = []
    tree = ast.parse(source, filename=label)

    def walk(node):
        if isinstance(node, _CALLABLE_NODES):
            limiter_idx = None
            router_idx = None
            for idx, dec in enumerate(node.decorator_list):
                kind = _decorator_kind(dec)
                if kind == "limiter.limit" and limiter_idx is None:
                    limiter_idx = idx
                elif kind is not None and kind.startswith("router."):
                    router_idx = idx
            if limiter_idx is not None and router_idx is not None and limiter_idx < router_idx:
                violations.append((label, node.lineno, node.name))
        for child in ast.iter_child_nodes(node):
            walk(child)

    walk(tree)
    return violations


def find_app_violations(app_dir):
    """AST-scan every backend/app/**/*.py file (recursive).

    Returns (sorted_file_list, violations) where each violation is
    (rel_path_from_backend_root, lineno, func_name).
    """
    files = sorted(
        glob.glob(os.path.join(app_dir, "**", "*.py"), recursive=True)
    )
    violations = []
    for path in files:
        with open(path, "r", encoding="utf-8") as fh:
            source = fh.read()
        rel = os.path.relpath(path, _BACKEND_ROOT).replace(os.sep, "/")
        violations.extend(_violations_in_source(source, rel))
    return files, violations


class TestIssue659NoLimiterAboveRouter(unittest.TestCase):
    """Zero `@limiter.limit`-above-`@router.*` decorator stacks under app/."""

    def test_no_limiter_above_router_in_app(self):
        files, violations = find_app_violations(_APP_DIR)
        self.assertTrue(
            os.path.isdir(_APP_DIR), f"app dir not found: {_APP_DIR}"
        )
        self.assertTrue(files, f"no python files found under {_APP_DIR}")

        if violations:
            for rel, lineno, func in violations:
                print(
                    f"{rel}:{lineno}: @limiter.limit stacked above a route "
                    f"decorator on {func}() — the limit never executes"
                )
            preview = ", ".join(f"{v[0]}:{v[1]}" for v in violations[:8])
            self.fail(
                f"{_SENTINEL_FAIL} — {len(violations)} violation(s): "
                f"@limiter.limit must sit DIRECTLY BELOW the route decorator "
                f"(@router.<method> outermost) so the router stores the "
                f"limiter-wrapped function (first: {preview})."
            )

        print(
            f"{_SENTINEL_PASS} — 0 violations "
            f"({len(files)} files scanned under app/)"
        )

    def test_walker_semantics(self):
        """The walker flags limiter-above-router only (no test theater).

        Pins the rule so the guard above cannot silently drift: inverted
        adjacent order -> violation; inverted non-adjacent order (anything
        between the two decorators) -> violation; correct order -> no
        violation; single-kind decorator stacks -> no violation.
        """
        cases = [
            # (source, expected violation count)
            (
                # The #659 defect shape: limiter directly above the route.
                '@limiter.limit("5/hour")\n'
                '@router.post("/register")\n'
                "async def register(request):\n"
                "    pass\n",
                1,
            ),
            (
                # The correct (fixed) shape: route outermost, limiter below.
                '@router.post("/register")\n'
                '@limiter.limit("5/hour")\n'
                "async def register(request):\n"
                "    pass\n",
                0,
            ),
            (
                # Non-adjacent inverted: decorators between still leave the
                # router applying last, so the limiter stays orphaned.
                '@limiter.limit("10/minute")\n'
                "@some_decorator\n"
                '@router.post("/login")\n'
                "async def login(request):\n"
                "    pass\n",
                1,
            ),
            (
                # Non-adjacent correct: other decorators below the limiter
                # are still inside the route decorator.
                '@router.post("/login")\n'
                "@some_decorator\n"
                '@limiter.limit("10/minute")\n'
                "async def login(request):\n"
                "    pass\n",
                0,
            ),
            (
                # Route decorator only: no limiter to mis-stack.
                '@router.get("/me")\n'
                "async def me(request):\n"
                "    pass\n",
                0,
            ),
            (
                # Limiter decorator only (no route on the same callable).
                '@limiter.limit("1/second")\n'
                "def helper():\n"
                "    pass\n",
                0,
            ),
            (
                # Methods count too, not just module-level functions.
                "class C:\n"
                '    @limiter.limit("5/hour")\n'
                '    @router.post("/x")\n'
                "    async def h(self, request):\n"
                "        pass\n",
                1,
            ),
        ]
        for source, expected in cases:
            got = _violations_in_source(source, "<case>")
            self.assertEqual(
                len(got),
                expected,
                f"unexpected walker result for source:\n{source}\ngot {got}",
            )


if __name__ == "__main__":
    unittest.main()
