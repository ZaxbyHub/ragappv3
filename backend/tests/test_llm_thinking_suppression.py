"""Tests for LLM client thinking-content suppression in streaming and non-streaming.

Verifies that:
- ``reasoning_content`` deltas are NEVER yielded to users.
- ``<think>...</think>`` blocks are stripped, even when fragmented across chunks.
- ``_lhs/_rhs`` blocks are stripped.
- ``Thinking Process:...</think>`` blocks are stripped.
- Unterminated thinking blocks at stream end never leak.
"""

import json
import os
import sys
import unittest
from typing import AsyncIterator, Dict, Iterable, List
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app.services.llm_client import LLMClient


@pytest.fixture(autouse=True)
def _patch_ssrf():
    with patch("app.services.llm_client.assert_url_safe"):
        yield


def _delta_chunk(content: str = None, reasoning: str = None) -> Dict:
    delta: Dict = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    return {"choices": [{"delta": delta}]}


class _FakeStreamResponse:
    def __init__(self, lines: Iterable[str], content_type: str = "text/event-stream"):
        self._lines = list(lines)
        self.headers = {"content-type": content_type}

    def raise_for_status(self):
        pass

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeStreamCM:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return None


def _sse_lines_from_deltas(deltas: List[Dict]) -> List[str]:
    out: List[str] = []
    for d in deltas:
        out.append(f"data: {json.dumps(d)}")
        out.append("")  # blank line between events
    out.append("data: [DONE]")
    return out


async def _collect(gen: AsyncIterator[str]) -> str:
    out = []
    async for piece in gen:
        out.append(piece)
    return "".join(out)


class TestNonStreamingSanitizer(unittest.IsolatedAsyncioTestCase):
    async def test_strip_thinking_complete_response(self):
        client = LLMClient()
        self.assertEqual(client._strip_thinking_content("<think>x</think>visible"), "visible")
        self.assertEqual(client._strip_thinking_content("_lhsy_rhs done"), "done")
        self.assertEqual(
            client._strip_thinking_content("Thinking Process: foo</think>final"),
            "final",
        )
        # Idempotent
        self.assertEqual(client._strip_thinking_content("clean"), "clean")


class TestStreamingSanitizer(unittest.IsolatedAsyncioTestCase):
    async def _stream(self, deltas: List[Dict]) -> str:
        client = LLMClient()
        await client.start()
        try:
            fake = _FakeStreamResponse(_sse_lines_from_deltas(deltas))
            with patch.object(client, "_client", autospec=False) as mock_http:
                mock_http.stream = MagicMock(return_value=_FakeStreamCM(fake))
                return await _collect(client.chat_completion_stream(messages=[]))
        finally:
            await client.close()

    async def test_pure_reasoning_content_never_yielded(self):
        deltas = [_delta_chunk(reasoning="hidden plan"), _delta_chunk(content="hello")]
        out = await self._stream(deltas)
        self.assertEqual(out.strip(), "hello")
        self.assertNotIn("hidden plan", out)

    async def test_mixed_reasoning_and_content(self):
        deltas = [
            _delta_chunk(reasoning="r1"),
            _delta_chunk(content="A"),
            _delta_chunk(reasoning="r2"),
            _delta_chunk(content="B"),
        ]
        out = await self._stream(deltas)
        self.assertEqual(out.strip(), "AB")

    async def test_strip_think_block(self):
        deltas = [_delta_chunk(content="<think>hidden</think>visible")]
        out = await self._stream(deltas)
        self.assertEqual(out.strip(), "visible")

    async def test_fragmented_think_open_close(self):
        # Tag fragmented across chunks: <thi + nk> + body + </think>visible
        deltas = [
            _delta_chunk(content="<thi"),
            _delta_chunk(content="nk>body"),
            _delta_chunk(content="</think>visible"),
        ]
        out = await self._stream(deltas)
        self.assertEqual(out.strip(), "visible")
        self.assertNotIn("body", out)

    async def test_lhs_rhs(self):
        deltas = [_delta_chunk(content="_lhshidden_rhsvisible")]
        out = await self._stream(deltas)
        self.assertEqual(out.strip(), "visible")

    async def test_thinking_process_pattern(self):
        deltas = [
            _delta_chunk(content="Thinking Process: i will plan."),
            _delta_chunk(content="</think>final answer"),
        ]
        out = await self._stream(deltas)
        self.assertEqual(out.strip(), "final answer")

    async def test_unterminated_think_block_does_not_leak(self):
        # Stream ends inside <think>... — must not yield any thinking text.
        deltas = [_delta_chunk(content="<think>secret never closed")]
        out = await self._stream(deltas)
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
