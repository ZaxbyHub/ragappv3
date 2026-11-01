"""Issue #713 (finding T1-32-K-11): a blank non-streaming completion must be
classified as a failed visible answer, exactly like the streaming sibling.

``_get_llm_response`` treats an empty (blank or reasoning-only)
``chat_completion`` result as success — it yields the empty content chunk and
returns — while ``_stream_llm_response`` treats the identical outcome as a
failure and falls through to the next fallback client (issue #554,
tests/test_llm_reasoning_fallback.py). A blank 200 answer is therefore
persisted instead of the fallback client's real content.

The check below asserts the DESIRED behaviour: RED at the base commit
(465f76d2), GREEN once the non-streaming path uses the same visible-answer
predicate as the streaming path.
"""

from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from app.services.llm_client import LLMClient
from app.services.rag_engine import RAGEngine


@pytest.fixture(autouse=True)
def _patch_ssrf():
    # Sibling convention (tests/test_llm_reasoning_fallback.py): the
    # LLMClient constructor SSRF-checks its base_url; the fakes here never
    # touch the network, so skip the resolver.
    with patch("app.services.llm_client.assert_url_safe"):
        yield


class _CompletionClient(LLMClient):
    """LLMClient whose chat_completion returns a fixed string — no IO."""

    def __init__(self, content: str, model: str):
        super().__init__(
            base_url="http://localhost:9",
            model=model,
            cb_name="c08_nonstream_blank_fallback",
        )
        self._content = content

    async def chat_completion(self, messages, **kwargs) -> str:
        return self._content


def _engine_with_clients(primary, fallback) -> RAGEngine:
    """Bare RAGEngine whose fallback chain is exactly [primary, fallback].

    Skips RAGEngine.__init__ (heavy retrieval wiring); _get_llm_response only
    touches the client chain, per-instance metric attributes and module-level
    contextvar helpers, all safe on a bare instance. Distinct model names keep
    _fallback_clients' (base_url, model) dedup from collapsing the pair.
    """
    engine = RAGEngine.__new__(RAGEngine)
    engine._instant_client_override = fallback
    engine._thinking_client_override = None
    engine.llm_client = fallback
    return engine


async def test_blank_primary_falls_through_to_fallback():
    """AC5 (issue #713, T1-32-K-11): when the non-streaming primary client
    returns a blank completion and a fallback client returns visible text, the
    answer content must be the fallback's text. RED at the base commit (the
    blank content is yielded as success), GREEN after the fix."""
    primary = _CompletionClient("", model="p-primary")
    fallback = _CompletionClient("real answer", model="p-fallback")
    engine = _engine_with_clients(primary, fallback)

    chunks: List[Dict[str, Any]] = []
    async for chunk in engine._get_llm_response(
        [{"role": "user", "content": "q"}], client=primary, max_tokens=64
    ):
        chunks.append(chunk)

    content_chunks = [c for c in chunks if c.get("type") == "content"]
    assert content_chunks, f"expected a content chunk, got {chunks!r}"
    assert content_chunks[0]["content"] == "real answer", (
        "a blank primary completion must fall through to the fallback "
        f"client's visible content, got {content_chunks[0]['content']!r}"
    )
