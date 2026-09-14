"""Engine-level regression tests for the issue #554 reasoning channel.

Covers the two reviewer-confirmed follow-ups from the PR #598 review:

- C1L-3: a primary client whose stream yields ONLY ``ReasoningDelta``
  objects must NOT count as a visible answer — ``_stream_llm_response``
  must try the next fallback client and persist real content instead of a
  blank assistant message. (The delta channel must never set
  ``emitted_content``.)
- C5L-4: ``create_editorial_client`` opts into neither the reasoning
  control nor the template control — the #554 control vocabulary must not
  leak into the editorial desk client.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.services.llm_client import LLMClient, ReasoningDelta
from app.services.rag_engine import RAGEngine


@pytest.fixture(autouse=True)
def _patch_ssrf():
    # Sibling convention (test_llm_finish_reason.py): the LLMClient
    # constructor SSRF-checks its base_url; the fakes here never touch the
    # network, so skip the resolver.
    with patch("app.services.llm_client.assert_url_safe"):
        yield


class _FakeClient(LLMClient):
    """LLMClient whose stream replays pre-built typed chunks — no IO."""

    def __init__(self, chunks, model="reasoning-fallback-test"):
        super().__init__(
            base_url="http://localhost:9",
            model=model,
            cb_name="test_reasoning_fallback",
        )
        self._chunks = chunks

    async def chat_completion_stream(self, messages, **kwargs):
        for chunk in self._chunks:
            yield chunk


def _engine_with_clients(primary, fallback):
    """Bare RAGEngine whose fallback chain is exactly [primary, fallback].

    Skips RAGEngine.__init__ (heavy retrieval wiring); _stream_llm_response
    only touches the client chain, per-instance metric attributes and
    module-level contextvar helpers, all safe on a bare instance. Distinct
    model names keep _fallback_clients' (base_url, model) dedup from
    collapsing the pair.
    """
    engine = RAGEngine.__new__(RAGEngine)
    engine._instant_client_override = fallback
    engine._thinking_client_override = None
    engine.llm_client = fallback
    return engine


@pytest.mark.asyncio
async def test_reasoning_only_primary_falls_through_to_fallback():
    """C1L-3 (PR #598 review): reasoning-only stream is NOT a visible
    answer — the engine must continue to the fallback client, whose
    content becomes the answer. Guards the emitted_content contract that
    keeps a blank assistant message from being persisted."""
    primary = _FakeClient([ReasoningDelta(text="plan only, no answer")], model="p-primary")
    fallback = _FakeClient(["real answer"], model="p-fallback")
    engine = _engine_with_clients(primary, fallback)

    chunks = []
    async for chunk in engine._stream_llm_response(
        [{"role": "user", "content": "q"}], client=primary, max_tokens=64
    ):
        chunks.append(chunk)

    types = [c.get("type") for c in chunks]
    assert types == ["reasoning_delta", "content"], (
        "C1L-3 F-001: expected the primary's reasoning_delta to be surfaced "
        f"AND the fallback's content to become the answer, got {chunks!r}"
    )
    assert chunks[0]["text"] == "plan only, no answer"
    assert chunks[1]["content"] == "real answer"


@pytest.mark.asyncio
async def test_reasoning_deltas_do_not_mark_stream_visible():
    """Companion pin: a reasoning-only stream from the ONLY client yields
    the deltas but ends in the engine's error chunk instead of a silently
    blank success (never persisted as a complete answer)."""
    only = _FakeClient(
        [ReasoningDelta(text="thinking..."), ReasoningDelta(text="still thinking")],
        model="p-only",
    )
    engine = _engine_with_clients(only, only)

    chunks = []
    async for chunk in engine._stream_llm_response(
        [{"role": "user", "content": "q"}], client=only, max_tokens=64
    ):
        chunks.append(chunk)

    types = [c.get("type") for c in chunks]
    assert types == ["reasoning_delta", "reasoning_delta", "error"], (
        "C1L-3 F-001: a reasoning-only stream must surface its deltas and "
        f"then the LLM_ERROR chunk, got {chunks!r}"
    )


def test_editorial_client_carries_no_reasoning_or_template_control():
    """C5L-4 (PR #598 review): the editorial desk client must send neither
    the #554 reasoning control nor the Qwen-style template control."""
    from app.services import llm_client as mod
    from app.services.llm_client import create_editorial_client

    fake_settings = MagicMock(
        editorial_chat_url="",
        editorial_chat_model="",
        ollama_chat_url="http://localhost:9",
        chat_model="editorial-test-model",
    )
    with patch.object(mod, "settings", fake_settings), patch.object(
        mod, "assert_url_safe"
    ):
        client = create_editorial_client()
    assert client.reasoning_effort is None, (
        "C5L-4 F-002: the editorial client must not carry a reasoning_effort "
        f"control, got {client.reasoning_effort!r}"
    )
    assert not client.chat_template_kwargs, (
        "C5L-4 F-002: the editorial client must not carry chat_template_kwargs, "
        f"got {client.chat_template_kwargs!r}"
    )
