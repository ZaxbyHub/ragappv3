"""Structural guardrail for issue #785's capability-gate defect class.

Every mutating Draft Room route must call ``_require_enabled()`` unless it is
on SPEC section 9.2's documented cleanup/disabled-mode allowlist: job cancel,
whole-draft delete, and revision export (owner read/export stays available
while disabled; capabilities and owner list/read are GETs). The audit finding
T1-08-K-04 shipped precisely because ``DELETE /drafts/{{draft_id}}/inputs/{{input_id}}``
was the one mutation missing the gate; this census fails if any mutating route
regresses to that shape.

AST-based on purpose (a line-regex census was tried first and mis-read this
file: multiline ``@router.post(`` decorators hid two routes entirely and one
route's span absorbed the next route's gate call). Parsing the module means
every decorated handler is seen exactly once and a gate call attributes to
the handler that contains it, so deleting a route's gate cannot pass silently
for any route the census covers.
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

# SPEC section 9.2 cleanup/disabled-mode allowlist, as (method, path-suffix)
# pairs. Export is ungated by design: owner export stays available while the
# feature is disabled (pinned by test_draft_security.py's disabled-mode test).
ALLOWLIST = {
    ("POST", "/jobs/{job_id}/cancel"),
    ("DELETE", "/drafts/{draft_id}"),
    ("POST", "/revisions/{revision_id}/export"),
}

MUTATING_METHODS = {"POST", "PATCH", "DELETE", "PUT"}


def _route_census(source: str):
    """Yield ``(method, path, gated)`` for every route handler in the module."""
    routes = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.FunctionDef) and not isinstance(
            node, ast.AsyncFunctionDef
        ):
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
            if not decorator.args or not isinstance(decorator.args[0], ast.Constant):
                continue
            routes.append(
                {
                    "method": func.attr.upper(),
                    "path": decorator.args[0].value,
                    "gated": any(
                        isinstance(item, ast.Name) and item.id == "_require_enabled"
                        or (
                            isinstance(item, ast.Call)
                            and isinstance(item.func, ast.Name)
                            and item.func.id == "_require_enabled"
                        )
                        for item in ast.walk(node)
                    ),
                }
            )
    return routes


def _is_allowlisted(method: str, path: str) -> bool:
    for allow_method, suffix in ALLOWLIST:
        if method == allow_method and (path == suffix or path.endswith(suffix)):
            return True
    return False


class TestDraftRoomCapabilityGateCensus(unittest.TestCase):
    def test_every_mutating_route_is_gated_or_allowlisted(self):
        routes = _route_census(ROUTE_FILE.read_text(encoding="utf-8"))
        mutating = [r for r in routes if r["method"] in MUTATING_METHODS]
        # The census must see the whole mutation surface; if this floor breaks,
        # the parser has gone blind again (see module docstring).
        self.assertGreaterEqual(len(mutating), 16)

        ungated = [
            (r["method"], r["path"])
            for r in mutating
            if not r["gated"] and not _is_allowlisted(r["method"], r["path"])
        ]

        self.assertEqual(
            ungated,
            [],
            "mutating Draft Room routes without _require_enabled() and not on "
            "SPEC 9.2's allowlist: "
            f"{ungated} (see issue #785, finding T1-08-K-04)",
        )

    def test_allowlist_entries_actually_exist(self):
        routes = _route_census(ROUTE_FILE.read_text(encoding="utf-8"))
        for method, suffix in sorted(ALLOWLIST):
            self.assertTrue(
                any(
                    r["method"] == method
                    and (r["path"] == suffix or r["path"].endswith(suffix))
                    for r in routes
                ),
                f"allowlisted {method} {suffix} no longer exists; prune the "
                "allowlist so it cannot mask a future regression",
            )


if __name__ == "__main__":
    unittest.main()
