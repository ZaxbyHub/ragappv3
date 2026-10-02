"""Issue #697 guardrail: structural sync-call census (Workstream B PR 8).

Two AST censuses over production sources (no runtime imports):

1. ``test_record_file_centroid_dispatched_off_loop_census`` — every
   production reference to ``near_duplicates.record_file_centroid`` must be
   dispatched through ``asyncio.to_thread``/``run_in_executor`` (the reference
   is the callable ARGUMENT of the executor call, or the call itself is
   off-loop by construction inside a sync function that async callers
   dispatch). RED at base 6e93b139: the single reference was the ``func`` of a
   direct Call inside async ``_finalize_indexed_success``
   (document_processor.py). GREEN with the #697 fix.

2. ``test_row_builder_calls_from_async_pass_enrichment_map_census`` — every
   call to ``_row_to_document_response`` made from an ``async def`` body in
   api/routes/documents.py must pass a non-None ``enrichment_map`` keyword,
   and no ``async def`` body may call ``is_enrichment_enabled_for_file``
   directly. RED at base: all three async-frame call sites
   (list/get/toggle) passed no such kwarg and reached the helper through the
   sync builder. GREEN with the fix (the map-guarded fallback inside the
   sync builder is allowlisted: it is unreachable from async frames in this
   module and exists for standalone callers).

This is a scoped site census for #697's surfaces; the repo-wide structural
sync-call guard is owned by #803 (Workstream Q PR 1).
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_DP = _ROOT / "backend" / "app" / "services" / "document_processor.py"
_DOCS = _ROOT / "backend" / "app" / "api" / "routes" / "documents.py"

_EXECUTOR_FUNC_NAMES = {"to_thread", "run_in_executor"}
_CENTROID_REF = "record_file_centroid"


def _is_executor_call(node: ast.AST) -> bool:
    """True when node is asyncio.to_thread(...) / loop.run_in_executor(...)."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in _EXECUTOR_FUNC_NAMES
    if isinstance(func, ast.Attribute):
        return func.attr in _EXECUTOR_FUNC_NAMES
    return False


def _centroid_dispatch_violations(path: Path) -> list[str]:
    """References to record_file_centroid NOT dispatched via an executor.

    A reference is clean when its innermost enclosing call is an executor
    call whose callable argument it is (``asyncio.to_thread(ref, ...)``) —
    this covers both the Attribute-argument form and a bare-Name pass. A
    reference that is itself the ``func`` of a direct Call (or floats loose)
    is a violation.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    parent_of: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parent_of[id(child)] = parent

    violations: list[str] = []
    for node in ast.walk(tree):
        is_ref = (
            isinstance(node, ast.Attribute) and node.attr == _CENTROID_REF
        ) or (isinstance(node, ast.Name) and node.id == _CENTROID_REF)
        if not is_ref:
            continue
        # Walk up to the innermost enclosing Call and classify.
        cursor: ast.AST = node
        chain: list[ast.AST] = [node]
        while id(cursor) in parent_of:
            cursor = parent_of[id(cursor)]
            chain.append(cursor)
            if isinstance(cursor, ast.Call):
                if _is_executor_call(cursor) and any(
                    arg is node for arg in cursor.args
                ):
                    break  # dispatched as the executor's callable argument
                if isinstance(cursor.func, ast.AST) and cursor.func is node:
                    violations.append(
                        f"line {node.lineno}: direct call of "
                        f"{_CENTROID_REF} (not executor-dispatched)"
                    )
                    break
                # Some other enclosing call (e.g. a wrapper) — keep walking
                # outward so a loose reference never passes silently.
                continue
        else:
            # Reached the root without an enclosing call: a first-class
            # reference that is not an executor argument.
            violations.append(
                f"line {getattr(node, 'lineno', '?')}: bare reference to "
                f"{_CENTROID_REF} outside any call"
            )
    return violations


def _async_frame_builder_violations(path: Path) -> list[str]:
    """_row_to_document_response calls from async bodies lacking the map kwarg,
    plus direct is_enrichment_enabled_for_file calls from async bodies."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            func = sub.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name == "_row_to_document_response":
                kw = next(
                    (k for k in sub.keywords if k.arg == "enrichment_map"), None
                )
                if kw is None or (
                    isinstance(kw.value, ast.Constant) and kw.value.value is None
                ):
                    violations.append(
                        f"line {sub.lineno}: async-frame "
                        f"_row_to_document_response call without enrichment_map"
                    )
            if name == "is_enrichment_enabled_for_file":
                violations.append(
                    f"line {sub.lineno}: async-frame direct call of "
                    f"is_enrichment_enabled_for_file"
                )
    return violations


def test_record_file_centroid_dispatched_off_loop_census() -> None:
    violations = _centroid_dispatch_violations(_DP)
    assert violations == []


def test_row_builder_calls_from_async_pass_enrichment_map_census() -> None:
    violations = _async_frame_builder_violations(_DOCS)
    assert violations == []
