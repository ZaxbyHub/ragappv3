"""Issue #698 (Workstream B PR 9) — contextualizer prompt-budget clamps.

End-to-end (through ``contextualize_chunks``, the C7 observable: the summed
``len(m["content"])`` the LLM actually receives) pins of the AC7 clamp
family the frozen check C7 cannot discriminate with its plain "z" input:

- escape inflation on BOTH the document and the chunk sides (catches a
  double-escaping implementation and a raw-text budget);
- two chunks of different lengths (catches a once-per-call truncation sized
  for the wrong chunk — the budget depends on each chunk's escaped size);
- the fixed-floor degradation at a tiny model_context_tokens;
- canonical chunk text untouched by prompt-only budgeting.
"""

from __future__ import annotations

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


class _RecordingLLM:
    """Stub LLM recording the summed content length of every prompt."""

    def __init__(self):
        self.lengths: list[int] = []
        self.payloads: list[str] = []

    async def chat_completion(self, messages=None, **kwargs):
        self.lengths.append(sum(len(m["content"]) for m in messages))
        self.payloads.append("\n".join(m["content"] for m in messages))
        return "ctx"


async def test_escape_heavy_input_bounded_end_to_end(monkeypatch):
    from app.config import settings
    from app.services.chunking import ProcessedChunk
    from app.services.contextual_chunking import ContextualChunker

    monkeypatch.setattr(settings, "model_context_tokens", 4096)
    llm = _RecordingLLM()
    doc = "&" * 60_000  # escapes to &amp; — 5x inflation
    chunk = ProcessedChunk(text="<" * 8_000, metadata={}, chunk_index=0)
    await ContextualChunker(llm).contextualize_chunks(doc, [chunk], "f.txt")
    assert llm.lengths, "stub LLM was never called"
    assert max(llm.lengths) <= 4 * 4096


async def test_two_chunks_of_different_lengths_use_per_chunk_budget(monkeypatch):
    """Per-chunk budgeting: each prompt's DOCUMENT share reflects its own chunk.

    Note (review): this test discriminates once-per-call vs per-chunk
    budgeting via document composition; the chunk-half CLAMP itself is
    pinned by test_escape_inflation_stays_bounded_end_to_end and
    TestBoundedPromptBudget (an 8000-char chunk is below `remaining`, so
    removing the clamp does not move this test).
    """
    from app.config import settings
    from app.services.chunking import ProcessedChunk
    from app.services.contextual_chunking import ContextualChunker

    monkeypatch.setattr(settings, "model_context_tokens", 4096)
    llm = _RecordingLLM()
    chunks = [
        ProcessedChunk(text="y" * 100, metadata={}, chunk_index=0),
        ProcessedChunk(text="y" * 8_000, metadata={}, chunk_index=1),
    ]
    await ContextualChunker(llm).contextualize_chunks(
        "z" * 60_000, chunks, "f.txt"
    )
    assert len(llm.lengths) == 2
    assert max(llm.lengths) <= 4 * 4096
    # Chunk + doc always consume exactly `remaining` when the doc is large,
    # so the two totals are equal by construction — the PER-CHUNK evidence is
    # the composition: the larger chunk's prompt carries FEWER document
    # characters than the small chunk's. A once-per-call truncation (sized
    # for the wrong chunk) would give both prompts the same doc view.
    assert llm.payloads[0].count("z") > llm.payloads[1].count("z")


async def test_tiny_context_degrades_to_fixed_floor(monkeypatch):
    from app.config import settings
    from app.services.chunking import ProcessedChunk
    from app.services.contextual_chunking import ContextualChunker

    monkeypatch.setattr(settings, "model_context_tokens", 100)
    llm = _RecordingLLM()
    chunks = [ProcessedChunk(text="y" * 60_000, metadata={}, chunk_index=0)]
    await ContextualChunker(llm).contextualize_chunks(
        "z" * 60_000, chunks, "f.txt"
    )
    assert max(llm.lengths) < 2_000  # scaffolding only — no payload survives


async def test_prompt_budgeting_never_touches_canonical_chunk_text(monkeypatch):
    """Budgeting is prompt-only: chunk.text/raw_text keep their full content."""
    from app.config import settings
    from app.services.chunking import ProcessedChunk
    from app.services.contextual_chunking import ContextualChunker

    monkeypatch.setattr(settings, "model_context_tokens", 4096)
    llm = _RecordingLLM()
    original = "y" * 8_000
    chunk = ProcessedChunk(text=original, metadata={}, chunk_index=0)
    await ContextualChunker(llm).contextualize_chunks("z" * 60_000, [chunk], "f.txt")
    # The context prefix is prepended (enrichment), but the original text is
    # preserved verbatim in raw_text and as the suffix of the enriched text.
    assert chunk.raw_text == original
    assert chunk.text.endswith(original)
