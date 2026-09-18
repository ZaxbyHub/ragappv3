"""Route-level unconfigured-chat behavior (PR #619 review finding C3).

test_unconfigured_models.py pins the factory/engine/checker layers; these
tests pin the HTTP layer of the same contract: an unconfigured mode answers
409 with setup guidance, including the instant mode specifically and the
partial-configuration state (URL set, model empty) — which must NOT slip
through the pair check. Dependencies are overridden so the guard is what is
under test, not auth or engine wiring.
"""

from contextlib import contextmanager

# This module mentions csrf only to override the dependency for direct
# route testing; it does not test CSRF itself (issue #202 / ENH-008).
CSRF_TEST_POLICY = "naive"

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


@contextmanager
def _settings_values(**overrides):
    previous = {name: getattr(settings, name) for name in overrides}
    for name, value in overrides.items():
        setattr(settings, name, value)
    try:
        yield
    finally:
        for name, value in previous.items():
            setattr(settings, name, value)


class _UnconfiguredChatRoutes:
    def setup_method(self):
        from app.api.deps import (
            get_current_active_user,
            get_evaluate_policy,
            require_model_ready,
        )
        from app.api.routes.chat import get_rag_engine
        from app.security import csrf_protect

        self._overrides = [
            get_current_active_user,
            get_evaluate_policy,
            get_rag_engine,
            require_model_ready,
            csrf_protect,
        ]
        app.dependency_overrides[get_current_active_user] = lambda: {
            "id": 0,
            "username": "admin",
            "role": "superadmin",
            "is_active": 1,
            "must_change_password": 0,
        }
        app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"

        async def allow_policy(user, resource_type, resource_id, action):
            return True

        app.dependency_overrides[get_evaluate_policy] = lambda: allow_policy
        app.dependency_overrides[get_rag_engine] = lambda: object()
        app.dependency_overrides[require_model_ready] = lambda: object()

    def teardown_method(self):
        for dep in self._overrides:
            app.dependency_overrides.pop(dep, None)

    def _post_chat(self, mode=None):
        payload = {"message": "hello"}
        if mode is not None:
            payload["mode"] = mode
        # raise_server_exceptions=False: the placeholder engine is expected
        # to fail downstream once past the 409 guard under test.
        client = TestClient(app, raise_server_exceptions=False)
        return client.post("/api/chat", json=payload)


class TestUnconfiguredChatRoutes(_UnconfiguredChatRoutes):
    def test_instant_mode_unconfigured_is_409_with_guidance(self):
        with _settings_values(
            ollama_chat_url="", chat_model="", instant_chat_url="", instant_chat_model=""
        ):
            response = self._post_chat(mode="instant")
        assert response.status_code == 409
        assert "Settings" in response.json()["detail"]

    def test_partial_config_url_set_model_empty_is_409(self):
        # URL alone must not satisfy the pair check (PR #619 review C3).
        with _settings_values(ollama_chat_url="http://localhost:11434", chat_model=""):
            response = self._post_chat(mode="thinking")
        assert response.status_code == 409
        assert "CHAT_MODEL" in response.json()["detail"]

    def test_partial_config_model_set_url_empty_is_409(self):
        with _settings_values(ollama_chat_url="", chat_model="some-model"):
            response = self._post_chat()
        assert response.status_code == 409

    def test_configured_thinking_passes_the_guard(self):
        # The guard must not over-reject: with a complete pair the request
        # proceeds past the 409 — proven here by a request-version 400 (the
        # placeholder engine has no async query), never a 409.
        with _settings_values(
            ollama_chat_url="http://localhost:11434", chat_model="some-model"
        ):
            response = self._post_chat()
        assert response.status_code != 409
        assert response.status_code >= 400  # placeholder engine fails downstream


class TestUnconfiguredDeepHealth:
    def test_check_models_with_empty_chat_urls_returns_not_configured(self):
        """F-003: empty URLs must short-circuit, not crash the SSRF gate."""
        import asyncio

        from app.services.model_checker import ModelChecker

        async def run():
            return await ModelChecker().check_models()

        with _settings_values(
            ollama_embedding_url="",
            ollama_chat_url="",
            chat_model="",
            instant_chat_url="",
            instant_chat_model="",
        ):
            result = asyncio.run(run())
        assert result["chat_model"]["status"] == "not_configured"
        assert result["instant_chat_model"]["status"] == "not_configured"
        # No URL ever reaches the SSRF gate or DNS: every empty pair
        # short-circuits (the embedding URL is emptied here only to keep the
        # probe offline-safe; it ships configured in production).
