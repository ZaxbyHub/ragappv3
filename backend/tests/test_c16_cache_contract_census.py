"""Acceptance checks (C16, C10/C11) — structural census of cache-write
contract and recorded design decisions in query_transformer.py.

Both checks are AST/source-based and deliberately NAME-AGNOSTIC about how
the implementation chooses to structure itself:

- C10 (DISCRIMINATING): every redis ``setex`` write reference in the
  module — whether a direct ``client.setex(...)`` call or a bare
  ``client.setex`` attribute passed to a wrapper such as ``redis_call`` —
  must sit inside EXACTLY ONE function/method definition, that definition
  must NOT be named ``transform`` or ``plan`` (i.e. the write must be
  factored into a shared helper), and that helper must be called from at
  least 3 distinct call sites in the module (the transform step-back
  path, the HyDE path, and the planner path). At the pre-fix base setex
  writes live inside both ``transform`` (two) and ``plan`` (one) (RED).
- C11 (DISCRIMINATING): the module source must record all three
  accepted-cost decision markers, each at least once:
  ``DECISION(no-negative-cache):``, ``DECISION(shared-breaker):`` and
  ``DECISION(content-addressed-keys):``. At the pre-fix base none exist
  (RED).
"""

import ast
from pathlib import Path

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "app" / "services" / "query_transformer.py"
)

_FUNC_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _census_setex_references(tree):
    """Return [(lineno, enclosing_def_name_or_None)] for every Attribute
    node named ``setex``.

    Covers BOTH write shapes: a direct ``client.setex(...)`` call (the
    Attribute is the Call's func) and a bare ``client.setex`` reference
    passed to a wrapper such as ``redis_call`` (the Attribute is an
    argument). Either way it is a redis cache write site.
    """
    sites = []

    def walk(node, enclosing):
        for child in ast.iter_child_nodes(node):
            child_enclosing = enclosing
            if isinstance(child, _FUNC_NODES):
                child_enclosing = child.name
            if isinstance(child, ast.Attribute) and child.attr == "setex":
                sites.append((getattr(child, "lineno", -1), enclosing))
            walk(child, child_enclosing)

    walk(tree, None)
    return sites


def _count_call_sites(tree, name):
    """Count Call nodes whose callee resolves to ``name`` — either a plain
    name call (``name(...)``) or an attribute call (``self.name(...)``)."""
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == name:
                count += 1
            elif isinstance(func, ast.Attribute) and func.attr == name:
                count += 1
    return count


def test_all_setex_writes_live_in_one_shared_helper_called_from_three_paths():
    """C10: one shared cache-write helper, used by step-back/HyDE/planner."""
    source = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    sites = _census_setex_references(tree)
    assert sites, (
        f"expected at least one redis setex write reference in {MODULE_PATH}; "
        "the census found none, so the module's cache-write surface changed "
        "shape — update this contract check alongside the implementation"
    )

    enclosing_defs = {name for _, name in sites}
    assert None not in enclosing_defs, (
        "cache-write contract: a redis setex write sits at module level "
        "(outside any function); all cache writes must live inside the one "
        "shared helper"
    )
    assert len(enclosing_defs) == 1, (
        "cache-write contract: every redis setex write in "
        "query_transformer.py must sit inside EXACTLY ONE shared helper "
        "function so the write shape (key versioning, TTL, "
        "no-negative-cache) is enforced in a single place; found setex "
        f"writes across {len(enclosing_defs)} defs "
        f"{sorted(enclosing_defs)} at lines {[ln for ln, _ in sites]}"
    )

    helper = next(iter(enclosing_defs))
    assert helper not in ("transform", "plan"), (
        "cache-write contract: the helper holding the setex writes must not "
        "be 'transform' or 'plan' themselves — the write path must be "
        f"factored into a shared helper both can call (found {helper!r})"
    )

    call_sites = _count_call_sites(tree, helper)
    assert call_sites >= 3, (
        f"cache-write contract: the shared cache-write helper {helper!r} "
        "must be called from at least 3 distinct call sites in the module "
        "(the transform step-back path, the HyDE path, and the planner "
        f"path); found only {call_sites}"
    )


def test_accepted_cost_decisions_are_recorded_in_code():
    """C11: the three accepted-cost DECISION markers must exist in source."""
    source = MODULE_PATH.read_text(encoding="utf-8")
    for marker in (
        "DECISION(no-negative-cache):",
        "DECISION(shared-breaker):",
        "DECISION(content-addressed-keys):",
    ):
        assert marker in source, (
            f"the accepted-cost decision marker {marker!r} must be recorded "
            "in query_transformer.py — accepted costs stay documented in "
            "code next to the code that accepts them"
        )
