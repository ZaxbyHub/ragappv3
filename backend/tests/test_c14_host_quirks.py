"""Host-quirk LLM client acceptance checks for issue #719 (AC2, AC8)."""

import logging
import re
from unittest.mock import patch

import httpx
import pytest

from app.services.llm_client import LLMClient


@pytest.fixture(autouse=True)
def _patch_ssrf():
    with patch("app.services.llm_client.assert_url_safe"):
        yield


class FakeStreamResponse:
    def __init__(self, sse_lines):
        self._lines = list(sse_lines)
        self.headers = {"content-type": "text/event-stream"}

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class FakeStreamCM:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeAsyncHTTP:
    """Exposes exactly the streaming surface LLMClient uses; ``stream``
    deliberately has no ``headers=`` parameter (correlation headers are
    only bound inside request telemetry context)."""

    def __init__(self, stream_lines=None):
        self._stream_lines = stream_lines or []

    def stream(self, method, url, json=None):
        return FakeStreamCM(FakeStreamResponse(self._stream_lines))


class TestInstantReasoningLeak:
    @pytest.mark.asyncio
    async def test_instant_reasoning_leak_is_logged(self, caplog):
        """A no-think Instant client whose stream still emits a reasoning
        delta must surface the leak in the client's WARNING logs."""
        # Built the way create_instant_client builds it for a Qwen-family
        # model with instant_enable_thinking disabled (issue #554 posture).
        client = LLMClient(
            base_url="http://lm-studio.local:1234",
            model="qwen3.5-8b-instant",
            cb_name="llm_instant",
            chat_template_kwargs={"enable_thinking": False},
        )
        client._client = FakeAsyncHTTP(
            stream_lines=[
                'data: {"choices": [{"delta": {"reasoning_content": "leak"}}]}',
                "",
                "data: [DONE]",
                "",
            ]
        )
        with caplog.at_level(logging.WARNING, logger="app.services.llm_client"):
            async for _ in client.chat_completion_stream(
                [{"role": "user", "content": "q"}]
            ):
                pass
        hits = [
            record
            for record in caplog.records
            if record.name == "app.services.llm_client"
            and record.levelno >= logging.WARNING
            and re.search(r"(?i)reason|think", record.getMessage())
        ]
        assert len(hits) >= 1

    @pytest.mark.asyncio
    async def test_reasoning_leak_warns_once_per_stream(self, caplog):
        """PRR-007: the warning is one-shot PER STREAM, not per delta. Three
        reasoning deltas must produce exactly one warning — an
        implementation that warns on every delta fails here."""
        client = LLMClient(
            base_url="http://lm-studio.local:1234",
            model="qwen3.5-8b-instant",
            cb_name="llm_instant",
            chat_template_kwargs={"enable_thinking": False},
        )
        client._client = FakeAsyncHTTP(
            stream_lines=[
                'data: {"choices": [{"delta": {"reasoning_content": "one"}}]}',
                "",
                'data: {"choices": [{"delta": {"reasoning_content": "two"}}]}',
                "",
                'data: {"choices": [{"delta": {"reasoning_content": "three"}}]}',
                "",
                "data: [DONE]",
                "",
            ]
        )
        with caplog.at_level(logging.WARNING, logger="app.services.llm_client"):
            async for _ in client.chat_completion_stream(
                [{"role": "user", "content": "q"}]
            ):
                pass
        hits = [
            record
            for record in caplog.records
            if record.name == "app.services.llm_client"
            and record.levelno >= logging.WARNING
            and re.search(r"(?i)reason|think", record.getMessage())
        ]
        assert len(hits) == 1

    @pytest.mark.asyncio
    async def test_new_lm_studio_404_warning_is_actionable(self, caplog):
        """EXT-NEW-2: a 404 from the LM Studio load endpoint can mean either
        a pre-v1 server or an unknown model id; the warning must not blame
        the API version unconditionally, and must name the model."""
        requests: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request.url.path)
            return httpx.Response(
                404, json={"error": "model not found"}, request=request
            )

        with patch("app.services.llm_client.assert_url_safe"):
            llm = LLMClient(
                base_url="http://lm-studio.local:1234",
                model="ghost-model",
                context_length=4096,
            )
        llm._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with caplog.at_level(logging.WARNING, logger="app.services.llm_client"):
                ok = await llm.prime_residency()
        finally:
            await llm._client.aclose()
        assert ok is False
        assert requests == ["/api/v1/models/load"]
        warnings = [
            record
            for record in caplog.records
            if record.name == "app.services.llm_client"
            and record.levelno >= logging.WARNING
        ]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        # Names the model so an operator can tell "wrong model id" from
        # "old server", and does not assert the v1 API is the cause.
        assert "ghost-model" in message
        assert "does not know the model id" in message


class TestNoV0LoadFallback:
    @pytest.mark.asyncio
    async def test_no_v0_load_fallback(self):
        """When the LM Studio v1 load endpoint 404s, the legacy v0 endpoint
        must not be probed; priming reports failure."""
        requests: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request.url.path)
            return httpx.Response(
                404, json={"error": "not found"}, request=request
            )

        llm = LLMClient(
            base_url="http://lm-studio.local:1234",
            model="m",
            context_length=4096,
        )
        llm._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            ok = await llm.prime_residency()
        finally:
            await llm._client.aclose()
        v0_loads = [path for path in requests if path == "/api/v0/models/load"]
        assert len(v0_loads) == 0
        assert ok is False
