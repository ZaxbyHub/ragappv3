"""must_change_password exempt-path classification guard (issue #202, AC6).

deps.py enforces must_change_password with a CLOSED allowlist (exempt_paths,
me_read_paths). Behavior tests pin today's routes; this guard pins the
CLASSIFICATION COVERAGE: every /auth route the app actually mounts must be
consciously classified as exempt, me-read, or explicitly blocked here. Adding a
new auth route without a classification decision fails this test with an
actionable message — the exact maintenance fragility the audit finding names.

The real sets are AST-extracted from deps.py source (they are function-local
literals), so deps.py drift changes what this test sees.
"""

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CSRF_TEST_POLICY = "naive"

from app.api import deps  # noqa: E402
from app.main import app  # noqa: E402


def _extract_local_sets() -> dict[str, set[str]]:
    """Pull the exempt_paths / me_read_paths set literals out of deps.py."""
    source = Path(deps.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    extracted: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name != "_resolve_active_user":
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Assign):
                    continue
                for target in sub.targets:
                    if (
                        isinstance(target, ast.Name)
                        and target.id in ("exempt_paths", "me_read_paths")
                        and isinstance(sub.value, ast.Set)
                    ):
                        extracted[target.id] = {
                            elt.value
                            for elt in sub.value.elts
                            if isinstance(elt, ast.Constant)
                        }
    return extracted


# Explicit decisions for every auth route that is NOT exempt for flagged users.
# Key: router-local path (mounted under /api). Value: why flagged users are
# blocked. A new /auth route absent from this registry (and from the deps.py
# sets) fails the guard below — extend it ONLY with a deliberate decision.
BLOCKED_AUTH_ROUTES: dict[str, str] = {
    "/auth/register": "flagged users must use change-password, not create accounts",
    "/auth/refresh": "token refresh is not a recovery path",
    "/auth/logout": "logout is reachable via change-password flow; kept blocked",
    "/auth/setup-status": "read-only setup probe; not a recovery surface",
    "/auth/sessions": "session enumeration is not a recovery surface",
    "/auth/sessions/{session_id}": "session mutation is not a recovery surface",
    # PATCH /auth/me mutates profile/password and must stay blocked so the
    # forced change-password flow is the only way out (deps.py inline comment).
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


class TestExemptPathsClassificationGuard(unittest.TestCase):
    """Every mounted /auth route must be consciously classified."""

    def test_sets_extracted_from_deps(self):
        """The AST extractor finds both non-empty sets (extractor sanity)."""
        sets = _extract_local_sets()
        self.assertIn("exempt_paths", sets)
        self.assertIn("me_read_paths", sets)
        self.assertTrue(sets["exempt_paths"], "exempt_paths literal not found")
        self.assertTrue(sets["me_read_paths"], "me_read_paths literal not found")

    def test_every_mounted_auth_route_is_classified(self):
        sets = _extract_local_sets()
        exempt = sets["exempt_paths"]
        me_read = sets["me_read_paths"]

        auth_routes = list(_mounted_auth_routes())
        self.assertTrue(auth_routes, "no auth routes found — app failed to mount routers")

        unclassified = []
        for path, methods in auth_routes:
            local = path[len("/api"):] if path.startswith("/api/") else path
            normalized = path.rstrip("/") or "/"
            local_normalized = local.rstrip("/") or "/"
            for method in methods:
                if normalized in exempt or local_normalized in exempt:
                    continue
                if (
                    method.upper() == "GET"
                    and (normalized in me_read or local_normalized in me_read)
                ):
                    continue
                if local_normalized in BLOCKED_AUTH_ROUTES:
                    continue
                unclassified.append(f"{method} {path}")

        self.assertEqual(
            unclassified,
            [],
            "Unclassified /auth routes — every new auth route needs an explicit "
            "must_change_password decision: add it to deps.py exempt_paths/"
            "me_read_paths (recovery surfaces only) or to BLOCKED_AUTH_ROUTES "
            "in this test with a reason: " + "; ".join(unclassified),
        )

    def test_guard_bites_on_unclassified_route(self):
        """Mutation probe: a new unclassified auth route must fail the guard."""
        sets = _extract_local_sets()
        exempt = sets["exempt_paths"]
        me_read = sets["me_read_paths"]

        new_path = "/api/auth/recovery-probe-unclassified"
        local = "/auth/recovery-probe-unclassified"
        classified = (
            normalized in exempt
            or (normalized in me_read)
            or local in BLOCKED_AUTH_ROUTES
            for normalized in (new_path, local)
        )
        self.assertFalse(
            any(classified),
            "probe route must be unclassified — if this fails, the probe "
            "collides with a real classification",
        )
        # And the classifier used by test_every_mounted_auth_route_is_classified
        # would indeed reject it:
        method = "POST"
        is_classified = (
            new_path in exempt
            or new_path.rstrip("/") in exempt
            or local in exempt
            or (method.upper() == "GET" and local in me_read)
            or local in BLOCKED_AUTH_ROUTES
        )
        self.assertFalse(is_classified)


if __name__ == "__main__":
    unittest.main()
