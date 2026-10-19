"""Stream body-drain acceptance check for issue #719 (AC6)."""

from unittest.mock import patch

import pytest

from app.services.llm_client import LLMClient


@pytest.fixture(autouse=True)
def _patch_ssrf():
    with patch("app.services.llm_client.assert_url_safe"):
        yield


class DrainingStreamResponse:
    """Fake SSE response that tracks whether its body was drained.

    ``exhausted`` flips True only on a real drain: consuming
    ``aiter_lines()`` to natural EOF, ``aread()``, ``aclose()``, or
    ``aiter_bytes()`` to EOF. Breaking out of the line iterator mid-body —
    the behavior after ``[DONE]`` when nothing drains the remainder —
    leaves it False.
    """

    def __init__(self, sse_lines):
        self._lines = list(sse_lines)
        self.headers = {"content-type": "text/event-stream"}
        self.exhausted = False

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line
        # The generator ran to the natural end of the body: that IS a drain.
        self.exhausted = True

    async def aiter_bytes(self):
        yield b""
        self.exhausted = True

    async def aread(self):
        self.exhausted = True
        return b""

    async def aclose(self):
        self.exhausted = True


class FakeStreamCM:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        # Deliberately does NOT close the response: draining the body is
        # the client's contract under test, not the context manager's.
        return False


class FakeAsyncHTTP:
    """Exposes exactly the streaming surface LLMClient uses; ``stream``
    deliberately has no ``headers=`` parameter, and the last response is
    kept for inspection."""

    def __init__(self, stream_lines=None):
        self._stream_lines = stream_lines or []
        self.last_response = None

    def stream(self, method, url, json=None):
        self.last_response = DrainingStreamResponse(self._stream_lines)
        return FakeStreamCM(self.last_response)


class TestBodyDrainedAfterDone:
    @pytest.mark.asyncio
    async def test_body_drained_after_done(self):
        """After the [DONE] event the response body must be drained — the
        remainder of the stream (comment + trailing event) cannot be left
        unread on the connection."""
        fake = FakeAsyncHTTP(
            stream_lines=[
                'data: {"choices": [{"delta": {"content": "partial"}}]}',
                "",
                "data: [DONE]",
                "",
                ": stream complete",
                'data: {"choices": [{"delta": {"content": "unconsumed"}}]}',
                "",
            ]
        )
        client = LLMClient(base_url="http://llm-test-host:1234", model="m")
        client._client = fake
        async for _ in client.chat_completion_stream(
            [{"role": "user", "content": "q"}]
        ):
            pass
        # Written with `==` so the frozen base failure line reads exactly:
        # assert False == True
        assert fake.last_response.exhausted == True  # noqa: E712
