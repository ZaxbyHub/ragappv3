"""Tests for off-loop sentence dedup + sentence cap in ContextDistiller
(issue #511 FULL-ENH-03, AC13).

Contract:
- the O(K^2) greedy cosine dedup loop runs OFF the event loop (worker
  thread via asyncio.to_thread), so a concurrent watchdog task keeps
  ticking while distill() runs (generous margin per the frozen check C13)
- total sentences are capped at ``context_distiller_max_sentences`` BEFORE
  dedup (earliest sources' sentences kept first, one INFO with counts);
  embed_batch receives at most the capped set
- under-cap inputs produce outputs identical to the pre-change semantics
"""

import asyncio
import logging
import math
import random
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.context_distiller import ContextDistiller, DistillResult
from app.services.rag_engine import RAGSource

GAP_THRESHOLD_MS = 150.0  # generous: pre-fix blocks ~500-850ms; post-fix ~15-31ms


def make_vector(i: int, dim: int = 8):
    """Deterministic near-orthogonal unit vector: nearly every sentence
    SURVIVES the dedup threshold, maximizing kept-embedding comparisons."""
    rng = random.Random(1000 + i)
    v = [rng.gauss(0.0, 1.0) for _ in range(dim)]
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


class FakeEmbeddingService:
    """Deterministic, no-network embedding service (dim-8 vectors)."""

    def __init__(self, dim: int = 8):
        self.dim = dim
        self.calls = []

    async def embed_batch(self, texts):
        self.calls.append(list(texts))
        return [make_vector(i, self.dim) for i in range(len(texts))]


class Watchdog:
    """Loop-responsiveness probe: max gap between 5ms-sleep wakeups."""

    def __init__(self):
        self.max_gap = 0.0
        self.iterations = 0
        self._last = None
        self._stop = asyncio.Event()

    async def run(self):
        self._last = time.monotonic()
        while not self._stop.is_set():
            await asyncio.sleep(0.005)
            now = time.monotonic()
            if now - self._last > self.max_gap:
                self.max_gap = now - self._last
            self.iterations += 1
            self._last = now

    def stop(self):
        self._stop.set()


class TestGreedyDedupHelper:
    """Unit tests for the extracted module-level sync helper."""

    def test_exists_and_is_sync(self):
        import inspect

        from app.services.context_distiller import _greedy_dedup

        assert not inspect.iscoroutinefunction(_greedy_dedup)

    def test_first_source_sentences_always_kept(self):
        from app.services.context_distiller import _greedy_dedup

        embeddings = [[1.0, 0.0], [1.0, 0.0], [1.0, 0.0]]
        sentence_map = [(0, 0), (0, 1), (1, 0)]
        is_dup = _greedy_dedup(embeddings, sentence_map, threshold=0.92)
        # Both source-0 sentences kept unconditionally — even though they are
        # identical vectors (first/highest-ranked source always wins).
        assert is_dup[0] is False
        assert is_dup[1] is False
        # The source-1 sentence duplicates a kept embedding -> flagged.
        assert is_dup[2] is True

    def test_unique_lower_sentence_kept(self):
        from app.services.context_distiller import _greedy_dedup

        embeddings = [[1.0, 0.0], [0.0, 1.0]]
        sentence_map = [(0, 0), (1, 0)]
        is_dup = _greedy_dedup(embeddings, sentence_map, threshold=0.92)
        assert is_dup == [False, False]


class TestDedupOffEventLoop:
    """The heavy greedy loop must not block the loop (check C13 behavior)."""

    @pytest.mark.asyncio
    async def test_watchdog_keeps_ticking_during_distill(self):
        with patch("app.config.settings") as mock_settings:
            mock_settings.context_distillation_dedup_threshold = 0.92
            mock_settings.context_distillation_synthesis_enabled = False
            # No int cap under a MagicMock: the cap code must tolerate it
            # (real deployments always have the int setting).
            mock_settings.context_distiller_max_sentences = 600

            embedder = FakeEmbeddingService()
            distiller = ContextDistiller(embedder, llm_client=None)
            sources = []
            for s in range(4):
                sentences = [
                    f"Fact {s:02d}{i:05d}: the observatory recorded "
                    f"{i % 13} signals near sector {i % 7} during cycle {i}."
                    for i in range(300)
                ]
                sources.append(
                    RAGSource(
                        text=" ".join(sentences),
                        file_id=f"dedupfile{s}",
                        score=0.9 - s * 0.1,
                        metadata={"source_file": f"dedupdoc{s}.txt"},
                    )
                )

            wd = Watchdog()
            wd_task = asyncio.create_task(wd.run())
            await asyncio.sleep(0.05)  # let the watchdog start ticking

            result = await distiller.distill(
                "observatory signals", sources, eval_result="CONFIDENT"
            )

            await asyncio.sleep(0.05)  # observe the tail
            wd.stop()
            await wd_task

        assert isinstance(result, DistillResult)
        assert result.sources, "distill returned no sources"
        gap_ms = wd.max_gap * 1000
        assert gap_ms < GAP_THRESHOLD_MS, (
            f"sentence dedup blocked the event loop: max watchdog gap "
            f"{gap_ms:.0f} ms >= {GAP_THRESHOLD_MS:.0f} ms threshold "
            f"({wd.iterations} iterations)"
        )


class TestSentenceCap:
    """Input sentences are capped before dedup (earliest first)."""

    @pytest.fixture
    def embedder(self):
        return FakeEmbeddingService()

    @pytest.mark.asyncio
    async def test_over_cap_truncates_before_embedding(
        self, embedder, caplog
    ):
        cap = 5
        with patch("app.config.settings") as mock_settings, caplog.at_level(
            logging.INFO, logger="app.services.context_distiller"
        ):
            mock_settings.context_distiller_max_sentences = cap
            mock_settings.context_distillation_dedup_threshold = 0.92
            mock_settings.context_distillation_synthesis_enabled = False

            sources = [
                RAGSource(
                    text=" ".join(
                        f"Source{si} sentence {i} with words." for i in range(3)
                    ),
                    file_id=f"f{si}",
                    score=0.9 - si * 0.1,
                    metadata={},
                )
                for si in range(3)  # 9 sentences total, cap 5
            ]
            distiller = ContextDistiller(embedder)
            result = await distiller._deduplicate(sources, threshold=0.92)

        # embed_batch saw exactly the first 5 sentences (earliest sources
        # first, order preserved).
        assert len(embedder.calls) == 1
        embedded = embedder.calls[0]
        assert len(embedded) == cap
        assert embedded[0] == "Source0 sentence 0 with words."
        assert embedded[2] == "Source0 sentence 2 with words."
        assert embedded[3] == "Source1 sentence 0 with words."
        assert embedded[4] == "Source1 sentence 1 with words."

        # Source 2 lost every sentence to truncation -> dropped by the
        # existing <50-char guard; sources 0 and 1 survive.
        assert [s.file_id for s in result.sources] == ["f0", "f1"]

        # One INFO line with counts.
        cap_logs = [r for r in caplog.records if "cap" in r.getMessage().lower()]
        assert len(cap_logs) == 1
        msg = cap_logs[0].getMessage()
        assert "9" in msg and "5" in msg

    @pytest.mark.asyncio
    async def test_under_cap_output_identical(self):
        """Under the cap the result matches the pre-change semantics
        (deterministic known-small scenario, exact expected output)."""
        embedder = MagicMock()
        embedder.embed_batch = AsyncMock(return_value=[[1.0, 0.0], [1.0, 0.0]])

        with patch("app.config.settings") as mock_settings:
            mock_settings.context_distiller_max_sentences = 600
            mock_settings.context_distillation_dedup_threshold = 0.92
            mock_settings.context_distillation_synthesis_enabled = False

            sources = [
                RAGSource(
                    text="The rotation policy runs nightly across vaults.",
                    file_id="file1",
                    score=0.9,
                    metadata={},
                ),
                RAGSource(
                    text="The rotation policy runs nightly across vaults.",
                    file_id="file2",
                    score=0.8,
                    metadata={},
                ),
            ]
            # Identical embedding for both -> the second is a duplicate.

            distiller = ContextDistiller(embedder)
            result = await distiller._deduplicate(sources, threshold=0.92)

        # No truncation: embed_batch received both sentences in order.
        embedded_texts = embedder.embed_batch.call_args[0][0]
        assert len(embedded_texts) == 2, "under-cap input must not truncate"
        # file1 survives; file2 emptied by dedup and dropped by the guard.
        assert [s.file_id for s in result.sources] == ["file1"]
        assert result.sources[0].text == (
            "The rotation policy runs nightly across vaults."
        )
        assert len(result.sentence_provenance) == 1
        assert result.sentence_provenance[0].source_file_id == "file1"
        assert result.sentence_provenance[0].source_index == 0

    @pytest.mark.asyncio
    async def test_cap_equal_to_input_no_truncation(self, embedder):
        with patch("app.config.settings") as mock_settings:
            mock_settings.context_distiller_max_sentences = 2
            mock_settings.context_distillation_dedup_threshold = 0.92
            mock_settings.context_distillation_synthesis_enabled = False

            sources = [
                RAGSource(text="One unique sentence.", file_id="f1", score=0.9, metadata={}),
                RAGSource(text="Another unique sentence.", file_id="f2", score=0.8, metadata={}),
            ]
            distiller = ContextDistiller(embedder)
            result = await distiller._deduplicate(sources, threshold=0.92)

        assert len(embedder.calls[0]) == 2
        assert len(result.sources) == 2

    @pytest.mark.asyncio
    async def test_mock_settings_without_int_cap_still_works(self, embedder):
        """Existing suites patch app.config.settings with a bare MagicMock
        (no int cap attribute value). The cap read must tolerate that."""
        with patch("app.config.settings") as mock_settings:
            mock_settings.context_distillation_dedup_threshold = 0.92
            mock_settings.context_distillation_synthesis_enabled = False
            # context_distiller_max_sentences left as MagicMock attribute.

            sources = [
                RAGSource(
                    text="A unique sentence that survives.", file_id="f1",
                    score=0.9, metadata={},
                ),
            ]
            distiller = ContextDistiller(embedder)
            result = await distiller._deduplicate(sources, threshold=0.92)

        assert len(embedder.calls[0]) == 1
        assert len(result.sources) == 1
