"""Issue #709 recurrence guardrail: CSRF stays scoped to mutating routes.

Defect class (b) of the #709 audit bundle: mutation-scoped CSRF protection
applied to safe (GET) methods. A header-token API client with a valid
admin token gets 403 "CSRF token missing or mismatch" on reads because
``csrf_protect`` demands the double-submit cookie/header handshake
designed for mutations (docs/engineering/conventions.md scopes CSRF to
state-changing routes).

This guardrail walks EVERY ``APIRouter`` defined at module level under
``app.api.routes`` — including secondary routers such as
``vault_members.group_access_router`` — plus each router's routes, so a
future route that reintroduces a GET-level ``csrf_protect`` dependency
anywhere fails here regardless of which module or router object added
it. The dependency walk is TRANSITIVE (a nested ``Depends`` chain
counts), mirroring the frozen check's ``_walk_dependant``. Known limit:
dependencies attached at app-assembly time via
``app.include_router(dependencies=[...])`` live in ``app.main``, not on
the router objects; none exist today (verified by census) and none may
be added for GET routes.
"""

import importlib
import pkgutil

from fastapi import APIRouter

from app.api import routes as routes_pkg
from app.security import csrf_protect


def _tree_contains_csrf(dependencies) -> bool:
    """True when any dependency (transitively) calls csrf_protect."""
    for dep in dependencies:
        if getattr(dep, "call", None) is csrf_protect:
            return True
        if _tree_contains_csrf(getattr(dep, "dependencies", []) or []):
            return True
    return False


def _iter_module_routers(module):
    """Yield every APIRouter instance defined at the module's top level.

    Enumerating module attributes (not just the conventional ``router``
    name) keeps secondary routers — e.g. ``vault_members
    .group_access_router`` — inside the guardrail's blast radius.
    """
    for value in vars(module).values():
        if isinstance(value, APIRouter):
            yield value


def _iter_get_routes_with_routers():
    """Yield (router_name, route) for every GET route on every router."""
    for module_info in pkgutil.iter_modules(routes_pkg.__path__):
        module = importlib.import_module(
            f"{routes_pkg.__name__}.{module_info.name}"
        )
        for router in _iter_module_routers(module):
            for route in router.routes:
                methods = getattr(route, "methods", None) or set()
                if "GET" not in methods:
                    continue
                yield f"{module_info.name}.{router.prefix}", route


def _collect_get_csrf_offenders():
    offenders = []
    for router_name, route in _iter_get_routes_with_routers():
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        if _tree_contains_csrf(getattr(dependant, "dependencies", []) or []):
            offenders.append(f"{router_name}: GET {route.path}")
    return offenders


def test_no_get_route_depends_on_csrf_protect_repo_wide():
    """No safe-method route on any API router may require the CSRF handshake.

    At the pre-fix base this failed with five offenders (the four prompts
    GETs and documents' GET /reindex/jobs/{job_id}); the guardrail exists
    to keep that class at zero.
    """
    offenders = _collect_get_csrf_offenders()
    assert offenders == []


def test_guardrail_discovery_covers_secondary_routers():
    """The discovery must see routers beyond the conventional ``router``
    attribute — this pins the fix for the ``group_access_router`` blind
    spot (a secondary APIRouter in vault_members.py) so the repo-wide
    claim stays true if route modules grow more router objects."""
    discovered_paths = {
        route.path
        for _, route in _iter_get_routes_with_routers()
    }
    from app.api.routes.vault_members import group_access_router

    group_access_gets = [
        route.path
        for route in group_access_router.routes
        if "GET" in (getattr(route, "methods", None) or set())
    ]
    assert group_access_gets, "expected GET routes on group_access_router"
    for path in group_access_gets:
        assert path in discovered_paths, (
            f"group_access_router GET {path} is invisible to the "
            "repo-wide CSRF guardrail"
        )
