"""Guardrail (issue #511 FULL-ENH-04 defect class): no sync Redis client calls
directly inside async functions under ``app/services``.

The defect class: the synchronous ``redis`` client's ``get``/``setex`` called
directly on the event loop inside an async request path blocks the ENTIRE loop
for the socket round-trip (one slow Redis stalls every concurrent request).
The sanctioned pattern is ``await redis_call(client.get, ...)`` (worker thread
+ bounded timeout, degrading to cache-miss).

This test AST-walks every ``app/services/*.py`` module and fails when an
``async def`` contains a direct attribute call chain
``<expr>.get(...) / .setex(...) / .set(...) / .delete(...)`` whose receiver is
a redis client attribute (``self._redis_client`` / ``self._redis``) or the
``redis_call`` helper is bypassed. Sync methods are allowed (auth-critical
Redis, e.g. CSRF, deliberately keeps its fail-closed sync path).

Demonstrated load-bearing (issue #511 Phase 4.2 mutation probe): unwrapping
one ``redis_call`` in ``query_transformer.py`` back to a direct
``self._redis_client.get(...)`` makes this test FAIL; restoring the wrapper
makes it PASS.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SERVICES_DIR = Path(__file__).resolve().parents[1] / "app" / "services"

_SYNC_CLIENT_ATTRS = {"_redis_client", "_redis"}
_BANNED_METHODS = {"get", "set", "setex", "delete", "expire", "hget", "hset"}


def _direct_sync_redis_calls(tree: ast.AST) -> list[str]:
    """Return 'file:line <desc>' for direct sync redis calls inside async defs."""
    hits: list[str] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self._async_stack: list[bool] = []

        def _visit_function(self, node: ast.AST, is_async: bool) -> None:
            self._async_stack.append(is_async)
            self.generic_visit(node)
            self._async_stack.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_function(node, True)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_function(node, False)

        def visit_Call(self, node: ast.Call) -> None:
            if self._async_stack and self._async_stack[-1]:
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr in _BANNED_METHODS
                    and isinstance(func.value, ast.Attribute)
                    and isinstance(func.value.value, ast.Name)
                    and func.value.value.id == "self"
                    and func.value.attr in _SYNC_CLIENT_ATTRS
                ):
                    hits.append(
                        f"line {node.lineno}: self.{func.value.attr}.{func.attr}(...) "
                        "called directly in async def — wrap with await redis_call(...)"
                    )
            self.generic_visit(node)

    Visitor().visit(tree)
    return hits


@pytest.mark.parametrize(
    "service_file", sorted(p for p in SERVICES_DIR.glob("*.py") if p.name != "__init__.py")
)
def test_no_sync_redis_calls_on_event_loop(service_file: Path) -> None:
    tree = ast.parse(service_file.read_text(encoding="utf-8"))
    hits = _direct_sync_redis_calls(tree)
    assert not hits, (
        f"{service_file.name}: sync Redis client calls on the event loop "
        f"(issue #511 FULL-ENH-04 class): {hits}"
    )
