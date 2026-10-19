"""In-band SSE error event acceptance check for issue #719 (AC5)."""

from unittest.mock import patch

import pytest

from app.services.llm_client import LLMClient, LLMError


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
    deliberately has no ``headers=`` parameter."""

    def __init__(self, stream_lines=None):
        self._stream_lines = stream_lines or []

    def stream(self, method, url, json=None):
        return FakeStreamCM(FakeStreamResponse(self._stream_lines))


class TestInbandErrorRaises:
    @pytest.mark.asyncio
    async def test_inband_error_event_raises(self):
        """An SSE event carrying a top-level ``error`` object mid-stream
        must surface as LLMError, not be swallowed past ``[DONE]``."""
        client = LLMClient(base_url="http://llm-test-host:1234/v1", model="m")
        client._client = FakeAsyncHTTP(
            stream_lines=[
                'data: {"choices": [{"delta": {"content": "partial"}}]}',
                "",
                'data: {"error": {"message": "model overloaded", "code": 500}}',
                "",
                "data: [DONE]",
                "",
            ]
        )
        with pytest.raises(LLMError):
            async for _ in client.chat_completion_stream(
                [{"role": "user", "content": "q"}]
            ):
                pass
