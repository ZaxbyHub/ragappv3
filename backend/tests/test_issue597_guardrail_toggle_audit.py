"""Guardrail for issue #597: admin toggle writes must carry their audit row.

Defect class: an admin toggle write path that mutates persistent state
(``admin_toggles`` / ``system_flags``) without an HMAC-signed
``audit_toggle_log`` row in the same transaction. The AST census below pins
the three structural facts that keep the class shut:

1. every production ``set_flag`` call either lives inside the maintenance
   service itself or passes the audit payload (a 2-argument call from a route
   is exactly the unaudited shape #597 shipped with);
2. ``audit_toggle_log`` INSERTs and ``system_flags`` UPDATEs only appear in
   the two sanctioned files (``maintenance.py``, ``admin.py``);
3. ``ToggleManager.set_toggle`` — the unaudited write API — has zero
   production references outside ``toggle_manager.py``.

Run via ``python -m pytest backend/tests/test_issue597_guardrail_toggle_audit.py``
from the repo root (no app imports: the test is a pure AST/file census and
must not depend on runtime settings).
"""

import ast
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1] / "app"

MAINTENANCE_SERVICE = APP_ROOT / "services" / "maintenance.py"
ADMIN_ROUTES = APP_ROOT / "api" / "routes" / "admin.py"
TOGGLE_MANAGER = APP_ROOT / "services" / "toggle_manager.py"

AUDIT_TABLE = "audit_toggle_log"
FLAGS_TABLE = "system_flags"


def _py_files() -> list[Path]:
    return sorted(APP_ROOT.rglob("*.py"))


def _attr_references(attr: str) -> list[tuple[Path, ast.Attribute, ast.Call | None]]:
    """Yield (file, reference, innermost-enclosing-call) for ``<obj>.<attr>``.

    A reference may be a direct call (``x.f(...)`` -> the reference is the
    Call's ``func``) or a first-class pass (``asyncio.to_thread(x.f, ...)`` ->
    the reference is an argument of the executor Call). Both shapes must be
    classified, not just Call nodes — the production set_flag call site uses
    the executor form.
    """
    hits: list[tuple[Path, ast.Attribute, ast.Call | None]] = []
    for path in _py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))

        def visit(node: ast.AST, call: ast.Call | None) -> None:
            if isinstance(node, ast.Attribute) and node.attr == attr:
                hits.append((path, node, call))
            child_call: ast.Call | None = call
            if isinstance(node, ast.Call):
                child_call = node
            for child in ast.iter_child_nodes(node):
                visit(child, child_call)

        visit(tree, None)
    return hits


def _sql_files(fragment: str) -> set[Path]:
    """Files under backend/app containing an SQL string literal with fragment."""
    hits: set[Path] = set()
    for path in _py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if fragment in node.value:
                    hits.add(path)
    return hits


def test_issue597_guardrail_every_set_flag_call_passes_audit() -> None:
    unaudited = []
    for path, ref, call in _attr_references("set_flag"):
        if path == MAINTENANCE_SERVICE:
            continue  # the service's own internal/retry context is sanctioned
        if call is None:
            unaudited.append(f"{path.relative_to(APP_ROOT)}:{ref.lineno} (no call)")
            continue
        # The audit payload is set_flag's 3rd parameter. When the reference is
        # the call's func, the audit arg is call.args[2]; when the reference is
        # passed first-class to an executor (asyncio.to_thread(x.set_flag, ...)),
        # the executor's args[0] is the callable itself, so the audit arg is
        # call.args[3]. Both shapes are covered by: arguments after the
        # callable must number at least 3 (enabled, reason, audit).
        args_after_callable = len(call.args) - (0 if call.func is ref else 1)
        if args_after_callable < 3:
            unaudited.append(f"{path.relative_to(APP_ROOT)}:{ref.lineno}")
    assert not unaudited, (
        "issue597 guardrail: unaudited set_flag call sites found "
        f"(must pass MaintenanceAudit as the 3rd argument): {unaudited}"
    )


def test_issue597_guardrail_state_and_audit_sql_only_in_sanctioned_files() -> None:
    audit_writers = _sql_files(f"INSERT INTO {AUDIT_TABLE}")
    assert audit_writers <= {MAINTENANCE_SERVICE, ADMIN_ROUTES}, (
        "issue597 guardrail: audit_toggle_log INSERT outside sanctioned files: "
        f"{sorted(str(p.relative_to(APP_ROOT)) for p in audit_writers)}"
    )
    flag_writers = _sql_files(f"UPDATE {FLAGS_TABLE}")
    assert flag_writers == {MAINTENANCE_SERVICE}, (
        "issue597 guardrail: system_flags UPDATE outside the maintenance service: "
        f"{sorted(str(p.relative_to(APP_ROOT)) for p in flag_writers)}"
    )


def test_issue597_guardrail_unaudited_set_toggle_api_unused_in_production() -> None:
    refs = [
        f"{path.relative_to(APP_ROOT)}:{ref.lineno}"
        for path, ref, _call in _attr_references("set_toggle")
        if ref.attr == "set_toggle" and path != TOGGLE_MANAGER
    ]
    assert not refs, (
        "issue597 guardrail: ToggleManager.set_toggle (unaudited write API) "
        f"referenced outside toggle_manager.py: {refs}"
    )
