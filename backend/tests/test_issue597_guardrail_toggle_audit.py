"""Guardrail for issue #597: admin toggle writes must carry their audit row.

Defect class: an admin toggle write path that mutates persistent state
(``admin_toggles`` / ``system_flags``) without an HMAC-signed
``audit_toggle_log`` row in the same transaction. The AST census below pins
the call SHAPE that keeps the class shut — a structural pin, not a
behavioral proof (the behavioral contract lives in
``test_issue597_maintenance_audit.py``):

1. every production reference to ``set_flag`` outside the audited callable's
   own definition passes the audit payload — positional form (3 arguments
   after the callable), keyword form (``audit=...``), or the executor form
   (``asyncio.to_thread(service.set_flag, enabled, reason, audit)``);
2. ``audit_toggle_log`` INSERTs appear only in ``maintenance.py`` and
   ``admin.py``; ``system_flags`` UPDATEs only in ``maintenance.py``;
3. ``ToggleManager.set_toggle`` — the unaudited write API — has zero
   production references outside ``toggle_manager.py``, and neither audited
   callable is resolved via literal-form ``getattr`` (builtin, qualified,
   import-aliased, or assignment-aliased, with a literal or module-constant
   target name). Fully computed target names are out of scope for a
   structural pin and are covered by review plus the behavioral tests.

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
AUDITED_TARGETS = ("set_flag", "set_toggle")
AUDIT_KWARG = "audit"


def _parse_trees() -> list[tuple[Path, ast.Module]]:
    return [
        (path, ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted((APP_ROOT).rglob("*.py"))
    ]


def _enclosing_call_map(tree: ast.Module) -> dict[int, ast.Call | None]:
    """Map each node id to its innermost enclosing Call (None if none).

    A reference may be a direct call (``x.f(...)`` -> the reference is the
    Call's ``func``) or a first-class pass (``asyncio.to_thread(x.f, ...)`` ->
    the reference is an argument of the executor Call). Both shapes must be
    classified, not just Call nodes — the production set_flag call site uses
    the executor form.
    """
    enclosing: dict[int, ast.Call | None] = {}

    def visit(node: ast.AST, call: ast.Call | None) -> None:
        for child in ast.iter_child_nodes(node):
            child_call: ast.Call | None = node if isinstance(node, ast.Call) else call
            enclosing[id(child)] = child_call
            visit(child, child_call)

    visit(tree, None)
    return enclosing


def _attr_references(trees: list[tuple[Path, ast.Module]], attr: str):
    """Yield (file, reference, innermost-enclosing-call) for ``<obj>.<attr>``."""
    hits = []
    for path, tree in trees:
        enclosing = _enclosing_call_map(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == attr:
                hits.append((path, node, enclosing.get(id(node))))
    return hits


def _audited_def_roots(tree: ast.Module) -> set[int]:
    """Node ids of the audited service's own ``set_flag`` definition.

    Precisely ``MaintenanceService.set_flag``: a FunctionDef named
    ``set_flag`` whose immediate parent is the ClassDef
    ``MaintenanceService`` (or a module-level ``def set_flag`` if the
    service is ever de-classed). A same-named helper on any OTHER class is
    NOT a root — its internal unaudited calls must trip the census.
    """
    roots: set[int] = set()
    service_classes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "MaintenanceService"
    ]
    for cls in service_classes:
        for child in cls.body:
            if (
                isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child.name == "set_flag"
            ):
                roots.add(id(child))
    for node in tree.body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "set_flag"
        ):
            roots.add(id(node))
    return roots


def _parents(tree: ast.Module) -> dict[int, int]:
    parents: dict[int, int] = {}

    def link(parent: ast.AST) -> None:
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = id(parent)
            link(child)

    link(tree)
    return parents


def _inside(node_id: int, parents: dict[int, int], roots: set[int]) -> bool:
    current = node_id
    while current in parents:
        current = parents[current]
        if current in roots:
            return True
    return False


def _is_audited_call(call: ast.Call, ref: ast.Attribute) -> bool:
    """Does this call deliver the audit payload to the audited callable?

    Positional form: >=3 arguments after the callable (enabled, reason,
    audit). Keyword form: an ``audit=...`` keyword. Executor form: the
    callable is args[0] of to_thread/run_in_executor, so the audit payload
    is the 3rd of the remaining arguments.
    """
    if any(kw.arg == AUDIT_KWARG for kw in call.keywords):
        return True
    args_after_callable = len(call.args) - (0 if call.func is ref else 1)
    return args_after_callable >= 3


def test_issue597_guardrail_every_set_flag_call_passes_audit() -> None:
    trees = _parse_trees()
    unaudited = []
    for path, ref, call in _attr_references(trees, "set_flag"):
        # Exempt only MaintenanceService.set_flag's own definition subtree in
        # the service — not the whole file, and not same-named helpers on
        # other classes — so an unaudited wrapper added in maintenance.py
        # still trips the census.
        if path == MAINTENANCE_SERVICE and call is not None:
            service_tree = dict(trees)[path]
            roots = _audited_def_roots(service_tree)
            if _inside(id(ref), _parents(service_tree), roots):
                continue
        if call is None:
            unaudited.append(f"{path.relative_to(APP_ROOT)}:{ref.lineno} (no call)")
            continue
        if not _is_audited_call(call, ref):
            unaudited.append(f"{path.relative_to(APP_ROOT)}:{ref.lineno}")
    assert not unaudited, (
        "issue597 guardrail: unaudited set_flag call sites found "
        f"(must pass MaintenanceAudit positionally or as audit=): {unaudited}"
    )
    # Self-test (F-001 regression pin): a same-named ``set_flag`` helper on a
    # different class inside maintenance.py must NOT be exempt — an unaudited
    # 2-argument call inside it trips the census, while references inside the
    # real MaintenanceService.set_flag definition stay exempt.
    service_src = (
        "class MaintenanceService:\n"
        "    def set_flag(self, enabled, reason, audit=None):\n"
        "        reason = reason or self._default_reason\n"
        "class UnrelatedHelper:\n"
        "    def set_flag(self, enabled, reason):\n"
        "        MaintenanceService().set_flag(enabled, reason)\n"
    )
    helper_tree = ast.parse(service_src)
    helper_refs = _attr_references([(MAINTENANCE_SERVICE, helper_tree)], "set_flag")
    roots = _audited_def_roots(helper_tree)
    parents = _parents(helper_tree)
    helper_unaudited = [
        ref.lineno
        for _p, ref, call in helper_refs
        if call is not None
        and not _is_audited_call(call, ref)
        and not _inside(id(ref), parents, roots)
    ]
    assert helper_unaudited == [6], helper_unaudited


def _sql_files(trees: list[tuple[Path, ast.Module]], fragment: str) -> set[Path]:
    """Files containing an SQL string literal with fragment."""
    hits: set[Path] = set()
    for path, tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if fragment in node.value:
                    hits.add(path)
    return hits


def test_issue597_guardrail_state_and_audit_sql_only_in_sanctioned_files() -> None:
    trees = _parse_trees()
    audit_writers = _sql_files(trees, f"INSERT INTO {AUDIT_TABLE}")
    assert audit_writers <= {MAINTENANCE_SERVICE, ADMIN_ROUTES}, (
        "issue597 guardrail: audit_toggle_log INSERT outside sanctioned files: "
        f"{sorted(str(p.relative_to(APP_ROOT)) for p in audit_writers)}"
    )
    flag_writers = _sql_files(trees, f"UPDATE {FLAGS_TABLE}")
    assert flag_writers == {MAINTENANCE_SERVICE}, (
        "issue597 guardrail: system_flags UPDATE outside the maintenance service: "
        f"{sorted(str(p.relative_to(APP_ROOT)) for p in flag_writers)}"
    )


def _getattr_resolutions(tree: ast.Module) -> list[int]:
    """Line numbers of getattr-style resolution of audited target names.

    Matched forms: bare ``getattr(x, "set_flag")``; qualified
    ``builtins.getattr(...)`` (the receiver must literally be the builtins
    module — an unrelated object's ``.getattr()`` method does not match);
    an import alias (``from builtins import getattr as resolve``); an
    assignment alias (``resolve = getattr`` or ``resolve = <existing
    alias>``). The target argument may be a literal from AUDITED_TARGETS or
    a module-level Name bound to one. The module-level binding table is
    order-sensitive: a later rebind of a tracked alias or constant name to
    an unrecognized value invalidates the earlier binding. Fully computed
    target names (concatenation, f-strings) are out of scope for a
    structural pin — the docstring says so explicitly; they are covered by
    review plus the behavioral tests.
    """
    getattr_aliases = {"getattr"}
    target_names = set(AUDITED_TARGETS)

    def is_builtins_getattr(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "getattr"
            and isinstance(node.value, ast.Name)
            and node.value.id == "builtins"
        )

    # Order-sensitive pass over module-level bindings: later rebinds to
    # unrecognized values invalidate earlier tracked bindings.
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "builtins":
            for alias in node.names:
                if alias.name == "getattr":
                    getattr_aliases.add(alias.asname or alias.name)
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            name = node.targets[0].id
            value = node.value
            if isinstance(value, ast.Name) and value.id in getattr_aliases:
                getattr_aliases.add(name)
            elif isinstance(value, ast.Constant) and value.value in AUDITED_TARGETS:
                target_names.add(name)
            else:
                getattr_aliases.discard(name)
                target_names.discard(name)
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        func = node.func
        is_getattr = (
            isinstance(func, ast.Name) and func.id in getattr_aliases
        ) or is_builtins_getattr(func)
        target = node.args[1]
        target_ok = (
            isinstance(target, ast.Constant) and target.value in AUDITED_TARGETS
        ) or (isinstance(target, ast.Name) and target.id in target_names)
        if is_getattr and target_ok:
            hits.append(node.lineno)
    return hits


def test_issue597_guardrail_unaudited_set_toggle_api_unused_in_production() -> None:
    trees = _parse_trees()
    refs = [
        f"{path.relative_to(APP_ROOT)}:{ref.lineno}"
        for path, ref, _call in _attr_references(trees, "set_toggle")
        if path != TOGGLE_MANAGER
    ]
    assert not refs, (
        "issue597 guardrail: ToggleManager.set_toggle (unaudited write API) "
        f"referenced outside toggle_manager.py: {refs}"
    )
    dynamic = [
        f"{path.relative_to(APP_ROOT)}:{lineno}"
        for path, tree in trees
        for lineno in _getattr_resolutions(tree)
    ]
    assert not dynamic, (
        "issue597 guardrail: dynamic getattr() resolution of the audited "
        f"toggle callables (evades the shape census): {dynamic}"
    )
    # Self-tests (F-001 regression pins): the detector must catch the
    # builtin form, the qualified builtins.getattr form, an import alias,
    # an assignment alias, and a module-constant target name — and NOT flag
    # an unrelated alias or computed target.
    snippet = ast.parse(
        "import builtins\n"
        "from builtins import getattr as resolve\n"
        "s = None\n"
        "resolve_alias = getattr\n"
        "TARGET = 'set_flag'\n"
        "def f(e, r):\n"
        "    getattr(s, 'set_flag')(e, r)\n"
        "    builtins.getattr(s, 'set_toggle')(e, r)\n"
        "    resolve(s, 'set_flag')(e, r)\n"
        "    resolve_alias(s, 'set_toggle')(e, r)\n"
        "    resolve(s, TARGET)(e, r)\n"
        "    resolve(s, 'unrelated')(e, r)\n"
        "    resolve(s, 'set_' + 'flag')(e, r)\n"
    )
    assert _getattr_resolutions(snippet) == [7, 8, 9, 10, 11], _getattr_resolutions(
        snippet
    )
    # Non-match pins (critic round 2): an unrelated object's .getattr()
    # method is not the builtin; a rebound alias and a rebound constant
    # invalidate their earlier tracked bindings.
    nonmatch = ast.parse(
        "resolver = None\n"
        "resolve = getattr\n"
        "resolve = lambda *args: None\n"
        "TARGET = 'set_flag'\n"
        "TARGET = 'unrelated'\n"
        "s = None\n"
        "def f(e, r):\n"
        "    resolver.getattr(s, 'set_flag')(e, r)\n"
        "    resolve(s, 'set_flag')(e, r)\n"
        "    getattr(s, TARGET)(e, r)\n"
    )
    assert _getattr_resolutions(nonmatch) == [], _getattr_resolutions(nonmatch)
