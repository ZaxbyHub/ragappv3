"""Acceptance checks for issue #622 (first-setup wizard: model endpoints).

FROZEN SPEC — the fix copies this file VERBATIM to
``backend/tests/test_settings_probe_and_api_key.py`` and makes it pass. The
assertions below are the interface contract; they are not to be edited.

Backend contract under test (issue #622, gap map G2-G5):

1. ``POST /api/settings/probe`` (settings router; admin role like PUT
   /settings). Request JSON ``{"target": "thinking"|"instant", "base_url":
   str, "model": str, "api_key"?: str}``. The route validates base_url with
   the repo's ``assert_url_safe`` (422 ``Unsafe ... URL: ...`` on rejection;
   422 when base_url or model is empty — the detail says both are required),
   then probes the endpoint and returns ``{"status": ..., "detail": str}``
   where status is exactly one of ``ok`` / ``unreachable`` /
   ``model_mismatch``. A request with a non-empty base_url+model NEVER
   answers ``not_configured`` (AC3).

2. ``SettingsResponse`` gains ``chat_configured`` and ``instant_configured``
   (true iff the respective URL+model pair is non-empty; computed
   server-side from the UNREDACTED pair so every role sees the same signal),
   ``chat_api_key`` (always ``""`` — write-only) and ``chat_api_key_set``
   (true iff a key is stored; blanked/False for non-admin reads alongside
   the INFRA_REDACTED_FIELDS pipeline).

3. ``SettingsUpdate`` gains optional ``chat_api_key``. It persists through
   the normal settings_kv pipeline (row readable via sqlite) and replays at
   startup via ``lifespan._load_persisted_settings`` (NEW_DIRECT_KEYS /
   PERSISTED_FUNCTIONAL_FIELDS).

4. ``LLMClient`` gains optional ``api_key``: requests carry
   ``Authorization: Bearer <key>``; ``reconfigure(api_key=...)`` is
   supported; ``_hot_rebind_llm_clients`` passes ``settings.chat_api_key``
   to newly activated clients (thinking and instant paths).

Method-name note: the probe-route tests live in ``TestSettingsProbeRoute``
and the secret/flag tests in ``TestChatApiKeySecretHandling``. The drivers
select the two disjoint halves with ``-k TestSettingsProbeRoute`` /
``-k TestChatApiKeySecretHandling`` (CLASS-name filters — the file name
itself contains "probe", so a substring filter would select everything).
"""

import asyncio
import json
import logging
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# This module mentions csrf only to declare that the pytest-only bypass may
# apply (the routes' double-submit validator itself is NOT under test here —
# issue #202 / ENH-008 classification).
CSRF_TEST_POLICY = "naive"

# CI installs requirements-ci.txt which omits unstructured; the per-file stub
# below is load-bearing there (see docs/engineering/testing.md §2).
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
    _unstructured.chunking = types.ModuleType("unstructured.chunking")
    _unstructured.chunking.__path__ = []
    _unstructured.chunking.title = types.ModuleType("unstructured.chunking.title")
    _unstructured.chunking.title.chunk_by_title = lambda *args, **kwargs: []
    _unstructured.documents = types.ModuleType("unstructured.documents")
    _unstructured.documents.__path__ = []
    _unstructured.documents.elements = types.ModuleType(
        "unstructured.documents.elements"
    )
    _unstructured.documents.elements.Element = type("Element", (), {})
    sys.modules["unstructured"] = _unstructured
    sys.modules["unstructured.partition"] = _unstructured.partition
    sys.modules["unstructured.partition.auto"] = _unstructured.partition.auto
    sys.modules["unstructured.chunking"] = _unstructured.chunking
    sys.modules["unstructured.chunking.title"] = _unstructured.chunking.title
    sys.modules["unstructured.documents"] = _unstructured.documents
    sys.modules["unstructured.documents.elements"] = _unstructured.documents.elements

# Add parent directory to path for imports (same as test_settings.py).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

TEST_DATA_DIR = tempfile.mkdtemp()
TEST_DB_PATH = str(Path(TEST_DATA_DIR) / "test.db")

from app.models.database import init_db, run_migrations  # noqa: E402

init_db(TEST_DB_PATH)
run_migrations(TEST_DB_PATH)

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

# A sentinel secret: distinctive enough that an accidental leak into an
# assertion message is obvious, and greppable in log output.
SECRET_KEY = "sk-wizard-622-do-not-log"

_MISSING = object()


@contextmanager
def _settings_values(**overrides):
    """Temporarily override settings attributes (restored on exit).

    Overrides the conftest ``_default_configured_chat_endpoints`` autouse
    fixture for the test's duration (same idiom as
    test_chat_unconfigured_routes.py).
    """
    previous = {name: getattr(settings, name) for name in overrides}
    for name, value in overrides.items():
        setattr(settings, name, value)
    try:
        yield
    finally:
        for name, value in previous.items():
            setattr(settings, name, value)


def _env_without_allow_local_services():
    """patch.dict env view with ALLOW_LOCAL_SERVICES removed (SSRF strict)."""
    env = {k: v for k, v in os.environ.items() if k != "ALLOW_LOCAL_SERVICES"}
    return patch.dict(os.environ, env, clear=True)


class _CaptureHandler(logging.Handler):
    """Collect every log record's formatted message for leak assertions."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.messages = []

    def emit(self, record):
        try:
            self.messages.append(record.getMessage())
        except Exception:  # pragma: no cover - defensive
            self.messages.append(str(record.msg))


class _SettingsRouteHarness(unittest.TestCase):
    """Shared harness: temp DB pool, get_db + role overrides, state hygiene.

    Follows the canonical settings-suite shape (test_settings.py): pooled
    sqlite connection override, ``get_current_active_user`` override for the
    caller role, snapshot/restore of the settings singleton and of
    ``app.state`` LLM-client slots (PUT /settings late-activates clients on
    the real app), and a settings_kv wipe between tests so the shared DB
    file never leaks rows across files (issue #565 randomized-order class of
    pollution).
    """

    def setUp(self):
        from app.api.deps import get_current_active_user, get_db
        from app.models.database import get_pool

        self._pool = get_pool(TEST_DB_PATH)

        # PR #644 review hardening (PRR-004): model_checker_cb is a module
        # singleton shared with the probe route — reset so a breaker opened
        # by an earlier test file (pytest-randomly ordering) cannot flip
        # probe expectations to "circuit breaker open" mismatches.
        from app.services import circuit_breaker as _cb

        _cb.model_checker_cb.reset()
        self.addCleanup(_cb.model_checker_cb.reset)

        def override_get_db():
            conn = self._pool.get_connection()
            try:
                yield conn
            finally:
                self._pool.release_connection(conn)

        app.dependency_overrides[get_db] = override_get_db
        self.addCleanup(app.dependency_overrides.pop, get_db, None)

        self._get_current_active_user = get_current_active_user
        self.addCleanup(app.dependency_overrides.pop, get_current_active_user, None)
        self._set_role("admin")

        self._snapshot_settings()
        self._isolate_app_state()
        self.addCleanup(self._delete_settings_kv_rows)

        self.client = TestClient(app)

    def _set_role(self, role):
        app.dependency_overrides[self._get_current_active_user] = lambda: {
            "id": 1,
            "username": f"test-{role}",
            "role": role,
            "is_active": 1,
            "must_change_password": 0,
        }

    def _snapshot_settings(self):
        self._settings_snapshot = {
            name: getattr(settings, name) for name in type(settings).model_fields
        }
        self.addCleanup(self._restore_settings_snapshot)

    def _restore_settings_snapshot(self):
        for name, value in self._settings_snapshot.items():
            setattr(settings, name, value)

    def _isolate_app_state(self):
        saved = {}
        for attr in ("thinking_llm_client", "instant_llm_client", "llm_client"):
            saved[attr] = getattr(app.state, attr, _MISSING)
            setattr(app.state, attr, None)  # force the late-activation path

        def _restore():
            for attr, value in saved.items():
                if value is _MISSING:
                    try:
                        delattr(app.state, attr)
                    except AttributeError:
                        pass
                else:
                    setattr(app.state, attr, value)

        self.addCleanup(_restore)

    def _delete_settings_kv_rows(self):
        conn = self._pool.get_connection()
        try:
            conn.execute("DELETE FROM settings_kv")
            conn.commit()
        finally:
            self._pool.release_connection(conn)

    # ── probe transport doubles ─────────────────────────────────────────
    #
    # The upstream transport is mocked; no test in this file touches the
    # network. Both the settings router and ModelChecker do ``import httpx``
    # and construct ``httpx.AsyncClient``; because they share the one httpx
    # module object, patching the attribute through the settings-router path
    # covers every construction site a compliant implementation uses (same
    # technique as test_settings.py's /settings/connection tests).

    def _fake_async_client(self, get_response=None, get_error=None):
        """Build an httpx.AsyncClient factory double.

        GET probes return ``get_response`` (or raise ``get_error``); the
        double is usable both as ``AsyncClient(...)`` and via
        ``async with AsyncClient(...) as c:``.
        """
        instance = AsyncMock()
        if get_error is not None:
            instance.get = AsyncMock(side_effect=get_error)
        else:
            instance.get = AsyncMock(return_value=get_response)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=None)
        return MagicMock(return_value=instance)

    @staticmethod
    def _listing_response(models):
        """A 200 response whose JSON lists ``models`` in BOTH dialect shapes.

        Covers the Ollama ``/api/tags`` shape (``{"models": [{"name": ...}]}``)
        and the OpenAI-compatible ``/v1/models`` shape
        (``{"data": [{"id": ...}]}``) so whichever dialect the probe speaks
        finds (or misses) the model.
        """
        response = MagicMock()
        response.status_code = 200
        response.raise_for_status = MagicMock(return_value=None)
        response.json = MagicMock(
            return_value={
                "models": [{"name": name} for name in models],
                "data": [{"id": name} for name in models],
            }
        )
        return response


class TestSettingsProbeRoute(_SettingsRouteHarness):
    """POST /api/settings/probe — the wizard's Test-connection backend."""

    PROBE_URL = "/api/settings/probe"

    def _probe(self, payload):
        return self.client.post(self.PROBE_URL, json=payload)

    @patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"})
    def test_probe_returns_ok_for_both_targets_when_model_served(self):
        for target in ("thinking", "instant"):
            with patch(
                "app.api.routes.settings.httpx.AsyncClient",
                new=self._fake_async_client(
                    get_response=self._listing_response(["wizard-model"])
                ),
            ):
                response = self._probe(
                    {
                        "target": target,
                        "base_url": "http://localhost:11434",
                        "model": "wizard-model",
                        # api_key is optional and must be accepted, not 422'd
                        "api_key": "sk-probe-optional",
                    }
                )
            self.assertEqual(response.status_code, 200, response.text)
            data = response.json()
            self.assertEqual(data["status"], "ok", data)
            # AC3: a filled endpoint never reports the not_configured state.
            self.assertNotEqual(data["status"], "not_configured")
            self.assertIsInstance(data["detail"], str)

    @patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"})
    def test_probe_returns_unreachable_on_transport_error(self):
        import httpx

        with patch(
            "app.api.routes.settings.httpx.AsyncClient",
            new=self._fake_async_client(
                get_error=httpx.ConnectError("Connection refused")
            ),
        ):
            response = self._probe(
                {
                    "target": "thinking",
                    "base_url": "http://localhost:11434",
                    "model": "wizard-model",
                }
            )
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["status"], "unreachable", data)
        self.assertNotEqual(data["status"], "not_configured")

    @patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"})
    def test_probe_returns_model_mismatch_when_model_absent(self):
        with patch(
            "app.api.routes.settings.httpx.AsyncClient",
            new=self._fake_async_client(
                get_response=self._listing_response(["some-other-model"])
            ),
        ):
            response = self._probe(
                {
                    "target": "thinking",
                    "base_url": "http://localhost:11434",
                    "model": "wizard-model",
                }
            )
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["status"], "model_mismatch", data)
        self.assertNotEqual(data["status"], "not_configured")

    @patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"})
    def test_probe_rejects_empty_base_url_with_422(self):
        response = self._probe(
            {"target": "thinking", "base_url": "", "model": "wizard-model"}
        )
        self.assertEqual(response.status_code, 422, response.text)
        # The refusal is a validation error naming both required fields —
        # never a 200 with status "not_configured".
        detail = str(response.json().get("detail", ""))
        self.assertIn("required", detail.lower())

    @patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"})
    def test_probe_rejects_empty_model_with_422(self):
        response = self._probe(
            {"target": "thinking", "base_url": "http://localhost:11434", "model": ""}
        )
        self.assertEqual(response.status_code, 422, response.text)
        detail = str(response.json().get("detail", ""))
        self.assertIn("required", detail.lower())

    def test_probe_rejects_unsafe_url_with_422(self):
        # Loopback is denied unless ALLOW_LOCAL_SERVICES=1: with the env var
        # removed, http://127.0.0.1:11434 must trip the assert_url_safe gate
        # (the same 422 "Unsafe ... URL: ..." contract PUT /settings uses).
        with _env_without_allow_local_services():
            response = self._probe(
                {
                    "target": "thinking",
                    "base_url": "http://127.0.0.1:11434",
                    "model": "wizard-model",
                }
            )
        self.assertEqual(response.status_code, 422, response.text)
        detail = str(response.json().get("detail", ""))
        self.assertIn("unsafe", detail.lower())
        self.assertIn("url", detail.lower())

    def test_probe_rejects_unknown_target_with_422(self):
        with patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"}):
            response = self._probe(
                {
                    "target": "editorial",
                    "base_url": "http://localhost:11434",
                    "model": "wizard-model",
                }
            )
        self.assertEqual(response.status_code, 422, response.text)

    def test_probe_is_admin_gated_viewer_gets_403(self):
        # Same role gate as PUT /settings: a viewer must not probe arbitrary
        # URLs through the server (SSRF surface is admin-only).
        self._set_role("viewer")
        response = self._probe(
            {
                "target": "thinking",
                "base_url": "http://localhost:11434",
                "model": "wizard-model",
            }
        )
        self.assertEqual(response.status_code, 403, response.text)


class TestChatApiKeySecretHandling(_SettingsRouteHarness):
    """chat_api_key storage, redaction, restart replay, and Bearer transport.

    Also pins the role-safe chat_configured / instant_configured signals the
    first-login banner consumes (computed server-side for every role).
    """

    def _put_wizard_settings(self, extra=None):
        payload = {
            "ollama_chat_url": "http://localhost:11434",
            "chat_model": "wizard-model",
        }
        payload.update(extra or {})
        return self.client.put("/api/settings", json=payload)

    @patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"})
    def test_put_chat_api_key_persists_to_settings_kv(self):
        response = self._put_wizard_settings({"chat_api_key": SECRET_KEY})
        self.assertEqual(response.status_code, 200, response.text)

        conn = self._pool.get_connection()
        try:
            row = conn.execute(
                "SELECT value FROM settings_kv WHERE key = 'chat_api_key'"
            ).fetchone()
        finally:
            self._pool.release_connection(conn)
        self.assertIsNotNone(row, "chat_api_key must persist to settings_kv")
        self.assertEqual(json.loads(row[0]), SECRET_KEY)

    @patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"})
    def test_get_settings_admin_chat_api_key_empty_and_set_flag_true(self):
        self.assertEqual(
            self._put_wizard_settings({"chat_api_key": SECRET_KEY}).status_code,
            200,
        )
        response = self.client.get("/api/settings")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        # Write-only: a GET never echoes the value, but reports presence.
        self.assertEqual(data["chat_api_key"], "")
        self.assertTrue(data["chat_api_key_set"])

    @patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"})
    def test_get_settings_non_admin_chat_api_key_redacted_and_set_flag_false(self):
        self.assertEqual(
            self._put_wizard_settings({"chat_api_key": SECRET_KEY}).status_code,
            200,
        )
        self._set_role("viewer")
        response = self.client.get("/api/settings")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["chat_api_key"], "")
        self.assertFalse(
            bool(data["chat_api_key_set"]),
            "viewer must see chat_api_key_set blanked alongside INFRA_REDACTED_FIELDS",
        )
        self.assertNotIn(SECRET_KEY, response.text)

    @patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"})
    def test_chat_api_key_survives_simulated_restart_replay(self):
        """PUT persists the key; a simulated restart (singleton reset +
        lifespan._load_persisted_settings against the same DB file) restores
        it — the NEW_DIRECT_KEYS / PERSISTED_FUNCTIONAL_FIELDS replay the
        CONFIG-003 contract requires."""
        self.assertEqual(
            self._put_wizard_settings({"chat_api_key": SECRET_KEY}).status_code,
            200,
        )

        settings.chat_api_key = ""  # in-memory value lost, as on restart
        from app.lifespan import _load_persisted_settings

        _load_persisted_settings(TEST_DB_PATH)
        self.assertEqual(getattr(settings, "chat_api_key", None), SECRET_KEY)

    @patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"})
    def test_chat_api_key_never_appears_in_logs_during_put_and_get(self):
        handler = _CaptureHandler()
        root = logging.getLogger()
        old_level = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)
        self.addCleanup(root.setLevel, old_level)

        put = self._put_wizard_settings({"chat_api_key": SECRET_KEY})
        self.assertEqual(put.status_code, 200, put.text)
        got = self.client.get("/api/settings")
        self.assertEqual(got.status_code, 200)

        leaked = [m for m in handler.messages if SECRET_KEY in m]
        self.assertEqual(leaked, [], f"secret leaked into log records: {leaked!r}")
        # Vacuous-pass guard: the flow itself must have run to completion.
        self.assertTrue(got.json()["chat_api_key_set"])

    # ── Bearer transport ────────────────────────────────────────────────

    def _drive_completion_capture_headers(self, llm_client):
        """Run one chat completion with the transport mocked; return the
        merged headers seen at BOTH the AsyncClient construction site and
        the POST call (the fix may attach Authorization at either level)."""
        completion = MagicMock()
        completion.status_code = 200
        completion.raise_for_status = MagicMock(return_value=None)
        completion.json = MagicMock(
            return_value={"choices": [{"message": {"content": "pong"}}]}
        )
        instance = AsyncMock()
        instance.post = AsyncMock(return_value=completion)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=None)
        captured: dict = {}

        def _capturing_factory(*args, **kwargs):
            captured.update(kwargs.get("headers") or {})
            return instance

        with patch(
            "app.services.llm_client.httpx.AsyncClient", new=_capturing_factory
        ):
            asyncio.run(
                llm_client.chat_completion([{"role": "user", "content": "ping"}])
            )
        merged = dict(captured)
        for call in instance.post.call_args_list:
            merged.update((call.kwargs or {}).get("headers") or {})
        return merged

    @patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"})
    def test_hot_rebind_thinking_client_carries_api_key_bearer_header(self):
        # Route-level: PUT completes the pair WITH a key on an app whose
        # thinking client slot is None (no lifespan ran) — late activation
        # must construct the client with the key, and one chat completion
        # through that client must send Authorization: Bearer <key>.
        put = self._put_wizard_settings({"chat_api_key": SECRET_KEY})
        self.assertEqual(put.status_code, 200, put.text)

        activated = getattr(app.state, "thinking_llm_client", None)
        self.assertIsNotNone(
            activated, "PUT with a complete pair must late-activate the client"
        )

        merged = self._drive_completion_capture_headers(activated)
        self.assertEqual(
            merged.get("Authorization"),
            f"Bearer {SECRET_KEY}",
            f"expected Bearer auth header, saw headers: {merged!r}",
        )

    @patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"})
    def test_hot_rebind_instant_client_receives_chat_api_key(self):
        put = self.client.put(
            "/api/settings",
            json={
                "instant_chat_url": "http://localhost:1234",
                "instant_chat_model": "instant-model",
                "chat_api_key": SECRET_KEY,
            },
        )
        self.assertEqual(put.status_code, 200, put.text)

        activated = getattr(app.state, "instant_llm_client", None)
        self.assertIsNotNone(activated, "instant pair PUT must activate instant client")
        self.assertEqual(getattr(activated, "api_key", None), SECRET_KEY)

    @patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"})
    def test_reconfigure_updates_api_key_bearer_header(self):
        from app.services.llm_client import LLMClient

        with _settings_values(
            ollama_chat_url="http://localhost:11434",
            chat_model="wizard-model",
            chat_api_key="sk-first-622",
        ):
            llm = LLMClient(
                base_url="http://localhost:11434",
                model="wizard-model",
                api_key="sk-first-622",
            )
            llm.reconfigure(api_key=SECRET_KEY)

        # reconfigure may keep or rebuild the transport pool; force a rebuild
        # so the header is observed whichever level the fix attaches it at.
        llm._client = None
        merged = self._drive_completion_capture_headers(llm)
        self.assertEqual(merged.get("Authorization"), f"Bearer {SECRET_KEY}")

    @patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"})
    def test_reconfigure_closes_retired_transport_under_loop(self):
        """PRR-005: a key change drops the old AsyncClient (sync callers fall
        back to GC); the async-caller close path is pinned by
        TestReconfigureRetiresTransport below."""
        from app.services.llm_client import LLMClient

        with _settings_values(
            ollama_chat_url="http://localhost:11434",
            chat_model="wizard-model",
            chat_api_key="sk-first-622",
        ):
            llm = LLMClient(
                base_url="http://localhost:11434",
                model="wizard-model",
                api_key="sk-first-622",
            )
            llm.reconfigure(api_key=SECRET_KEY)
        # Sync threadpool context: no running loop — the drop is a GC
        # fallback and must not raise.
        self.assertIsNone(llm._client)

    # ── role-safe configured signals (banner input, gap G5) ────────────

    def test_chat_configured_false_when_pair_empty(self):
        with _settings_values(ollama_chat_url="", chat_model=""):
            response = self.client.get("/api/settings")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["chat_configured"])

    def test_chat_configured_true_for_non_admin_when_pair_configured(self):
        # G5 regression: redaction blanks chat_model for viewers, but the
        # configured signal is computed server-side from the UNREDACTED pair
        # — a non-admin on a configured system must NOT get a false
        # "unconfigured" banner state.
        self._set_role("viewer")
        with _settings_values(
            ollama_chat_url="http://localhost:11434", chat_model="wizard-model"
        ):
            response = self.client.get("/api/settings")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["chat_model"], "")  # redaction still applies
        self.assertTrue(data["chat_configured"])

    def test_instant_configured_reflects_instant_pair(self):
        with _settings_values(
            ollama_chat_url="http://localhost:11434",
            chat_model="wizard-model",
            instant_chat_url="",
            instant_chat_model="",
        ):
            response = self.client.get("/api/settings")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["chat_configured"])
        self.assertFalse(data["instant_configured"])


class TestChatApiKeyLifecycle(_SettingsRouteHarness):
    """Key lifecycle edges beyond the frozen acceptance contract.

    Not selected by the frozen C3/C8 drivers (``-k`` class filters); these
    run with the ordinary suite and pin the plan-critic-required edges:
    empty-key clearing (R6), the instant-override over the chat-key
    fallback, and probe-path log safety.
    """

    def _put_wizard_settings(self, extra=None):
        payload = {
            "ollama_chat_url": "http://localhost:11434",
            "chat_model": "wizard-model",
        }
        payload.update(extra or {})
        return self.client.put("/api/settings", json=payload)

    def _probe(self, payload):
        return self.client.post("/api/settings/probe", json=payload)

    @patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"})
    def test_put_empty_chat_api_key_clears_stored_key(self):
        """Submitting an empty key CLEARS a previously stored key (R6).

        ``""`` is not None, so ``_validate_settings_update`` persists it —
        which is exactly the clearing semantics; the set flag must flip to
        False and the stored row must read empty.
        """
        self.assertEqual(
            self._put_wizard_settings({"chat_api_key": SECRET_KEY}).status_code,
            200,
        )
        clear = self._put_wizard_settings({"chat_api_key": ""})
        self.assertEqual(clear.status_code, 200, clear.text)

        conn = self._pool.get_connection()
        try:
            row = conn.execute(
                "SELECT value FROM settings_kv WHERE key = 'chat_api_key'"
            ).fetchone()
        finally:
            self._pool.release_connection(conn)
        self.assertIsNotNone(row)
        self.assertEqual(json.loads(row[0]), "")

        data = self.client.get("/api/settings").json()
        self.assertEqual(data["chat_api_key"], "")
        self.assertFalse(data["chat_api_key_set"])

    @patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"})
    def test_instant_api_key_overrides_chat_api_key_fallback(self):
        """A dedicated instant key beats the thinking-key fallback (R1)."""
        put = self.client.put(
            "/api/settings",
            json={
                "instant_chat_url": "http://localhost:1234",
                "instant_chat_model": "instant-model",
                "chat_api_key": SECRET_KEY,
                "instant_api_key": "sk-instant-622",
            },
        )
        self.assertEqual(put.status_code, 200, put.text)

        activated = getattr(app.state, "instant_llm_client", None)
        self.assertIsNotNone(activated)
        self.assertEqual(getattr(activated, "api_key", None), "sk-instant-622")

    def test_probe_with_api_key_never_logs_it(self):
        """The probe path must not leak the operator-supplied key either."""
        handler = _CaptureHandler()
        root = logging.getLogger()
        old_level = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)
        self.addCleanup(root.setLevel, old_level)

        with patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"}):
            with patch(
                "app.api.routes.settings.httpx.AsyncClient",
                new=self._fake_async_client(
                    get_response=self._listing_response(["wizard-model"])
                ),
            ):
                response = self._probe(
                    {
                        "target": "thinking",
                        "base_url": "http://localhost:11434",
                        "model": "wizard-model",
                        "api_key": SECRET_KEY,
                    }
                )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "ok")
        leaked = [m for m in handler.messages if SECRET_KEY in m]
        self.assertEqual(leaked, [], f"secret leaked into log records: {leaked!r}")

    @patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"})
    def test_probe_delivers_api_key_as_bearer_header(self):
        """PRR-008: the probe route must attach the supplied key as an
        Authorization header on the outbound request's client."""
        instance = AsyncMock()
        instance.get = AsyncMock(
            return_value=self._listing_response(["wizard-model"])
        )
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=None)
        captured: dict = {}

        def _capturing_factory(*args, **kwargs):
            captured.update(kwargs.get("headers") or {})
            return instance

        with patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"}):
            with patch(
                "app.api.routes.settings.httpx.AsyncClient",
                new=_capturing_factory,
            ):
                response = self._probe(
                    {
                        "target": "thinking",
                        "base_url": "http://localhost:11434",
                        "model": "wizard-model",
                        "api_key": SECRET_KEY,
                    }
                )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            captured.get("Authorization"),
            f"Bearer {SECRET_KEY}",
            f"probe must send Bearer auth, saw client headers: {captured!r}",
        )

    @patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"})
    def test_instant_api_key_empty_clears_stored_key(self):
        """PRR-009: clearing mirrors the chat-key contract for the instant key."""
        self.assertEqual(
            self.client.put(
                "/api/settings",
                json={
                    "instant_chat_url": "http://localhost:1234",
                    "instant_chat_model": "instant-model",
                    "instant_api_key": SECRET_KEY,
                },
            ).status_code,
            200,
        )
        clear = self._put_wizard_settings({"instant_api_key": ""})
        self.assertEqual(clear.status_code, 200, clear.text)

        conn = self._pool.get_connection()
        try:
            row = conn.execute(
                "SELECT value FROM settings_kv WHERE key = 'instant_api_key'"
            ).fetchone()
        finally:
            self._pool.release_connection(conn)
        self.assertIsNotNone(row)
        self.assertEqual(json.loads(row[0]), "")

        data = self.client.get("/api/settings").json()
        self.assertEqual(data["instant_api_key"], "")
        self.assertFalse(data["instant_api_key_set"])

    @patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"})
    def test_editorial_client_carries_chat_api_key(self):
        """PRR-010: editorial stages fall back to the thinking pair, so they
        must inherit its key (else they send unauthenticated requests)."""
        from app.services.llm_client import create_editorial_client

        with _settings_values(
            ollama_chat_url="http://localhost:11434",
            chat_model="wizard-model",
            chat_api_key=SECRET_KEY,
        ):
            client = create_editorial_client()
        self.assertEqual(getattr(client, "api_key", None), SECRET_KEY)

    @patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"})
    def test_put_response_includes_new_flags(self):
        """PRR-011: the PUT response (same _build_settings_dict shape as GET)
        must carry the configured signals and key presence flags."""
        put = self._put_wizard_settings({"chat_api_key": SECRET_KEY})
        self.assertEqual(put.status_code, 200, put.text)
        data = put.json()
        self.assertTrue(data["chat_api_key_set"])
        self.assertTrue(data["chat_configured"])
        self.assertEqual(data["chat_api_key"], "")

    def test_probe_rejects_control_characters_with_422(self):
        """PRR-014: a CRLF-bearing api_key must get a clean validation error,
        not an h11 'Illegal header value' surfacing as 'unreachable'."""
        response = self._probe(
            {
                "target": "thinking",
                "base_url": "http://localhost:11434",
                "model": "wizard-model",
                "api_key": "sk-bad\r\nX-Injected: 1",
            }
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("control characters", response.text)

    def test_probe_rejects_control_characters_in_model(self):
        response = self._probe(
            {
                "target": "thinking",
                "base_url": "http://localhost:11434",
                "model": "wizard-model\nDROP",
            }
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("control characters", response.text)

    def test_probe_rejects_oversized_base_url(self):
        response = self._probe(
            {
                "target": "thinking",
                "base_url": "http://localhost:11434/" + "a" * 2100,
                "model": "wizard-model",
            }
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("2048", response.text)


class TestReconfigureRetiresTransport(unittest.IsolatedAsyncioTestCase):
    """PRR-005: a key change not only drops the old AsyncClient — under a
    running loop (async callers) the retired pool is closed promptly.

    Lives outside the shared sync harness because it drives the client
    directly. Its class name deliberately does not match the frozen C3/C8
    ``-k`` filters; it runs with the ordinary suite.
    """

    @patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"})
    async def test_key_change_schedules_retired_client_close(self):
        from app.services.llm_client import LLMClient

        with _settings_values(
            ollama_chat_url="http://localhost:11434",
            chat_model="wizard-model",
            chat_api_key="sk-first-622",
        ):
            llm = LLMClient(
                base_url="http://localhost:11434",
                model="wizard-model",
                api_key="sk-first-622",
            )
        await llm._ensure_started()
        old_client = llm._client
        self.assertIsNotNone(old_client)
        close_spy = AsyncMock(wraps=old_client.aclose)
        old_client.aclose = close_spy  # type: ignore[method-assign]

        llm.reconfigure(api_key=SECRET_KEY)
        self.assertIsNone(llm._client)
        # Let the scheduled aclose task run.
        await asyncio.sleep(0)
        close_spy.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
