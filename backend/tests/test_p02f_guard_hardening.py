"""PR #1198 feedback F-03: harden the users.py single-connection guard.

The frozen C4 census counts ``get_connection_async()`` calls in users.py;
this companion closes the evasion shapes that census cannot see — a future
regression that re-introduces ``async with get_pool(...).connection_async()``
(or ANY standalone pool checkout) in a users handler fails here even though
the C4 call-count check stays silent. NOT a frozen check file.

Guard scope (AST, source-level): zero connection-pool imports/references and
zero connection-family attribute calls in ``app/api/routes/users.py``.
``ThreadPoolExecutor`` is excluded explicitly — it is a thread-pool, not the
SQLite connection pool (legitimately used by two users.py helpers).
"""
import ast
from pathlib import Path

USERS_PATH = Path(__file__).resolve().parents[1] / "app" / "api" / "routes" / "users.py"

# Attribute-call names that constitute a standalone pooled-connection
# checkout/release in a route module.
_CONNECTION_CALL_NAMES = {
    "connection",
    "connection_async",
    "get_connection",
    "get_connection_async",
    "release_connection",
}


def _pool_reference_offenders(tree: ast.AST) -> list[str]:
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if "pool" in alias.name.lower() and "threadpoolexecutor" not in alias.name.lower():
                    offenders.append(f"import {alias.name} @ line {node.lineno}")
        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").lower()
            if "pool" in module and "threadpoolexecutor" not in module:
                offenders.append(f"from {node.module} import ... @ line {node.lineno}")
        elif isinstance(node, ast.Name) and "pool" in node.id.lower():
            if "threadpoolexecutor" not in node.id.lower():
                offenders.append(f"Name '{node.id}' @ line {node.lineno}")
        elif isinstance(node, ast.Attribute) and "pool" in node.attr.lower():
            if "threadpoolexecutor" not in node.attr.lower():
                offenders.append(f"Attribute '.{node.attr}' @ line {node.lineno}")
    return offenders


def _connection_call_offenders(tree: ast.AST) -> list[str]:
    offenders: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _CONNECTION_CALL_NAMES
        ):
            offenders.append(f"{node.func.attr}() @ line {node.lineno}")
    return offenders


class TestUsersSingleConnectionGuardHardening:
    def test_no_pool_imports_or_references_in_users_routes(self):
        tree = ast.parse(USERS_PATH.read_text(encoding="utf-8"))
        offenders = _pool_reference_offenders(tree)
        assert offenders == [], (
            "users.py must hold ONE request-scoped connection (Depends(get_db)); "
            f"connection-pool references found: {offenders}"
        )

    def test_no_standalone_connection_calls_in_users_routes(self):
        tree = ast.parse(USERS_PATH.read_text(encoding="utf-8"))
        offenders = _connection_call_offenders(tree)
        assert offenders == [], (
            "users.py handlers must not call any connection-family method on a "
            f"standalone object: {offenders}"
        )
