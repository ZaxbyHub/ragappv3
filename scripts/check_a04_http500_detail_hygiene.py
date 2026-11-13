#!/usr/bin/env python3
"""Gate: no raw-exception interpolation in HTTP 5xx detail strings.

Interpolating a raw exception object into an ``HTTPException(status_code=500,
detail=f"...{e}...")`` leaks internals (driver messages, filesystem paths,
SQL fragments) to API clients. The audited route modules must instead log the
full exception server-side and return a generic detail string.

This gate walks the audited modules with the stdlib ``ast`` module and fails
(exit 1) while any audited exception call has BOTH:

  - a ``status_code`` that resolves to a 5xx-CAPABLE value-set: either a
    literal integer >= 500, OR — the issue #1119 widening for the variable-
    status shape issue #917 was filed about — a runtime-computed status whose
    local binding analysis (inline, time-ordered per scope) can resolve to a
    set of possible values where ANY value is >= 500. Bindings resolve from
    int literals, mapping dicts (module-level or function-local) and
    ``MAP.get(key, DEFAULT)`` calls; a dominating ``if NAME >= 500: return/
    raise`` guard proves the fall-through is < 500 for ``NAME`` until it is
    rebound (strict ``>`` guards do NOT refine: 500 stays reachable); non-
    guard branches merge conservatively. Statuses that are function
    parameters or otherwise unresolvable are not flagged. Sync functions,
    async functions, and module-level code are all covered — the literal
    arm of the pre-widening gate is preserved everywhere, AND
  - a ``detail`` that is an f-string (JoinedStr) containing at least one
    interpolation (FormattedValue) or a ``str(...)`` call — read from the
    ``detail`` keyword or the second positional argument.

Issue #917 extended the audited set with the Draft Room: ``DraftRoomHTTPError``
carries the same leak surface as ``HTTPException`` (it renders ``detail`` to
the client through the registered handler), so calls of either name in
``draft_room.py`` are inspected with both keyword and positional shapes.

Usage (from the repo root):

    python scripts/check_a04_http500_detail_hygiene.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

AUDITED_MODULES = [
    "backend/app/api/routes/memories.py",
    "backend/app/api/routes/vaults.py",
    "backend/app/api/routes/admin.py",
    "backend/app/api/routes/draft_room.py",
]

#: Exception classes whose ``detail`` reaches the client. ``HTTPException`` is
#: FastAPI's own; ``DraftRoomHTTPError`` is its Draft Room subclass with the
#: positional ``(status_code, detail, code)`` signature (issue #917).
AUDITED_EXCEPTION_NAMES = ("HTTPException", "DraftRoomHTTPError")


def _is_str_call(node: ast.expr) -> bool:
    """True for a ``str(...)`` call — the other raw-exception echo shape."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "str"
    )


def _interpolated_detail(detail: ast.expr | None) -> bool:
    if detail is None:
        return False
    if isinstance(detail, ast.JoinedStr) and any(
        isinstance(value, ast.FormattedValue) for value in detail.values
    ):
        return True
    return _is_str_call(detail)


def _dict_values(expr: ast.expr) -> set[int] | None:
    """Value-set for a dict literal of int values, else None."""
    if not isinstance(expr, ast.Dict):
        return None
    vals: set[int] = set()
    for v in expr.values:
        if isinstance(v, ast.Constant) and isinstance(v.value, int):
            vals.add(v.value)
        else:
            return None
    return vals


def _binding_values(expr: ast.expr, dicts: dict[str, set[int]]) -> set[int] | None:
    """Value-set for a status binding expression, or None if unresolvable."""
    if isinstance(expr, ast.Constant) and isinstance(expr.value, int):
        return {expr.value}
    if isinstance(expr, ast.Name):
        v = dicts.get(expr.id)
        return set(v) if isinstance(v, set) else None
    if (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Attribute)
        and expr.func.attr == "get"
        and isinstance(expr.func.value, ast.Name)
        and isinstance(dicts.get(expr.func.value.id), set)
    ):
        if len(expr.args) >= 2:
            default = expr.args[1]
            if (
                isinstance(default, ast.Constant)
                and isinstance(default.value, int)
            ):
                return set(dicts[expr.func.value.id]) | {default.value}
        return None
    return None


class _ScopeState:
    """Time-ordered binding state for one analysis scope: local variable
    name -> value-set (empty set = refined away), plus visible mapping
    dicts (name -> value-set)."""

    def __init__(self, module_dicts: dict[str, set[int]] | None = None):
        self.vars: dict[str, set[int]] = {}
        self.dicts: dict[str, set[int]] = dict(module_dicts or {})

    def clone(self) -> "_ScopeState":
        s = _ScopeState()
        s.vars = {k: set(v) for k, v in self.vars.items()}
        s.dicts = {k: set(v) for k, v in self.dicts.items()}
        return s

    def merge(self, other: "_ScopeState") -> None:
        """Conservative join: union of value-sets; a name present in only one
        side keeps that side's set (the other branch left it untouched).
        Dicts bound on BOTH sides union their value-sets — keeping one
        side's values would make the result depend on branch order and drop
        reachable 5xx values (final-critic round-1 finding)."""
        for k in set(self.vars) | set(other.vars):
            if k not in self.vars:
                self.vars[k] = other.vars[k]
            elif k in other.vars:
                self.vars[k] = set(self.vars[k]) | set(other.vars[k])
        for k in set(self.dicts) | set(other.dicts):
            if k not in self.dicts:
                self.dicts[k] = other.dicts[k]
            elif k in other.dicts:
                self.dicts[k] = set(self.dicts[k]) | set(other.dicts[k])


def _check_calls_in(node: ast.AST, state: _ScopeState, hits: list[int]) -> None:
    """Classify every audited exception call inside ``node`` with the CURRENT
    binding state (callers invoke this BEFORE applying their statement's
    binding effects, so every call sees the state at its own moment)."""
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name not in AUDITED_EXCEPTION_NAMES:
            continue
        status_code: ast.expr | None = None
        detail: ast.expr | None = None
        for keyword in sub.keywords:
            if keyword.arg == "status_code":
                status_code = keyword.value
            elif keyword.arg == "detail":
                detail = keyword.value
        if status_code is None and len(sub.args) >= 1:
            status_code = sub.args[0]
        if detail is None and len(sub.args) >= 2:
            detail = sub.args[1]
        if status_code is None:
            continue
        values = _binding_values(status_code, state.dicts)
        if values is None and isinstance(status_code, ast.Name):
            bound = state.vars.get(status_code.id)
            if bound is not None:
                values = set(bound)
        if not values:
            continue
        if not any(v >= 500 for v in values):
            continue
        if _interpolated_detail(detail):
            hits.append(sub.lineno)


def _visit(
    stmts: list[ast.stmt], state: _ScopeState, hits: list[int]
) -> None:
    """Ordered traversal: statements apply their binding effects as they are
    reached, so every audited call is classified with the state at the moment
    of the call (issue #1119: inline, time-ordered analysis)."""
    for stmt in stmts:
        if isinstance(stmt, ast.Assign):
            # The RHS evaluates with the PRE-assignment state.
            _check_calls_in(stmt.value, state, hits)
            for tgt in stmt.targets:
                if isinstance(tgt, ast.Name):
                    dv = _dict_values(stmt.value)
                    if dv is not None:
                        state.dicts[tgt.id] = set(dv)
                    else:
                        bound = _binding_values(stmt.value, state.dicts)
                        state.vars[tgt.id] = bound if bound is not None else set()
            continue
        if isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            # `status: int = 503` binds exactly like a plain assignment
            # (final-critic round-1 finding).
            _check_calls_in(stmt.value, state, hits)
            if isinstance(stmt.target, ast.Name):
                dv = _dict_values(stmt.value)
                if dv is not None:
                    state.dicts[stmt.target.id] = set(dv)
                else:
                    bound = _binding_values(stmt.value, state.dicts)
                    state.vars[stmt.target.id] = (
                        bound if bound is not None else set()
                    )
            continue
        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            # The body runs in order on the SAME state (assignments inside
            # the block bind for the code after it — final-critic round-1
            # finding; AsyncWith joined per implementation-review round 3 —
            # the audited FastAPI routes' natural idiom is `async with`).
            for item in stmt.items:
                _check_calls_in(item.context_expr, state, hits)
            _visit(stmt.body, state, hits)
            continue
        if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
            # Zero or more iterations: the body may run, so its bindings are
            # POSSIBLE — visit on a clone and merge back (a loop-assigned
            # status must not be treated as its pre-loop value alone).
            if isinstance(stmt, (ast.For, ast.AsyncFor)):
                _check_calls_in(stmt.iter, state, hits)
            else:
                _check_calls_in(stmt.test, state, hits)
            body_state = state.clone()
            _visit(stmt.body, body_state, hits)
            if stmt.orelse:
                _visit(stmt.orelse, body_state, hits)
            state.merge(body_state)
            continue
        if isinstance(stmt, getattr(ast, "TryStar", ())):
            # `except*` handlers: same shape contract as ast.Try.
            body_state = state.clone()
            _visit(stmt.body, body_state, hits)
            _visit(stmt.orelse, body_state, hits)
            handler_base = state.clone()
            for h in stmt.handlers:
                hs = handler_base.clone()
                _visit(h.body, hs, hits)
                handler_base.merge(hs)
            state.merge(body_state)
            state.merge(handler_base)
            _visit(stmt.finalbody, state, hits)
            continue
        if isinstance(stmt, ast.If):
            _check_calls_in(stmt.test, state, hits)
            test = stmt.test
            dominating = (
                isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and len(test.ops) == 1
                and isinstance(test.ops[0], ast.GtE)
                and isinstance(test.comparators[0], ast.Constant)
                and test.comparators[0].value == 500
                and all(
                    isinstance(s, (ast.Return, ast.Raise)) for s in stmt.body
                )
            )
            if dominating:
                # The guard body's calls see the pre-refinement state; the
                # fall-through (and any else-arm) sees NAME refined to < 500.
                for s in stmt.body:
                    _check_calls_in(s, state, hits)
                refined = state.clone()
                nm = test.left.id
                if nm in refined.vars:
                    refined.vars[nm] = {
                        v for v in refined.vars[nm] if v < 500
                    }
                _visit(stmt.orelse, refined, hits)
                state.vars = refined.vars
                state.dicts = refined.dicts
                continue
            arm_a = state.clone()
            arm_b = state.clone()
            _visit(stmt.body, arm_a, hits)
            _visit(stmt.orelse, arm_b, hits)
            state.merge(arm_a)
            state.merge(arm_b)
            continue
        if isinstance(stmt, ast.Try):
            body_state = state.clone()
            _visit(stmt.body, body_state, hits)
            _visit(stmt.orelse, body_state, hits)
            handler_base = state.clone()
            for h in stmt.handlers:
                hs = handler_base.clone()
                _visit(h.body, hs, hits)
                handler_base.merge(hs)
            state.merge(body_state)
            state.merge(handler_base)
            _visit(stmt.finalbody, state, hits)
            continue
        if isinstance(
            stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue  # separate scope, visited by the driver
        # Return / Raise / Expr / With / For / While / ...: calls in the
        # statement see the current state.
        _check_calls_in(stmt, state, hits)


def raw_500_detail_lines(source: str) -> list[int]:
    """Line numbers of audited exception calls whose status resolves to a
    5xx-CAPABLE value-set at the moment of the call (inline, time-ordered
    scope analysis — see the module docstring) with an interpolated detail."""
    hits: list[int] = []
    tree = ast.parse(source)
    module_dicts: dict[str, set[int]] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            dv = _dict_values(stmt.value)
            if (
                dv is not None
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
            ):
                module_dicts[stmt.targets[0].id] = dv
    module_state = _ScopeState(module_dicts)
    outer = [
        s
        for s in tree.body
        if not isinstance(
            s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        )
    ]
    _visit(outer, module_state, hits)
    for fn in [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]:
        _visit(fn.body, _ScopeState(module_dicts), hits)
    return sorted(hits)


def main() -> int:
    findings: list[str] = []
    for rel_path in AUDITED_MODULES:
        path = ROOT / rel_path
        if not path.is_file():
            print(f"MISSING-AUDIT-TARGET: {rel_path}")
            findings.append(f"missing target {rel_path}")
            continue
        source = path.read_text(encoding="utf-8")
        for lineno in raw_500_detail_lines(source):
            findings.append(f"{rel_path}:{lineno}")
            print(f"RAW-EXCEPTION-DETAIL: {rel_path}:{lineno}")

    if findings:
        print(
            f"FAIL: {len(findings)} raw-exception 500 detail(s) in audited route modules"
        )
        return 1
    print(
        "OK: no raw-exception 500 details in audited route modules "
        f"({len(AUDITED_MODULES)} files checked)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
