"""Guardrail against the permission-surface defect classes (issue #560,
check C8).

Three sub-contracts, each red at base:

(a) Backend single-rule delegation: every role-mutation route handler in
    backend/app/api/routes/users.py (create_user, update_user,
    update_user_role) must delegate to the shared role-assignment helper —
    the helper's def (name containing "can_assign_role") exists in the
    module AND each handler's body references it. At base there is no
    helper, so this is RED.

(b) Frontend shared-helper adoption: the three role surfaces —
    frontend/src/pages/AdminUsersPage.tsx,
    frontend/src/pages/AdminUsersPage/CreateUserDialog.tsx,
    frontend/src/pages/AdminUsersPage/EditUserDialog.tsx — must NOT contain
    the hand-filter idiom that caused the drift: an inline
    `... value !== "superadmin" || <actor-flag>` filter over a local
    ROLE_OPTIONS list. At base both dialogs contain exactly that pattern,
    so this is RED.

(c) Invite resolution guard: create_org_invite in
    backend/app/api/routes/organizations.py must resolve the invitee
    identifier against the users table and raise an explicit not-found 400
    when no row matches. Anchors (chosen against the CURRENT code): the
    handler body must contain a `FROM users WHERE username` lookup (this
    anchor already exists at base, used by the below-member check) AND an
    HTTPException with status_code=400 whose detail wording indicates the
    user was not found / does not exist / no matching user / is
    unresolvable / is unknown. At base the only 400 in that body is the
    below-member rejection, whose wording matches none of those phrases,
    so this is RED.

Base-expected outcome: RED (DISCRIMINATING) — all three sub-asserts fail
at base.
"""

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
FRONTEND_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"

USERS_ROUTE = BACKEND / "app" / "api" / "routes" / "users.py"
ORGANIZATIONS_ROUTE = BACKEND / "app" / "api" / "routes" / "organizations.py"

FRONTEND_ROLE_SURFACES = (
    FRONTEND_SRC / "pages" / "AdminUsersPage.tsx",
    FRONTEND_SRC / "pages" / "AdminUsersPage" / "CreateUserDialog.tsx",
    FRONTEND_SRC / "pages" / "AdminUsersPage" / "EditUserDialog.tsx",
)

ROLE_MUTATING_HANDLERS = ("create_user", "update_user", "update_user_role")

HELPER_DEF = re.compile(r"def\s+\w*can_assign_role\w*\s*\(")
HELPER_REFERENCE = re.compile(r"\b\w*can_assign_role\w*\s*\(")

# The hand-filter idiom: `.filter((r) => r.value !== "superadmin" ||
# isSuperAdmin)` — filtering a local ROLE_OPTIONS list by "not superadmin
# OR actor-is-superadmin" instead of deriving options from the shared
# helper. The actor-flag name is left open; the banned shape is the
# `value !== "superadmin" ||` comparison itself.
HAND_FILTER = re.compile(r'value\s*!==\s*["\']superadmin["\']\s*\|\|')

# A not-found-style 400 raise: HTTPException(status_code=400, ...) whose
# detail wording (before the closing paren) indicates the invitee could
# not be resolved. The base below-member 400 ("Cannot invite a user with a
# global role below member") deliberately matches none of these phrases.
NOT_FOUND_400 = re.compile(
    r"HTTPException\(\s*status_code=400[^)]*?"
    r"(?:not\s+found|no\s+(?:matching\s+)?user|does\s+not\s+exist"
    r"|unresolvable|unknown\s+user)",
    re.I | re.S,
)


def _source(path: Path) -> str:
    assert path.exists(), f"{path} not found"
    return path.read_text(encoding="utf-8")


def _handler_body(source: str, name: str) -> str:
    """Slice one handler: from its ``async def`` line to the next route
    decorator (or end of module)."""
    start = source.index(f"async def {name}(")
    next_decorator = source.find("\n@router.", start)
    end = next_decorator if next_decorator != -1 else len(source)
    return source[start:end]


def test_role_mutation_handlers_delegate_to_shared_helper():
    """(a) users.py defines the shared can_assign_role helper and every
    role-mutating handler references it."""
    source = _source(USERS_ROUTE)
    assert HELPER_DEF.search(source), (
        "users.py must define a shared role-assignment helper whose name "
        "contains 'can_assign_role'"
    )
    missing = [
        name
        for name in ROLE_MUTATING_HANDLERS
        if not HELPER_REFERENCE.search(_handler_body(source, name))
    ]
    assert not missing, (
        "role-mutating handlers not delegating to the shared "
        f"can_assign_role helper: {missing}"
    )


def test_frontend_role_surfaces_do_not_hand_filter_superadmin():
    """(b) None of the three frontend role surfaces hand-filters the
    superadmin option inline; option lists must come from the shared
    helper (tested behaviorally in
    frontend/src/pages/AdminUsersPage/roleOptions.test.tsx)."""
    offenders = [
        str(path)
        for path in FRONTEND_ROLE_SURFACES
        if HAND_FILTER.search(_source(path))
    ]
    assert not offenders, (
        "frontend role surfaces still hand-filter 'superadmin' inline "
        f"(must derive options from the shared roleOptionsFor helper): "
        f"{offenders}"
    )


def test_create_org_invite_resolves_invitee_and_raises_not_found_400():
    """(c) create_org_invite looks the invitee up by username and raises a
    not-found 400 when no user matches (see anchors in the module
    docstring)."""
    source = _source(ORGANIZATIONS_ROUTE)
    body = _handler_body(source, "create_org_invite")
    assert "FROM users WHERE username" in body, (
        "create_org_invite must fetch the invitee user row via a "
        "FROM users WHERE username lookup"
    )
    match = NOT_FOUND_400.search(body)
    assert match, (
        "create_org_invite must raise HTTPException(status_code=400, ...) "
        "with not-found wording (e.g. 'no user found for identifier ...') "
        "when the identifier resolves to no user"
    )
