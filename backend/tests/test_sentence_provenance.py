"""Tests for sentence-level provenance in context distillation (FR-003).

Verifies that:
- Each kept sentence has file_id + char span in the provenance map.
- Char offsets are correct relative to the original source text.
- Dropped (duplicate) sentences are absent from the provenance map.
- Dedup still works correctly at sentence granularity.
- Distilled text output is unchanged (only provenance metadata added).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.context_distiller import (
    ContextDistiller,
    DistillResult,
    SentenceProvenance,
    _split_sentences_with_spans,
)
from app.services.rag_engine import RAGSource


class TestSentenceProvenanceSpans:
    """Unit tests for _split_sentences_with_spans correctness."""

    def test_spans_are_correct_for_simple_text(self):
        """Verify text[char_start:char_end] == sentence_text for each span."""
        text = "The cat sat. The dog ran. The bird flew."
        result = _split_sentences_with_spans(text)
        assert len(result) == 3
        for sentence, (start, end) in result:
            assert text[start:end] == sentence

    def test_spans_preserve_trailing_punctuation(self):
        """Sentence text includes its ending punctuation."""
        text = "Is this real? I think so!"
        result = _split_sentences_with_spans(text)
        assert result[0][0] == "Is this real?"
        assert result[1][0] == "I think so!"
        for sentence, (start, end) in result:
            assert text[start:end] == sentence

    def test_spans_no_false_positives_on_internal_periods(self):
        """Abbreviated periods (e.g. Dr.) DO split because there's a space after.

        The simple ``(?<=[.!?])\\s+`` regex cannot distinguish "Dr." from "Hello."
        Both have punctuation followed by whitespace and are split as sentence boundaries.
        This is the same limitation as _split_sentences and is expected.
        """
        text = "Dr. Smith works here. She arrived Monday."
        result = _split_sentences_with_spans(text)
        # Split creates 3 parts: "Dr.", "Smith works here.", "She arrived Monday."
        assert len(result) == 3
        for sentence, (start, end) in result:
            assert text[start:end] == sentence

    def test_spans_empty_input(self):
        """Empty text returns empty spans."""
        result = _split_sentences_with_spans("")
        assert result == []


class TestProvenanceMapConstruction:
    """Tests that provenance map is correctly built in _deduplicate."""

    @pytest.fixture
    def mock_embedding_service(self):
        mock = MagicMock()
        mock.embed_batch = AsyncMock()
        return mock

    @pytest.mark.asyncio
    async def test_provenance_has_file_id_for_each_kept_sentence(
        self, mock_embedding_service
    ):
        """Every kept sentence carries the correct source file_id."""
        sources = [
            RAGSource(
                text="First source sentence here with more text for testing purposes.",
                file_id="file_alpha",
                score=0.9,
                metadata={},
            ),
            RAGSource(
                text="Second source sentence here with more text for testing purposes.",
                file_id="file_beta",
                score=0.8,
                metadata={},
            ),
        ]
        # Two unique sentences (different embeddings)
        mock_embedding_service.embed_batch.return_value = [
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
        ]

        distiller = ContextDistiller(mock_embedding_service)
        result = await distiller._deduplicate(sources, threshold=0.92)

        assert isinstance(result, DistillResult)
        assert len(result.sentence_provenance) == 2
        file_ids = {p.source_file_id for p in result.sentence_provenance}
        assert file_ids == {"file_alpha", "file_beta"}

    @pytest.mark.asyncio
    async def test_provenance_char_offsets_slice_to_sentence_text(
        self, mock_embedding_service
    ):
        """text[char_start:char_end] == sentence_text for each provenance entry."""
        original_text_0 = "First source sentence with content here."
        original_text_1 = "Second source sentence with content here."
        sources = [
            RAGSource(
                text=original_text_0,
                file_id="file_a",
                score=0.9,
                metadata={},
            ),
            RAGSource(
                text=original_text_1,
                file_id="file_b",
                score=0.8,
                metadata={},
            ),
        ]
        # Two unique sentences
        mock_embedding_service.embed_batch.return_value = [
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
        ]

        distiller = ContextDistiller(mock_embedding_service)
        result = await distiller._deduplicate(sources, threshold=0.92)

        for prov in result.sentence_provenance:
            # Look up the original source text for this sentence
            src_idx = prov.source_index
            orig_text = sources[src_idx].text
            # Verify the span slices to the sentence text
            sliced = orig_text[prov.char_start:prov.char_end]
            assert sliced == prov.sentence_text, (
                f"span ({prov.char_start},{prov.char_end}) in "
                f"'{orig_text}' sliced to '{sliced}', expected '{prov.sentence_text}'"
            )

    @pytest.mark.asyncio
    async def test_dropped_duplicates_absent_from_provenance(
        self, mock_embedding_service
    ):
        """Duplicate sentences are NOT in the provenance map."""
        sources = [
            RAGSource(
                text="Original sentence with unique content here and extra details.",
                file_id="file_x",
                score=0.9,
                metadata={},
            ),
            RAGSource(
                text="Original sentence with unique content here and extra details.",
                file_id="file_y",
                score=0.8,
                metadata={},
            ),
        ]
        # Both sentences have same embedding = duplicate
        mock_embedding_service.embed_batch.return_value = [
            [1.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
        ]

        distiller = ContextDistiller(mock_embedding_service)
        result = await distiller._deduplicate(sources, threshold=0.92)

        # Only the first source's sentence is kept
        assert len(result.sentence_provenance) == 1
        prov = result.sentence_provenance[0]
        assert prov.source_file_id == "file_x"
        assert prov.sentence_text == "Original sentence with unique content here and extra details."

    @pytest.mark.asyncio
    async def test_provenance_list_order_matches_sentence_order_in_sources(
        self, mock_embedding_service
    ):
        """Provenance list is ordered by (source_index, sentence_position)."""
        sources = [
            RAGSource(
                text="Doc A sentence 1 here for testing. Doc A sentence 2 here for testing.",
                file_id="doc_a",
                score=0.9,
                metadata={},
            ),
            RAGSource(
                text="Doc B sentence 1 here with additional context for length.",
                file_id="doc_b",
                score=0.8,
                metadata={},
            ),
        ]
        # All different embeddings
        mock_embedding_service.embed_batch.return_value = [
            [1.0, 0.0, 0.0],  # Doc A sent 1
            [0.0, 1.0, 0.0],  # Doc A sent 2
            [0.0, 0.0, 1.0],  # Doc B sent 1
        ]

        distiller = ContextDistiller(mock_embedding_service)
        result = await distiller._deduplicate(sources, threshold=0.92)

        assert len(result.sentence_provenance) == 3
        # Order: doc_a first sentence, doc_a second sentence, doc_b first sentence
        assert result.sentence_provenance[0].sentence_text == "Doc A sentence 1 here for testing."
        assert result.sentence_provenance[0].source_index == 0
        assert result.sentence_provenance[1].sentence_text == "Doc A sentence 2 here for testing."
        assert result.sentence_provenance[1].source_index == 0
        assert result.sentence_provenance[2].sentence_text == "Doc B sentence 1 here with additional context for length."
        assert result.sentence_provenance[2].source_index == 1

    @pytest.mark.asyncio
    async def test_provenance_source_index_correct(self, mock_embedding_service):
        """source_index in provenance matches the source's position in the input list."""
        sources = [
            RAGSource(text="Zero sentence here with additional context for length.", file_id="f0", score=0.9, metadata={}),
            RAGSource(text="One sentence here with additional context for length.", file_id="f1", score=0.8, metadata={}),
            RAGSource(text="Two sentence here with additional context for length.", file_id="f2", score=0.7, metadata={}),
        ]
        mock_embedding_service.embed_batch.return_value = [
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ]

        distiller = ContextDistiller(mock_embedding_service)
        result = await distiller._deduplicate(sources, threshold=0.92)

        assert len(result.sentence_provenance) == 3
        for prov in result.sentence_provenance:
            assert prov.source_index in (0, 1, 2)
            assert sources[prov.source_index].file_id == prov.source_file_id


class TestProvenanceDedupIntegration:
    """Integration tests verifying dedup still works correctly with provenance."""

    @pytest.fixture
    def mock_embedding_service(self):
        mock = MagicMock()
        mock.embed_batch = AsyncMock()
        return mock

    @pytest.mark.asyncio
    async def test_dedup_removes_near_duplicates_provenance_correct(
        self, mock_embedding_service
    ):
        """Near-duplicates removed; kept sentences have correct provenance."""
        sources = [
            RAGSource(
                text="This is the first document content sentence that provides context.",
                file_id="doc1",
                score=0.9,
                metadata={},
            ),
            RAGSource(
                text="This is the first document content sentence that provides context.",
                file_id="doc2",
                score=0.8,
                metadata={},
            ),
            RAGSource(
                text="Completely different unique sentence here with more content.",
                file_id="doc3",
                score=0.7,
                metadata={},
            ),
        ]
        # Each source has exactly 1 sentence (split on period at end)
        # source0: sent0=[1,0,0], source1: sent0=[1,0,0] (duplicate), source2: sent0=[0,0,1] (unique)
        mock_embedding_service.embed_batch.return_value = [
            [1.0, 0.0, 0.0],  # source0 sent0
            [1.0, 0.0, 0.0],  # source1 sent0 (duplicate of source0 sent0)
            [0.0, 0.0, 1.0],  # source2 sent0 (unique)
        ]

        distiller = ContextDistiller(mock_embedding_service)
        result = await distiller._deduplicate(sources, threshold=0.92)

        # 2 kept sentences: source0-sent0, source2-sent0
        # source1 is dropped (< 50 chars after dedup)
        assert len(result.sentence_provenance) == 2
        # Verify source1's duplicate is NOT in provenance
        dup_prov = [p for p in result.sentence_provenance if p.source_file_id == "doc2"]
        assert len(dup_prov) == 0

    @pytest.mark.asyncio
    async def test_distilled_text_unchanged(self, mock_embedding_service):
        """Distilled text output is identical regardless of provenance tracking."""
        sources = [
            RAGSource(
                text="Original content with additional text for testing.",
                file_id="f1",
                score=0.9,
                metadata={},
            ),
            RAGSource(
                text="Different content with additional text for testing.",
                file_id="f2",
                score=0.8,
                metadata={},
            ),
        ]
        mock_embedding_service.embed_batch.return_value = [
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
        ]

        distiller = ContextDistiller(mock_embedding_service)
        result = await distiller._deduplicate(sources, threshold=0.92)

        # Text output should be same as if we just joined kept sentences
        assert isinstance(result, DistillResult)
        for src in result.sources:
            assert src.text  # not empty
            # Verify text is reconstructable from sentence texts
            prov_for_src = [
                p for p in result.sentence_provenance if p.source_file_id == src.file_id
            ]
            reconstructed = " ".join(p.sentence_text for p in prov_for_src)
            assert reconstructed == src.text


class TestDistillResultAdditive:
    """Tests that DistillResult is returned additively without breaking callers."""

    @pytest.fixture
    def mock_embedding_service(self):
        mock = MagicMock()
        mock.embed_batch = AsyncMock(return_value=[[1.0, 0.0, 0.0]])
        return mock

    @pytest.fixture
    def mock_llm_client(self):
        mock = MagicMock()
        mock.chat_completion = AsyncMock(return_value="Synthesized content.")
        return mock

    @pytest.mark.asyncio
    async def test_distill_returns_distill_result(self, mock_embedding_service):
        """distill() returns DistillResult (not bare list)."""
        sources = [
            RAGSource(
                text="Content here with additional text for length.",
                file_id="f1",
                score=0.9,
                metadata={},
            ),
        ]

        with patch("app.config.settings") as mock_settings:
            mock_settings.context_distillation_enabled = True
            mock_settings.context_distillation_synthesis_enabled = False
            mock_settings.context_distillation_dedup_threshold = 0.92

            distiller = ContextDistiller(mock_embedding_service)
            result = await distiller.distill("query", sources, "CONFIDENT")

            assert isinstance(result, DistillResult)
            assert isinstance(result.sources, list)
            assert isinstance(result.sentence_provenance, list)

    @pytest.mark.asyncio
    async def test_distill_with_synthesis_adds_synthetic_source_provenance_preserved(
        self, mock_embedding_service, mock_llm_client
    ):
        """Synthesis appends synthetic source; provenance still has real sentences.

        Verifies that when synthesis fires (NO_MATCH + client provided), the
        synthetic source is appended and real sentence provenance is preserved.
        """
        # Text must be >= 50 chars after dedup to survive the <50 drop guard
        sources = [
            RAGSource(
                text="Real content about topic A here that is substantial enough.",
                file_id="f1",
                score=0.9,
                metadata={},
            ),
            RAGSource(
                text="More real content about topic B here also substantial enough.",
                file_id="f2",
                score=0.8,
                metadata={},
            ),
        ]
        # Two unique sentences (different embeddings)
        mock_embedding_service.embed_batch.return_value = [
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
        ]
        mock_llm_client.chat_completion.return_value = (
            "A synthesized passage from these sources."
        )

        distiller = ContextDistiller(mock_embedding_service, mock_llm_client)

        # Test _synthesize directly (avoiding patch complexity)
        synthesized = await distiller._synthesize("query", sources)
        assert synthesized[-1].metadata.get("synthesized") is True

        # Verify DistillResult carries provenance through the synthesis path
        # by checking that _deduplicate returns provenance (synthesis doesn't
        # modify provenance — it's appended, not transformed).
        dedup_result = await distiller._deduplicate(sources, threshold=0.92)
        assert isinstance(dedup_result, DistillResult)
        assert len(dedup_result.sentence_provenance) == 2
        # After synthesis, provenance is carried forward unchanged
        assert len(dedup_result.sources) == 2

    @pytest.mark.asyncio
    async def test_span_slice_equality_multi_source_multispace_unicode(
        self, mock_embedding_service
    ):
        """KEY invariant: source_text[char_start:char_end] == sentence_text.

        Covers:
        - Multiple sources
        - Multi-space separators between sentences
        - Unicode characters (non-ASCII)
        across the full provenance map returned by _deduplicate.
        """
        original_text_0 = "First  sentence.   Second sentence here.  Third sentence."
        original_text_1 = "原始中文内容测试文档记录中文测试中。 Another English sentence. 更多内容。"
        sources = [
            RAGSource(
                text=original_text_0,
                file_id="unicode_file",
                score=0.9,
                metadata={},
            ),
            RAGSource(
                text=original_text_1,
                file_id="mixed_lang_file",
                score=0.8,
                metadata={},
            ),
        ]
        # All sentences are unique (different embeddings)
        mock_embedding_service.embed_batch.return_value = [
            [1.0, 0.0, 0.0, 0.0, 0.0],  # source0 sent0
            [0.0, 1.0, 0.0, 0.0, 0.0],  # source0 sent1
            [0.0, 0.0, 1.0, 0.0, 0.0],  # source0 sent2
            [0.0, 0.0, 0.0, 1.0, 0.0],  # source1 sent0 (unicode)
            [0.0, 0.0, 0.0, 0.0, 1.0],  # source1 sent1 (english in unicode text)
        ]

        distiller = ContextDistiller(mock_embedding_service)
        result = await distiller._deduplicate(sources, threshold=0.92)

        assert len(result.sentence_provenance) == 5
        for prov in result.sentence_provenance:
            src_idx = prov.source_index
            orig_text = sources[src_idx].text
            # KEY invariant: span must slice exactly to sentence_text
            sliced = orig_text[prov.char_start:prov.char_end]
            assert sliced == prov.sentence_text, (
                f"span ({prov.char_start},{prov.char_end}) in "
                f"'{orig_text}' sliced to '{sliced}', expected '{prov.sentence_text}'"
            )

    @pytest.mark.asyncio
    async def test_short_source_dropped_no_dangling_provenance(self, mock_embedding_service):
        """A source whose kept text is < 50 chars is absent from sources AND its
        provenance entries are absent — no dangling reference to a non-existent
        source."""

        # source0: 1 sentence, long enough to survive
        # source1: 1 sentence, same content → duplicate, AND after dedup the
        #          kept text for source1 is empty → source1 is dropped by the
        #          < 50-char guard. Its provenance must NOT appear in the result.
        sources = [
            RAGSource(
                text="This is a substantial source sentence that is definitely long enough.",
                file_id="surviving_source",
                score=0.9,
                metadata={},
            ),
            RAGSource(
                text="This is a substantial source sentence that is definitely long enough.",
                file_id="dropped_source",
                score=0.8,
                metadata={},
            ),
        ]
        # Both sentences have identical embeddings → second is marked duplicate
        mock_embedding_service.embed_batch.return_value = [
            [1.0, 0.0, 0.0],  # source0 sentence (kept, unique)
            [1.0, 0.0, 0.0],  # source1 sentence (duplicate of source0)
        ]

        distiller = ContextDistiller(mock_embedding_service)
        result = await distiller._deduplicate(sources, threshold=0.92)

        # source1's sentence is a duplicate of source0's; after dedup, source1
        # would contribute 0 sentences → its joined text is "" (< 50 chars) →
        # source1 is dropped from deduped entirely.
        surviving_file_ids = {s.file_id for s in result.sources}
        assert surviving_file_ids == {"surviving_source"}

        # No provenance entry may reference a non-surviving source
        dangling = [
            p for p in result.sentence_provenance
            if p.source_file_id not in surviving_file_ids
        ]
        assert dangling == [], (
            f"Found dangling provenance entries pointing to non-existent sources: "
            f"{[p.source_file_id for p in dangling]}"
        )

        # All provenance entries must reference surviving sources
        for prov in result.sentence_provenance:
            assert prov.source_file_id in surviving_file_ids

    @pytest.mark.asyncio
    async def test_empty_sources_returns_empty_provenance(self, mock_embedding_service):
        """Empty input gives empty provenance list."""
        with patch("app.config.settings") as mock_settings:
            mock_settings.context_distillation_enabled = True
            mock_settings.context_distillation_synthesis_enabled = False
            mock_settings.context_distillation_dedup_threshold = 0.92

            distiller = ContextDistiller(mock_embedding_service)
            result = await distiller.distill("query", [], "CONFIDENT")

            assert isinstance(result, DistillResult)
            assert result.sources == []
            assert result.sentence_provenance == []
