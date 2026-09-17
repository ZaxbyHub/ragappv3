"""Unconfigured-model behavior (issue #570 re-scope).

The system ships no model defaults: chat endpoints are configured by the
operator (first setup / Settings -> Models, or env vars). These tests pin
the designed "unconfigured" mode:

- client factories refuse to construct with an empty URL/model pair and
  raise ``ModelNotConfiguredError`` with actionable guidance instead of
  silently building a client that would send garbage model names;
- the draft pipeline's model-name resolver no longer substitutes the
  literal strings "thinking"/"instant" when nothing is configured;
- the chat route guard turns an unconfigured mode into HTTP 409 with
  setup guidance;
- the model checker reports ``not_configured`` without probing.
"""

from contextlib import contextmanager

import pytest

from app.config import settings


@contextmanager
def _settings_values(**overrides):
    """Temporarily override settings attributes (restored on exit)."""
    previous = {name: getattr(settings, name) for name in overrides}
    for name, value in overrides.items():
        setattr(settings, name, value)
    try:
        yield
    finally:
        for name, value in previous.items():
            setattr(settings, name, value)


class TestFactoryGuards:
    def test_thinking_factory_raises_when_unconfigured(self):
        from app.services.llm_client import (
            ModelNotConfiguredError,
            create_thinking_client,
        )

        with _settings_values(ollama_chat_url="", chat_model=""):
            with pytest.raises(ModelNotConfiguredError, match="Settings"):
                create_thinking_client()

    def test_instant_factory_raises_when_unconfigured(self):
        from app.services.llm_client import (
            ModelNotConfiguredError,
            create_instant_client,
        )

        with _settings_values(instant_chat_url="", instant_chat_model=""):
            with pytest.raises(ModelNotConfiguredError, match="Settings"):
                create_instant_client()

    def test_partial_configuration_still_raises(self):
        from app.services.llm_client import (
            ModelNotConfiguredError,
            create_thinking_client,
        )

        with _settings_values(ollama_chat_url="http://localhost:11434", chat_model=""):
            with pytest.raises(ModelNotConfiguredError):
                create_thinking_client()

    def test_editorial_factory_raises_when_thinking_also_unconfigured(self):
        from app.services.llm_client import (
            ModelNotConfiguredError,
            create_editorial_client,
        )

        with _settings_values(
            editorial_chat_url="", editorial_chat_model="", ollama_chat_url="", chat_model=""
        ):
            with pytest.raises(ModelNotConfiguredError):
                create_editorial_client()

    def test_configured_thinking_factory_constructs(self, monkeypatch):
        monkeypatch.setenv("ALLOW_LOCAL_SERVICES", "1")
        from app.services.llm_client import create_thinking_client

        with _settings_values(
            ollama_chat_url="http://localhost:11434", chat_model="some-model"
        ):
            client = create_thinking_client()
        assert client.model == "some-model"


class TestDraftModelNameResolution:
    def test_thinking_name_is_not_the_literal_fallback(self):
        from app.services.draft_pipeline import _provider_model_name

        with _settings_values(chat_model="", editorial_chat_model=""):
            assert _provider_model_name("thinking") != "thinking"
            assert _provider_model_name("editorial") != "thinking"

    def test_instant_name_is_not_the_literal_fallback(self):
        from app.services.draft_pipeline import _provider_model_name

        with _settings_values(instant_chat_model=""):
            assert _provider_model_name("instant") != "instant"

    def test_configured_names_resolve_normally(self):
        from app.services.draft_pipeline import _provider_model_name

        with _settings_values(chat_model="cfg-model", instant_chat_model="cfg-instant"):
            assert _provider_model_name("thinking") == "cfg-model"
            assert _provider_model_name("instant") == "cfg-instant"


class TestChatModeGuard:
    def test_unconfigured_mode_returns_409_with_guidance(self):
        from fastapi import HTTPException

        from app.api.routes.chat import require_mode_configured

        with _settings_values(ollama_chat_url="", chat_model=""):
            with pytest.raises(HTTPException) as excinfo:
                require_mode_configured("thinking")
        assert excinfo.value.status_code == 409
        assert "Settings" in excinfo.value.detail

    def test_configured_mode_passes(self):
        from app.api.routes.chat import require_mode_configured

        with _settings_values(
            ollama_chat_url="http://localhost:11434", chat_model="some-model"
        ):
            assert require_mode_configured("thinking") is None


class TestModelCheckerNotConfigured:
    def test_empty_pair_reports_not_configured_without_probe(self):
        import asyncio

        from app.services.model_checker import ModelChecker

        async def run():
            checker = ModelChecker()
            return await checker._check_model_availability(
                None, "", ""
            )

        result = asyncio.run(run())
        assert result["status"] == "not_configured"


class TestLifespanClientCreation:
    """The lifespan creation block returns None clients when unconfigured."""

    def test_creation_helper_returns_none_pair_when_unconfigured(self):
        from app.lifespan import _create_llm_clients

        with _settings_values(
            ollama_chat_url="", chat_model="", instant_chat_url="", instant_chat_model=""
        ):
            thinking, instant = _create_llm_clients()
        assert thinking is None
        assert instant is None

    def test_creation_helper_builds_clients_when_configured(self, monkeypatch):
        monkeypatch.setenv("ALLOW_LOCAL_SERVICES", "1")
        from app.lifespan import _create_llm_clients

        with _settings_values(
            ollama_chat_url="http://localhost:11434",
            chat_model="some-model",
            instant_chat_url="http://localhost:1234",
            instant_chat_model="some-instant",
        ):
            thinking, instant = _create_llm_clients()
        assert thinking is not None and thinking.model == "some-model"
        assert instant is not None and instant.model == "some-instant"

    def test_keepalive_helper_is_a_noop_for_none_client(self):
        from app.lifespan import _start_llm_keepalive

        assert _start_llm_keepalive(None) is None


def _bare_rag_engine():
    """A real RAGEngine with lightweight dummy dependencies (no services)."""
    from app.services.rag_engine import RAGEngine

    return RAGEngine(
        embedding_service=object(),
        vector_store=object(),
        memory_store=object(),
        llm_client=None,
        reranking_service=None,
    )


class TestRAGEngineNoneClient:
    """RAGEngine accepts llm_client=None (unconfigured boot) without
    constructing a garbage client, and rebind_clients wires late activation."""

    def test_constructor_accepts_none_llm_client(self):
        engine = _bare_rag_engine()
        assert engine.llm_client is None
        assert engine.thinking_client is None
        assert engine.instant_client is None

    def test_rebind_clients_wires_late_activation(self, monkeypatch):
        monkeypatch.setenv("ALLOW_LOCAL_SERVICES", "1")
        from app.services.llm_client import create_thinking_client

        engine = _bare_rag_engine()
        with _settings_values(
            ollama_chat_url="http://localhost:11434", chat_model="late-model"
        ):
            client = create_thinking_client()
        engine.rebind_clients(thinking_client=client)
        assert engine.thinking_client is client
        assert engine.instant_client is None


class TestHotRebindActivation:
    """Configuring a model via Settings activates a previously-None client.

    ``_hot_rebind_llm_clients`` stays synchronous: late-activated clients are
    constructed and assigned here; LLMClient's lazy ``_ensure_started`` starts
    the transport on the first request's event loop (no cross-loop hazard),
    so the sync settings handlers need no loop.
    """

    def _fake_app(self):
        import types

        state = types.SimpleNamespace(
            thinking_llm_client=None,
            instant_llm_client=None,
            llm_client=None,
            rag_engine=_bare_rag_engine(),
        )
        return types.SimpleNamespace(state=state)

    def test_completing_the_thinking_pair_activates_the_client(self, monkeypatch):
        monkeypatch.setenv("ALLOW_LOCAL_SERVICES", "1")
        from app.api.routes.settings import SettingsUpdate, _hot_rebind_llm_clients

        app = self._fake_app()
        with _settings_values(
            ollama_chat_url="http://localhost:11434",
            chat_model="late-model",
        ):
            update = SettingsUpdate(
                ollama_chat_url="http://localhost:11434", chat_model="late-model"
            )
            _hot_rebind_llm_clients(app, update)
        assert app.state.thinking_llm_client is not None
        assert app.state.thinking_llm_client.model == "late-model"
        assert app.state.llm_client is app.state.thinking_llm_client
        assert app.state.rag_engine.thinking_client is app.state.thinking_llm_client

    def test_unconfigured_update_leaves_clients_none(self):
        from app.api.routes.settings import SettingsUpdate, _hot_rebind_llm_clients

        app = self._fake_app()
        with _settings_values(ollama_chat_url="", chat_model=""):
            update = SettingsUpdate()
            _hot_rebind_llm_clients(app, update)
        assert app.state.thinking_llm_client is None
        assert app.state.instant_llm_client is None
        assert app.state.rag_engine.llm_client is None


class TestIngestionDetachOnClearedPair:
    """Final-critic Round 3: clearing a selected mode's endpoint pair must
    detach the background processor's client, not merely park it."""

    def _fake_app(self, ingestion_mode, has_processor=True):
        import types

        calls = []

        class _Recorder:
            def set_llm_client(self, client):
                calls.append(client)

        state = types.SimpleNamespace(
            thinking_llm_client=object(),
            instant_llm_client=object(),
            background_processor=_Recorder() if has_processor else None,
            rag_engine=_bare_rag_engine(),
        )
        state.llm_client = state.thinking_llm_client
        app = types.SimpleNamespace(state=state)
        return app, calls

    def test_clearing_selected_instant_pair_detaches_ingestion_client(self):
        from app.api.routes.settings import SettingsUpdate, _hot_rebind_llm_clients

        app, calls = self._fake_app("instant")
        with _settings_values(
            ingestion_llm_mode="instant",
            instant_chat_url="http://localhost:1234",
            instant_chat_model="m",
        ):
            update = SettingsUpdate(instant_chat_url="")
            settings.instant_chat_url = ""
            _hot_rebind_llm_clients(app, update)
        assert calls == [None], f"expected detach, got {calls}"

    def test_clearing_selected_thinking_pair_detaches_ingestion_client(self):
        # The Settings schema forbids an empty chat_model at validation, so
        # the realistic clearing path is the URL field.
        from app.api.routes.settings import SettingsUpdate, _hot_rebind_llm_clients

        app, calls = self._fake_app("thinking")
        with _settings_values(
            ingestion_llm_mode="thinking",
            ollama_chat_url="http://localhost:11434",
            chat_model="m",
        ):
            update = SettingsUpdate(ollama_chat_url="")
            settings.ollama_chat_url = ""
            _hot_rebind_llm_clients(app, update)
        assert calls == [None], f"expected detach, got {calls}"

    def test_untouched_pair_does_not_touch_processor(self):
        from app.api.routes.settings import SettingsUpdate, _hot_rebind_llm_clients

        app, calls = self._fake_app("instant")
        with _settings_values(ingestion_llm_mode="instant"):
            update = SettingsUpdate()  # nothing relevant
            _hot_rebind_llm_clients(app, update)
        assert calls == []

    def test_mode_change_still_rebinds(self):
        from app.api.routes.settings import SettingsUpdate, _hot_rebind_llm_clients

        app, calls = self._fake_app("disabled")
        with _settings_values(ingestion_llm_mode="disabled"):
            update = SettingsUpdate(ingestion_llm_mode="disabled")
            _hot_rebind_llm_clients(app, update)
        assert calls == [None]
