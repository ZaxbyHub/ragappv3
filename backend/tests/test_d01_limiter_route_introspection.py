"""Issue #720 AC8 — repo-wide limiter-wrapper route-introspection guard (D-01).

Contract: every APIRoute mounted on the real ``app.main`` application whose
endpoint is registered in the shared slowapi limiter's ``_route_limits`` map
must store the LIMITER-WRAPPED endpoint — the function object the router
holds must carry ``__wrapped__`` and must not be the raw handler itself.
That holds exactly when ``@router.<method>(...)`` is the OUTERMOST decorator
and ``@limiter.limit`` sits below it: with that order the router stores the
wrapper produced by the limiter decorator; with the reverse order (the
defect class of issue #659 / PR #418 / 560f8c7e / 8ad8eb86) the router
stores the raw handler. slowapi runs its limit check INSIDE the wrapper the
decorator returns, so a router that stored the raw handler never executes
the check and enforcement silently never fires (the historical 429 probes of
issue #659 are the behavioral proof; the runtime 429 tests in
test_auth_route_rate_limit_runtime.py remain the unforgeable backstop for
the auth trio).

Known residual: a contrived ``functools.wraps``-applying decorator stacked
in the broken order could hand this guard a ``__wrapped__``-carrying raw
handler; the additional marked-set identity check below (the stored endpoint
must not BE one of the raw handlers the limiter decorator registered) closes
the plain recurrence, and only deliberate decorator collusion can evade it.

This guard is pure runtime introspection of the mounted application: it reads
no source text, runs no regex or AST scan, and does not care which router
variable registered a route (the AST scan only recognizes literal receivers
named ``router``/``limiter``, so a second router variable is invisible to
it). Enumerating the routes requires expanding this FastAPI version's lazy
``_IncludedRouter`` wrappers: top-level ``app.routes`` holds a couple of bare
APIRoutes plus the wrappers, and the real routes live on
``wrapper.original_router.routes`` — the walker below expands both shapes
recursively. ``_route_limits``, ``_dynamic_route_limits``,
``_Limiter__marked_for_limiting`` and ``original_router`` are private
slowapi/FastAPI attributes this guard pins deliberately: a dependency upgrade
that renames or removes them makes this test fail loudly (AttributeError)
rather than pass vacuously. Known walker boundary (latent, documented):
non-``APIRoute`` shapes — ``Mount``ed sub-applications and
``WebSocketRoute``s — are outside the walker's view; slowapi's decorator
accepts websocket handlers, so a future limiter-limited websocket route
would need the walker extended before this guard covers it (no such route
exists today; the only ``Mount`` is StaticFiles).

The check cannot pass vacuously: the matched set must be non-empty, must
include the three auth routes (endpoint names ``login``, ``register``,
``refresh`` from ``app.api.routes.auth``) named by audit H-003, AND must
reconcile exactly with the limiter's own registries — every key in
``_route_limits`` (static limits) union ``_dynamic_route_limits`` (callable
limit values, which slowapi stores separately) must correspond to a mounted,
checked route, and vice versa. That catches a router dropped from
``include_router`` and puts the dynamic-limit registry under the walk.

Decorator ABSENCE is a different direction again: registry and mounted set
are both derived from the same decorators, so they shrink together when a
decorator is deleted and no equality between them can catch it. The
``_LIMITED_ENDPOINT_ROSTER`` below is the independent expectation (the L07
a11y-gate roster pattern): a lost ``@limiter.limit`` fails the roster
assertion naming the exact route.

Classification: NEW-SURFACE (no equivalent guard existed on the trace base;
the base signature before this file was a pytest exit 4 "file or directory
not found"). The guard is GREEN on the current tree — every limited route
stores a limiter-wrapped endpoint — and must STAY green after any future
change.
"""

import os
import sys
import unittest

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies (CI installs requirements-ci.txt)
try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow  # noqa: F401
except ImportError:
    import types

    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    import types

    _unstructured = types.ModuleType("unstructured")
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType("unstructured.partition")
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType("unstructured.partition.auto")
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    sys.modules["unstructured"] = _unstructured
    sys.modules["unstructured.partition"] = _unstructured.partition
    sys.modules["unstructured.partition.auto"] = _unstructured.partition.auto

from fastapi.routing import APIRoute  # noqa: E402

from app.limiter import limiter  # noqa: E402
from app.main import app  # noqa: E402

# The three historically-defective auth routes named by audit H-003; requiring
# them in the matched set is what makes an empty match set fail the guard.
_AUTH_ROUTE_ENDPOINT_NAMES = ("login", "register", "refresh")
_AUTH_ROUTE_MODULE = "app.api.routes.auth"

# The policy roster: every endpoint that MUST carry a limiter registration,
# captured from the review-time census (43 registrations across 12 route
# modules). The registry and the mounted walk are BOTH derived from the same
# decorators, so they shrink together when a decorator is deleted — the
# reconciliation below cannot see that direction. The roster is the
# independent expectation that catches it: a lost ``@limiter.limit`` leaves
# the endpoint's key out of ``matched_keys`` and fails the roster assertion
# naming the exact route (the L07 a11y-gate roster pattern). Adding a new
# limited route requires adding its name here — a deliberate, reviewable
# act; the size floor keeps the roster itself from being gutted silently.
_LIMITED_ENDPOINT_ROSTER = frozenset(
    {
        "app.api.routes.auth.change_password",
        "app.api.routes.auth.login",
        "app.api.routes.auth.refresh",
        "app.api.routes.auth.register",
        "app.api.routes.canvas.create_session_artifact",
        "app.api.routes.canvas.edit_canvas_range",
        "app.api.routes.canvas.restore_canvas_version",
        "app.api.routes.canvas.save_canvas_version",
        "app.api.routes.chat.add_message",
        "app.api.routes.chat.add_messages_batch",
        "app.api.routes.chat.chat",
        "app.api.routes.chat.chat_stream",
        "app.api.routes.chat.create_session",
        "app.api.routes.chat.delete_session",
        "app.api.routes.chat.fork_session",
        "app.api.routes.chat.set_message_feedback",
        "app.api.routes.chat.truncate_session_messages",
        "app.api.routes.chat.update_session",
        "app.api.routes.documents.cancel_document_ingest",
        "app.api.routes.documents.get_reindex_job_status",
        "app.api.routes.documents.reindex_documents",
        "app.api.routes.documents.retry_document",
        "app.api.routes.documents.retry_failed_chunks",
        "app.api.routes.draft_room.compile_draft",
        "app.api.routes.draft_room.promote_draft_source",
        "app.api.routes.draft_room.retry_draft_job",
        "app.api.routes.draft_room.upload_draft_input",
        "app.api.routes.folders.move_documents",
        "app.api.routes.health.health_check",
        "app.api.routes.health.llm_mode_health",
        "app.api.routes.memories.create_memory",
        "app.api.routes.memories.delete_memory",
        "app.api.routes.memories.update_memory",
        "app.api.routes.onboarding.dismiss_checklist",
        "app.api.routes.onboarding.mark_citation_opened",
        "app.api.routes.quality_reports.compare_eval_case",
        "app.api.routes.quality_reports.convert_report_to_eval_case",
        "app.api.routes.quality_reports.submit_quality_report",
        "app.api.routes.search.search",
        "app.api.routes.settings.post_settings",
        "app.api.routes.settings.probe_model_endpoint",
        "app.api.routes.settings.put_settings",
        "app.api.routes.vaults.create_vault",
    }
)


def _iter_api_routes(routes):
    """Recursively yield every APIRoute mounted anywhere on the application.

    Top-level ``app.routes`` mostly holds lazy ``_IncludedRouter`` wrappers
    (one per ``app.include_router`` call); each wrapper exposes the router it
    wraps as ``original_router``, whose ``.routes`` carry the real APIRoute
    objects. Bare APIRoutes (routes added directly on the app) are yielded as
    is. Anything else without an ``original_router`` (Mounts, hosts, ...) is
    skipped — it cannot carry a limiter-decorated endpoint.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
            continue
        original_router = getattr(route, "original_router", None)
        if original_router is not None and hasattr(original_router, "routes"):
            yield from _iter_api_routes(original_router.routes)


class TestLimiterRouteIntrospection(unittest.TestCase):
    """AC8: every route the limiter knows about stores the wrapped endpoint."""

    def test_every_limited_route_endpoint_is_limiter_wrapped(self):
        # The census protects PRODUCTION routes: co-run test fixtures
        # decorate locally-defined functions with the shared limiter (e.g.
        # test_chat_fork_rate_limit_compat's dummy_route), registering keys
        # like "test_chat_fork_rate_limit_compat.dummy_route" that can never
        # be mounted on the app — noise here, not lost enforcement. Every
        # production route lives in the app package, so scope the census to
        # app.* registrations.
        route_limits = {
            key: value
            for key, value in limiter._route_limits.items()
            if key.startswith("app.")
        }
        self.assertTrue(
            route_limits,
            "limiter._route_limits is empty — no @limiter.limit registrations "
            "were found at import time, so the guard has nothing to check",
        )
        # The limiter decorator registers each RAW handler it received, keyed
        # by "module.funcname"; if the router ever stores that raw handler
        # itself (the broken decorator order), the stored endpoint IS one of
        # these objects and enforcement can never fire.
        marked_raw_handlers = {
            fn
            for fns in limiter._Limiter__marked_for_limiting.values()
            for fn in fns
        }

        unwrapped = []
        raw_stored = []
        matched_keys = set()
        for route in _iter_api_routes(app.routes):
            endpoint = route.endpoint
            endpoint_key = f"{endpoint.__module__}.{endpoint.__name__}"
            if endpoint_key not in route_limits:
                continue
            matched_keys.add(endpoint_key)
            if not hasattr(endpoint, "__wrapped__"):
                unwrapped.append(f"{endpoint_key} ({getattr(route, 'path', '?')})")
            if endpoint in marked_raw_handlers:
                raw_stored.append(f"{endpoint_key} ({getattr(route, 'path', '?')})")

        # (1) Every matched route must be the limiter-WRAPPED endpoint. slowapi's
        # limit decorator applies functools.wraps, so the wrapper preserves
        # __name__/__module__ while carrying __wrapped__ — its absence means the
        # router stored an unwrapped handler and the limit can never fire.
        self.assertEqual(
            unwrapped,
            [],
            "limited routes whose router stores an UNWRAPPED endpoint (limiter "
            "limit silently never fires — decorator order must be "
            "@router.<method> outermost, @limiter.limit below): "
            + "; ".join(unwrapped),
        )

        # (1b) The stored endpoint must not BE a raw handler the limiter
        # decorator registered — the direct identity signal of the broken
        # order, independent of any intermediate __wrapped__-carrying wrapper.
        self.assertEqual(
            raw_stored,
            [],
            "limited routes whose router stores the RAW handler object the "
            "limiter decorated (broken decorator order; the limit check "
            "inside the wrapper never executes): " + "; ".join(raw_stored),
        )

        # (2) NON-VACUOUS: the matched set must include the three auth routes
        # from issue #659/#720, so an empty or gutted match set cannot pass.
        for endpoint_name in _AUTH_ROUTE_ENDPOINT_NAMES:
            expected_key = f"{_AUTH_ROUTE_MODULE}.{endpoint_name}"
            self.assertIn(
                expected_key,
                matched_keys,
                f"expected limited route {expected_key} to be mounted and "
                f"matched by the guard (matched {len(matched_keys)} limited "
                f"routes); the guard is vacuous without the auth trio",
            )

        # (3) The matched set must be non-empty independent of the auth trio.
        self.assertTrue(
            matched_keys,
            "no mounted APIRoute matched any limiter._route_limits key — the "
            "introspection walker or the limiter registration is broken",
        )

        # (4) REGISTRY RECONCILIATION, both directions: every key the limiter
        # registered — static limits in _route_limits AND callable limit
        # values in _dynamic_route_limits (slowapi stores them separately) —
        # must correspond to a mounted route the walk above checked. A route
        # that LOSES its @limiter.limit decorator leaves its key unmatched
        # and fails here (decorator absence is a silent-enforcement failure
        # exactly like a wrong order); a registration whose handler is never
        # mounted fails too. Today both dynamic keys and unmatched keys are
        # empty; this assertion keeps them that way.
        registered_keys = set(route_limits) | {
            key
            for key in (getattr(limiter, "_dynamic_route_limits", {}) or {})
            if key.startswith("app.")
        }
        self.assertEqual(
            registered_keys,
            matched_keys,
            "limiter registrations and mounted limited routes diverged "
            f"(registered-but-not-mounted: {sorted(registered_keys - matched_keys)}; "
            f"mounted-but-not-registered: {sorted(matched_keys - registered_keys)}) — "
            "a lost @limiter.limit decorator or a dropped router removes "
            "enforcement silently",
        )

        # (5) ROSTER: the independent expectation of which endpoints must be
        # limited. Registry and mounted set shrink TOGETHER when a decorator
        # is deleted (both derive from the same decorators), so assertion (4)
        # cannot see that direction — the roster can. A lost decorator fails
        # here naming the exact route.
        self.assertGreaterEqual(
            len(_LIMITED_ENDPOINT_ROSTER),
            40,
            "the limited-endpoint roster itself shrank below the census "
            "floor — roster entries must never be removed to reach green",
        )
        unrostered_lost = _LIMITED_ENDPOINT_ROSTER - matched_keys
        self.assertEqual(
            unrostered_lost,
            set(),
            "rostered limited endpoints no longer registered+mounted (rate "
            "limit silently removed): " + "; ".join(sorted(unrostered_lost)),
        )


if __name__ == "__main__":
    unittest.main()
