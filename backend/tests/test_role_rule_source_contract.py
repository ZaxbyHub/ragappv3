"""Source contract: users.py routes role assignment through ONE shared
helper (issue #560, check C2).

Contract (source scan of backend/app/api/routes/users.py):
(a) a single shared role-assignment rule helper exists — a ``def`` whose
    name contains "can_assign_role";
(b) all three role-mutating handlers (create_user, update_user,
    update_user_role) reference that helper in their bodies, so the rule
    cannot be re-implemented per route;
(c) the dead duplicate privilege-escalation guard (base users.py:334-340,
    unreachable because its condition is strictly implied by the raises at
    :312-321) is deleted: its exact detail string must not appear.

Base-expected outcome: RED (DISCRIMINATING) — no helper exists at base, no
handler references one, and the dead-guard string is present.
"""

import re
from pathlib import Path

USERS_ROUTE = (
    Path(__file__).resolve().parents[1] / "app" / "api" / "routes" / "users.py"
)

# (a)/(b): the shared rule helper's def site and any reference to it.
HELPER_DEF = re.compile(r"def\s+\w*can_assign_role\w*\s*\(")
HELPER_REFERENCE = re.compile(r"\b\w*can_assign_role\w*\s*\(")

# The three handlers that mutate users.role.
ROLE_MUTATING_HANDLERS = ("create_user", "update_user", "update_user_role")

# (c): the exact detail string of the unreachable guard block at base
# users.py:334-340 — must be deleted, not preserved as a second layer.
DEAD_GUARD_DETAIL = "Only a superadmin can grant or modify the superadmin role"


def _source() -> str:
    return USERS_ROUTE.read_text(encoding="utf-8")


def _handler_body(source: str, name: str) -> str:
    """Slice one handler: from its ``async def`` line to the next route
    decorator (or end of module)."""
    start = source.index(f"async def {name}(")
    next_decorator = source.find("\n@router.", start)
    end = next_decorator if next_decorator != -1 else len(source)
    return source[start:end]


def test_shared_role_assignment_helper_exists():
    """(a) users.py defines a shared helper whose name contains
    can_assign_role."""
    match = HELPER_DEF.search(_source())
    assert match, (
        "users.py must define a single shared role-assignment rule helper "
        "whose name contains 'can_assign_role' (one rule, not per-route "
        "re-implementations)"
    )


def test_all_role_mutating_handlers_reference_helper():
    """(b) create_user, update_user, and update_user_role all delegate to
    the shared helper."""
    source = _source()
    missing = [
        name
        for name in ROLE_MUTATING_HANDLERS
        if not HELPER_REFERENCE.search(_handler_body(source, name))
    ]
    assert not missing, (
        "role-mutating handlers that do not reference the shared "
        f"can_assign_role helper: {missing} — the rule must be applied "
        "through the helper in every handler"
    )


def test_dead_duplicate_guard_eradicated():
    """(c) the unreachable duplicate guard's detail string is gone."""
    source = _source()
    assert DEAD_GUARD_DETAIL not in source, (
        "the unreachable duplicate privilege-escalation guard "
        "(base users.py:334-340) must be deleted, not kept as a second "
        "layer behind the shared helper"
    )
