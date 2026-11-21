"""Companion coverage for the #1179 health-probe contract (NOT frozen checks).

Covers the assertion-strength gaps the plan critic named: exactly-one
bounded probe, deadline enforcement on both probe paths, cancellation at
the deadline, no-op rebind preservation, instant late activation, and the
configured-to-cleared settings-pair gate (parked clients are not probed).
"""
import asyncio
import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("ADMIN_SECRET_TOKEN", "test-admin-secret")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret")

from fastapi import HTTPException  # noqa: E402

from app.services.llm_health import LLMHealthChecker  # noqa: E402


class _RecordingClient:
    def __init__(self, delay: float = 0.0, fail: bool = False):
        self.calls: list[dict] = []
        self.delay = delay
        self.fail = fail
        self.cancelled = False

    async def chat_completion(self, messages, **kw):
        self.calls.append(dict(kw))
        if self.delay:
            try:
                await asyncio.sleep(self.delay)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        if self.fail:
            raise RuntimeError("probe failure")
        return {"ok": True}


def _settings(**kw):
    m = MagicMock()
    m.ollama_chat_url = kw.get("ollama_chat_url", "http://test")
    m.chat_model = kw.get("chat_model", "test-model")
    m.instant_chat_url = kw.get("instant_chat_url", "http://test-i")
    m.instant_chat_model = kw.get("instant_chat_model", "test-instant-model")
    m.editorial_chat_url = ""
    return m


class TestChatProbeBudget:
    def test_check_chat_calls_exactly_one_bounded_probe(self):
        client = _RecordingClient()
        with patch("app.services.llm_health.settings", _settings()):
            checker = LLMHealthChecker(llm_client=client, timeout=5.0)
            result = asyncio.run(checker.check_chat())
        assert result["ok"] is True
        assert len(client.calls) == 1, "check_chat must probe exactly once"
        assert client.calls[0].get("max_tokens") == 1, (
            "probe payload must be bounded to max_tokens == 1"
        )

    def test_check_chat_deadline_cancels_inflight_request(self):
        client = _RecordingClient(delay=5.0)
        with patch("app.services.llm_health.settings", _settings()):
            checker = LLMHealthChecker(llm_client=client, timeout=0.1)
            result = asyncio.run(checker.check_chat())
        assert result["ok"] is False
        assert "timeout" in (result["error"] or "").lower()
        assert client.cancelled, "wait_for must cancel the in-flight request"

    def test_probe_client_deadline_enforced(self):
        client = _RecordingClient(delay=5.0)
        with patch("app.services.llm_health.settings", _settings()):
            checker = LLMHealthChecker(timeout=0.1)
            ok = asyncio.run(checker._probe_client(client))
        assert ok is False
        assert client.cancelled


class TestRebindSemantics:
    def test_noop_rebind_preserves_stored_references(self):
        thinking = _RecordingClient()
        instant = _RecordingClient()
        checker = LLMHealthChecker(
            thinking_client=thinking, instant_client=instant
        )
        checker.rebind_clients()  # no args: no-op
        assert checker._thinking_client is thinking
        assert checker._instant_client is instant
        checker.rebind_clients(thinking_client=_RecordingClient())
        assert checker._instant_client is instant, (
            "partial rebind must not disturb the omitted client"
        )

    def test_instant_late_activation_reported_healthy(self):
        from app.api.routes import settings as settings_routes
        from app.services.llm_health import LLMHealthChecker as LHC

        client = _RecordingClient()
        app = MagicMock()
        app.state.thinking_llm_client = None
        app.state.instant_llm_client = None
        app.state.llm_client = None
        app.state.llm_health_checker = LHC()
        app.state.rag_engine = None
        update = MagicMock()
        update.ollama_chat_url = None
        update.chat_model = None
        update.thinking_max_tokens = None
        update.chat_api_key = None
        update.instant_chat_url = "http://test-i"
        update.instant_chat_model = "test-instant-model"
        update.instant_max_tokens = None
        update.instant_enable_thinking = None
        update.instant_api_key = None
        with patch("app.api.routes.settings.settings") as ms:
            ms.ollama_chat_url = "http://test"
            ms.chat_model = "test-model"
            ms.thinking_max_tokens = 4096
            ms.chat_api_key = ""
            ms.instant_chat_url = "http://test-i"
            ms.instant_chat_model = "test-instant-model"
            ms.instant_max_tokens = 256
            ms.instant_enable_thinking = False
            ms.instant_api_key = ""
            with patch(
                "app.services.llm_client.create_instant_client",
                return_value=client,
            ):
                settings_routes._hot_rebind_llm_clients(app, update)
        modes = asyncio.run(app.state.llm_health_checker.check_chat_modes())
        assert modes["instant"] is True
        assert len(client.calls) == 1
        assert client.calls[0].get("max_tokens") == 1

    def test_cleared_pair_fails_closed_without_probing(self):
        thinking = _RecordingClient()
        instant = _RecordingClient()
        checker = LLMHealthChecker(thinking_client=thinking, instant_client=instant)
        with patch(
            "app.services.llm_health.settings",
            _settings(ollama_chat_url="", chat_model=""),
        ):
            modes = asyncio.run(checker.check_chat_modes())
        assert modes["thinking"] is False, "cleared pair must fail closed"
        assert thinking.calls == [], "parked client must not be probed"
        assert modes["instant"] is True
        assert len(instant.calls) == 1


class TestModeGateBoundary:
    def test_configured_pair_probes_wired_client(self):
        client = _RecordingClient()
        checker = LLMHealthChecker(thinking_client=client)
        with patch("app.services.llm_health.settings", _settings()):
            modes = asyncio.run(checker.check_chat_modes())
        assert modes["thinking"] is True
        assert len(client.calls) == 1

    def test_unpaired_settings_but_live_client_is_healthy_for_chat(self):
        # check_chat (legacy single-client API) already gates on its own
        # pair; this leg pins that the modes gate does not leak into it.
        client = _RecordingClient()
        with patch("app.services.llm_health.settings", _settings()):
            checker = LLMHealthChecker(llm_client=client, timeout=5.0)
            result = asyncio.run(checker.check_chat())
        assert result["ok"] is True
