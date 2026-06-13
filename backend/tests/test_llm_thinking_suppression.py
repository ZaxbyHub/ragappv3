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

    async def test_mid_content_lhs_substring_does_not_truncate(self):
        # Regression for issue #227: a bare "_lhs" substring inside a normal
        # answer (e.g. an identifier like ``expr_lhs`` quoted from a RAG
        # document) must not be treated as a thinking-block open marker.
        # The legacy Qwen _lhs/_rhs pattern is only valid as a *prefix* of
        # the model response.
        deltas = [
            _delta_chunk(content="Here is the expr_lhs = 42; result follows."),
        ]
        out = await self._stream(deltas)
        self.assertEqual(out.strip(), "Here is the expr_lhs = 42; result follows.")
        self.assertNotIn("</think>", out)

    async def test_mid_content_thinking_process_substring_does_not_truncate(self):
        # Regression for issue #227: a bare "Thinking Process:" substring
        # appearing mid-answer (e.g. quoted from a document) must not be
        # treated as a thinking-block open marker. The qwen3.5-122b style
        # "Thinking Process:" prefix is only valid at the start of the
        # model response.
        deltas = [
            _delta_chunk(
                content=(
                    "Sure. The relevant section is titled "
                    "\"Thinking Process: a primer\" and the answer is 7."
                )
            ),
        ]
        out = await self._stream(deltas)
        self.assertIn("Thinking Process: a primer", out)
        self.assertIn("the answer is 7", out)
        self.assertNotIn("</think>", out)

    async def test_fragmented_mid_content_lhs_does_not_truncate(self):
        # The bug also reproduces when the dangerous substring is split across
        # chunks: arriving content "Here is the expr_l" + "hs = 42." must
        # not be treated as a thinking-open. (Note: the streaming code is
        # expected to hold the buffer until a non-marker char arrives, and
        # then yield the held text as a legitimate answer.)
        deltas = [
            _delta_chunk(content="Here is the expr_l"),
            _delta_chunk(content="hs = 42; result follows."),
        ]
        out = await self._stream(deltas)
        self.assertEqual(
            out.strip(), "Here is the expr_lhs = 42; result follows."
        )

    async def test_lhs_rhs_after_preamble_does_not_truncate(self):
        # Once any non-thinking text has been streamed, a bare "_lhs" / "_rhs"
        # substring in later deltas is no longer a valid thinking-block
        # marker. The first delta here is plain prose, the second delta
        # contains a Qwen-style "_lhs..._rhs" substring as if it were
        # part of quoted document content.
        deltas = [
            _delta_chunk(content="ok then "),
            _delta_chunk(content="_lhshidden_rhsclean"),
        ]
        out = await self._stream(deltas)
        # The second delta must NOT be swallowed by the thinking filter.
        self.assertIn("ok then ", out)
        self.assertIn("_lhshidden_rhsclean", out)

    async def test_lhs_with_leading_whitespace_is_filtered(self):
        # A whitespace-prefixed "_lhs" at the start of the response is still a
        # thinking-block marker (Qwen models emit it as the first content), and
        # must be filtered. The ``lstrip().startswith("_lhs")`` guard handles
        # this case safely.
        deltas = [
            _delta_chunk(content="\n_lhshidden_rhsvisible"),
        ]
        out = await self._stream(deltas)
        self.assertNotIn("hidden", out)
        self.assertIn("visible", out)

    async def test_unterminated_lhs_block_does_not_leak(self):
        # Stream ends inside _lhs... with no _rhs — must suppress the thinking
        # content (same invariant as the <think> unterminated test below).
        deltas = [_delta_chunk(content="_lhsnever closed")]
        out = await self._stream(deltas)
        self.assertEqual(out, "")

    async def test_unterminated_think_block_does_not_leak(self):
        # Stream ends inside <think>... — must not yield any thinking text.
        deltas = [_delta_chunk(content="<think>secret never closed")]
        out = await self._stream(deltas)
        self.assertEqual(out, "")

    async def test_lhs_substring_mid_content_not_treated_as_marker(self):
        # A bare "_lhs" that appears AFTER real content has streamed is genuine
        # answer text (e.g. an identifier) and must not flip the filter into
        # thinking mode and swallow the rest of the response.
        deltas = [
            _delta_chunk(content="The variable "),
            _delta_chunk(content="expr_lhs holds the left operand."),
        ]
        out = await self._stream(deltas)
        self.assertEqual(out.strip(), "The variable expr_lhs holds the left operand.")

    async def test_thinking_process_mid_content_not_treated_as_marker(self):
        # "Thinking Process:" mid-answer is real text, not a reasoning marker.
        deltas = [
            _delta_chunk(content="Here is my "),
            _delta_chunk(content="Thinking Process: a numbered list."),
        ]
        out = await self._stream(deltas)
        self.assertEqual(out.strip(), "Here is my Thinking Process: a numbered list.")

    async def test_thinking_process_marker_split_across_chunks_at_start(self):
        # The "Thinking Process:" prefix split across chunk boundaries at the
        # very start must still be held (partial-prefix accumulation) and
        # suppressed as reasoning, with the close marker arriving later.
        deltas = [
            _delta_chunk(content="Thinking"),
            _delta_chunk(content=" Process: planning the answer"),
            _delta_chunk(content="</think>visible answer"),
        ]
        out = await self._stream(deltas)
        self.assertEqual(out.strip(), "visible answer")

    async def test_thinking_process_marker_split_across_chunks_mid_content(self):
        # The same split AFTER content has been emitted is genuine text: the
        # partial-prefix hold is bypassed once content_emitted is set.
        deltas = [
            _delta_chunk(content="Answer. "),
            _delta_chunk(content="Thinking"),
            _delta_chunk(content=" Process: my steps."),
        ]
        out = await self._stream(deltas)
        self.assertEqual(out.strip(), "Answer. Thinking Process: my steps.")


if __name__ == "__main__":
    unittest.main()
