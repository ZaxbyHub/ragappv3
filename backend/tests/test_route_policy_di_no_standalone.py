"""Route authorization must use request-scoped DI policy evaluation.

The legacy ``evaluate_policy`` helper intentionally opens its own SQLite pool
connection for backward compatibility. Route modules that already depend on
``get_db`` or ``get_current_active_user`` must not import or call it directly.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROUTES_DIR = Path(__file__).resolve().parents[1] / "app" / "api" / "routes"


def test_route_modules_do_not_import_standalone_evaluate_policy():
    offenders: list[str] = []

    for path in sorted(ROUTES_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "app.api.deps":
                for alias in node.names:
                    if alias.name == "evaluate_policy":
                        offenders.append(f"{path.relative_to(ROUTES_DIR)}:{node.lineno}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "app.api.deps":
                        offenders.append(f"{path.relative_to(ROUTES_DIR)}:{node.lineno}")

    assert offenders == [], (
        "Route modules must use get_evaluate_policy(db) or Depends(get_evaluate_policy), "
        f"not standalone evaluate_policy imports: {offenders}"
    )


def test_route_modules_do_not_call_standalone_evaluate_policy():
    offenders: list[str] = []

    for path in sorted(ROUTES_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        standalone_names = {"evaluate_policy"}
        deps_aliases = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "app.api.deps":
                for alias in node.names:
                    if alias.name == "evaluate_policy":
                        standalone_names.add(alias.asname or alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "app.api.deps":
                        deps_aliases.add(alias.asname or alias.name.split(".")[-1])

        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in standalone_names:
                    offenders.append(f"{path.relative_to(ROUTES_DIR)}:{node.lineno}")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if (
                    node.func.attr == "evaluate_policy"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in deps_aliases
                ):
                    offenders.append(f"{path.relative_to(ROUTES_DIR)}:{node.lineno}")

    assert offenders == [], (
        "Route modules must not call standalone evaluate_policy because it opens "
        f"a second pooled DB connection: {offenders}"
    )
