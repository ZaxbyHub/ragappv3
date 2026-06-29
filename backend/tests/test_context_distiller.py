"""Tests for ContextDistiller service."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.context_distiller import (
    ContextDistiller,
    DistillResult,
    SentenceProvenance,
    _cosine_similarity,
    _split_sentences,
    _split_sentences_with_spans,
)
from app.services.rag_engine import RAGSource


class TestCosineSimilarity:
    """Tests for _cosine_similarity helper function."""

    def test_cosine_similarity_identical(self):
        """Returns 1.0 for identical vectors."""
        a = [1.0, 2.0, 3.0]
        b = [1.0, 2.0, 3.0]
        result = _cosine_similarity(a, b)
        assert result == 1.0

    def test_cosine_similarity_orthogonal(self):
        """Returns 0.0 for orthogonal vectors."""
        a = [1.0, 0.0, 0.0]
        b = [0.0, 1.0, 0.0]
        result = _cosine_similarity(a, b)
        assert result == 0.0

    def test_cosine_similarity_zero_vector(self):
        """Returns 0.0 when one vector is zero."""
        a = [0.0, 0.0, 0.0]
        b = [1.0, 2.0, 3.0]
        result = _cosine_similarity(a, b)
        assert result == 0.0

    def test_cosine_similarity_zero_vector_reversed(self):
        """Returns 0.0 when the other vector is zero (reversed)."""
        a = [1.0, 2.0, 3.0]
        b = [0.0, 0.0, 0.0]
        result = _cosine_similarity(a, b)
        assert result == 0.0

    def test_cosine_similarity_both_zero(self):
        """Returns 0.0 when both vectors are zero."""
        a = [0.0, 0.0, 0.0]
        b = [0.0, 0.0, 0.0]
        result = _cosine_similarity(a, b)
        assert result == 0.0


class TestSplitSentences:
    """Tests for _split_sentences helper function."""

    def test_split_sentences_basic(self):
        """Splits on period."""
        text = "Hello world. This is a test. Final sentence."
        result = _split_sentences(text)
        assert result == ["Hello world.", "This is a test.", "Final sentence."]

    def test_split_sentences_multiple_punctuation(self):
        """Splits on ! and ?."""
        text = "Hello world! Is this working? Yes it is."
        result = _split_sentences(text)
        assert result == ["Hello world!", "Is this working?", "Yes it is."]

    def test_split_sentences_no_punctuation(self):
        """Returns single element when no punctuation."""
        text = "Hello world"
        result = _split_sentences(text)
        assert result == ["Hello world"]

    def test_split_sentences_multiple_spaces(self):
        """Handles multiple spaces correctly."""
        text = "First.    Second.   Third."
        result = _split_sentences(text)
        assert result == ["First.", "Second.", "Third."]

    def test_split_sentences_empty_string(self):
        """Returns empty list for empty string."""
        result = _split_sentences("")
        assert result == []


class TestSplitSentencesWithSpans:
    """Tests for _split_sentences_with_spans helper function."""

    def test_basic_spans(self):
        """Spans correctly identify sentence boundaries."""
        text = "Hello world. This is a test."
        result = _split_sentences_with_spans(text)
        assert result == [
            ("Hello world.", (0, 12)),
            ("This is a test.", (13, 28)),
        ]
        # Verify slice matches sentence text
        assert text[result[0][1][0]:result[0][1][1]] == "Hello world."
        assert text[result[1][1][0]:result[1][1][1]] == "This is a test."

    def test_multiple_punctuation_spans(self):
        """Spans work with ! and ? punctuation."""
        text = "Hello world! Is this working? Yes it is."
        result = _split_sentences_with_spans(text)
        assert result[0][0] == "Hello world!"
        assert text[result[0][1][0]:result[0][1][1]] == "Hello world!"
        assert result[1][0] == "Is this working?"
        assert text[result[1][1][0]:result[1][1][1]] == "Is this working?"
        assert result[2][0] == "Yes it is."
        assert text[result[2][1][0]:result[2][1][1]] == "Yes it is."

    def test_no_punctuation_span(self):
        """Single sentence without punctuation."""
        text = "Hello world"
        result = _split_sentences_with_spans(text)
        assert result == [("Hello world", (0, 11))]
        assert text[result[0][1][0]:result[0][1][1]] == "Hello world"

    def test_multiple_spaces_spans(self):
        """Multiple spaces between sentences."""
        text = "First.    Second.   Third."
        result = _split_sentences_with_spans(text)
        assert result[0][0] == "First."
        assert text[result[0][1][0]:result[0][1][1]] == "First."
        assert result[1][0] == "Second."
        assert text[result[1][1][0]:result[1][1][1]] == "Second."
        assert result[2][0] == "Third."
        assert text[result[2][1][0]:result[2][1][1]] == "Third."

    def test_empty_string(self):
        """Empty string returns empty list."""
        result = _split_sentences_with_spans("")
        assert result == []

    def test_trailing_whitespace(self):
        """Trailing whitespace handled correctly."""
        text = "First. Second.   "
        result = _split_sentences_with_spans(text)
        assert result[0][0] == "First."
        assert result[1][0] == "Second."

    def test_consecutive_punctuation(self):
        """Handles sentences ending with multiple punctuation."""
        text = "Wait! Really?? Yes!"
        result = _split_sentences_with_spans(text)
        assert result[0][0] == "Wait!"
        assert result[1][0] == "Really??"
        assert result[2][0] == "Yes!"


class TestDeduplicate:
    """Tests for _deduplicate method."""

    @pytest.fixture
    def mock_embedding_service(self):
        """Create a mock embedding service."""
        mock = MagicMock()
        mock.embed_batch = AsyncMock()
        return mock

    @pytest.fixture
    def sample_sources(self):
        """Create sample RAGSource objects for testing."""
        return [
            RAGSource(
                text="First source with unique content.",
                file_id="file1",
                score=0.9,
                metadata={},
            ),
            RAGSource(
                text="First source repeated here.",
                file_id="file2",
                score=0.8,
                metadata={},
            ),
            RAGSource(
                text="Another unique sentence here.",
                file_id="file3",
                score=0.7,
                metadata={},
            ),
        ]

    @pytest.mark.asyncio
    async def test_deduplicate_removes_duplicates(
        self, mock_embedding_service, sample_sources
    ):
        """Near-duplicate sentences removed."""
        # Test with multiple sentences per source
        # Source 0 has 2 sentences, source 1 has 1, source 2 has 1
        sources = [
            RAGSource(
                text="First sentence here with additional content. Second sentence.",
                file_id="file1",
                score=0.9,
                metadata={},
            ),
            RAGSource(
                text="Similar to first sentence here with additional content.",
                file_id="file2",
                score=0.8,
                metadata={},
            ),
            RAGSource(
                text="Completely different content in this sentence here.",
                file_id="file3",
                score=0.7,
                metadata={},
            ),
        ]
        # Embeddings for: source0-sent0, source0-sent1, source1-sent0, source2-sent0
        # source1-sent0 is duplicate of source0-sent0
        embeddings = [
            [1.0, 0.0, 0.0],  # source0 sentence 0
            [0.0, 1.0, 0.0],  # source0 sentence 1 (different)
            [1.0, 0.0, 0.0],  # source1 - same as source0 sentence 0 (duplicate)
            [0.0, 0.0, 1.0],  # source2 - unique
        ]
        mock_embedding_service.embed_batch.return_value = embeddings

        distiller = ContextDistiller(mock_embedding_service)
        result = await distiller._deduplicate(sources, threshold=0.92)

        # Should have fewer sources (duplicates removed)
        # source0: 2 sentences kept, source1: 0 (all dup), source2: 1 kept
        # source1 gets dropped as < 50 chars
        assert isinstance(result, DistillResult)
        assert len(result.sources) == 2

    @pytest.mark.asyncio
    async def test_deduplicate_first_source_kept(self, mock_embedding_service):
        """src_idx==0 sentences always kept."""
        sources = [
            RAGSource(
                text="First source sentence one. First source sentence two.",
                file_id="file1",
                score=0.9,
                metadata={},
            ),
            RAGSource(
                text="Completely different content here.",
                file_id="file2",
                score=0.8,
                metadata={},
            ),
        ]
        # All different embeddings
        embeddings = [
            [1.0, 0.0, 0.0],  # Sentence 1 from source 0
            [0.5, 0.5, 0.0],  # Sentence 2 from source 0
            [0.0, 1.0, 0.0],  # Sentence from source 1
        ]
        mock_embedding_service.embed_batch.return_value = embeddings

        distiller = ContextDistiller(mock_embedding_service)
        result = await distiller._deduplicate(sources, threshold=0.92)

        # First source should be kept entirely (src_idx=0 always kept)
        assert isinstance(result, DistillResult)
        assert len(result.sources) >= 1
        assert "First source" in result.sources[0].text

    @pytest.mark.asyncio
    async def test_deduplicate_small_chunks_dropped(self, mock_embedding_service):
        """Chunks < 50 chars after dedup dropped."""
        sources = [
            RAGSource(
                text="This is a longer source with substantial content here.",
                file_id="file1",
                score=0.9,
                metadata={},
            ),
            RAGSource(
                text="This is a longer source with substantial content.",
                file_id="file2",
                score=0.8,
                metadata={},
            ),  # Duplicate of first
        ]
        # Both sentences are near-duplicates (same embedding)
        embeddings = [
            [1.0, 0.0, 0.0],  # source0 sentence
            [1.0, 0.0, 0.0],  # source1 - duplicate
        ]
        mock_embedding_service.embed_batch.return_value = embeddings

        distiller = ContextDistiller(mock_embedding_service)
        result = await distiller._deduplicate(sources, threshold=0.92)

        # source0 keeps sentence, source1 is dropped as < 50 chars
        assert isinstance(result, DistillResult)
        assert len(result.sources) == 1

    @pytest.mark.asyncio
    async def test_deduplicate_empty_sources(self, mock_embedding_service):
        """Returns DistillResult with empty sources when input is empty."""
        distiller = ContextDistiller(mock_embedding_service)
        result = await distiller._deduplicate([], threshold=0.92)
        assert isinstance(result, DistillResult)
        assert result.sources == []
        assert result.sentence_provenance == []

    @pytest.mark.asyncio
    async def test_deduplicate_embedding_error_returns_original(
        self, mock_embedding_service
    ):
        """Exception returns unmodified sources."""
        sources = [
            RAGSource(text="Some content.", file_id="file1", score=0.9, metadata={}),
        ]
        mock_embedding_service.embed_batch.side_effect = Exception("Embedding failed")

        distiller = ContextDistiller(mock_embedding_service)
        # The exception should propagate (the wrapper in distill catches it)
        with pytest.raises(Exception, match="Embedding failed"):
            await distiller._deduplicate(sources, threshold=0.92)


class TestSynthesize:
    """Tests for _synthesize method."""

    @pytest.fixture
    def mock_llm_client(self):
        """Create a mock LLM client."""
        mock = MagicMock()
        mock.chat_completion = AsyncMock()
        return mock

    @pytest.fixture
    def mock_embedding_service(self):
        """Create a mock embedding service."""
        mock = MagicMock()
        mock.embed_batch = AsyncMock(return_value=[[1.0, 0.0, 0.0]])
        return mock

    @pytest.mark.asyncio
    async def test_synthesize_appends_synthetic_source_keeping_real_chunks(
        self, mock_embedding_service, mock_llm_client
    ):
        """LLM synthesis APPENDS a synthetic source while KEEPING the real chunks.

        Superseded behavior: synthesis used to REPLACE the top-3 real chunks with
        a single lossy summary (``[synthetic] + rest``). That destroyed raw
        evidence on the least-confident (NO_MATCH) verdict. The corrected
        behavior keeps all real chunks and appends the synthesized summary last.
        """
        sources = [
            RAGSource(
                text="Content about topic A.",
                file_id="file1",
                score=0.9,
                metadata={"source_file": "a.pdf"},
            ),
            RAGSource(
                text="More details about topic A.",
                file_id="file2",
                score=0.8,
                metadata={"source_file": "b.pdf"},
            ),
            RAGSource(
                text="Additional info on the subject.",
                file_id="file3",
                score=0.7,
                metadata={"source_file": "c.pdf"},
            ),
        ]
        mock_llm_client.chat_completion.return_value = (
            "A synthesized answer about the topic drawn from multiple sources here."
        )

        distiller = ContextDistiller(mock_embedding_service, mock_llm_client)
        result = await distiller._synthesize("What is this about?", sources)

        # Real chunks are kept; synthetic is appended last (not a replacement).
        assert len(result) == len(sources) + 1
        assert result[: len(sources)] == sources
        synthetic = result[-1]
        assert synthetic.metadata.get("synthesized") is True
        # Provenance preserved: contributing filenames + ids carried; honest
        # display label instead of "Unknown document".
        assert synthetic.metadata.get("source_file") == "Synthesized from 3 sources"
        assert synthetic.metadata.get("synthesized_from_files") == [
            "file1",
            "file2",
            "file3",
        ]
        assert synthetic.metadata.get("synthesized_from_names") == [
            "a.pdf",
            "b.pdf",
            "c.pdf",
        ]
        # Synthetic source must NOT borrow a real document's identity: no
        # file_id (no "open document" action) and an explicit synthetic
        # chunk id/type so the UI cannot fetch real chunk context for it.
        assert synthetic.file_id == ""
        assert synthetic.metadata.get("source_type") == "synthesized"
        assert synthetic.metadata.get("_chunk_id") == "synthesized"

    @pytest.mark.asyncio
    async def test_synthesize_no_relevant_content_exact_sentinel(
        self, mock_embedding_service, mock_llm_client
    ):
        """Exact NO_RELEVANT_CONTENT sentinel → keep real chunks, inject nothing."""
        sources = [
            RAGSource(
                text="Some content here.", file_id="file1", score=0.9, metadata={}
            ),
        ]
        mock_llm_client.chat_completion.return_value = "NO_RELEVANT_CONTENT"

        distiller = ContextDistiller(mock_embedding_service, mock_llm_client)
        result = await distiller._synthesize("Unrelated query?", sources)

        # Should return original sources unchanged (no synthetic injected).
        assert result == sources

    @pytest.mark.asyncio
    async def test_synthesize_paraphrased_refusal_not_injected(
        self, mock_embedding_service, mock_llm_client
    ):
        """A PARAPHRASED refusal must not become an injected source.

        Regression test for the fabrication bug: a NO_MATCH verdict caused the
        model to reply with prose like "The provided source passages do not
        contain any information regarding X." The old exact-string check let that
        through and injected it as an "Unknown document / Highly Relevant"
        source. It must now be detected as a refusal and dropped.
        """
        sources = [
            RAGSource(
                text="Some content here.", file_id="file1", score=0.9, metadata={}
            ),
        ]
        mock_llm_client.chat_completion.return_value = (
            "The provided source passages do not contain any information "
            "regarding silent installation parameters."
        )

        distiller = ContextDistiller(mock_embedding_service, mock_llm_client)
        result = await distiller._synthesize("silent parameters?", sources)

        # No synthetic source injected; real chunks preserved unchanged.
        assert result == sources
        assert all(not s.metadata.get("synthesized") for s in result)

    @pytest.mark.asyncio
    async def test_synthesize_legitimate_absence_summary_is_kept(
        self, mock_embedding_service, mock_llm_client
    ):
        """A real summary that merely DESCRIBES an absence must NOT be dropped.

        Guards against over-aggressive refusal detection: phrases like "does not
        contain a bundled JRE" or "no mention in version 1" are valid answers,
        not refusals, because the absence is about the subject matter — not about
        the source passages themselves. These must be injected as a synthesized
        source.
        """
        sources = [
            RAGSource(
                text="Installer details about runtime dependencies.",
                file_id="file1",
                score=0.2,
                metadata={"source_file": "install.pdf"},
            ),
        ]
        mock_llm_client.chat_completion.return_value = (
            "The installer does not contain a bundled JRE and requires a system "
            "Java runtime. There is no mention of ARM support in version 1, but "
            "version 2 adds it."
        )

        distiller = ContextDistiller(mock_embedding_service, mock_llm_client)
        result = await distiller._synthesize("runtime requirements?", sources)

        # Legitimate summary IS injected (appended) with provenance.
        assert len(result) == len(sources) + 1
        assert result[-1].metadata.get("synthesized") is True
        assert "does not contain a bundled JRE" in result[-1].text

    @pytest.mark.asyncio
    async def test_is_no_content_response_patterns(self):
        """Unit-level matrix for the refusal detector."""
        from app.services.context_distiller import _is_no_content_response

        # Refusals (source-anchored or sentinel) → True
        assert _is_no_content_response("NO_RELEVANT_CONTENT") is True
        assert _is_no_content_response(
            "The provided source passages do not contain any information "
            "regarding silent installation parameters."
        ) is True
        assert _is_no_content_response(
            "There is no relevant information in the documents about this topic."
        ) is True
        assert _is_no_content_response("too short") is True

        # Legitimate content describing an absence → False (kept)
        assert _is_no_content_response(
            "The installer does not contain a bundled JRE and requires a system "
            "Java runtime to be installed beforehand."
        ) is False
        assert _is_no_content_response(
            "There is no mention of ARM support in version 1, but version 2 "
            "introduces native ARM builds for all platforms."
        ) is False

    @pytest.mark.asyncio
    async def test_synthesize_llm_error_returns_sources(
        self, mock_embedding_service, mock_llm_client
    ):
        """Fail-open on LLM error."""
        sources = [
            RAGSource(
                text="Original content.", file_id="file1", score=0.9, metadata={}
            ),
        ]
        mock_llm_client.chat_completion.side_effect = Exception("LLM failed")

        distiller = ContextDistiller(mock_embedding_service, mock_llm_client)
        result = await distiller._synthesize("Test query?", sources)

        # Should return original sources on error (fail-open)
        assert result == sources


class TestDistill:
    """Tests for distill method (main entry point)."""

    @pytest.fixture
    def mock_embedding_service(self):
        """Create a mock embedding service."""
        mock = MagicMock()
        mock.embed_batch = AsyncMock()
        return mock

    @pytest.fixture
    def mock_llm_client(self):
        """Create a mock LLM client."""
        mock = MagicMock()
        mock.chat_completion = AsyncMock()
        return mock

    @pytest.mark.asyncio
    async def test_distill_with_synthesis_disabled(self, mock_embedding_service):
        """Only dedup runs when synthesis is disabled."""
        sources = [
            RAGSource(
                text="Original content here with additional text for length.",
                file_id="file1",
                score=0.9,
                metadata={},
            ),
            RAGSource(
                text="More original content here with additional text for length.",
                file_id="file2",
                score=0.8,
                metadata={},
            ),
        ]
        mock_embedding_service.embed_batch.return_value = [
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
        ]

        with patch("app.config.settings") as mock_settings:
            mock_settings.context_distillation_enabled = True
            mock_settings.context_distillation_synthesis_enabled = False
            mock_settings.context_distillation_dedup_threshold = 0.92

            distiller = ContextDistiller(mock_embedding_service)
            result = await distiller.distill("test query", sources, "NO_MATCH")

            # Returns DistillResult
            assert isinstance(result, DistillResult)
            # Should return deduped sources (both sentences are different)
            assert len(result.sources) >= 1

    @pytest.mark.asyncio
    async def test_distill_with_synthesis_enabled_no_match(
        self, mock_embedding_service, mock_llm_client
    ):
        """Both dedup + synthesis run when enabled and eval_result is NO_MATCH."""
        sources = [
            RAGSource(
                text="Content about the topic with additional information here.",
                file_id="file1",
                score=0.9,
                metadata={},
            ),
            RAGSource(
                text="More details here about the same topic.",
                file_id="file2",
                score=0.8,
                metadata={},
            ),
            RAGSource(
                text="Additional information on this particular subject.",
                file_id="file3",
                score=0.7,
                metadata={},
            ),
        ]
        mock_embedding_service.embed_batch.return_value = [
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ]
        mock_llm_client.chat_completion.return_value = (
            "Synthesized response based on the sources."
        )

        with patch("app.config.settings") as mock_settings:
            mock_settings.context_distillation_enabled = True
            mock_settings.context_distillation_synthesis_enabled = True
            mock_settings.context_distillation_dedup_threshold = 0.92

            distiller = ContextDistiller(mock_embedding_service, mock_llm_client)
            result = await distiller.distill("test query", sources, "NO_MATCH")

            # Returns DistillResult
            assert isinstance(result, DistillResult)
            # Should have called synthesis
            assert mock_llm_client.chat_completion.called
            # Synthesized source is APPENDED last (real chunks kept), not first.
            assert result.sources[-1].metadata.get("synthesized") is True
            assert len(result.sources) >= 2  # real chunk(s) preserved + synthetic appended

    @pytest.mark.asyncio
    async def test_distill_confident_no_synthesis(
        self, mock_embedding_service, mock_llm_client
    ):
        """Synthesis does not run for CONFIDENT eval_result."""
        sources = [
            RAGSource(
                text="Content about the topic.", file_id="file1", score=0.9, metadata={}
            ),
        ]
        mock_embedding_service.embed_batch.return_value = [[1.0, 0.0, 0.0]]

        with patch("app.config.settings") as mock_settings:
            mock_settings.context_distillation_enabled = True
            mock_settings.context_distillation_synthesis_enabled = True
            mock_settings.context_distillation_dedup_threshold = 0.92

            distiller = ContextDistiller(mock_embedding_service, mock_llm_client)
            result = await distiller.distill("test query", sources, "CONFIDENT")

            # Returns DistillResult
            assert isinstance(result, DistillResult)
            # Synthesis should NOT be called for CONFIDENT
            mock_llm_client.chat_completion.assert_not_called()

    @pytest.mark.asyncio
    async def test_distill_ambiguous_does_not_trigger_synthesis(
        self, mock_embedding_service, mock_llm_client
    ):
        """Synthesis should NOT run for AMBIGUOUS — real chunks may be present."""
        sources = [
            RAGSource(
                text="Content about the topic with additional information here.",
                file_id="file1",
                score=0.9,
                metadata={},
            ),
            RAGSource(
                text="More details here about the same topic.",
                file_id="file2",
                score=0.8,
                metadata={},
            ),
        ]
        mock_embedding_service.embed_batch.return_value = [
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
        ]
        mock_llm_client.chat_completion.return_value = "Synthesized for ambiguous case."

        with patch("app.config.settings") as mock_settings:
            mock_settings.context_distillation_enabled = True
            mock_settings.context_distillation_synthesis_enabled = True
            mock_settings.context_distillation_dedup_threshold = 0.92

            distiller = ContextDistiller(mock_embedding_service, mock_llm_client)
            result = await distiller.distill("test query", sources, "AMBIGUOUS")

            # Returns DistillResult
            assert isinstance(result, DistillResult)
            # Synthesis should NOT be called for AMBIGUOUS
            assert not mock_llm_client.chat_completion.called

    @pytest.mark.asyncio
    async def test_distill_disabled_returns_original(self, mock_embedding_service):
        """ContextDistiller always runs - feature flag is checked at RAG engine level."""
        sources = [
            RAGSource(
                text="Original content with additional text for length and testing.",
                file_id="file1",
                score=0.9,
                metadata={},
            ),
        ]
        # When called directly, ContextDistiller always runs (flag is checked by RAG engine)
        mock_embedding_service.embed_batch.return_value = [[1.0, 0.0, 0.0]]

        with patch("app.config.settings") as mock_settings:
            # Flag doesn't matter - distiller always runs when called directly
            mock_settings.context_distillation_enabled = False
            mock_settings.context_distillation_synthesis_enabled = False
            mock_settings.context_distillation_dedup_threshold = 0.92

            distiller = ContextDistiller(mock_embedding_service)
            result = await distiller.distill("test query", sources, "NO_MATCH")

            # Returns DistillResult
            assert isinstance(result, DistillResult)
            # Distiller runs dedup and returns result (flag checked by caller)
            assert len(result.sources) >= 1
            mock_embedding_service.embed_batch.assert_called()

    @pytest.mark.asyncio
    async def test_distill_no_llm_client_no_synthesis(self, mock_embedding_service):
        """No synthesis when llm_client is not provided."""
        sources = [
            RAGSource(text="Content.", file_id="file1", score=0.9, metadata={}),
            RAGSource(text="More content.", file_id="file2", score=0.8, metadata={}),
        ]
        mock_embedding_service.embed_batch.return_value = [
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
        ]

        with patch("app.config.settings") as mock_settings:
            mock_settings.context_distillation_enabled = True
            mock_settings.context_distillation_synthesis_enabled = True
            mock_settings.context_distillation_dedup_threshold = 0.92

            # No LLM client provided
            distiller = ContextDistiller(mock_embedding_service)
            result = await distiller.distill("test query", sources, "NO_MATCH")

            # Returns DistillResult
            assert isinstance(result, DistillResult)
            # embed_batch should have been called for dedup
            assert mock_embedding_service.embed_batch.called


class TestSynthesizedSourceMetadataContract:
    """A synthesized source must not render or behave like a real document.

    Covers the PR-review provenance/identity contract: the synthetic RAGSource
    produced by ContextDistiller, when passed through
    DocumentRetrievalService.to_source_metadata, must (a) omit the relevance
    score so no relevance label renders, (b) carry the synthetic chunk id (not a
    derived "<file_id>_<index>" id), and (c) carry no real file_id so document
    actions are unavailable.
    """

    @pytest.fixture
    def mock_embedding_service(self):
        mock = MagicMock()
        mock.embed_batch = AsyncMock(return_value=[[1.0, 0.0, 0.0]])
        return mock

    @pytest.fixture
    def mock_llm_client(self):
        mock = MagicMock()
        mock.chat_completion = AsyncMock(
            return_value=(
                "A synthesized answer about the topic drawn from the sources here."
            )
        )
        return mock

    @pytest.mark.asyncio
    async def test_synthesized_source_metadata_omits_score_and_actions(
        self, mock_embedding_service, mock_llm_client
    ):
        from app.services.document_retrieval import DocumentRetrievalService

        sources = [
            RAGSource(
                text="Content about topic A.",
                file_id="file1",
                score=0.05,  # would render "Highly Relevant" if borrowed
                metadata={"source_file": "a.pdf"},
            ),
        ]
        distiller = ContextDistiller(mock_embedding_service, mock_llm_client)
        result = await distiller._synthesize("What is this about?", sources)
        synthetic = result[-1]
        assert synthetic.metadata.get("synthesized") is True

        svc = DocumentRetrievalService.__new__(DocumentRetrievalService)
        meta = svc.to_source_metadata(synthetic, source_index=2)

        # No score key at all → every relevance-rendering surface skips it.
        assert "score" not in meta
        # Synthetic id, not a derived "<file_id>_<index>" id.
        assert meta["id"] == "synthesized"
        # No real document backing → no open-document action.
        assert meta["file_id"] == ""
        # Honest, non-borrowed filename label.
        assert meta["filename"] == "Synthesized from 1 source"

    @pytest.mark.asyncio
    async def test_real_source_metadata_keeps_score(self):
        from app.services.document_retrieval import DocumentRetrievalService

        real = RAGSource(
            text="Real chunk text.",
            file_id="file1",
            score=0.1,
            metadata={"source_file": "a.pdf", "chunk_index": "3"},
        )
        svc = DocumentRetrievalService.__new__(DocumentRetrievalService)
        meta = svc.to_source_metadata(real, source_index=1)
        # Real sources still carry their score for relevance rendering.
        assert meta["score"] == 0.1
        assert meta["file_id"] == "file1"
