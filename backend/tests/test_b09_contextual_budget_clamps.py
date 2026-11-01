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


async def test_chunk_view_clamped_to_half_when_between_half_and_remaining(
    monkeypatch,
):
    """The chunk view is capped at half the remaining budget, not `remaining`.

    A 12,000-char chunk at model_context_tokens=4096 fits WITHIN the whole
    remaining budget (~15.4k) but exceeds HALF of it — so this prompt
    discriminates the half split specifically: with the half clamp present
    the payload carries ~7.7k chunk chars; with the clamp loosened to the
    full remaining budget (or removed) it carries all 12,000 (issue #698
    review round 2: no other test discriminated the half split).
    """
    from app.config import settings
    from app.services.chunking import ProcessedChunk
    from app.services.contextual_chunking import ContextualChunker

    monkeypatch.setattr(settings, "model_context_tokens", 4096)
    llm = _RecordingLLM()
    chunker = ContextualChunker(llm)
    chunk = ProcessedChunk(text="y" * 12_000, metadata={}, chunk_index=0)
    await chunker.contextualize_chunks("z" * 60_000, [chunk], "f.txt")

    payload = llm.payloads[0]
    budget = 4 * 4096
    # Measure the exact fixed scaffolding the way the production budget does
    # (PRR-010: the clamp is half of REMAINING, not half of the budget — the
    # budget//2 bound is ~340 chars looser than the real invariant), and
    # subtract the scaffolding's own share of the counted character so the
    # assertion measures the chunk view alone.
    fixed_prompt = chunker._build_prompt(
        document_text="",
        chunk_text="",
        chunk_index=0,
        total_chunks=1,
        source_filename="f.txt",
    )
    fixed = sum(len(m["content"]) for m in fixed_prompt)
    baseline_y = sum(m["content"].count("y") for m in fixed_prompt)
    remaining = budget - fixed
    y_count = payload.count("y") - baseline_y
    # The chunk view is clamped well below the raw 12,000 chars (half of
    # remaining ≈ 7.8k) while the document absorbs the rest of the budget.
    assert y_count < 12_000
    assert y_count <= remaining // 2
    assert max(llm.lengths) <= budget


async def test_truncation_warning_fires_once_per_call(monkeypatch, caplog):
    """One WARNING for the first per-chunk truncation; repeats log DEBUG.

    Copilot review on PR #848 (contextual_chunking.py:144/215): the bounded
    view is rebuilt per chunk, so an over-budget document emitted one
    identical WARNING per chunk — log spam for routine behavior.
    """
    import logging

    from app.config import settings
    from app.services.chunking import ProcessedChunk
    from app.services.contextual_chunking import ContextualChunker

    monkeypatch.setattr(settings, "model_context_tokens", 4096)
    llm = _RecordingLLM()
    chunks = [
        ProcessedChunk(text=f"y{i}", metadata={}, chunk_index=i) for i in range(4)
    ]
    with caplog.at_level(
        logging.DEBUG, logger="app.services.contextual_chunking"
    ):
        await ContextualChunker(llm).contextualize_chunks(
            "z" * 60_000, chunks, "f.txt"
        )
    truncation_warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "Document view truncated" in r.message
    ]
    truncation_debugs = [
        r
        for r in caplog.records
        if r.levelno == logging.DEBUG and "Document view truncated" in r.message
    ]
    assert len(truncation_warnings) == 1
    assert len(truncation_debugs) >= 1


async def test_escape_once_path_matches_direct_escape(monkeypatch):
    """The per-call pre-escaped document yields the same prompt as raw+escape.

    Pins the escape-once refactor (N-5): contextualize_chunks passes the
    once-escaped document through, and _build_prompt must not re-escape it.
    """
    from html import escape as _xml_escape

    from app.config import settings
    from app.services.chunking import ProcessedChunk
    from app.services.contextual_chunking import ContextualChunker

    monkeypatch.setattr(settings, "model_context_tokens", 4096)
    doc = "a&b<c " * 500
    llm = _RecordingLLM()
    chunk = ProcessedChunk(text="y" * 100, metadata={}, chunk_index=0)
    await ContextualChunker(llm).contextualize_chunks(doc, [chunk], "f.txt")
    # The escaped form of the document head appears verbatim (escaped once,
    # not double-escaped: "&amp;" would become "&amp;amp;" under re-escape).
    assert "&amp;" in llm.payloads[0]
    assert "&amp;amp;" not in llm.payloads[0]
    assert _xml_escape("a&b<c") in llm.payloads[0]


async def test_two_chunks_of_different_lengths_use_per_chunk_budget(monkeypatch):
    """Per-chunk budgeting: each prompt's DOCUMENT share reflects its own chunk.

    Note (review): this test discriminates once-per-call vs per-chunk
    budgeting via document composition; it does NOT pin the chunk-half
    clamp (both chunks here sit below `remaining`). The clamp is pinned by
    test_chunk_view_clamped_to_half_when_between_half_and_remaining, and
    the budget TRUNCATION (any clamping at all) by
    test_escape_heavy_input_bounded_end_to_end and TestBoundedPromptBudget.
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
