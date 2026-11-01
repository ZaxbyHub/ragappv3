"""Acceptance checks for the LLM client HTTP transport contract (issue #717).

Four transport-level criteria, exercised against a real ``LLMClient`` with
only the HTTP layer faked (``httpx.MockTransport`` wire doubles for the
request/response tests, the ``FakeAsyncHTTP``/``FakeStreamCM`` SSE fakes for
the stream-parser tests):

- AC1: a request-time ``URLBlocked`` (SSRFSafeTransport re-resolution
  failure surfacing from the transport) must escape ``chat_completion`` as
  ``LLMError`` carrying the ``URLBlocked`` as its cause.
- AC2: a non-SSE HTTP 200 answer to a ``stream=True`` request must be read
  from that single already-issued POST — no second request on the wire.
- AC8: a stream aborted by an open circuit breaker must not leave stale
  ``last_metrics["status"] == "ok"`` from an earlier successful call.
- AC10: whitespace yielded ahead of a legacy ``_lhs ... _rhs`` thinking
  marker must be counted in ``completion_tokens_estimate``.

RED at base (master), GREEN after the fix.
"""

import json
import unittest
from typing import Iterable
from unittest.mock import patch

import httpx
import pytest

from app.services.circuit_breaker import CircuitBreakerState
from app.services.llm_client import LLMClient, LLMError
from app.services.ssrf import URLBlocked


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
        model="c12-transport-model",
        cb_name="test_c12_transport",
    )
    client._client = fake
    return client


class _SSRFPatchedTestCase(unittest.IsolatedAsyncioTestCase):
    """Shared setUp: neutralize the constructor-time URL guard only."""

    def setUp(self):
        patcher = patch("app.services.llm_client.assert_url_safe")
        patcher.start()
        self.addCleanup(patcher.stop)


class TestURLBlockedMapping(_SSRFPatchedTestCase):
    async def test_url_blocked_maps_to_llm_error(self):
        """AC1 (issue #717): WHEN the HTTP transport under
        ``LLMClient.chat_completion`` raises ``URLBlocked`` (request-time
        re-resolution failure) THE SYSTEM SHALL raise ``LLMError`` from
        ``chat_completion``, with the ``URLBlocked`` as its cause.
        RED at base (master), GREEN after the fix.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            raise URLBlocked("URL host did not resolve at request time.")

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = _client(http)
        try:
            with pytest.raises(LLMError) as excinfo:
                await client.chat_completion([{"role": "user", "content": "q"}])
            assert isinstance(excinfo.value.__cause__, URLBlocked)
        finally:
            await http.aclose()


class TestNonSSEStreamFallback(_SSRFPatchedTestCase):
    async def test_non_sse_stream_reads_first_body(self):
        """AC2 (issue #717): WHEN a ``stream=True`` request returns HTTP 200
        with ``application/json`` (non-SSE) carrying a complete answer THE
        SYSTEM SHALL issue exactly one POST and yield that response's answer
        text. RED at base (master), GREEN after the fix.
        """
        calls = {"posts": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["posts"] += 1
            answer = "FIRST" if calls["posts"] == 1 else "SECOND"
            body = {
                "choices": [{"message": {"role": "assistant", "content": answer}}]
            }
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                text=json.dumps(body),
            )

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = _client(http)
        try:
            chunks = []
            async for chunk in client.chat_completion_stream(
                [{"role": "user", "content": "q"}]
            ):
                if isinstance(chunk, str):
                    chunks.append(chunk)
            assert calls["posts"] == 1
            assert "".join(chunks) == "FIRST"
        finally:
            await http.aclose()


class TestStreamCircuitOpenMetrics(_SSRFPatchedTestCase):
    async def test_circuit_open_stream_does_not_keep_prior_ok_metrics(self):
        """AC8 (issue #717): WHEN ``chat_completion_stream`` raises because
        the circuit is open, after an earlier successful call left
        ``last_metrics["status"] == "ok"`` THE SYSTEM SHALL leave
        ``last_metrics["status"]`` at a value other than "ok".
        RED at base (master), GREEN after the fix.
        """
        client = _client(FakeAsyncHTTP())
        client.last_metrics = {"status": "ok", "latency_ms": 5}
        async with client._circuit_breaker._lock:
            for _ in range(5):
                client._circuit_breaker.record_failure()
        assert client._circuit_breaker.current_state == CircuitBreakerState.OPEN
        with pytest.raises(LLMError):
            async for _ in client.chat_completion_stream(
                [{"role": "user", "content": "q"}]
            ):
                pass
        assert client.last_metrics.get("status") != "ok"


class TestPreLhsTokenEstimate(_SSRFPatchedTestCase):
    async def test_pre_lhs_text_counted_in_token_estimate(self):
        """AC10 (issue #717): WHEN a streamed response's first delta is 8
        spaces, then the legacy ``_lhs x _rhs`` block, then `` abcdefgh``,
        so the stream yields whitespace before the marker THE SYSTEM SHALL
        set ``last_metrics["completion_tokens_estimate"]`` to
        ``max(1, total_yielded_chars // 4)`` (4 for the 17 yielded
        characters). RED at base (master), GREEN after the fix.
        """
        delta = "        _lhs x _rhs abcdefgh"
        lines = [
            "data: " + json.dumps({"choices": [{"delta": {"content": delta}}]}),
            "",
            "data: [DONE]",
            "",
        ]
        client = _client(FakeAsyncHTTP(stream_lines=lines))
        chunks = [
            chunk
            async for chunk in client.chat_completion_stream(
                [{"role": "user", "content": "q"}]
            )
        ]
        visible = [chunk for chunk in chunks if isinstance(chunk, str)]
        total_chars = sum(len(chunk) for chunk in visible)
        assert total_chars == 17
        assert "".join(visible) == "        " + " abcdefgh"
        assert (
            client.last_metrics["completion_tokens_estimate"]
            == max(1, total_chars // 4)
        )
