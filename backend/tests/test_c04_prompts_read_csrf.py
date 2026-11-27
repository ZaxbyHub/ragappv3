"""Issue #709 acceptance check (C04, AC3): prompts GET routes must not
depend on ``csrf_protect``.

The prompts router currently declares ``Depends(csrf_protect)`` on ALL its
endpoints, including the four read-only GET handlers (list, /active,
/ab-experiments, /{version}). Reads must not require the mutation
handshake — a GET that demands a CSRF token breaks cookie-less/API clients
and caches for zero security benefit (CSRF protects against cross-site
state changes, which a read cannot make).

This is a STRUCTURAL check: it walks the FastAPI dependency tree of every
GET route on ``app.api.routes.prompts.router`` (each ``route.dependant``
holds its ``Depends(...)`` parameters as nested ``Dependant`` entries,
each with a ``.call`` and possibly further ``.dependencies``) and counts
the routes whose tree contains ``app.security.csrf_protect``. No app is
built and no request is issued, so the conftest CSRF bypass policy is
irrelevant here (verified by running: no CSRF_TEST_POLICY declaration is
needed).

Issue #945 AC2 adds the sibling pin in the same dedicated file: the four
MUTATING prompts routes (POST /prompts, POST /{version}/activate,
POST /ab-experiments, POST /ab-experiments/{id}/end) must KEEP
``csrf_protect`` — the #709 fix removed the dependency from reads only.
The retention test moved here from ``test_c04_review_hardening.py`` so
this file pins both directions: reads must NOT, mutations MUST.
"""

import app.api.routes.prompts as prompts_module
import app.security as security_module


def _walk_dependant(node):
    """Yield ``node`` and every nested Dependant in its dependency tree."""
    yield node
    for sub in getattr(node, "dependencies", None) or []:
        yield from _walk_dependant(sub)


def test_get_routes_do_not_depend_on_csrf_protect():
    """No GET route on the prompts router may depend on csrf_protect."""
    count = 0
    get_routes = 0
    for route in prompts_module.router.routes:
        methods = getattr(route, "methods", None) or set()
        if "GET" not in methods:
            continue
        get_routes += 1
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        for node in _walk_dependant(dependant):
            if getattr(node, "call", None) is security_module.csrf_protect:
                count += 1
                break

    # Precondition: the four read handlers exist as GET routes.
    assert get_routes >= 4, f"expected the GET handlers to exist, saw {get_routes}"
    assert count == 0


def test_mutating_prompts_routes_still_depend_on_csrf_protect():
    """Every mutating prompts route must keep the csrf_protect handshake.

    The #709 fix stripped ``csrf_protect`` from the read-only GET
    handlers only; a mutation without the double-submit handshake
    reopens the CSRF hole that fix closed. Losing the dependency on any
    of the four mutating routes must fail here (issue #945 AC2).
    """
    mutating = [
        ("POST", "/prompts"),
        ("POST", "/prompts/{version}/activate"),
        ("POST", "/prompts/ab-experiments"),
        ("POST", "/prompts/ab-experiments/{experiment_id}/end"),
    ]
    for method, path in mutating:
        hit = False
        for route in prompts_module.router.routes:
            methods = getattr(route, "methods", None) or set()
            if method in methods and route.path == path:
                dependant = getattr(route, "dependant", None)
                nodes = list(_walk_dependant(dependant)) if dependant else []
                assert any(
                    getattr(node, "call", None) is security_module.csrf_protect
                    for node in nodes
                ), f"{method} {path} lost its csrf_protect dependency"
                hit = True
                break
        assert hit, f"{method} {path} not found on the prompts router"
