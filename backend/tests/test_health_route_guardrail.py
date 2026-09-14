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

import os
import re
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

_DECORATOR_OR_DEF = re.compile(r"^(@[\w.]+|async def |def )")


def _route_blocks(source: str) -> list[tuple[str, str]]:
    """Split the module source into (header, body) route blocks.

    A block starts at a ``@router.<method>(`` decorator line and runs to the
    next blank-line-separated decorator group or end of file. The header is
    the decorator stack (all leading @ lines); the body is the ``async def``
    signature onward.
    """
    lines = source.splitlines()
    blocks: list[tuple[str, str]] = []
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("@router."):
            header_lines = []
            while i < len(lines) and lines[i].lstrip().startswith("@"):
                header_lines.append(lines[i].strip())
                i += 1
            body_lines = []
            while i < len(lines) and lines[i].strip() and not lines[i].lstrip().startswith("@router."):
                if _DECORATOR_OR_DEF.match(lines[i]) or body_lines:
                    body_lines.append(lines[i])
                i += 1
            blocks.append(("\n".join(header_lines), "\n".join(body_lines)))
        else:
            i += 1
    return blocks


def _block_name(body: str) -> str:
    match = re.search(r"^(?:async )?def (\w+)", body, re.MULTILINE)
    return match.group(1) if match else "<unknown>"


class TestHealthRouteGuardrail(unittest.TestCase):
    """Every provider-probing route in health.py is auth-gated and limited."""

    def setUp(self):
        with open(health_module.__file__, "r", encoding="utf-8") as fh:
            self.source = fh.read()

    def test_probing_routes_are_gated_and_limited(self):
        offenders = []
        seen_probing = 0
        for header, body in _route_blocks(self.source):
            if not any(dep in body for dep in _CHECKER_DEPS):
                continue
            seen_probing += 1
            name = _block_name(body)
            if "@limiter.limit(" not in header:
                offenders.append(f"{name}: missing @limiter.limit decorator")
            if not any(dep in body for dep in _AUTH_DEPS):
                offenders.append(
                    f"{name}: missing require_health_probe_auth/"
                    f"require_deep_health_probe_auth dependency"
                )

        self.assertGreaterEqual(
            seen_probing, 2,
            "expected at least the two known provider-probing routes "
            "(health_check, llm_mode_health); the scanner found "
            f"{seen_probing} - if health.py was restructured, update this "
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
