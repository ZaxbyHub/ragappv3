"""Issue #698 (Workstream B PR 9) — contextualization sibling cancellation.

Pins the AC8 contract beyond the frozen check C8 (which asserts only
``changed == 0`` after the raise): the failure must still RE-RAISE after the
siblings are cancelled (a suppress-instead-of-reraise implementation would
pass C8 while swallowing the error), and an OUTER cancellation must pass
through unchanged (CancelledError is not converted or swallowed).
"""

from __future__ import annotations

import asyncio
import os

import pytest

_B09_ENV = {
    "ADMIN_SECRET_TOKEN": "test-secret",
    "USERS_ENABLED": "false",
    "JWT_SECRET_KEY": "test-jwt-secret-key-for-testing-only",
    "REDIS_URL": "",
}


@pytest.fixture(autouse=True, scope="module")
def _b09_hermetic_env():
    """Hermetic env before any app import; restored on teardown."""
    saved = {key: os.environ.get(key) for key in _B09_ENV}
    os.environ.update(_B09_ENV)
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _chunks(n):
    from app.services.chunking import ProcessedChunk

    return [
        ProcessedChunk(text=f"t{i}", metadata={}, chunk_index=i) for i in range(n)
    ]


class _RacyLLM:
    """First call raises a non-LLMError; others sleep then return context."""

    def __init__(self, sleep_s=0.2):
        self.calls = 0
        self.sleep_s = sleep_s

    async def chat_completion(self, messages=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise AttributeError("boom")
        await asyncio.sleep(self.sleep_s)
        return "ctx-marker"


async def test_non_llm_error_still_reraises_after_cancellation():
    """The original exception re-raises; no sibling mutates after the raise."""
    from app.services.contextual_chunking import ContextualChunker

    chunks = _chunks(3)
    chunker = ContextualChunker(_RacyLLM())
    raised = None
    try:
        await chunker.contextualize_chunks("doc text", chunks, "f.txt")
    except AttributeError as exc:
        raised = exc
    assert raised is not None, "suppressing the re-raise violates the contract"
    await asyncio.sleep(0.5)
    changed = sum(1 for c in chunks if c.text != f"t{c.chunk_index}")
    assert changed == 0


async def test_outer_cancellation_passes_through_unchanged():
    """CancelledError from the caller propagates as CancelledError."""
    from app.services.contextual_chunking import ContextualChunker

    class SlowLLM:
        async def chat_completion(self, messages=None, **kwargs):
            await asyncio.sleep(5)
            return "ctx"

    chunks = _chunks(3)
    chunker = ContextualChunker(SlowLLM())
    task = asyncio.create_task(
        chunker.contextualize_chunks("doc text", chunks, "f.txt")
    )
    await asyncio.sleep(0.05)  # let the tasks start and enter the LLM await
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_all_success_path_unaffected_by_cancellation_machinery():
    """AC12 shape: successful contextualization still enriches every chunk."""
    from app.services.contextual_chunking import ContextualChunker

    class OkLLM:
        async def chat_completion(self, messages=None, **kwargs):
            return "ctx"

    chunks = _chunks(2)
    await ContextualChunker(OkLLM()).contextualize_chunks("doc", chunks, "f.txt")
    assert all(c.metadata.get("contextualized") is True for c in chunks)
    assert all(c.text.startswith("ctx") for c in chunks)
