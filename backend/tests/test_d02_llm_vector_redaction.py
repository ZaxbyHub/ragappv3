"""Issue #672 acceptance checks (D-02, llm/vector sections): deep-health
infrastructure-error disclosure.

Companion to ``test_d02_deep_health_redaction.py`` (models section, AC9-11 +
AC15). The shipped fix redacts, for BELOW-ADMIN principals only, the
identifying error text carried in the deep-health response's:

- ``llm`` section — ``embeddings.error`` / ``chat.error`` / the joined
  top-level ``error`` string produced by ``LLMHealthChecker.check_all``
  (provider URLs, hostnames) — covered here via the
  ``get_llm_health_checker`` dependency override, mocked in the EXACT shape
  the real producer emits (``llm_health.py`` check_all L179-195: ``ok`` is
  False only when a sub-leg fails, and the top-level ``error`` is ALWAYS the
  ``"; "``-join of every failing sub-error), with the distinctive seed in
  ALL THREE keys;
- ``vector_store`` section — ``error`` (probe/transport failures) and
  ``stale_embeddings_detail`` (stored-dim vs configured-dim mismatch) —
  covered here via (a) a vector-store mock whose ``table.count_rows``
  raises, steering the branch into ``{"ok": False, "error": str(exc)}``, and
  (b) a second leg pair where ``_get_expected_embedding_dim`` returns 64
  against ``settings.embedding_dim = 65``, steering the
  ``stale_embeddings_detail`` branch in ``_collect_deep_state``.

Admins/superadmins and the X-API-Key "monitor" principal keep everything.

Expected status at the PRE-FIX code (intentional — the discriminating checks
prove the fix is required):

  FAIL (RED at base, each "AssertionError: True is not false"):
      TestViewerDeepHealthOmitsInfraErrorDetail::
          test_viewer_deep_health_omits_llm_error_detail
      TestViewerDeepHealthOmitsInfraErrorDetail::
          test_viewer_deep_health_omits_vector_store_error_detail
  PASS (GREEN at base and after the fix, PRESERVING, in-file only — the
  frozen monitor contract is pinned by C15 on the models section):
      TestMonitorKeyKeepsLlmAndVectorDetail::
          test_monitor_key_principal_keeps_llm_and_vector_detail

Harness: real app via TestClient with the REAL ``ModelChecker.check_models``
(never stubbed) kept in its canonical not_configured state — every model
URL/name setting is blanked so no model probe fires; the module-level
``httpx`` reference in ``app.services.model_checker`` is still swapped for
the MockTransport-backed shim purely as a defensive net (any probe that ever
did fire would answer from the canned handler, never real network), and
``assert_url_safe`` is no-op'd the same way. The LLM checker is
dependency-mocked (that is the surface under test), the vector store is a
deterministic mock, and the route module's cache globals, app.state
attributes, dependency overrides, and settings fields are all
snapshot/restored (mirroring ``test_health_deep_auth.py``). Each viewer test
first proves with a superadmin leg that the seed IS in the response text,
then re-issues the request as a viewer over a reset deep cache and asserts
the seed is gone — pinning any failure to the principal, not the harness.
"""

import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies (CI installs requirements-ci.txt)
try:
    import lancedb  # noqa: F401
except ImportError:
    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow  # noqa: F401
except ImportError:
    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    _unstructured = types.ModuleType("unstructured")
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType("unstructured.partition")
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType("unstructured.partition.auto")
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    sys.modules["unstructured"] = _unstructured
    sys.modules["unstructured.partition"] = _unstructured.partition
    sys.modules["unstructured.partition.auto"] = _unstructured.partition.auto

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api.deps import (  # noqa: E402
    get_current_active_user,
    get_llm_health_checker,
    get_model_checker,
)
from app.api.routes import health as health_module  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.services import model_checker as model_checker_module  # noqa: E402
from app.services.model_checker import ModelChecker  # noqa: E402

LLM_CHECK_ALL_OK = {
    "ok": True,
    "embeddings": {"ok": True, "error": None},
    "chat": {"ok": True, "error": None},
    "error": None,
}
LLM_MODES_RESULT = {"thinking": True, "instant": False}

# Every settings field any test in this file may touch (restored in tearDown).
# embedding_dim is mutated only by the stale_embeddings_detail leg pair.
_SETTINGS_FIELDS = (
    "embedding_model",
    "ollama_embedding_url",
    "chat_model",
    "ollama_chat_url",
    "instant_chat_url",
    "instant_chat_model",
    "health_check_api_key",
    "embedding_dim",
)

# Every app.state attribute any test in this file may touch.
_STATE_ATTRS = (
    "vector_store",
    "model_checker",
    "background_processor",
    "db_pool",
    "embedding_service",
    "maintenance_service",
    "migrations_ok",
)


def _canned_provider_handler(request):
    """Defensive net only: answer any model probe from canned data.

    This file blanks every model URL/name, so ``check_models`` short-circuits
    each leg to ``not_configured`` and never probes. If a future settings
    default ever leaked a configured endpoint into this suite, the shimmed
    transport answers here instead of hitting real network.
    """
    path = request.url.path
    if path.endswith("/api/tags"):
        return httpx.Response(200, json={"models": [{"name": "some-other-model"}]})
    if path.endswith("/v1/models"):
        return httpx.Response(200, json={"data": [{"id": "other-model"}]})
    if path.endswith("/info"):
        return httpx.Response(200, json={"model_id": "different-live-model"})
    return httpx.Response(404, json={"detail": f"no canned answer for {path}"})


class _MockTransportHttpxShim(types.ModuleType):
    """Module-shape stand-in for the ``httpx`` reference model_checker holds.

    Delegates every attribute to the real httpx module — so the checker's
    ``except httpx.HTTPStatusError`` / ``httpx.TimeoutException`` /
    ``httpx.RequestError`` clauses keep comparing against the REAL exception
    classes — except ``AsyncClient``: the factory drops whatever transport
    the caller passed (the SSRF-safe production transport) and injects
    ``httpx.MockTransport(handler)`` so no outbound probe can leave the
    process.
    """

    def __init__(self, real_httpx, handler):
        super().__init__(real_httpx.__name__)
        self._real_httpx = real_httpx
        self._handler = handler

    def __getattr__(self, name):
        return getattr(self._real_httpx, name)

    def AsyncClient(self, **kwargs):
        kwargs.pop("transport", None)
        return self._real_httpx.AsyncClient(
            transport=self._real_httpx.MockTransport(self._handler),
            **kwargs,
        )


class _LlmVectorRedactionTestBase(unittest.TestCase):
    """Shared harness: real app, REAL (not_configured) ModelChecker behind a
    defensive canned transport, dependency-mocked LLM checker, deterministic
    vector-store mock, strict snapshot/restore isolation."""

    def setUp(self):
        # --- save & reset the route module's cache globals ------------------
        self._orig_cache = health_module._deep_cache
        self._orig_in_flight = health_module._deep_refresh_in_flight
        self._orig_tasks = health_module._refresh_tasks
        health_module._deep_cache = {"services": None, "ts": 0.0}
        health_module._deep_refresh_in_flight = False
        health_module._refresh_tasks = set()

        # --- snapshot & blank the settings this file owns -------------------
        self._settings_snapshot = {
            name: getattr(settings, name) for name in _SETTINGS_FIELDS
        }
        # Canonical not_configured state: check_models short-circuits every
        # leg (no model probes), so the llm/vector sections are the only
        # possible carriers of identifying text in the response.
        settings.embedding_model = ""
        settings.ollama_embedding_url = ""
        settings.chat_model = ""
        settings.ollama_chat_url = ""
        settings.instant_chat_url = ""
        settings.instant_chat_model = ""

        # --- snapshot app.state (restored in tearDown) ----------------------
        self._state_snapshot = {
            name: (hasattr(app.state, name), getattr(app.state, name, None))
            for name in _STATE_ATTRS
        }

        # --- defensive canned transport + open SSRF gate in model_checker ---
        self._real_httpx = model_checker_module.httpx
        self._real_assert_url_safe = model_checker_module.assert_url_safe
        model_checker_module.httpx = _MockTransportHttpxShim(
            self._real_httpx, _canned_provider_handler
        )
        model_checker_module.assert_url_safe = lambda url: None
        # The real ModelChecker runs (not_configured); reset the process-
        # global per-host breaker registry so a breaker left OPEN by an
        # earlier test in this worker cannot turn a probe into a
        # "circuit breaker open" error mid-test (peer suites reset it too).
        from app.services.circuit_breaker import model_checker_cb

        model_checker_cb.reset()

        # --- LLM checker: dependency-mocked; the ok baseline is overridden
        # per test with the distinctive leaking error payload.
        self.llm_checker = MagicMock()
        self.llm_checker.check_all = AsyncMock(return_value=dict(LLM_CHECK_ALL_OK))
        self.llm_checker.check_chat_modes = AsyncMock(return_value=LLM_MODES_RESULT)

        # --- the REAL ModelChecker in not_configured state, wired the way
        # the route reads it (get_model_checker is NOT overridden).
        app.state.model_checker = ModelChecker()

        # Deterministic healthy vector store by default; the vector test
        # replaces it on app.state with the raising variant.
        vector_store = MagicMock()
        vector_store.table.count_rows = AsyncMock(return_value=7)
        vector_store._get_expected_embedding_dim = AsyncMock(return_value=None)
        app.state.vector_store = vector_store

        app.dependency_overrides[get_llm_health_checker] = lambda: self.llm_checker
        # Defensive: never inherit overrides leaked by an earlier test.
        app.dependency_overrides.pop(get_model_checker, None)
        app.dependency_overrides.pop(get_current_active_user, None)

        self.client = TestClient(app)

    def tearDown(self):
        model_checker_module.httpx = self._real_httpx
        model_checker_module.assert_url_safe = self._real_assert_url_safe
        app.dependency_overrides.pop(get_llm_health_checker, None)
        app.dependency_overrides.pop(get_model_checker, None)
        app.dependency_overrides.pop(get_current_active_user, None)
        for name, (present, value) in self._state_snapshot.items():
            if present:
                setattr(app.state, name, value)
            elif hasattr(app.state, name):
                delattr(app.state, name)
        for name, value in self._settings_snapshot.items():
            setattr(settings, name, value)
        health_module._deep_cache = self._orig_cache
        health_module._deep_refresh_in_flight = self._orig_in_flight
        health_module._refresh_tasks = self._orig_tasks

    # --- helpers -------------------------------------------------------------

    def _install_user_override(self, role, username, user_id):
        """Install the standard user-auth dependency override for a principal."""
        app.dependency_overrides[get_current_active_user] = lambda: {
            "id": user_id,
            "username": username,
            "role": role,
            "is_active": 1,
            "must_change_password": 0,
        }

    def _reset_deep_cache_between_legs(self):
        """Reset the last-known cache so each leg re-collects deep state."""
        health_module._deep_cache = {"services": None, "ts": 0.0}

    def _get_deep_health(self, headers=None):
        return self.client.get("/api/health?deep=true", headers=headers or {})

    def _install_llm_leak(self, seed):
        """Point the (mocked) LLM checker at BOTH sub-legs failing, in the
        exact shape the real ``check_all`` emits (llm_health.py L179-195):
        ``ok`` False only because sub-legs fail, and the top-level ``error``
        ALWAYS the ``"; "``-join of every failing sub-error — so all THREE
        keys (embeddings.error, chat.error, joined error) carry the
        distinctive seed, and the viewer-omission assertFalse covers them
        all."""
        embeddings_error = (
            "Embedding service error: connect to "
            f"http://{seed}:9000 failed"
        )
        chat_error = (
            "Chat service error: request to "
            f"http://{seed}:9000/v1 failed"
        )
        self.llm_checker.check_all = AsyncMock(
            return_value={
                "ok": False,
                "embeddings": {"ok": False, "error": embeddings_error},
                "chat": {"ok": False, "error": chat_error},
                "error": f"{embeddings_error}; {chat_error}",
            }
        )

    def _install_vector_leak(self, seed):
        """Make the vector-store probe raise so ``_collect_deep_state``'s
        outer except branch yields {"ok": False, "error": <seed>}."""
        vector_store = MagicMock()
        vector_store.table.count_rows = AsyncMock(
            side_effect=RuntimeError(seed)
        )
        app.state.vector_store = vector_store

    def _install_vector_stale_dim(self, stored_dim):
        """Make the vector probe succeed with a stored embedding dimension
        that mismatches ``settings.embedding_dim``, steering the branch that
        builds ``vector_status["stale_embeddings_detail"]`` ("...built with
        <stored>-dim embeddings but EMBEDDING_DIM is now <configured>...")."""
        vector_store = MagicMock()
        vector_store.table.count_rows = AsyncMock(return_value=7)
        vector_store._get_expected_embedding_dim = AsyncMock(
            return_value=stored_dim
        )
        app.state.vector_store = vector_store

    def _assert_viewer_omits_seed(self, seed):
        """Superadmin precondition leg, then the discriminating viewer leg."""
        # PRECONDITION: the superadmin control leg must carry the seed — the
        # seeded section really reaches the response body. Stays true after
        # the fix (admins keep full detail).
        self._install_user_override("superadmin", "admin", 0)
        admin_resp = self._get_deep_health()
        self.assertEqual(
            admin_resp.status_code,
            200,
            f"superadmin precondition leg failed: {admin_resp.text[:300]}",
        )
        self.assertIn(
            seed,
            admin_resp.text,
            "PRECONDITION failed: the superadmin deep-health body must carry "
            "the seeded error detail (harness broken — the seeded section did "
            f"not reach the response): {admin_resp.text[:400]}",
        )

        # Do not leak state between legs: reset the deep cache and re-issue
        # the request under the viewer principal.
        self._reset_deep_cache_between_legs()
        self._install_user_override("viewer", "viewer", 1)
        viewer_resp = self._get_deep_health()
        self.assertEqual(
            viewer_resp.status_code,
            200,
            f"viewer deep-health leg failed: {viewer_resp.text[:300]}",
        )
        self.assertFalse(
            seed in viewer_resp.text,
            "viewer deep-health body must not disclose the seeded "
            f"infrastructure error detail {seed!r} (below-admin principals "
            f"get redacted llm/vector error text): {viewer_resp.text[:400]}",
        )


class TestViewerDeepHealthOmitsInfraErrorDetail(_LlmVectorRedactionTestBase):
    """llm-section and vector_store-section redaction (DISCRIMINATING).

    RED at base (no redaction on the deep-health surface): the viewer legs
    fail with "AssertionError: True is not false".
    """

    def test_viewer_deep_health_omits_llm_error_detail(self):
        """llm section: viewer body must not carry the provider-URL seed in
        ANY of the three keys the real check_all populates —
        embeddings.error, chat.error, and the joined top-level error."""
        seed = "d02-llm-leak-host-7k2p"
        self._install_llm_leak(seed)
        self._assert_viewer_omits_seed(seed)

    def test_viewer_deep_health_omits_vector_store_error_detail(self):
        """vector_store section: viewer body must not carry the probe-error
        seed emitted by the vector branch's except clause, NOR the
        stale_embeddings_detail built when the stored embedding dimension
        mismatches the configured one — two superadmin/viewer leg pairs.

        The error-branch pair runs FIRST so the first failure at base (and
        its frozen expect regex) is anchored on the error-branch seed.
        """
        seed = "d02-vector-leak-path-8w4q"
        self._install_vector_leak(seed)
        self._assert_viewer_omits_seed(seed)

        # Second leg pair: the stale_embeddings_detail branch — stored dim
        # 64 against settings.embedding_dim 65 builds "...built with 64-dim
        # embeddings but EMBEDDING_DIM is now 65..." (embedding_dim is
        # snapshot/restored via _SETTINGS_FIELDS).
        stored_marker = "64-dim"
        self._reset_deep_cache_between_legs()
        self._install_vector_stale_dim(stored_dim=64)
        settings.embedding_dim = 65
        self._assert_viewer_omits_seed(stored_marker)


class TestMonitorKeyKeepsLlmAndVectorDetail(_LlmVectorRedactionTestBase):
    """Monitor-key leg for the llm/vector sections (PRESERVING, in-file only —
    the frozen monitor contract is pinned by C15 on the models section).

    GREEN at base (no redaction anywhere) and after the fix (the monitor
    marker principal minted by require_health_probe_auth's key path is
    exempt). No user-auth override is installed: the request authenticates
    ONLY through the header key, so a 200 proves the key path was taken.
    """

    def test_monitor_key_principal_keeps_llm_and_vector_detail(self):
        llm_seed = "d02-llm-leak-host-7k2p"
        vector_seed = "d02-vector-leak-path-8w4q"
        test_key = "d02-monitor-test-api-key"
        self._install_llm_leak(llm_seed)
        self._install_vector_leak(vector_seed)
        settings.health_check_api_key = test_key

        response = self._get_deep_health(headers={"X-API-Key": test_key})

        self.assertEqual(
            response.status_code,
            200,
            f"monitor-key deep-health leg failed: {response.text[:300]}",
        )
        self.assertIn(
            llm_seed,
            response.text,
            "the X-API-Key monitoring principal (role 'monitor') must keep "
            "the full llm error detail — redacting it would regress the C12 "
            f"monitoring contract (issue #551): {response.text[:400]}",
        )
        self.assertIn(
            vector_seed,
            response.text,
            "the X-API-Key monitoring principal (role 'monitor') must keep "
            "the full vector_store error detail — redacting it would regress "
            f"the C12 monitoring contract (issue #551): {response.text[:400]}",
        )


class TestRoleMatrixAndSignalPreservationLlmVector(_LlmVectorRedactionTestBase):
    """Role-matrix and preserved-signal pins for the llm/vector redaction.

    Mirrors TestRoleMatrixAndSignalPreservation in the models file: the
    member level (2) must be redacted, the level-3 admin boundary must KEEP
    detail, the preserved signal fields must survive, the replacement text
    must be the exact module constants, and the stale_embeddings_detail
    replacement is asserted on ITS OWN FIELD (not a whole-body token scan).
    """

    def _viewer_body(self):
        self._install_user_override("viewer", "viewer", 1)
        self._reset_deep_cache_between_legs()
        resp = self._get_deep_health()
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        return resp.json()

    def test_viewer_llm_and_vector_exact_constants_and_signal_fields(self):
        """Pins the preserved half per field: llm ok/embeddings.ok stay, the
        replaced texts are exactly the module constants, and the stale
        detail carries the dimension-neutral stale-index message (not the
        generic unavailable one that would contradict ok:true)."""
        llm_seed = "d02-preserve-llm-host-9x2k"
        self._install_llm_leak(llm_seed)
        self._install_vector_stale_dim(stored_dim=64)
        settings.embedding_dim = 65

        body = self._viewer_body()

        self.assertNotIn(llm_seed, str(body["llm"]))
        self.assertEqual(
            body["llm"]["embeddings"]["error"],
            health_module._DEEP_SERVICE_DETAIL_HIDDEN,
        )
        self.assertEqual(
            body["llm"]["chat"]["error"],
            health_module._DEEP_SERVICE_DETAIL_HIDDEN,
        )
        self.assertEqual(
            body["llm"]["error"], health_module._DEEP_SERVICE_DETAIL_HIDDEN
        )
        self.assertIs(body["llm"]["ok"], False)
        self.assertEqual(
            body["vector_store"]["stale_embeddings_detail"],
            health_module._DEEP_STALE_DETAIL_HIDDEN,
            "the stale-index message must be exactly the dimension-neutral "
            "constant on its own field",
        )
        self.assertNotIn("64-dim", body["vector_store"]["stale_embeddings_detail"])
        self.assertIs(body["vector_store"]["stale_embeddings"], True)
        self.assertEqual(
            set(body["services"].keys()),
            {"backend", "embeddings", "chat", "vector_store"},
            f"services booleans must survive for a viewer: {body['services']}",
        )

    def test_member_llm_detail_redacted_like_viewer(self):
        llm_seed = "d02-member-llm-host-3m8q"
        self._install_llm_leak(llm_seed)

        self._install_user_override("member", "member", 2)
        resp = self._get_deep_health()
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        body = resp.json()

        self.assertNotIn(
            llm_seed,
            str(body["llm"]),
            f"member deep-health body must not disclose the provider host "
            f"{llm_seed!r}: {body['llm']}",
        )
        self.assertEqual(
            body["llm"]["error"], health_module._DEEP_SERVICE_DETAIL_HIDDEN
        )

    def test_admin_role_keeps_llm_and_vector_detail(self):
        """Level-3 admin at the exact boundary keeps full llm/vector detail."""
        llm_seed = "d02-admin-llm-host-5v7t"
        self._install_llm_leak(llm_seed)
        self._install_vector_stale_dim(stored_dim=64)
        settings.embedding_dim = 65

        self._install_user_override("admin", "admin", 3)
        resp = self._get_deep_health()
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        body = resp.json()

        self.assertIn(
            llm_seed,
            str(body["llm"]),
            "a level-3 admin must keep full unredacted llm detail: "
            f"{body['llm']}",
        )
        self.assertIn("64-dim", body["vector_store"]["stale_embeddings_detail"])


if __name__ == "__main__":
    unittest.main()
