"""Tests for finish_reason capture in LLMClient (issue #511 FULL-ENH-01, AC9).

``LLMClient.last_metrics`` — the established diagnostics channel — must carry
``finish_reason`` on both the non-streaming and streaming success paths:

- non-stream: read from ``choices[0].finish_reason`` (None when absent)
- stream: the last non-null ``finish_reason`` seen on SSE choice deltas
  (the pre-fix parser skipped delta-only chunks before it could see it)
- error-metrics paths leave the key absent, exactly as today
- return types and the yielded event vocabulary are unchanged
"""

import json
from typing import Iterable
from unittest.mock import MagicMock, patch

import pytest

from app.services.llm_client import LLMClient


@pytest.fixture(autouse=True)
def _patch_ssrf():
    with patch("app.services.llm_client.assert_url_safe"):
        yield


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.text = json.dumps(payload)
        self.headers = {"content-type": "application/json"}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeStreamResponse:
    def __init__(self, sse_lines: Iterable[str]):
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
    """Exposes exactly the surface LLMClient uses: async post + stream CM."""

    def __init__(self, non_stream_payload=None, stream_lines=None):
        self._payload = non_stream_payload or {}
        self._stream_lines = stream_lines or []

    async def post(self, url, json=None):
        return FakeResponse(self._payload)

    def stream(self, method, url, json=None):
        return FakeStreamCM(FakeStreamResponse(self._stream_lines))


def _client(fake) -> LLMClient:
    client = LLMClient(
        base_url="http://localhost:9999",
        model="finish-reason-test-model",
        cb_name="test_finish_reason",
    )
    client._client = fake
    return client


def _non_stream_payload(finish_reason=None, content="Partial answer cut off"):
    choice = {
        "index": 0,
        "message": {"role": "assistant", "content": content},
    }
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    return {"id": "chatcmpl-test", "object": "chat.completion", "choices": [choice]}


def _stream_lines(finish_reason=None):
    events = [
        'data: {"choices": [{"delta": {"role": "assistant"}}]}',
        'data: {"choices": [{"delta": {"content": "Streamed partial answ"}}]}',
        'data: {"choices": [{"delta": {"content": "er cut at the limit."}}]}',
    ]
    if finish_reason is not None:
        events.append(
            'data: '
            + json.dumps(
                {"choices": [{"delta": {}, "finish_reason": finish_reason}]}
            )
        )
    events.append("data: [DONE]")
    # Proper SSE framing (issue #494 LLM-001): every event is terminated by
    # a blank line; consecutive data: lines without one are ONE multi-line
    # event per the SSE spec. The old fixtures omitted the blank lines,
    # which only worked against the pre-spec parser.
    lines = []
    for event in events:
        lines.append(event)
        lines.append("")
    return lines


class TestNonStreamFinishReason:
    @pytest.mark.asyncio
    async def test_finish_reason_captured(self):
        fake = FakeAsyncHTTP(non_stream_payload=_non_stream_payload("length"))
        client = _client(fake)
        text = await client.chat_completion(
            [{"role": "user", "content": "Explain the retention policy."}],
            max_tokens=64,
        )
        assert text == "Partial answer cut off"
        assert client.last_metrics["finish_reason"] == "length"
        assert client.last_metrics["status"] == "ok"

    @pytest.mark.asyncio
    async def test_finish_reason_none_when_absent(self):
        fake = FakeAsyncHTTP(non_stream_payload=_non_stream_payload(None))
        client = _client(fake)
        await client.chat_completion([{"role": "user", "content": "q"}])
        assert client.last_metrics["finish_reason"] is None

    @pytest.mark.asyncio
    async def test_stop_reason_captured(self):
        fake = FakeAsyncHTTP(non_stream_payload=_non_stream_payload("stop"))
        client = _client(fake)
        await client.chat_completion([{"role": "user", "content": "q"}])
        assert client.last_metrics["finish_reason"] == "stop"

    @pytest.mark.asyncio
    async def test_error_metrics_leave_finish_reason_absent(self):
        class BrokenJsonResponse(FakeResponse):
            def __init__(self):
                super().__init__({})
                self.text = "not-json"

            def json(self):
                raise json.JSONDecodeError("Expecting value", "not-json", 0)

        class BrokenFake(FakeAsyncHTTP):
            async def post(self, url, json=None):
                return BrokenJsonResponse()

        client = _client(BrokenFake())
        from app.services.llm_client import LLMError

        with pytest.raises(LLMError):
            await client.chat_completion([{"role": "user", "content": "q"}])
        assert client.last_metrics["status"] == "invalid_json"
        assert "finish_reason" not in client.last_metrics


class TestStreamFinishReason:
    @pytest.mark.asyncio
    async def test_finish_reason_captured_from_delta_only_chunk(self):
        """The final SSE chunk carries finish_reason with an EMPTY delta —
        the pre-fix parser skipped it before ever reading finish_reason."""
        fake = FakeAsyncHTTP(stream_lines=_stream_lines("length"))
        client = _client(fake)
        chunks = [
            chunk
            async for chunk in client.chat_completion_stream(
                [{"role": "user", "content": "q"}], max_tokens=64
            )
        ]
        assert "".join(chunks) == "Streamed partial answer cut at the limit."
        assert client.last_metrics["finish_reason"] == "length"
        assert client.last_metrics["status"] == "ok"
        assert client.last_metrics["stream"] is True

    @pytest.mark.asyncio
    async def test_last_non_null_wins(self):
        """When multiple finish_reason values appear, the LAST non-null one
        is the one surfaced."""
        lines = _stream_lines("stop")
        # Insert an earlier, superseded finish_reason chunk as its own
        # properly framed event (data line + blank line) after the first one.
        lines.insert(2, 'data: {"choices": [{"delta": {}, "finish_reason": "length"}]}')
        lines.insert(3, "")
        fake = FakeAsyncHTTP(stream_lines=lines)
        client = _client(fake)
        async for _ in client.chat_completion_stream(
            [{"role": "user", "content": "q"}]
        ):
            pass
        assert client.last_metrics["finish_reason"] == "stop"

    @pytest.mark.asyncio
    async def test_none_when_never_seen(self):
        fake = FakeAsyncHTTP(stream_lines=_stream_lines(None))
        client = _client(fake)
        async for _ in client.chat_completion_stream(
            [{"role": "user", "content": "q"}]
        ):
            pass
        assert client.last_metrics["status"] == "ok"
        assert client.last_metrics["finish_reason"] is None

    @pytest.mark.asyncio
    async def test_yielded_events_are_plain_content_strings(self):
        fake = FakeAsyncHTTP(stream_lines=_stream_lines("length"))
        client = _client(fake)
        chunks = [
            chunk
            async for chunk in client.chat_completion_stream(
                [{"role": "user", "content": "q"}]
            )
        ]
        assert chunks  # something was yielded
        assert all(isinstance(c, str) for c in chunks)

    @pytest.mark.asyncio
    async def test_stream_error_metrics_leave_finish_reason_absent(self):
        import httpx

        class FailingStream(FakeAsyncHTTP):
            def stream(self, method, url, json=None):
                raise httpx.RequestError("connection reset")

        client = _client(FailingStream())
        from app.services.llm_client import LLMError

        with pytest.raises(LLMError):
            async for _ in client.chat_completion_stream(
                [{"role": "user", "content": "q"}]
            ):
                pass
        assert client.last_metrics.get("status") in ("error", "request_error")
        assert "finish_reason" not in client.last_metrics
