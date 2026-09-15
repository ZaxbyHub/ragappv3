"""Generic trailing-slash schema-parity property test (issue #560, check C7).

Replaces the enumeration approach in test_route_parity.py (which hardcodes
4 route groups) with a property over the production app itself: for every
pair (or group) of routes that differ only by a trailing slash and share
the same method set, EXACTLY ONE member may be visible in the OpenAPI
schema — regardless of which direction the hidden twin goes.

Walker note: this FastAPI version wraps ``include_router`` results in
include-context objects, so iterating ``app.routes`` directly only sees
wrappers. The walker therefore enumerates effective routes with
``fastapi.routing.iter_route_contexts`` — the same iterator
``app.openapi()`` itself uses — so the walker's route view (final paths,
methods) matches the schema generator's view exactly. The SPA catch-all
(path containing "{full_path") is skipped, auto-added HEAD/OPTIONS methods
are stripped, and the grouping key is (frozenset of remaining methods,
path with ONE trailing slash stripped). A group member counts as
schema-visible when the openapi paths dict documents that member AND that
method.

The second test is a discrimination probe: a throwaway local router
registers a trailing-slash twin pair with BOTH members schema-visible; the
same walker must flag it (so the walker is proven able to catch a fifth
router's unguarded twin), and must still find zero violations on the real
app (so the probe cannot pass vacuously).

Base-expected outcome: GREEN (PRESERVING) — both tests pass at base: the
property holds today (19 hidden twin groups, each exactly-one-visible) and
the walker is new.
"""

import sys
import types
from collections import defaultdict

# Stub missing optional dependencies before importing the app (mirrors the
# bootstrap used by test_route_parity.py; backend/conftest.py restores the
# authoritative graphs before every test).
try:
    import lancedb  # noqa: F401
except ImportError:
    sys.modules["lancedb"] = types.ModuleType("lancedb")
try:
    import pyarrow  # noqa: F401
except ImportError:
    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

from fastapi import APIRouter, FastAPI
from fastapi.routing import iter_route_contexts

from app.main import app as production_app


def find_schema_parity_violations(app: FastAPI) -> list[dict]:
    """Walk the app's effective routes and return every trailing-slash
    twin group that does not have exactly one schema-visible member.

    A group is (methods, normalized path) where methods excludes
    HEAD/OPTIONS and the normalized path has one trailing slash stripped.
    Groups with fewer than two members are not twin groups and are ignored.
    """
    openapi_paths = app.openapi()["paths"]
    groups: dict[tuple[frozenset, str], set[str]] = defaultdict(set)
    for context in iter_route_contexts(app.routes):
        path = context.path
        methods = context.methods
        if not path or not methods:
            continue
        if "{full_path" in path:  # SPA catch-all — not part of the parity convention
            continue
        methods = set(methods) - {"HEAD", "OPTIONS"}
        if not methods:
            continue
        normalized = path[:-1] if len(path) > 1 and path.endswith("/") else path
        groups[(frozenset(methods), normalized)].add(path)

    violations: list[dict] = []
    for (methods, normalized), members in sorted(
        groups.items(), key=lambda item: (item[0][1], sorted(item[0][0]))
    ):
        if len(members) < 2:
            continue
        visible = sorted(
            member
            for member in members
            if any(
                str(method).lower() in openapi_paths.get(member, {})
                for method in methods
            )
        )
        if len(visible) != 1:
            violations.append(
                {
                    "methods": sorted(methods),
                    "members": sorted(members),
                    "normalized": normalized,
                    "schema_visible": visible,
                }
            )
    return violations


class TestRouteParityGeneric:
    """The parity property, walked over the real app."""

    def test_every_trailing_slash_twin_group_has_exactly_one_visible_member(self):
        violations = find_schema_parity_violations(production_app)
        assert violations == [], (
            "trailing-slash route groups without exactly one schema-visible "
            f"member: {violations}"
        )


class TestRouteParityWalkerDiscrimination:
    """Proves the walker catches a newly added unguarded twin."""

    def test_walker_flags_local_unguarded_twin_and_real_app_stays_clean(self):
        # A throwaway router whose twin pair has BOTH members visible —
        # exactly the defect class a fifth router could reintroduce.
        probe_app = FastAPI()
        probe_router = APIRouter(prefix="/widgets")

        @probe_router.get("")
        @probe_router.get("/")
        def list_widgets():  # pragma: no cover - never called
            return []

        probe_app.include_router(probe_router)

        probe_violations = find_schema_parity_violations(probe_app)
        assert probe_violations, (
            "walker failed to flag a local router whose trailing-slash "
            "twin pair has both members schema-visible — the property test "
            "above would be vacuous"
        )
        flagged = probe_violations[0]
        assert sorted(flagged["members"]) == ["/widgets", "/widgets/"]
        assert flagged["methods"] == ["GET"]
        assert flagged["schema_visible"] == ["/widgets", "/widgets/"]

        # The same walker must find zero violations on the real app, so the
        # probe cannot pass vacuously.
        assert find_schema_parity_violations(production_app) == []
