"""End-to-end coverage for the per-call LLM overrides added by issue #712.

PRR-001 (review of PR #931): every pre-existing test exercised the *client
default* payload, and the new evaluator tests use hand-written fakes that echo
the kwarg back — so reverting ``LLMClient.chat_completion``'s per-call override
logic to its pre-#712 form left the whole suite green. This file drives a REAL
``LLMClient`` over the repo's existing fake-HTTP surface and asserts what
actually reaches the wire payload, which is the composition the issue's AC6
depends on (the "none" control must override the client's configured "high").

Not frozen / not a manifest path — added in the PR-feedback round.
"""

import os
import sys
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.llm_client import LLMClient

_MESSAGES = [{"role": "user", "content": "q"}]


class _FakeResponse:
    def __init__(self, payload: Dict[str, Any]):
        self._payload = payload
        self.status_code = 200
        self.text = ""
        self.headers = {"content-type": "application/json"}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeAsyncHTTP:
    """The minimal surface LLMClient uses; records every payload sent."""

    def __init__(self, payload: Dict[str, Any]):
        self._payload = payload
        self.post_calls: List[Dict[str, Any]] = []

    async def post(self, url, json=None):
        self.post_calls.append({"url": url, "json": json or {}})
        return _FakeResponse(self._payload)


def _ok_payload(content: str = "NO_MATCH") -> Dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}}
        ],
    }


def _client(fake, **kwargs) -> LLMClient:
    with patch("app.services.llm_client.assert_url_safe"):
        client = LLMClient(
            base_url="http://localhost:9999",
            model="qwen/qwen3.5-eval-client",
            cb_name="test_c07_per_call",
            **kwargs,
        )
    client._client = fake
    return client


class TestPerCallReasoningOverride:
    """AC6's mechanism, end to end: the per-call value must reach the payload
    and must WIN over the client's configured default."""

    @pytest.mark.asyncio
    async def test_per_call_none_overrides_client_high_on_the_wire(self):
        fake = _FakeAsyncHTTP(_ok_payload())
        client = _client(fake, reasoning_effort="high")

        out = await client.chat_completion(_MESSAGES, max_tokens=64, reasoning_effort="none")

        assert out == "NO_MATCH"
        assert len(fake.post_calls) == 1
        payload = fake.post_calls[0]["json"]
        assert payload.get("reasoning_effort") == "none", (
            "the per-call override must reach the wire payload and beat the "
            f"client default 'high'; got {payload.get('reasoning_effort')!r}"
        )

    @pytest.mark.asyncio
    async def test_sentinel_default_leaves_client_default_on_the_wire(self):
        """Not passing the kwarg must behave exactly as before this change."""
        fake = _FakeAsyncHTTP(_ok_payload("CONFIDENT"))
        client = _client(fake, reasoning_effort="high")

        await client.chat_completion(_MESSAGES, max_tokens=64)

        payload = fake.post_calls[0]["json"]
        assert payload.get("reasoning_effort") == "high"

    @pytest.mark.asyncio
    async def test_explicit_none_strips_the_field_without_mutating_the_client(self):
        """The sentinel's documented contract (llm_client.py Args): an explicit
        ``None`` removes the field for THIS call only."""
        fake = _FakeAsyncHTTP(_ok_payload("CONFIDENT"))
        client = _client(fake, reasoning_effort="high")

        await client.chat_completion(_MESSAGES, max_tokens=64, reasoning_effort=None)
        assert "reasoning_effort" not in fake.post_calls[0]["json"]

        await client.chat_completion(_MESSAGES, max_tokens=64)
        assert fake.post_calls[1]["json"].get("reasoning_effort") == "high", (
            "a stripped field must not persist onto the client"
        )
        assert client.reasoning_effort == "high"

    @pytest.mark.asyncio
    async def test_per_call_chat_template_kwargs_reaches_the_wire(self):
        fake = _FakeAsyncHTTP(_ok_payload("NO_MATCH"))
        client = _client(fake, chat_template_kwargs={"enable_thinking": True})

        await client.chat_completion(
            _MESSAGES, max_tokens=512, chat_template_kwargs={"enable_thinking": False}
        )
        payload = fake.post_calls[0]["json"]
        assert payload.get("chat_template_kwargs") == {"enable_thinking": False}
        assert client.chat_template_kwargs == {"enable_thinking": True}, (
            "the per-call override must not mutate the client's own default"
        )

    @pytest.mark.asyncio
    async def test_client_without_reasoning_never_gains_the_field_by_default(self):
        """A client constructed without reasoning_effort (the instant client's
        shape) must not start sending the field on its own."""
        fake = _FakeAsyncHTTP(_ok_payload("CONFIDENT"))
        client = _client(fake)

        await client.chat_completion(_MESSAGES, max_tokens=64)
        assert "reasoning_effort" not in fake.post_calls[0]["json"]
