"""Structural guardrail for issue #785's capability-gate defect class.

Every mutating Draft Room route must call ``_require_enabled()`` as its first
executable statement unless it is on SPEC section 9.2's documented
cleanup/disabled-mode allowlist: job cancel, whole-draft delete, and revision
export (owner export stays available while disabled). The audit finding
T1-08-K-04 shipped precisely because ``DELETE /drafts/{{draft_id}}/inputs/{{input_id}}``
was the one mutation missing the gate; this census fails if any mutating route
regresses to that shape.

AST-based on purpose. A first version used a line regex and mis-read this
file: multiline ``@router.post(`` decorators hid two routes entirely and one
route's span absorbed the next route's gate call. This version parses the
module, so every decorated handler is seen exactly once.

Three properties the earlier revisions got wrong and this one pins:
- the gate must be a top-level *call* in the handler body, not any mention of
  the name (a dead reference after ``raise`` used to score "gated");
- a route whose path is not a literal is reported as unparseable and fails the
  census rather than being skipped;
- the allowlist is pinned to exact ``(method, path)`` pairs, so a future route
  that merely ends with an allowlisted suffix cannot be silently exempted.
"""

import ast
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

ROUTE_FILE = (
    Path(__file__).resolve().parents[2] / "app" / "api" / "routes" / "draft_room.py"
)

# SPEC section 9.2 cleanup/disabled-mode allowlist: exact (method, path) pairs.
ALLOWLIST = {
    ("POST", "/drafts/{draft_id}/jobs/{job_id}/cancel"),
    ("DELETE", "/drafts/{draft_id}"),
    ("POST", "/drafts/{draft_id}/revisions/{revision_id}/export"),
}

MUTATING_METHODS = {"POST", "PATCH", "DELETE", "PUT"}

# Floor on the mutating surface the census must see. Guards against the parser
# going blind; a legitimate route change updates it in the same commit.
MIN_MUTATING_ROUTES = 16


def _is_gate_call(statement) -> bool:
    """True for a bare ``_require_enabled()`` expression statement."""
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and isinstance(statement.value.func, ast.Name)
        and statement.value.func.id == "_require_enabled"
    )


def _is_nested_scope(node) -> bool:
    """True for a statement nested in a def/lambda (not a top-level handler statement)."""
    return isinstance(
        node,
        (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef),
    )


def _route_census(source: str):
    """Yield one record per decorated handler.

    Each record is ``(method, path, gated, gate_is_first, path_is_literal)``.
    ``gated`` requires a top-level ``_require_enabled()`` call anywhere in the
    handler body; ``gate_is_first`` requires it to be the first executable
    statement, which is what makes the gate a real precondition rather than
    something a later statement may skip past.
    """
    routes = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            func = decorator.func
            if not (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "router"
                and func.attr in ("get", "post", "patch", "delete", "put")
            ):
                continue
            path_literal = bool(
                decorator.args and isinstance(decorator.args[0], ast.Constant)
            )
            path = decorator.args[0].value if path_literal else None
            top_level = [
                stmt
                for stmt in node.body
                if not isinstance(stmt, (ast.Expr,)) or not isinstance(
                    getattr(stmt, "value", None), ast.Constant
                )
            ]
            gated = any(
                _is_gate_call(stmt) for stmt in top_level if not _is_nested_scope(stmt)
            )
            gate_is_first = bool(top_level) and _is_gate_call(top_level[0])
            routes.append(
                (func.attr.upper(), path, gated, gate_is_first, path_literal)
            )
    return routes


class TestDraftRoomCapabilityGateCensus(unittest.TestCase):
    def _census(self):
        return _route_census(ROUTE_FILE.read_text(encoding="utf-8"))

    def test_every_mutating_route_is_gated_or_allowlisted(self):
        routes = self._census()
        mutating = [r for r in routes if r[0] in MUTATING_METHODS]

        # Fail closed rather than silently skipping what the parser cannot read.
        self.assertEqual(
            [r for r in mutating if not r[4]],
            [],
            "mutating routes whose path is not a literal are invisible to this "
            "census; give the route a literal path or extend the census",
        )
        self.assertGreaterEqual(
            len(mutating),
            MIN_MUTATING_ROUTES,
            "the census lost mutating routes; the parser went blind",
        )

        ungated = [
            (r[0], r[1]) for r in mutating if not r[2] and (r[0], r[1]) not in ALLOWLIST
        ]
        self.assertEqual(
            ungated,
            [],
            "mutating Draft Room routes without _require_enabled() and not on "
            "SPEC 9.2's allowlist: "
            f"{ungated} (see issue #785, finding T1-08-K-04)",
        )

    def test_gate_is_the_first_executable_statement(self):
        # A gate that runs after the delete has already happened is not a gate.
        routes = self._census()
        offenders = [
            (r[0], r[1])
            for r in routes
            if r[0] in MUTATING_METHODS
            and r[2]
            and not r[3]
            and (r[0], r[1]) not in ALLOWLIST
        ]
        self.assertEqual(
            offenders,
            [],
            "mutating routes whose _require_enabled() is not the first statement: "
            f"{offenders}",
        )

    def test_allowlist_is_exact_pairs_not_suffixes(self):
        # Guards the matcher itself: a suffix-based allowlist would exempt any
        # future route ending in an allowlisted path.
        routes = self._census()
        by_key = {(r[0], r[1]): r for r in routes}
        for method, path in sorted(ALLOWLIST):
            self.assertIn(
                (method, path),
                by_key,
                f"allowlisted {method} {path} no longer exists; prune the "
                "allowlist so it cannot mask a future regression",
            )
            self.assertFalse(
                by_key[(method, path)][2],
                f"allowlisted {method} {path} is now gated; remove it from the "
                "allowlist so the census keeps enforcing it",
            )


if __name__ == "__main__":
    unittest.main()
