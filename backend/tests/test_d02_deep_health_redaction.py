"""Issue #672 acceptance checks (D-02): deep-health model-name disclosure.

Contract under test: ``GET /api/health?deep=true`` must stop disclosing the
CONFIGURED model names (and the provider inventory) that
``ModelChecker`` embeds inside ``models.*.error`` strings to below-admin
principals, while superadmins keep full detail (AC9-AC11 precondition leg)
and the X-API-Key monitoring principal keeps full unredacted detail (AC15,
the C12 monitoring contract from issue #551).

Expected status at the PRE-FIX code (intentional — the discriminating checks
prove the fix is required):

  FAIL (RED at base):
      TestViewerDeepHealthOmitsModelNames::
          test_viewer_deep_health_omits_embedding_model_name_ollama   (AC9)
      TestViewerDeepHealthOmitsModelNames::
          test_viewer_deep_health_omits_chat_model_name_openai        (AC10)
      TestViewerDeepHealthOmitsModelNames::
          test_viewer_deep_health_omits_embedding_model_name_tei      (AC11)
      Each fails as "AssertionError: True is not false" — the viewer body
      still contains the seeded model name.
  PASS (GREEN at base and after the fix, PRESERVING):
      TestMonitorKeyKeepsModelDetail::
          test_monitor_key_principal_keeps_model_detail               (AC15)

Harness: the REAL app via TestClient with the REAL
``ModelChecker.check_models`` (never stubbed) — only two things are canned:

- ``app.services.model_checker.assert_url_safe`` is monkeypatched to a no-op
  so the seeded localhost URLs pass the SSRF gate;
- the module-level ``httpx`` reference inside ``app.services.model_checker``
  is swapped for a shim that delegates EVERY attribute to the real httpx
  module (preserving the checker's ``except httpx.HTTPStatusError`` /
  ``httpx.TimeoutException`` clauses) but rebuilds ``AsyncClient(**kwargs)``
  to drop the production transport kwarg and ride ``httpx.MockTransport``
  with a canned handler — zero real outbound network.

The canned handler answers all three probe dialects with a listing that LACKS
the seeded model, which is exactly the condition that makes ModelChecker emit
``"Model '<configured model>' not found. Available models: ..."`` — the
disclosure under test. The LLM checker stays dependency-mocked (its section
is not under test), the vector store is a deterministic mock, the route
module's cache globals and every ``app.state``/``settings`` attribute touched
are snapshot/restored (mirroring ``test_health_deep_auth.py``), and each
viewer test first proves with a superadmin leg that the seed IS in the
response before asserting the viewer leg omits it — the leak is pinned to the
principal, not to a broken harness.
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

LLM_CHECK_ALL_RESULT = {
    "ok": True,
    "embeddings": {"ok": True, "error": None},
    "chat": {"ok": True, "error": None},
    "error": None,
}
LLM_MODES_RESULT = {"thinking": True, "instant": False}

# Every settings field any test in this file may touch (restored in tearDown).
_SETTINGS_FIELDS = (
    "embedding_model",
    "ollama_embedding_url",
    "chat_model",
    "ollama_chat_url",
    "instant_chat_url",
    "instant_chat_model",
    "health_check_api_key",
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
    """Answer every model-listing dialect from canned data (no real network).

    Each listing deliberately LACKS any seeded model, so the first dialect
    that answers returns the authoritative "model not found" error string
    that embeds the CONFIGURED model name — the #672 disclosure.
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
    ``httpx.MockTransport(handler)`` so every outbound probe answers from the
    canned handler with zero real network.
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


class _DeepHealthRedactionTestBase(unittest.TestCase):
    """Shared harness: real app + REAL ModelChecker.check_models over a canned
    transport, mocked LLM checker, strict snapshot/restore isolation for the
    route module's cache globals, app.state attributes, dependency overrides,
    and settings fields."""

    def setUp(self):
        # --- save & reset the route module's cache globals ------------------
        self._orig_cache = health_module._deep_cache
        self._orig_in_flight = health_module._deep_refresh_in_flight
        self._orig_tasks = health_module._refresh_tasks
        health_module._deep_cache = {"services": None, "ts": 0.0}
        health_module._deep_refresh_in_flight = False
        health_module._refresh_tasks = set()

        # --- snapshot & reseed the settings this file owns ------------------
        self._settings_snapshot = {
            name: getattr(settings, name) for name in _SETTINGS_FIELDS
        }
        # Isolate every model endpoint to the designed not_configured state;
        # each test seeds exactly the one dialect pair it exercises.
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

        # --- canned outbound transport + open SSRF gate in model_checker ----
        self._real_httpx = model_checker_module.httpx
        self._real_assert_url_safe = model_checker_module.assert_url_safe
        model_checker_module.httpx = _MockTransportHttpxShim(
            self._real_httpx, _canned_provider_handler
        )
        model_checker_module.assert_url_safe = lambda url: None
        # check_models probes go through the process-global per-host breaker
        # registry; a breaker left OPEN by an earlier test in this worker
        # would answer "No probe dialect matched ... circuit breaker open"
        # (no model name in the text) and fail the superadmin precondition
        # legs below with a misleading "harness broken" message. Peer suites
        # (test_ssrf_services, test_c12_model_checker_isolation, ...) reset
        # it for the same reason.
        from app.services.circuit_breaker import model_checker_cb

        model_checker_cb.reset()

        # --- LLM checker stays dependency-mocked (not the surface under test)
        self.llm_checker = MagicMock()
        self.llm_checker.check_all = AsyncMock(return_value=LLM_CHECK_ALL_RESULT)
        self.llm_checker.check_chat_modes = AsyncMock(return_value=LLM_MODES_RESULT)

        # --- the REAL ModelChecker, wired the way the route reads it ---------
        # get_model_checker reads app.state.model_checker and is deliberately
        # NOT dependency-overridden: check_models must run for real.
        app.state.model_checker = ModelChecker()

        # Deterministic vector store so the deep payload path never touches a
        # real LanceDB table another test may have left on the shared app.
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


class TestViewerDeepHealthOmitsModelNames(_DeepHealthRedactionTestBase):
    """AC9/AC10/AC11 (DISCRIMINATING): a viewer's deep-health body must not
    contain the configured model name, per provider dialect.

    Each test first proves with a superadmin leg (full detail) that the seed
    IS in the response text — pinning any failure to the principal-based
    redaction, not to a broken harness — then re-issues the request as a
    viewer over a reset deep cache and asserts the seed is gone.

    RED at base (no redaction on the deep-health surface): the viewer leg
    fails with "AssertionError: True is not false".
    """

    def _assert_viewer_omits_seed(self, seed):
        """Superadmin precondition leg, then the discriminating viewer leg."""
        # PRECONDITION: the superadmin control leg must carry the seed — the
        # probe chain really produces the disclosing error string. Stays true
        # after the fix (admins keep full detail).
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
            "the seeded model name (harness broken — the probe did not produce "
            f"the disclosing error string): {admin_resp.text[:400]}",
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
            "viewer deep-health body must not disclose the configured model "
            f"name {seed!r} (below-admin principals get redacted model "
            f"detail): {viewer_resp.text[:400]}",
        )

    def test_viewer_deep_health_omits_embedding_model_name_ollama(self):
        """AC9: Ollama dialect — seeded embedding model absent from /api/tags."""
        seed = "d02-health-leak-embedding-9zq7"
        settings.embedding_model = seed
        settings.ollama_embedding_url = "http://localhost:11434"
        # chat/instant stay not_configured (""): only the embedding leg probes.
        self._assert_viewer_omits_seed(seed)

    def test_viewer_deep_health_omits_chat_model_name_openai(self):
        """AC10: OpenAI-compatible dialect — chat model absent from /v1/models.

        Port 8000 makes the heuristic order openai_compatible first.
        """
        seed = "d02-health-leak-chat-4t8n"
        settings.chat_model = seed
        settings.ollama_chat_url = "http://localhost:8000"
        # embedding/instant stay not_configured (""): only the chat leg probes.
        self._assert_viewer_omits_seed(seed)

    def test_viewer_deep_health_omits_embedding_model_name_tei(self):
        """AC11: native TEI — /info reports a different live model_id.

        Port 8080 makes the heuristic order tei first.
        """
        seed = "d02-health-leak-tei-embedding-6w1m"
        settings.embedding_model = seed
        settings.ollama_embedding_url = "http://localhost:8080"
        # chat/instant stay not_configured (""): only the embedding leg probes.
        self._assert_viewer_omits_seed(seed)


class TestMonitorKeyKeepsModelDetail(_DeepHealthRedactionTestBase):
    """AC15 (PRESERVING): the X-API-Key monitoring principal keeps full
    unredacted deep-health detail (the C12 monitoring contract, issue #551).

    GREEN at base (no redaction anywhere) and after the fix (the monitor
    marker principal minted by require_health_probe_auth's key path is
    exempt). No user-auth override is installed: the request authenticates
    ONLY through the header key, so a 200 proves the key path was taken.
    """

    def test_monitor_key_principal_keeps_model_detail(self):
        seed = "d02-monitor-keeps-embedding-3v5x"
        test_key = "d02-monitor-test-api-key"
        settings.embedding_model = seed
        settings.ollama_embedding_url = "http://localhost:11434"
        settings.health_check_api_key = test_key

        response = self._get_deep_health(headers={"X-API-Key": test_key})

        self.assertEqual(
            response.status_code,
            200,
            f"monitor-key deep-health leg failed: {response.text[:300]}",
        )
        self.assertIn(
            seed,
            response.text,
            "the X-API-Key monitoring principal (role 'monitor') must keep "
            "full unredacted deep-health detail — redacting it would regress "
            "the C12 monitoring contract (issue #551): "
            f"{response.text[:400]}",
        )


class TestRoleMatrixAndSignalPreservation(_DeepHealthRedactionTestBase):
    """Role-matrix and preserved-signal pins for the models redaction.

    Closes the reviewed gap that the discriminating legs only exercised
    superadmin (control) and viewer (redacted): the boundary at
    ``UserRole.level(role) >= UserRole.level("admin")`` is off-by-one-able
    (an admin at level exactly 3 must KEEP detail), the member level (2)
    must be redacted exactly like the viewer, the preserved signal fields
    must survive redaction (an over-redaction that drops sections or blanks
    ``available``/``services`` must fail), the replacement text must be the
    exact module constant (any other string must fail), and the HTTP-error
    ``status_code`` a probe attaches must be hidden from below-admin
    principals (it distinguishes 401/404/5xx on admin-only endpoints).
    """

    def _seed_ollama_embedding(self, seed):
        settings.embedding_model = seed
        settings.ollama_embedding_url = "http://localhost:11434"

    def _deep_body(self):
        resp = self._get_deep_health()
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        return resp.json()

    def test_viewer_model_entry_keeps_signal_fields_and_exact_constant(self):
        """Preserved half of the contract, pinned per field (not just
        seed-absence): the redacted entry keeps ``available: false``, the
        error text is EXACTLY the module constant, the services booleans and
        the vector_store signal block survive untouched."""
        seed = "d02-preserve-embedding-4k2w"
        self._seed_ollama_embedding(seed)

        self._install_user_override("viewer", "viewer", 1)
        body = self._deep_body()
        entry = body["models"]["embedding_model"]
        self.assertIs(entry["available"], False, f"models entry: {entry}")
        self.assertEqual(
            entry["error"],
            health_module._DEEP_MODEL_DETAIL_HIDDEN,
            "the replacement text must be exactly the module constant — a "
            "different string (or the raw leaking text) must fail here",
        )
        self.assertNotIn(seed, str(entry))
        # Signal fields a dashboard reads must survive redaction wholesale.
        self.assertEqual(
            set(body["services"].keys()),
            {"backend", "embeddings", "chat", "vector_store"},
            f"services booleans must survive for a viewer: {body['services']}",
        )
        self.assertIn("ok", body["vector_store"])
        self.assertIn("rows", body["vector_store"])

    def test_member_deep_health_redacted_like_viewer(self):
        """Member (level 2) is below admin and must be redacted — the release
        fragment promises 'viewer or member'; without this leg a boundary
        loosened to viewer-only ships green while members see full detail."""
        seed = "d02-member-embedding-8j3n"
        self._seed_ollama_embedding(seed)

        self._install_user_override("member", "member", 2)
        body = self._deep_body()

        self.assertNotIn(
            seed,
            str(body["models"]),
            f"member deep-health body must not disclose the configured model "
            f"name {seed!r}: {body['models']}",
        )
        self.assertEqual(
            body["models"]["embedding_model"]["error"],
            health_module._DEEP_MODEL_DETAIL_HIDDEN,
        )

    def test_admin_role_keeps_full_detail(self):
        """Level-3 admin is exactly at the boundary and must KEEP detail —
        without this leg an off-by-one (``>=`` loosened to ``>``) redacts
        plain admins while every superadmin leg stays green."""
        seed = "d02-admin-keeps-embedding-5t9c"
        self._seed_ollama_embedding(seed)

        self._install_user_override("admin", "admin", 3)
        body = self._deep_body()

        self.assertIn(
            seed,
            str(body["models"]),
            "a level-3 admin must keep full unredacted model detail (only "
            f"below-admin principals are redacted): {body['models']}",
        )

    def test_http_error_status_code_hidden_from_viewer(self):
        """``status_code`` from an HTTP-error probe distinguishes
        401-vs-404-vs-5xx on admin-only endpoints (provider-key validity);
        below-admin bodies must carry it as None while admins keep it."""
        seed = "d02-statuscode-embedding-6r4v"
        self._seed_ollama_embedding(seed)

        handler = _canned_provider_handler

        def http_error_handler(request):
            if request.url.path.endswith("/api/tags"):
                return httpx.Response(500, json={"error": "boom"})
            return handler(request)

        # tearDown restores the REAL httpx module, so swapping the shim's
        # canned handler for the HTTP-error one here needs no extra cleanup.
        model_checker_module.httpx = _MockTransportHttpxShim(
            self._real_httpx, http_error_handler
        )

        # Admin control: the 500 probe attaches a non-None status_code (the
        # HTTP-error text does NOT carry the model name — only listing-based
        # "model not found" errors do — so the precondition pins the
        # status_code field itself).
        self._install_user_override("superadmin", "admin", 0)
        admin_body = self._deep_body()
        self.assertIsNotNone(
            admin_body["models"]["embedding_model"].get("status_code"),
            f"precondition: the HTTP-error probe must attach status_code for "
            f"admins: {admin_body['models']}",
        )
        self.assertIsNotNone(
            admin_body["models"]["embedding_model"].get("error"),
            f"precondition: the HTTP-error probe must produce an error text "
            f"for admins: {admin_body['models']}",
        )

        self._install_user_override("viewer", "viewer", 1)
        self._reset_deep_cache_between_legs()
        viewer_body = self._deep_body()
        self.assertIsNone(
            viewer_body["models"]["embedding_model"].get("status_code"),
            "the HTTP-error status_code must be hidden (None) from "
            f"below-admin principals: {viewer_body['models']}",
        )
        self.assertEqual(
            viewer_body["models"]["embedding_model"]["error"],
            health_module._DEEP_MODEL_DETAIL_HIDDEN,
        )


if __name__ == "__main__":
    unittest.main()
