"""Guardrail for the issue #551 defect class (recurrence sweep rung).

Class: an expensive/provider-probing route ships without an auth dependency
or a ``@limiter.limit`` decorator while its route family's conventions
require both — exactly how ``GET /api/health?deep=true`` and
``GET /api/llm-health/modes`` stayed anonymously probeable until #551.

This guardrail is source-level and module-scoped: every route registered in
``app/api/routes/health.py`` whose handler declares a provider-checker
dependency (``get_llm_health_checker`` / ``get_model_checker``) must

1. sit under a ``@limiter.limit(...)`` decorator, and
2. declare a health-probe auth dependency (``require_health_probe_auth`` or
   the deep-only ``require_deep_health_probe_auth`` wrapper) in its
   signature.

A future route added to ``health.py`` that probes providers without the gate
fails this file, independent of the behavioral regression family in
``test_health_deep_auth.py``. The runtime contract itself remains pinned by
that file; this module exists so the PATTERN cannot silently regress even if
behavioral tests are renamed or moved.
"""

import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies (CI installs requirements-ci.txt) before
# app.main is imported by the runtime half of this file.
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

from fastapi.testclient import TestClient

from app.api.routes import health as health_module

_CHECKER_DEPS = ("get_llm_health_checker", "get_model_checker")
_AUTH_DEPS = ("require_health_probe_auth", "require_deep_health_probe_auth")


def _walk_name(node: ast.AST) -> str:
    """Dotted-name tail of a Name/Attribute/Call node (best effort)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _walk_name(node.func)
    return ""


def _param_default_names(func: ast.AsyncFunctionDef) -> set[str]:
    """Names referenced inside parameter defaults (Depends(...) contents)."""
    names: set[str] = set()
    for default in list(func.args.defaults) + [
        d for d in func.args.kw_defaults if d is not None
    ]:
        for sub in ast.walk(default):
            if isinstance(sub, ast.Name):
                names.add(sub.id)
    return names


def _probe_routes(source: str) -> list[ast.AsyncFunctionDef]:
    """AST-walk every async function whose parameters declare a provider-
    checker dependency. AST (not text) so blank lines, docstrings, line
    wraps, and refactors cannot silently hide a route from this guardrail
    (PR #606 review C-551-003 + concurrent-session execution proof)."""
    tree = ast.parse(source)
    routes: list[ast.AsyncFunctionDef] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        default_names = _param_default_names(node)
        if any(dep in default_names for dep in _CHECKER_DEPS):
            routes.append(node)
    return routes


class TestHealthRouteGuardrail(unittest.TestCase):
    """Every provider-probing route in health.py is auth-gated and limited."""

    def setUp(self):
        with open(health_module.__file__, "r", encoding="utf-8") as fh:
            self.source = fh.read()

    def test_probing_routes_are_gated_and_limited(self):
        offenders = []
        routes = _probe_routes(self.source)
        for func in routes:
            name = func.name
            limited = any(
                _walk_name(d).endswith("limit") for d in func.decorator_list
            )
            if not limited:
                offenders.append(f"{name}: missing @limiter.limit decorator")
            default_names = _param_default_names(func)
            if not any(dep in default_names for dep in _AUTH_DEPS):
                offenders.append(
                    f"{name}: missing require_health_probe_auth/"
                    f"require_deep_health_probe_auth dependency"
                )

        self.assertGreaterEqual(
            len(routes), 2,
            "expected at least the two known provider-probing routes "
            "(health_check, llm_mode_health); the scanner found "
            f"{len(routes)} - if health.py was restructured, update this "
            "guardrail deliberately, do not weaken it",
        )
        self.assertEqual(
            offenders, [],
            "provider-probing health routes without the #551 gate: "
            + "; ".join(offenders),
        )

    def test_limiter_is_imported_in_health_module(self):
        """The decorator is dead code unless the module binds the limiter."""
        self.assertIn("from app.limiter import limiter", self.source)


class TestDeepAliasSpellings(unittest.TestCase):
    """Every query spelling pydantic parses as ``deep=true`` is gated.

    Final-critic round 1 found the bypass: pydantic's bool coercion accepts
    ``t``/``y`` (any case) which a hand-rolled ``{"true","1","yes","on"}``
    set missed, so ``GET /api/health?deep=t`` ran the full provider sweep
    unauthenticated AND limiter-exempt. The gate now parses with FastAPI
    (route dependency) and pydantic TypeAdapter(bool) (exempt_when); these
    tests pin the parity so the sets can never drift apart again.
    """

    ALIASES = ("t", "y", "T", "Y", "TRUE", "True", "on", "yes", "1")

    def setUp(self):
        from unittest.mock import AsyncMock, MagicMock

        from app.api.deps import (
            get_llm_health_checker,
            get_model_checker,
        )
        from app.api.routes import health as health_module
        from app.main import app

        self._app = app
        self._health = health_module
        self._orig_cache = health_module._deep_cache
        health_module._deep_cache = {"services": None, "ts": 0.0}
        self._llm = MagicMock()
        self._llm.check_all = AsyncMock(return_value={"ok": True})
        self._llm.check_chat_modes = AsyncMock(
            return_value={"thinking": False, "instant": False}
        )
        self._model = MagicMock()
        self._model.check_models = AsyncMock(return_value={})
        app.dependency_overrides[get_llm_health_checker] = lambda: self._llm
        app.dependency_overrides[get_model_checker] = lambda: self._model
        self.client = TestClient(app)

    def tearDown(self):
        from app.api.deps import (
            get_llm_health_checker,
            get_model_checker,
        )

        self._app.dependency_overrides.pop(get_llm_health_checker, None)
        self._app.dependency_overrides.pop(get_model_checker, None)
        self._health._deep_cache = self._orig_cache

    def test_unauthenticated_deep_aliases_are_401_with_zero_probes(self):
        for alias in self.ALIASES:
            self._llm.check_all.reset_mock()
            response = self.client.get(f"/api/health?deep={alias}")
            self.assertEqual(
                response.status_code, 401,
                f"deep spelling {alias!r} must be gated like deep=true, got "
                f"{response.status_code}",
            )
            self.assertEqual(
                self._llm.check_all.call_count, 0,
                f"deep spelling {alias!r} must not invoke the provider probe",
            )

    def test_authenticated_deep_aliases_are_served(self):
        from app.api.deps import get_current_active_user

        self._app.dependency_overrides[get_current_active_user] = lambda: {
            "id": 0,
            "username": "admin",
            "role": "superadmin",
            "is_active": 1,
            "must_change_password": 0,
        }
        self.addCleanup(self._app.dependency_overrides.pop, get_current_active_user, None)
        for alias in self.ALIASES:
            self._llm.check_all.reset_mock()
            response = self.client.get(f"/api/health?deep={alias}")
            self.assertEqual(
                response.status_code, 200,
                f"authenticated deep spelling {alias!r} must be served, got "
                f"{response.status_code}",
            )
            self.assertEqual(
                self._llm.check_all.call_count, 1,
                f"authenticated deep spelling {alias!r} must run the probe "
                f"(parity with deep=true)",
            )


class TestShallowPollNotRateLimited(unittest.TestCase):
    """Runtime half of the #551 shallow contract: anonymous shallow polls are
    NOT rate-limited (deep-only enforcement), so NAT-shared egress IPs cannot
    429 the frontend heartbeat. Found by the Phase 4.5 review: a route-level
    decorator capped shallow polls at health_probe_rate_limit per IP."""

    def setUp(self):
        from unittest.mock import AsyncMock, MagicMock

        from app.api.deps import (
            get_llm_health_checker,
            get_model_checker,
        )
        from app.api.routes import health as health_module
        from app.limiter import limiter
        from app.main import app

        self._app = app
        self._limiter = limiter
        self._health = health_module
        self._orig_cache = health_module._deep_cache
        health_module._deep_cache = {"services": None, "ts": 0.0}
        self._llm = MagicMock()
        self._llm.check_all = AsyncMock(return_value={"ok": True})
        self._llm.check_chat_modes = AsyncMock(
            return_value={"thinking": False, "instant": False}
        )
        self._model = MagicMock()
        self._model.check_models = AsyncMock(return_value={})
        app.dependency_overrides[get_llm_health_checker] = lambda: self._llm
        app.dependency_overrides[get_model_checker] = lambda: self._model
        self._addCleanup_pop = [
            get_llm_health_checker,
            get_model_checker,
        ]
        self._limiter._storage.reset()
        self.client = TestClient(app)

    def tearDown(self):
        for dep in self._addCleanup_pop:
            self._app.dependency_overrides.pop(dep, None)
        self._limiter._storage.reset()
        self._health._deep_cache = self._orig_cache

    def test_anonymous_shallow_polls_never_429(self):
        limit_count = int(
            str(getattr(self._health.settings, "health_probe_rate_limit", "30/minute")).split("/")[0]
        )
        for i in range(limit_count + 5):
            response = self.client.get("/api/health")
            self.assertEqual(
                response.status_code, 200,
                f"anonymous shallow poll {i + 1} returned "
                f"{response.status_code}: shallow polls must stay "
                f"unauthenticated-open AND unlimited (issue #551)",
            )


if __name__ == "__main__":
    unittest.main()


class TestModesRouteRateLimit(unittest.TestCase):
    """The /llm-health/modes limit has runtime proof (review PRR-004/PRR-010):
    the 429 boundary fires on excess authenticated calls, and rapid
    unauthenticated calls are 401-rejected before the limiter is consulted."""

    def setUp(self):
        from unittest.mock import AsyncMock, MagicMock

        from app.api.deps import (
            get_llm_health_checker,
            get_model_checker,
        )
        from app.api.routes import health as health_module
        from app.config import settings
        from app.limiter import limiter
        from app.main import app

        self._app = app
        self._limiter = limiter
        self._settings = settings
        self._deps = (
            get_llm_health_checker,
            get_model_checker,
        )
        self._orig_cache = health_module._deep_cache
        health_module._deep_cache = {"services": None, "ts": 0.0}
        llm = MagicMock()
        llm.check_all = AsyncMock(return_value={"ok": True})
        llm.check_chat_modes = AsyncMock(
            return_value={"thinking": True, "instant": True}
        )
        model = MagicMock()
        model.check_models = AsyncMock(return_value={})
        app.dependency_overrides[get_llm_health_checker] = lambda: llm
        app.dependency_overrides[get_model_checker] = lambda: model
        limiter._storage.reset()
        self.addCleanup(self._teardown)
        self.client = TestClient(app)

    def _override_user(self):
        """Authenticated-caller override for the 429-boundary test only, so
        the rapid-unauthenticated test below runs with no user override."""
        from app.api.deps import get_current_active_user

        self._app.dependency_overrides[get_current_active_user] = lambda: {
            "id": 0,
            "username": "admin",
            "role": "superadmin",
            "is_active": 1,
            "must_change_password": 0,
        }
        self.addCleanup(
            self._app.dependency_overrides.pop, get_current_active_user, None
        )

    def _teardown(self):
        from app.api.deps import (
            get_llm_health_checker,
            get_model_checker,
        )
        from app.limiter import limiter

        for dep in self._deps:
            self._app.dependency_overrides.pop(dep, None)
        limiter._storage.reset()

    def _limit_count(self) -> int:
        spec = getattr(self._settings, "health_probe_rate_limit", "30/minute")
        return int(str(spec).split("/")[0])

    def test_modes_429_on_excess_authenticated_calls(self):
        self._override_user()
        limit_count = self._limit_count()
        for i in range(limit_count):
            response = self.client.get("/api/llm-health/modes")
            self.assertNotEqual(
                response.status_code, 429,
                f"modes call {i + 1}/{limit_count} 429'd too early",
            )
        response = self.client.get("/api/llm-health/modes")
        self.assertEqual(
            response.status_code, 429,
            f"modes call {limit_count + 1} must be rate limited, got "
            f"{response.status_code}",
        )

    def test_rapid_unauthenticated_modes_calls_rejected(self):
        for i in range(2):
            response = self.client.get("/api/llm-health/modes")
            self.assertEqual(
                response.status_code, 401,
                f"rapid unauthenticated modes call {i + 1}/2 must be 401, "
                f"got {response.status_code}",
            )


class TestLazyDbCheckout(unittest.TestCase):
    """PRR-001 guard: the health-probe auth deps must acquire a pooled DB
    connection ONLY on the user-resolution path. Anonymous shallow polls and
    API-key-authenticated deep probes touch zero pool connections (#549 C02:
    the shallow heartbeat is DB-free)."""

    def setUp(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.api.deps import (
            get_llm_health_checker,
            get_model_checker,
        )
        from app.api.routes import health as health_module
        from app.config import settings
        from app.main import app

        self._app = app
        self._health = health_module
        self._orig_cache = health_module._deep_cache
        health_module._deep_cache = {"services": None, "ts": 0.0}
        llm = MagicMock()
        llm.check_all = AsyncMock(return_value={"ok": True})
        llm.check_chat_modes = AsyncMock(
            return_value={"thinking": False, "instant": False}
        )
        model = MagicMock()
        model.check_models = AsyncMock(return_value={})
        app.dependency_overrides[get_llm_health_checker] = lambda: llm
        app.dependency_overrides[get_model_checker] = lambda: model
        self._llm = llm

        self._pool = MagicMock()
        self._pool.get_connection = MagicMock(return_value=MagicMock())
        self._pool_patcher = patch("app.api.deps.get_pool", return_value=self._pool)
        self._pool_patcher.start()
        self.addCleanup(self._pool_patcher.stop)

        self._orig_key = settings.health_check_api_key
        self.addCleanup(setattr, settings, "health_check_api_key", self._orig_key)
        settings.health_check_api_key = "guardrail-monitor-key"

        from app.limiter import limiter

        limiter._storage.reset()
        self.addCleanup(limiter._storage.reset)
        self.client = TestClient(app)

    def tearDown(self):
        from app.api.deps import get_llm_health_checker, get_model_checker

        self._app.dependency_overrides.pop(get_llm_health_checker, None)
        self._app.dependency_overrides.pop(get_model_checker, None)
        self._health._deep_cache = self._orig_cache

    def test_anonymous_shallow_poll_never_touches_the_pool(self):
        self.client.get("/api/health")
        self.assertEqual(
            self._pool.get_connection.call_count, 0,
            "anonymous shallow heartbeat must be DB-free (#549 C02)",
        )

    def test_key_authed_deep_probe_never_touches_the_pool(self):
        response = self.client.get(
            "/api/health?deep=true", headers={"X-API-Key": "guardrail-monitor-key"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self._pool.get_connection.call_count, 0,
            "the X-API-Key path must authenticate without a pool checkout",
        )

    def test_user_authed_deep_probe_checks_out_and_releases_once(self):
        response = self.client.get("/api/health?deep=true")
        self.assertEqual(response.status_code, 401)  # anonymous: 401 after release
        self.assertEqual(self._pool.get_connection.call_count, 1)
        self.assertEqual(self._pool.release_connection.call_count, 1)
