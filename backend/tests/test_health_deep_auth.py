"""Issue #551 acceptance check — C12: gated, rate-limited deep health probes.

The deep health probe (``GET /api/health?deep=true``, handler
``health_check`` in ``app/api/routes/health.py``) and the LLM mode probe
(``GET /api/llm-health/modes``, handler ``llm_mode_health``) currently serve
unauthenticated requests and run the real LLM/embedding provider checkers
with no rate limit. The fix contract pinned here:

- A new auth dependency protects BOTH routes. It accepts either an
  authenticated user via the existing ``get_current_active_user`` mechanism
  (tests override it, the standard pattern from ``test_api_routes.py``) or
  header ``X-API-Key: <settings.health_check_api_key>`` when that setting is
  non-empty (monitoring credential; fail-closed 401 when unset or wrong).
- Unauthenticated calls return 401 and the LLM/model checkers are NEVER
  invoked (no provider probes).
- Authenticated calls keep today's 200 behavior (deep payload with
  "llm"/"models" keys on deep=true; ``{"thinking":..,"instant":..}`` on modes).
- A new Settings field ``health_probe_rate_limit`` (default "30/minute")
  decorates both routes via ``@limiter.limit(...)``; the existing limiter
  whitelist (``X-API-Key`` == ``settings.health_check_api_key``) exempts the
  key holder from the new limit.

Expected status at the PRE-FIX code (intentional — the discriminating checks
prove the fix is required):
  FAIL (RED): TestC12AuthGate.test_unauthenticated_deep_health_rejected
              TestC12AuthGate.test_unauthenticated_llm_modes_rejected
              TestC12AuthGate.test_rapid_unauthenticated_calls_both_rejected
              TestC12AuthGate.test_health_api_key_authenticates_both_routes
              TestC12RateLimit.test_limiter_blocks_rapid_authenticated_excess
  PASS (GREEN at base, PRESERVING):
              TestC12AuthGate.test_authenticated_deep_health_still_served
              TestC12RateLimit.test_health_key_whitelist_bypasses_new_limit
              TestC12PreserveShallow.test_shallow_health_and_healthz_stay_open

All deep/mode probes run against dependency-injected mock checkers
(``app.dependency_overrides``, exactly like ``test_api_routes.py``), so no
test performs real outbound provider calls; the C12 defect is encoded purely
as wire-level status assertions plus mock call counts. The route module's
cache globals are saved/reset around every test (mirroring
``test_issue494_ac29_health_cache_singleflight.py``) and every ``app.state``
attribute touched is snapshot/restored, so tests are order-independent.
"""

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

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

from fastapi.testclient import TestClient

from app.api.deps import (
    get_current_active_user,
    get_llm_health_checker,
    get_model_checker,
)
from app.api.routes import health as health_module
from app.config import settings
from app.main import app

LLM_CHECK_ALL_RESULT = {
    "ok": True,
    "embeddings": {"ok": True, "error": None},
    "chat": {"ok": True, "error": None},
    "error": None,
}
LLM_MODES_RESULT = {"thinking": True, "instant": False}
MODELS_CHECK_RESULT = {
    "embedding_model": {"available": True, "error": None},
    "chat_model": {"available": True, "error": None},
}

TEST_MONITORING_KEY = "issue551-test-monitoring-key"

# Every app.state attribute any test in this file may touch. `app` is a
# module-global shared by the whole suite, so setUp snapshots each attribute
# (including its ABSENCE) and tearDown restores it exactly (mirrors
# tests/test_health_readiness.py).
_STATE_ATTRS = (
    "vector_store",
    "db_pool",
    "embedding_service",
    "maintenance_service",
    "migrations_ok",
)


def _health_probe_limit_count() -> int:
    """Parse the health-probe limit spec ("<N>/minute") into N.

    The ``health_probe_rate_limit`` Settings field lands WITH the fix; until
    then fall back to the contract default "30/minute" so the RED comes from
    the missing 429 (route not limited at base), not an AttributeError.
    """
    spec = getattr(settings, "health_probe_rate_limit", "30/minute")
    return int(str(spec).split("/")[0])


def _reset_limiter_storage() -> None:
    """Clear the shared limiter's counters (works for memory:// and redis)."""
    from app.limiter import limiter

    limiter._storage.reset()


class _HealthAuthTestBase(unittest.TestCase):
    """Shared harness: TestClient(app), injected mock checkers, and strict
    snapshot/restore isolation for the route module's cache globals, every
    ``app.state`` attribute touched, and the dependency overrides installed."""

    def setUp(self) -> None:
        # --- save & reset the route module's cache globals ------------------
        self._orig_cache = health_module._deep_cache
        self._orig_in_flight = health_module._deep_refresh_in_flight
        self._orig_tasks = health_module._refresh_tasks
        health_module._deep_cache = {"services": None, "ts": 0.0}
        health_module._deep_refresh_in_flight = False
        health_module._refresh_tasks = set()

        # --- snapshot app.state (restored in tearDown) ----------------------
        self._state_snapshot = {
            name: (hasattr(app.state, name), getattr(app.state, name, None))
            for name in _STATE_ATTRS
        }

        # --- TestHealthEndpoint-style checker mocks --------------------------
        self.llm_checker = MagicMock()
        self.llm_checker.check_all = AsyncMock(return_value=LLM_CHECK_ALL_RESULT)
        self.llm_checker.check_chat_modes = AsyncMock(return_value=LLM_MODES_RESULT)
        self.model_checker = MagicMock()
        self.model_checker.check_models = AsyncMock(return_value=MODELS_CHECK_RESULT)

        # Deterministic vector store so the deep payload path never touches a
        # real LanceDB table another test may have left on the shared app.
        vector_store = MagicMock()
        vector_store.table.count_rows = AsyncMock(return_value=7)
        vector_store._get_expected_embedding_dim = AsyncMock(return_value=None)
        app.state.vector_store = vector_store

        app.dependency_overrides[get_llm_health_checker] = lambda: self.llm_checker
        app.dependency_overrides[get_model_checker] = lambda: self.model_checker
        # Defensive: never inherit a user-auth override leaked by an earlier
        # test in the same process (tests are order-independent).
        app.dependency_overrides.pop(get_current_active_user, None)

        self.client = TestClient(app)

    def tearDown(self) -> None:
        app.dependency_overrides.pop(get_llm_health_checker, None)
        app.dependency_overrides.pop(get_model_checker, None)
        app.dependency_overrides.pop(get_current_active_user, None)
        for name, (present, value) in self._state_snapshot.items():
            if present:
                setattr(app.state, name, value)
            elif hasattr(app.state, name):
                delattr(app.state, name)
        health_module._deep_cache = self._orig_cache
        health_module._deep_refresh_in_flight = self._orig_in_flight
        health_module._refresh_tasks = self._orig_tasks

    def _override_user(self) -> None:
        """Install the standard admin-user override (test_api_routes style)."""
        app.dependency_overrides[get_current_active_user] = lambda: {
            "id": 0, "username": "admin", "role": "superadmin",
            "is_active": 1, "must_change_password": 0,
        }


class TestC12AuthGate(_HealthAuthTestBase):
    """The deep health probe and the LLM mode probe require authentication."""

    def test_unauthenticated_deep_health_rejected(self):
        """No credentials + deep=true => 401 AND no provider probe at all.

        RED at base: the route is open, so the response is 200 and the deep
        collection runs (check_all called once).
        """
        response = self.client.get("/api/health?deep=true")

        self.assertEqual(
            response.status_code, 401,
            f"unauthenticated deep health must be rejected, got "
            f"{response.status_code}: {response.text[:200]}",
        )
        self.assertEqual(
            self.llm_checker.check_all.call_count, 0,
            "the LLM checker must NEVER be invoked for an unauthenticated "
            "deep health request (no real provider probes)",
        )

    def test_unauthenticated_llm_modes_rejected(self):
        """No credentials on /llm-health/modes => 401 AND no mode probe.

        RED at base: the route is open, so the response is 200 and
        check_chat_modes is called once.
        """
        response = self.client.get("/api/llm-health/modes")

        self.assertEqual(
            response.status_code, 401,
            f"unauthenticated LLM mode probe must be rejected, got "
            f"{response.status_code}: {response.text[:200]}",
        )
        self.assertEqual(
            self.llm_checker.check_chat_modes.call_count, 0,
            "check_chat_modes must NEVER be invoked for an unauthenticated "
            "LLM mode request (no real provider probes)",
        )

    def test_rapid_unauthenticated_calls_both_rejected(self):
        """Two immediate unauthenticated deep GETs => BOTH 401 (not 200, not
        429): the auth decision precedes the limiter.

        RED at base: both calls are served 200 by the open route.
        """
        for i in range(2):
            response = self.client.get("/api/health?deep=true")
            self.assertEqual(
                response.status_code, 401,
                f"unauthenticated deep health call {i + 1}/2 must be 401 "
                f"(auth precedes the limiter), got {response.status_code}",
            )

    def test_authenticated_deep_health_still_served(self):
        """With an authenticated user, deep=true keeps today's 200 payload.

        PRESERVING: GREEN at base (route open) and after the fix (user auth).
        """
        self._override_user()
        response = self.client.get("/api/health?deep=true")

        self.assertEqual(response.status_code, 200, response.text[:200])
        data = response.json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("llm", data, "deep payload must keep the llm section")
        self.assertIn("models", data, "deep payload must keep the models section")
        self.assertEqual(
            self.llm_checker.check_all.call_count, 1,
            "authenticated deep health runs the (mocked) deep collection once",
        )

    def test_health_api_key_authenticates_both_routes(self):
        """X-API-Key == settings.health_check_api_key authenticates both
        routes; a WRONG key is rejected 401 (fail-closed).

        RED at base via the wrong-key half: there is no auth at all, so the
        wrong-key calls are also served 200.
        """
        original_key = settings.health_check_api_key
        self.addCleanup(setattr, settings, "health_check_api_key", original_key)
        settings.health_check_api_key = TEST_MONITORING_KEY

        # Right key (no user auth): both routes serve their normal payloads.
        deep_resp = self.client.get(
            "/api/health?deep=true", headers={"X-API-Key": TEST_MONITORING_KEY}
        )
        self.assertEqual(deep_resp.status_code, 200, deep_resp.text[:200])
        modes_resp = self.client.get(
            "/api/llm-health/modes", headers={"X-API-Key": TEST_MONITORING_KEY}
        )
        self.assertEqual(modes_resp.status_code, 200, modes_resp.text[:200])
        self.assertEqual(modes_resp.json(), LLM_MODES_RESULT)

        # Wrong key: fail-closed 401 on BOTH routes.
        wrong_deep = self.client.get(
            "/api/health?deep=true", headers={"X-API-Key": "issue551-wrong-key"}
        )
        self.assertEqual(
            wrong_deep.status_code, 401,
            f"a wrong X-API-Key must not authenticate the deep probe, got "
            f"{wrong_deep.status_code}",
        )
        wrong_modes = self.client.get(
            "/api/llm-health/modes", headers={"X-API-Key": "issue551-wrong-key"}
        )
        self.assertEqual(
            wrong_modes.status_code, 401,
            f"a wrong X-API-Key must not authenticate the mode probe, got "
            f"{wrong_modes.status_code}",
        )


class TestC12RateLimit(_HealthAuthTestBase):
    """The new health-probe rate limit and its existing key whitelist."""

    def test_limiter_blocks_rapid_authenticated_excess(self):
        """N authenticated deep probes pass, call N+1 within the window is 429.

        RED at base: the route carries no @limiter.limit, so the (N+1)th call
        is served 200 instead of 429.
        """
        _reset_limiter_storage()
        self.addCleanup(_reset_limiter_storage)

        limit_count = _health_probe_limit_count()
        self._override_user()

        for i in range(limit_count):
            response = self.client.get("/api/health?deep=true")
            self.assertNotEqual(
                response.status_code, 429,
                f"authenticated deep call {i + 1}/{limit_count} returned 429 "
                f"too early",
            )
        response = self.client.get("/api/health?deep=true")
        self.assertEqual(
            response.status_code, 429,
            f"deep call {limit_count + 1} within the window should be rate "
            f"limited (health_probe_rate_limit), got {response.status_code}",
        )

    def test_health_key_whitelist_bypasses_new_limit(self):
        """The monitoring key holder is exempt from the new limit: limit+3
        rapid deep probes all return 200 (also proves the key authenticates).

        PRESERVING at base: no auth and no limit — every call is 200.
        """
        _reset_limiter_storage()
        self.addCleanup(_reset_limiter_storage)

        original_key = settings.health_check_api_key
        self.addCleanup(setattr, settings, "health_check_api_key", original_key)
        settings.health_check_api_key = TEST_MONITORING_KEY

        limit_count = _health_probe_limit_count()
        headers = {"X-API-Key": TEST_MONITORING_KEY}
        for i in range(limit_count + 3):
            response = self.client.get("/api/health?deep=true", headers=headers)
            self.assertEqual(
                response.status_code, 200,
                f"whitelisted deep call {i + 1}/{limit_count + 3} must bypass "
                f"the new limit (and authenticate), got {response.status_code}",
            )


class TestC12PreserveShallow(_HealthAuthTestBase):
    """The shallow poll and the readiness probe stay unauthenticated-open."""

    def test_shallow_health_and_healthz_stay_open(self):
        """GET /api/health without deep and GET /api/healthz remain public.

        PRESERVING: GREEN at base and after the fix (the gate protects only
        the expensive deep probe / mode probe, not the cheap polls).
        """
        # Healthy readiness state so /api/healthz answers 200: plain truthy
        # objects satisfy the presence checks (db_pool without a
        # recent_capacity_wait attribute is simply not consulted further),
        # migrations_ok=True, no maintenance service, and the setUp vector
        # store mock (truthy .table).
        app.state.db_pool = object()
        app.state.embedding_service = object()
        app.state.migrations_ok = True
        app.state.maintenance_service = None

        health_resp = self.client.get("/api/health")
        self.assertEqual(health_resp.status_code, 200, health_resp.text[:200])
        self.assertEqual(health_resp.json()["status"], "ok")

        healthz_resp = self.client.get("/api/healthz")
        self.assertEqual(
            healthz_resp.status_code, 200,
            f"the readiness probe must stay unauthenticated-open, got "
            f"{healthz_resp.status_code}: {healthz_resp.text[:200]}",
        )


if __name__ == "__main__":
    unittest.main()
