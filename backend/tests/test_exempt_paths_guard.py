"""must_change_password exempt-path classification guard (issue #202, AC6;
maintenance-subset guard added for issue #731; rescoped to the shared
recovery predicate by issue #1058).

The recovery-exemption decision now lives in ONE place —
``app/security/recovery_routes.py``'s ``_RECOVERY_EXEMPT`` table and its
``is_recovery_exempt(method, path)`` predicate — consumed by BOTH the
``must_change_password`` gate (deps.py) and the maintenance middleware's
write-block exemption. The two gates can no longer disagree by construction
(the #731 disagreeing-exempt-sets class), and the wiring itself is pinned by
``test_recovery_routes_guard.py``'s source-level + behavioral legs.

Behavior tests pin today's routes; this guard pins the CLASSIFICATION
COVERAGE: every (auth route, method) pair the app actually mounts must be
consciously classified as predicate-exempt (the table names the method) or
explicitly blocked here. Adding a new auth route without a classification
decision fails this test with an actionable message — the exact maintenance
fragility the audit finding names.

The table is AST-extracted from ``recovery_routes.py`` (a module-level dict
literal) so drift in the shipped table changes what this test sees; the
table's keys are additionally cross-checked against the app's real mounted
routes, so a router-local-only or otherwise dead entry fails loudly here
(the #1058 spelling class), not just at runtime.
"""

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CSRF_TEST_POLICY = "naive"

from app.api import deps  # noqa: E402,F401  (classification context)
from app.main import app  # noqa: E402
from app.security.recovery_routes import is_recovery_exempt  # noqa: E402


def _extract_recovery_exempt() -> dict[str, frozenset[str]]:
    """Pull the module-level ``_RECOVERY_EXEMPT`` dict literal out of
    ``app/security/recovery_routes.py`` (keys: mounted path constants;
    values: set literals of method constants)."""
    from app.security import recovery_routes

    source = Path(recovery_routes.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    extracted: dict[str, frozenset[str]] = {}
    for node in tree.body:
        # Accept both plain and annotated module-level assignments
        # (``_RECOVERY_EXEMPT: dict[...] = {...}`` parses as AnnAssign).
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
            value_node = node.value
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value_node = node.value
        else:
            continue
        if "_RECOVERY_EXEMPT" in names and isinstance(value_node, ast.Dict):
            for key, value in zip(value_node.keys, value_node.values):
                # Accept both a bare set literal and frozenset({...}) — the
                # shipped table writes frozenset per entry.
                if isinstance(value, ast.Call):
                    if not (
                        isinstance(value.func, ast.Name)
                        and value.func.id == "frozenset"
                        and value.args
                        and isinstance(value.args[0], ast.Set)
                    ):
                        continue
                    value = value.args[0]
                if not (isinstance(key, ast.Constant) and isinstance(value, ast.Set)):
                    continue
                methods = frozenset(
                    elt.value
                    for elt in value.elts
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                )
                if isinstance(key.value, str) and methods:
                    extracted[key.value] = methods
    return extracted


# Explicit decisions for every auth route that is NOT (fully) exempt for
# flagged users. Key: router-local path (mounted under /api). Value: why the
# NON-exempt methods of that route are blocked for flagged users. A new
# /auth route absent from this registry (and uncovered by the predicate's
# method-aware table) fails the guard below — extend it ONLY with a
# deliberate decision.
BLOCKED_AUTH_ROUTES: dict[str, str] = {
    "/auth/register": "flagged users must use change-password, not create accounts",
    "/auth/refresh": "token refresh is not a recovery path",
    "/auth/logout": "logout is reachable via change-password flow; kept blocked",
    "/auth/setup-status": "read-only setup probe; not a recovery surface",
    "/auth/sessions": "session enumeration is not a recovery surface",
    "/auth/sessions/{session_id}": "session mutation is not a recovery surface",
    # PATCH /auth/me mutates profile/password and must stay blocked so the
    # forced change-password flow is the only way out; GET /auth/me is
    # predicate-exempt (the me-read rehydration surface).
    "/auth/me": "PATCH is a mutation; only GET /auth/me is me-read exempt",
}


def _mounted_auth_routes():
    """Yield (full-path, methods) for every mounted /auth route.

    This FastAPI version wraps each include_router() in a lazily matched
    _IncludedRouter whose mount prefix lives on include_context and whose
    routes on original_router, so a plain app.routes walk sees no auth paths.
    """
    for route in app.routes:
        name = type(route).__name__
        if name == "_IncludedRouter":
            prefix = getattr(getattr(route, "include_context", None), "prefix", "") or ""
            router = getattr(route, "original_router", None)
            sub_routes = getattr(router, "routes", []) or []
        else:
            prefix = ""
            sub_routes = [route]
        for sub in sub_routes:
            path = getattr(sub, "path", None)
            if not path:
                continue
            full = prefix + path
            if full.startswith("/auth/") or full.startswith("/api/auth/"):
                yield full, sorted(getattr(sub, "methods", []) or [])


class TestRecoveryTableExtraction(unittest.TestCase):
    """The guard must see the shipped table (extractor sanity + shape)."""

    def test_table_extracted_and_mounted_spelled(self):
        table = _extract_recovery_exempt()
        self.assertTrue(table, "_RECOVERY_EXEMPT literal not found in recovery_routes.py")
        for path, methods in table.items():
            self.assertTrue(
                path.startswith("/api/"),
                f"table entry {path!r} must use the MOUNTED runtime spelling — "
                "a router-local-only entry is dead at runtime (issue #1058 "
                "spelling class)",
            )
            self.assertTrue(
                methods and all(m == m.upper() for m in methods),
                f"table entry {path!r} must name uppercase HTTP methods",
            )


class TestExemptPathsClassificationGuard(unittest.TestCase):
    """Every mounted (auth route, method) must be consciously classified."""

    def test_every_mounted_auth_route_method_is_classified(self):
        auth_routes = list(_mounted_auth_routes())
        self.assertTrue(auth_routes, "no auth routes found — app failed to mount routers")

        unclassified = []
        for path, methods in auth_routes:
            local = path[len("/api"):] if path.startswith("/api/") else path
            local_normalized = local.rstrip("/") or "/"
            for method in methods:
                # The predicate is method-aware and canonicalizes spellings
                # itself — exactly the runtime decision deps.py makes.
                if is_recovery_exempt(method, path):
                    continue
                if local_normalized in BLOCKED_AUTH_ROUTES:
                    continue
                unclassified.append(f"{method} {path}")

        self.assertEqual(
            unclassified,
            [],
            "Unclassified /auth route methods — every new auth route needs an "
            "explicit must_change_password decision: add it to the "
            "_RECOVERY_EXEMPT table in app/security/recovery_routes.py "
            "(recovery surfaces only, naming the allowed methods) or to "
            "BLOCKED_AUTH_ROUTES in this test with a reason: "
            + "; ".join(unclassified),
        )

    def test_guard_bites_on_unclassified_route(self):
        """Mutation probe: a new unclassified auth route method must fail the
        classification check."""
        new_path = "/api/auth/recovery-probe-unclassified"
        local = "/auth/recovery-probe-unclassified"
        for method in ("POST", "GET"):
            self.assertFalse(
                is_recovery_exempt(method, new_path),
                "probe route must be unexempt — if this fails, the probe "
                "collides with a real classification",
            )
        self.assertNotIn(local, BLOCKED_AUTH_ROUTES)

    def test_table_entries_reference_real_mounted_routes(self):
        """The #1058 spelling class, caught at the table: every predicate
        entry must be a real mounted auth route (a router-local-only or
        otherwise dead entry fails here, not silently at runtime)."""
        mounted = {path for path, _ in _mounted_auth_routes()}
        mounted_local = {
            p[len("/api"):] if p.startswith("/api/") else p for p in mounted
        }
        table = _extract_recovery_exempt()
        dead = [
            path
            for path in table
            if path not in mounted and path not in mounted_local
        ]
        self.assertEqual(
            dead,
            [],
            "_RECOVERY_EXEMPT entries that match no mounted /auth route — a "
            "dead entry is the router-local-only wedge shape (issue #1058 "
            "spelling class): " + "; ".join(dead),
        )

    def test_me_read_is_get_only(self):
        """The me-read surface survives as method-aware: GET /auth/me is the
        SPA's rehydration read (exempt); every other method stays blocked."""
        self.assertTrue(is_recovery_exempt("GET", "/api/auth/me"))
        self.assertTrue(is_recovery_exempt("GET", "/auth/me"))
        for method in ("PATCH", "POST", "PUT", "DELETE"):
            self.assertFalse(
                is_recovery_exempt(method, "/api/auth/me"),
                f"{method} /auth/me must stay blocked for flagged users",
            )


if __name__ == "__main__":
    unittest.main()
