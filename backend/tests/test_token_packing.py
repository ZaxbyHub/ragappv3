"""Tests for token packing in RAGEngine.

The _pack_context_by_token_budget method now returns a (chunks, stats) tuple
where stats contains token_pack_included, token_pack_skipped, token_pack_truncated.

Two strategies are supported via settings.token_pack_strategy:
  - 'reserved_best_fit'  (default): top-3 reserved, best-fit for rest (no break)
  - 'greedy'             (legacy):  first-fit with early break on overflow
"""

from unittest.mock import patch

import pytest

from app.services.document_retrieval import RAGSource
from app.services.rag_engine import RAGEngine


@pytest.fixture(autouse=True)
def _patch_ssrf():
    with patch("app.services.embeddings.assert_url_safe"), \
         patch("app.services.llm_client.assert_url_safe"):
        yield


def make_chunk(text: str, file_id: str = "file1", score: float = 0.9) -> RAGSource:
    """Helper to create RAGSource chunk."""
    return RAGSource(text=text, file_id=file_id, score=score, metadata={})


def pack(engine, chunks, max_tokens, strategy="reserved_best_fit"):
    """Helper: call _pack_context_by_token_budget with a patched strategy."""
    with patch("app.services.rag_engine.settings") as mock_settings:
        mock_settings.token_pack_strategy = strategy
        mock_settings.context_max_tokens = max_tokens
        return engine._pack_context_by_token_budget(chunks, max_tokens)


# ─────────────────────────────────────────────
# Greedy (legacy) strategy — backward-compat
# ─────────────────────────────────────────────

class TestGreedyStrategy:
    """Verify legacy greedy behavior is preserved when TOKEN_PACK_STRATEGY=greedy."""

    def _engine(self):
        return RAGEngine()

    def test_pack_within_budget(self):
        """All chunks fit within token budget."""
        chunks = [make_chunk("a" * 100), make_chunk("b" * 100), make_chunk("c" * 100)]
        packed, stats = pack(self._engine(), chunks, max_tokens=100, strategy="greedy")
        assert len(packed) == 3
        assert stats["token_pack_included"] == 3
        assert stats["token_pack_skipped"] == 0

    def test_pack_truncates_at_budget(self):
        """Greedy stops after first overflow.

        int(100/3.5) = 28 tokens per 100-char chunk.
        Budget 90: chunks 1+2+3 = 84 ≤ 90 (fit); chunk 4 = 112 > 90 → stop.
        """
        chunks = [
            make_chunk("a" * 100),  # 28 tokens
            make_chunk("b" * 100),  # 28 tokens
            make_chunk("c" * 100),  # 28 tokens — cumulative 84
            make_chunk("d" * 100),  # 28 tokens — would be 112 > 90 → stop
        ]
        packed, stats = pack(self._engine(), chunks, max_tokens=90, strategy="greedy")
        assert len(packed) == 3
        assert packed[-1].text == "c" * 100
        assert stats["token_pack_skipped"] == 1

    def test_pack_always_includes_first_chunk(self):
        """First chunk always included even if it exceeds budget alone."""
        chunks = [
            make_chunk("a" * 600),  # 150 tokens — exceeds budget of 100
            make_chunk("b" * 100),
        ]
        packed, stats = pack(self._engine(), chunks, max_tokens=100, strategy="greedy")
        assert len(packed) == 1
        assert packed[0].text == "a" * 600

    def test_pack_empty_list(self):
        """Empty list returns empty list."""
        packed, stats = pack(RAGEngine(), [], max_tokens=6000, strategy="greedy")
        assert packed == []
        assert stats["token_pack_included"] == 0

    def test_pack_single_chunk(self):
        """Single chunk always included regardless of size."""
        chunks = [make_chunk("x" * 10000)]  # 2500 tokens
        packed, stats = pack(RAGEngine(), chunks, max_tokens=100, strategy="greedy")
        assert len(packed) == 1

    def test_pack_exact_budget(self):
        """Chunks fitting exactly at budget limit are included.

        int(100/3.5) = 28 tokens per 100-char chunk.
        Budget 60: chunk1(28) + chunk2(28) = 56 ≤ 60 (fit); chunk3 = 84 > 60 → stop.
        """
        chunks = [
            make_chunk("a" * 100),  # 28 tokens → cumulative 28
            make_chunk("b" * 100),  # 28 tokens → cumulative 56 (fits in 60)
            make_chunk("c" * 100),  # 28 tokens → 84 > 60 → stop
        ]
        packed, stats = pack(RAGEngine(), chunks, max_tokens=60, strategy="greedy")
        assert len(packed) == 2
        assert packed[-1].text == "b" * 100


# ─────────────────────────────────────────────
# reserved_best_fit strategy (default)
# ─────────────────────────────────────────────

class TestReservedBestFitStrategy:
    """Verify reserved_best_fit: top-3 always included, best-fit for rest."""

    def _engine(self):
        return RAGEngine()

    def test_pack_within_budget(self):
        """All chunks fit — same result as greedy."""
        chunks = [make_chunk("a" * 100), make_chunk("b" * 100), make_chunk("c" * 100)]
        packed, stats = pack(self._engine(), chunks, max_tokens=100)
        assert len(packed) == 3
        assert stats["token_pack_included"] == 3
        assert stats["token_pack_skipped"] == 0
        assert stats["token_pack_truncated"] == 0

    def test_top3_always_included(self):
        """Top 3 are always included even when they exceed the budget."""
        # Budget: 40 tokens. Chunks 1-3 are 15 tokens each, total 45 > 40.
        chunks = [
            make_chunk("a" * 50),  # ~12 tokens
            make_chunk("b" * 50),  # ~12 tokens
            make_chunk("c" * 50),  # ~12 tokens
            make_chunk("d" * 50),  # ~12 tokens
        ]
        packed, stats = pack(self._engine(), chunks, max_tokens=30)
        # All 3 reserved are included; rank-4 likely won't fit
        assert len(packed) >= 3
        assert packed[0].text == "a" * 50
        assert packed[1].text == "b" * 50
        assert packed[2].text == "c" * 50

    def test_rank4_skip_does_not_break_rank5(self):
        """When rank-4 doesn't fit, rank-5 (if smaller) is still considered."""
        # Budget: 90 tokens
        # Rank 1-3: 20 tokens each (reserved, total 60)
        # Rank 4: 50 tokens — doesn't fit (60+50=110 > 90) → skip
        # Rank 5: 10 tokens — fits (60+10=70 ≤ 90) → include
        chunks = [
            make_chunk("a" * 70),   # ~20 tokens  rank-1 reserved
            make_chunk("b" * 70),   # ~20 tokens  rank-2 reserved
            make_chunk("c" * 70),   # ~20 tokens  rank-3 reserved
            make_chunk("d" * 175),  # ~50 tokens  rank-4, doesn't fit → skip
            make_chunk("e" * 35),   # ~10 tokens  rank-5, fits → include
        ]
        packed, stats = pack(self._engine(), chunks, max_tokens=90)
        texts = [c.text for c in packed]
        assert "a" * 70 in texts, "rank-1 must be included"
        assert "b" * 70 in texts, "rank-2 must be included"
        assert "c" * 70 in texts, "rank-3 must be included"
        assert "d" * 175 not in texts, "rank-4 should be skipped"
        assert "e" * 35 in texts, "rank-5 should be included (best-fit)"
        assert stats["token_pack_skipped"] == 1

    def test_skipped_counter_increments_for_each_overflow(self):
        """token_pack_skipped increments for every rank-4+ chunk that doesn't fit."""
        # Budget: 70 tokens
        # Rank 1-3: 20 tokens each (reserved, total 60)
        # Rank 4: 20 tokens → 60+20=80 > 70 → skip
        # Rank 5: 20 tokens → 60+20=80 > 70 → skip
        chunks = [
            make_chunk("a" * 70),  # 20 tokens — reserved
            make_chunk("b" * 70),  # 20 tokens — reserved
            make_chunk("c" * 70),  # 20 tokens — reserved
            make_chunk("d" * 70),  # 20 tokens — skip (no room)
            make_chunk("e" * 70),  # 20 tokens — skip (no room)
        ]
        packed, stats = pack(self._engine(), chunks, max_tokens=70)
        assert stats["token_pack_skipped"] == 2
        assert len(packed) == 3

    def test_reserved_over_budget_increments_truncated(self):
        """Reserved chunk that would push past budget increments token_pack_truncated."""
        # Budget: 25 tokens
        # Rank 1: 25 tokens — just fits
        # Rank 2: 25 tokens — 25+25=50 > 25, but reserved → include, truncated++
        # Rank 3: 25 tokens — 50+25 > 25, reserved → include, truncated++
        chunks = [
            make_chunk("a" * 100),  # 25 tokens
            make_chunk("b" * 100),  # 25 tokens
            make_chunk("c" * 100),  # 25 tokens
        ]
        packed, stats = pack(self._engine(), chunks, max_tokens=25)
        assert len(packed) == 3  # all 3 reserved chunks are always included
        assert stats["token_pack_truncated"] >= 1

    def test_empty_list(self):
        """Empty input returns empty output."""
        packed, stats = pack(RAGEngine(), [], max_tokens=6000)
        assert packed == []
        assert stats["token_pack_included"] == 0
        assert stats["token_pack_skipped"] == 0
        assert stats["token_pack_truncated"] == 0

    def test_single_chunk_always_included(self):
        """Single chunk always included regardless of size (reserved top-1)."""
        chunks = [make_chunk("x" * 10000)]
        packed, stats = pack(RAGEngine(), chunks, max_tokens=100)
        assert len(packed) == 1

    def test_preserves_order(self):
        """Packed chunks preserve original rank order."""
        chunks = [
            make_chunk("first", file_id="1"),
            make_chunk("second", file_id="2"),
            make_chunk("third", file_id="3"),
        ]
        packed, _ = pack(RAGEngine(), chunks, max_tokens=1000)
        assert [c.file_id for c in packed] == ["1", "2", "3"]

    def test_preserves_metadata(self):
        """Metadata and score survive packing unchanged."""
        chunk = make_chunk("text1", file_id="f1", score=0.5)
        chunk.metadata = {"page": 10, "source": "pdf"}
        packed, _ = pack(RAGEngine(), [chunk], max_tokens=100)
        assert packed[0].metadata == {"page": 10, "source": "pdf"}
        assert packed[0].score == 0.5

    def test_stats_included_count_matches_list_length(self):
        """token_pack_included always equals len(packed)."""
        chunks = [make_chunk("a" * 50 * i, file_id=str(i)) for i in range(1, 8)]
        packed, stats = pack(RAGEngine(), chunks, max_tokens=200)
        assert stats["token_pack_included"] == len(packed)

    def test_two_chunks_both_reserved(self):
        """With only 2 chunks both are reserved (n_reserved=min(3,2)=2)."""
        chunks = [make_chunk("x" * 1000), make_chunk("y" * 1000)]
        packed, stats = pack(RAGEngine(), chunks, max_tokens=10)
        assert len(packed) == 2
        assert stats["token_pack_skipped"] == 0


class TestPackContextByTokenBudgetEdgeCases:
    """Additional edge cases."""

    def test_large_text_handling(self):
        """Very large text single chunk is handled correctly."""
        large_text = "x" * 100000  # 25000 tokens
        chunks = [make_chunk(large_text)]
        packed, stats = pack(RAGEngine(), chunks, max_tokens=100)
        assert len(packed) == 1
        assert len(packed[0].text) == 100000

    def test_unicode_text(self):
        """Unicode characters are counted correctly."""
        chunks = [make_chunk("hello"), make_chunk("你好"), make_chunk("🌍🌎🌏")]
        packed, _ = pack(RAGEngine(), chunks, max_tokens=1000)
        assert len(packed) == 3
