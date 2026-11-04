"""Per-call metrics_sink mirror on LLMClient.chat_completion (issue #1066).

The enrichment service runs concurrent candidates through ONE shared client,
so reading ``last_metrics`` after ``await`` returns races sibling calls. The
opt-in ``metrics_sink`` kwarg mirrors each call's success metrics into a
caller-owned dict, populated synchronously before return.

These tests drive the REAL LLMClient against a fake HTTP layer (same harness
family as tests/test_llm_finish_reason.py) because the frozen failure specs'
fake client populates any dict kwarg itself and cannot detect a broken or
missing mirror in the real client (plan-critic round 1, finding 3).
"""

import asyncio
import json
import unittest
from unittest.mock import patch

from app.services.llm_client import LLMClient


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


class FakeAsyncHTTP:
    """Exposes exactly the surface the non-stream path uses: async post."""

    def __init__(self, payloads):
        # A list so interleaved calls can be answered in order.
        self._payloads = list(payloads)

    async def post(self, url, json=None):
        return FakeResponse(self._payloads.pop(0))


def _client(fake) -> LLMClient:
    client = LLMClient(
        base_url="http://localhost:9999",
        model="sink-test-model",
        cb_name="test_metrics_sink",
    )
    client._client = fake
    return client


def _payload(finish_reason, content, usage=None):
    choice = {
        "index": 0,
        "message": {"role": "assistant", "content": content},
        "finish_reason": finish_reason,
    }
    body = {"id": "chatcmpl-test", "object": "chat.completion", "choices": [choice]}
    if usage is not None:
        body["usage"] = usage
    return body


class _EnsureStartedShim:
    """Bypass _ensure_started's real client construction (harness already set)."""

    async def _ensure_started(self):
        return self._client


class TestMetricsSink(unittest.TestCase):
    def setUp(self):
        patcher = patch("app.services.llm_client.assert_url_safe")
        self._ssrf = patcher.start()
        self.addCleanup(patcher.stop)

    def test_sink_mirrors_stop_and_content_returned(self):
        fake = FakeAsyncHTTP([_payload("stop", "hello")])
        client = _client(fake)
        client._ensure_started = _EnsureStartedShim._ensure_started.__get__(client)
        sink: dict = {}

        content = asyncio.run(
            client.chat_completion([{"role": "user", "content": "hi"}], metrics_sink=sink)
        )

        self.assertEqual(content, "hello", "content-string return preserved")
        self.assertEqual(sink.get("finish_reason"), "stop")
        self.assertEqual(sink.get("status"), "ok")
        self.assertEqual(sink.get("model"), "sink-test-model")
        self.assertEqual(client.last_metrics.get("finish_reason"), "stop")

    def test_sink_mirrors_length(self):
        fake = FakeAsyncHTTP([_payload("length", '{"summary": "cut obj')])
        client = _client(fake)
        client._ensure_started = _EnsureStartedShim._ensure_started.__get__(client)
        sink: dict = {}

        content = asyncio.run(
            client.chat_completion([{"role": "user", "content": "hi"}], metrics_sink=sink)
        )

        self.assertEqual(content, '{"summary": "cut obj')
        self.assertEqual(sink.get("finish_reason"), "length")

    def test_provider_token_counts_reach_sink(self):
        fake = FakeAsyncHTTP(
            [_payload("stop", "counted", usage={"prompt_tokens": 11, "completion_tokens": 7})]
        )
        client = _client(fake)
        client._ensure_started = _EnsureStartedShim._ensure_started.__get__(client)
        sink: dict = {}

        asyncio.run(
            client.chat_completion([{"role": "user", "content": "hi"}], metrics_sink=sink)
        )

        self.assertEqual(sink.get("prompt_tokens"), 11)
        self.assertEqual(sink.get("completion_tokens"), 7)

    def test_interleaved_calls_each_keep_their_own_sink(self):
        fake = FakeAsyncHTTP(
            [_payload("stop", "first"), _payload("length", "second")]
        )
        client = _client(fake)
        client._ensure_started = _EnsureStartedShim._ensure_started.__get__(client)
        sink_a: dict = {}
        sink_b: dict = {}

        async def run_both():
            # Sequentially awaited here; the invariant under test is that the
            # sink is populated inside its own call, so a later call can never
            # overwrite an earlier sink (the last_metrics slot does move).
            await client.chat_completion(
                [{"role": "user", "content": "a"}], metrics_sink=sink_a
            )
            await client.chat_completion(
                [{"role": "user", "content": "b"}], metrics_sink=sink_b
            )

        asyncio.run(run_both())

        self.assertEqual(sink_a.get("finish_reason"), "stop")
        self.assertEqual(sink_b.get("finish_reason"), "length")
        self.assertEqual(
            client.last_metrics.get("finish_reason"), "length",
            "the shared slot reflects the LAST call while sinks do not",
        )

    def test_no_sink_leaves_behavior_unchanged(self):
        fake = FakeAsyncHTTP([_payload("stop", "plain")])
        client = _client(fake)
        client._ensure_started = _EnsureStartedShim._ensure_started.__get__(client)

        content = asyncio.run(
            client.chat_completion([{"role": "user", "content": "hi"}])
        )

        self.assertEqual(content, "plain")
        self.assertEqual(client.last_metrics.get("finish_reason"), "stop")


if __name__ == "__main__":
    unittest.main()
