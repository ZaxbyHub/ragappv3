"""Stream body-drain acceptance check for issue #719 (AC6).

Feedback round (PRR-004 / PRR-011, review run pr984-20261019): the original
fake advertised ``aread``/``aiter_bytes``/``aclose`` as equivalent valid drains,
but on real httpx once ``aiter_lines()`` has been entered the first two raise
``StreamConsumed`` and ``aclose()`` closes WITHOUT draining — so an
implementation that regressed connection pooling could pass. The fake now models
those real semantics, and the "trailing bytes are not parsed" contract is
asserted on the yielded events rather than assumed.
"""

from unittest.mock import patch

import httpx
import pytest

from app.services.llm_client import LLMClient


@pytest.fixture(autouse=True)
def _patch_ssrf():
    with patch("app.services.llm_client.assert_url_safe"):
        yield


class DrainingStreamResponse:
    """Fake SSE response that tracks whether its body was drained.

    ``exhausted`` flips True ONLY on a real drain: consuming
    ``aiter_lines()`` to natural EOF. The other body-accessors mirror real
    httpx semantics rather than accepting them as drains —
    ``aread()``/``aiter_bytes()`` raise ``StreamConsumed`` once the line
    iterator has been entered, and ``aclose()`` closes without reading the
    remainder (the exact pre-#719 behavior that cost the connection reuse).
    """

    def __init__(self, sse_lines):
        self._lines = list(sse_lines)
        self.headers = {"content-type": "text/event-stream"}
        self.exhausted = False
        self._iterating = False
        self.closed = False

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        self._iterating = True
        for line in self._lines:
            yield line
        # The generator ran to the natural end of the body: that IS a drain.
        self.exhausted = True

    def _reject_after_iteration(self):
        if self._iterating:
            raise httpx.StreamConsumed()

    async def aiter_bytes(self):
        self._reject_after_iteration()
        yield b""
        self.exhausted = True

    async def aread(self):
        self._reject_after_iteration()
        self.exhausted = True
        return b""

    async def aclose(self):
        # Real httpx: closing does not read the unread remainder.
        self.closed = True


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

    def __init__(self, stream_lines=None, tail_exc=None):
        self._stream_lines = stream_lines or []
        self._tail_exc = tail_exc
        self.last_response = None

    def stream(self, method, url, json=None):
        self.last_response = DrainingStreamResponse(self._stream_lines)
        response = self.last_response

        if self._tail_exc is not None:

            class _FailingResponse(DrainingStreamResponse):
                async def aiter_lines(self):
                    self._iterating = True
                    for line in self._lines:
                        yield line
                    raise self._tail_exc

            failing = _FailingResponse(self._stream_lines)
            failing._tail_exc = self._tail_exc
            self.last_response = failing
            response = failing

        return FakeStreamCM(response)


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
        events = []
        async for ev in client.chat_completion_stream(
            [{"role": "user", "content": "q"}]
        ):
            events.append(ev)
        # Written with `==` so the frozen base failure line reads exactly:
        # assert False == True
        assert fake.last_response.exhausted == True  # noqa: E712
        # PRR-011: trailing bytes are consumed but NOT parsed — the content
        # event after [DONE] must never reach the caller.
        assert events == ["partial"]

    @pytest.mark.asyncio
    async def test_tail_error_does_not_fail_completed_generation(self):
        """PRR-001/F-1: a tail that dies after [DONE] (truncated chunked
        body, peer reset, never-closed stream) must not turn a delivered
        answer into a failed generation or charge the circuit breaker."""
        fake = FakeAsyncHTTP(
            stream_lines=[
                'data: {"choices": [{"delta": {"content": "answer"}}]}',
                "",
                "data: [DONE]",
                "",
            ],
            tail_exc=httpx.RemoteProtocolError("incomplete chunked read"),
        )
        client = LLMClient(base_url="http://llm-test-host:1234", model="m")
        client._client = fake
        events = []
        async for ev in client.chat_completion_stream(
            [{"role": "user", "content": "q"}]
        ):
            events.append(ev)
        assert events == ["answer"]
        assert client.last_metrics.get("status") == "ok"
        assert client._circuit_breaker._fail_counter == 0

    @pytest.mark.asyncio
    async def test_tail_error_after_done_is_not_parsed(self):
        """PRR-011: an in-band error event AFTER [DONE] is trailer noise and
        must not raise — the generation already completed."""
        fake = FakeAsyncHTTP(
            stream_lines=[
                'data: {"choices": [{"delta": {"content": "answer"}}]}',
                "",
                "data: [DONE]",
                "",
                'data: {"error": {"message": "tail noise", "code": 503}}',
                "",
            ]
        )
        client = LLMClient(base_url="http://llm-test-host:1234", model="m")
        client._client = fake
        events = []
        async for ev in client.chat_completion_stream(
            [{"role": "user", "content": "q"}]
        ):
            events.append(ev)
        assert events == ["answer"]
        assert client.last_metrics.get("status") == "ok"
        assert client._circuit_breaker._fail_counter == 0
