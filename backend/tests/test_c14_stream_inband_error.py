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
        # PRR-005: the raise alone is not the contract — the defect this PR
        # fixes was that the payload was skipped AND recorded a breaker
        # success with status='ok'. Both halves are asserted here so a
        # regression that restores either is caught.
        assert client.last_metrics.get("status") == "inband_error"
        assert client._circuit_breaker._fail_counter == 1

    @pytest.mark.asyncio
    async def test_input_class_inband_error_does_not_charge_breaker(self):
        """PRR-002: OPS-002 classification — an input/config-class in-band
        error (HTTP 400) recurs on every retry and must NOT open the circuit,
        exactly as the HTTP status path already behaves."""
        client = LLMClient(base_url="http://llm-test-host:1234/v1", model="m")
        client._client = FakeAsyncHTTP(
            stream_lines=[
                'data: {"error": {"message": "bad request", "code": 400}}',
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
        assert client.last_metrics.get("status") == "inband_error"
        assert client._circuit_breaker._fail_counter == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "payload",
        [
            '{"message": "overloaded", "code": 503}',
            '{"code": 503}',  # no message -> falls back to the code
            '{"message": null, "code": 400}',  # null message
            '"plain string error"',  # non-dict error envelope
        ],
        ids=["message", "code-only", "null-message", "string"],
    )
    async def test_error_message_is_never_empty(self, payload):
        """PRR-024: an envelope with no usable message must still produce a
        diagnostic an operator can act on, not an empty or 'None' string."""
        client = LLMClient(base_url="http://llm-test-host:1234/v1", model="m")
        client._client = FakeAsyncHTTP(
            stream_lines=[
                'data: {"error": ' + payload + "}",
                "",
                "data: [DONE]",
                "",
            ]
        )
        with pytest.raises(LLMError) as excinfo:
            async for _ in client.chat_completion_stream(
                [{"role": "user", "content": "q"}]
            ):
                pass
        message = str(excinfo.value)
        detail = message.split("from provider: ", 1)[-1]
        assert detail.strip(), f"empty diagnostic for payload {payload}"
        assert detail != "None"

    @pytest.mark.asyncio
    async def test_bare_json_error_line_raises(self):
        """PRR-012: issue #719 named TWO in-band shapes — a ``data:``-framed
        error event AND a bare JSON error object on a non-``data`` line.
        Framing alone must not decide whether a failure is visible."""
        client = LLMClient(base_url="http://llm-test-host:1234/v1", model="m")
        client._client = FakeAsyncHTTP(
            stream_lines=[
                'data: {"choices": [{"delta": {"content": "partial"}}]}',
                "",
                '{"error": {"message": "model not found", "code": 503}}',
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
        assert client.last_metrics.get("status") == "inband_error"

    @pytest.mark.asyncio
    async def test_inband_error_telemetry_is_outage_class(self):
        """PRR-003: a breaker-charged mid-stream failure must be classified
        as an outage in telemetry, not fall through to a soft partial."""
        from app.services.telemetry import _status_to_outcome

        assert _status_to_outcome("inband_error") == "unavailable"
