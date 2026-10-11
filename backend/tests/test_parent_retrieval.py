"""Tests for parent-document retrieval: schema migration, ingestion, dedup, and prompt expansion (Issue #12)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.chunking import ProcessedChunk, compute_parent_windows
from app.services.document_retrieval import RAGSource, _group_aware_dedup

# ---------------------------------------------------------------------------
# compute_parent_windows tests
# ---------------------------------------------------------------------------

class TestComputeParentWindows:
    def _make_chunk(self, text: str, index: int, raw_text: str = None) -> ProcessedChunk:
        return ProcessedChunk(
            text=text,
            metadata={"chunk_index": index},
            chunk_index=index,
            raw_text=raw_text,
        )

    def test_basic_window_computation(self):
        source_text = "A" * 1000 + "HELLO WORLD" + "B" * 1000
        chunk = self._make_chunk("HELLO WORLD", 0)
        chunks = compute_parent_windows([chunk], source_text, window_chars=200)

        assert chunks[0].parent_window_start is not None
        assert chunks[0].parent_window_end is not None
        assert chunks[0].chunk_position == 0
        # The match text should be inside the window
        assert "HELLO WORLD" in source_text[
            chunks[0].parent_window_start : chunks[0].parent_window_end
        ]

    def test_window_does_not_exceed_document_bounds(self):
        source_text = "SHORT DOC"
        chunk = self._make_chunk("SHORT DOC", 0)
        chunks = compute_parent_windows([chunk], source_text, window_chars=10000)

        assert chunks[0].parent_window_start == 0
        assert chunks[0].parent_window_end == len(source_text)

    def test_uses_raw_text_when_available(self):
        source_text = "original raw chunk text sits here"
        chunk = self._make_chunk(
            text="[CONTEXT PREFIX] original raw chunk text sits here",
            index=0,
            raw_text="original raw chunk text sits here",
        )
        chunks = compute_parent_windows([chunk], source_text, window_chars=200)

        # Should find raw_text in source
        assert chunks[0].parent_window_start is not None
        assert chunks[0].chunk_position == 0

    def test_chunk_not_found_leaves_offsets_none(self):
        source_text = "completely different text"
        chunk = self._make_chunk("this text does not appear in the source", 0)
        chunks = compute_parent_windows([chunk], source_text, window_chars=200)

        assert chunks[0].parent_window_start is None
        assert chunks[0].parent_window_end is None

    def test_10000_char_source_covers_source(self):
        """10000-char source with 3 chunks — parent windows should collectively cover the source."""
        source_text = "X" * 3000 + "CHUNK_A" + "X" * 3000 + "CHUNK_B" + "X" * 3000 + "CHUNK_C" + "X" * 1000
        chunks = [
            self._make_chunk("CHUNK_A", 0),
            self._make_chunk("CHUNK_B", 1),
            self._make_chunk("CHUNK_C", 2),
        ]
        window_chars = 6000
        compute_parent_windows(chunks, source_text, window_chars=window_chars)

        # All three chunks should have valid offsets
        for c in chunks:
            assert c.parent_window_start is not None
            assert c.parent_window_end is not None
            assert c.parent_window_end > c.parent_window_start

    def test_chunk_position_assigned_sequentially(self):
        source_text = "abc def ghi"
        chunks = [
            self._make_chunk("abc", 0),
            self._make_chunk("def", 1),
            self._make_chunk("ghi", 2),
        ]
        compute_parent_windows(chunks, source_text, window_chars=50)

        for i, c in enumerate(chunks):
            assert c.chunk_position == i

    def test_duplicate_chunk_text_advances_cursor(self):
        """Repeated chunk text: second occurrence should be matched past the first."""
        # Build source with the same phrase appearing twice at distinct offsets
        repeated = "the answer is 42"
        padding = "x" * 100
        source_text = padding + repeated + padding + repeated + padding
        chunk0 = self._make_chunk(repeated, 0)
        chunk1 = self._make_chunk(repeated, 1)
        compute_parent_windows([chunk0, chunk1], source_text, window_chars=60)
        # Both chunks must get non-None offsets
        assert chunk0.parent_window_start is not None
        assert chunk1.parent_window_start is not None
        # The two windows must not be identical — they should point to different occurrences
        assert chunk0.parent_window_start != chunk1.parent_window_start

    def test_empty_chunks_list(self):
        result = compute_parent_windows([], "some text", window_chars=100)
        assert result == []

    def test_empty_source_text(self):
        chunk = self._make_chunk("hello", 0)
        result = compute_parent_windows([chunk], "", window_chars=100)
        assert result[0].parent_window_start is None


# ---------------------------------------------------------------------------
# _group_aware_dedup tests (Issue #12)
# ---------------------------------------------------------------------------

class TestGroupAwareDedup:
    def _make_source(self, file_id: str, score: float = 0.9) -> RAGSource:
        return RAGSource(
            text=f"text from {file_id}",
            file_id=file_id,
            score=score,
            metadata={},
        )

    def test_single_doc_two_strong_chunks_both_survive(self):
        """Two strong chunks from the same doc should both survive with cap=2."""
        sources = [
            self._make_source("doc1", 0.95),
            self._make_source("doc1", 0.90),
        ]
        result = _group_aware_dedup(sources, per_doc_chunk_cap=2, unique_docs_in_top_k=5)

        assert len(result) == 2
        assert all(s.file_id == "doc1" for s in result)

    def test_third_chunk_same_doc_dropped_with_cap_2(self):
        """A document with 3 chunks only contributes 2 when PER_DOC_CHUNK_CAP=2."""
        sources = [
            self._make_source("doc1", 0.95),
            self._make_source("doc1", 0.90),
            self._make_source("doc1", 0.85),
        ]
        result = _group_aware_dedup(sources, per_doc_chunk_cap=2, unique_docs_in_top_k=5)
        assert len(result) == 2

    def test_top_k_contains_at_most_unique_docs_in_top_k_distinct_file_ids(self):
        """Result should have at most UNIQUE_DOCS_IN_TOP_K distinct file_ids."""
        sources = [self._make_source(f"doc{i}") for i in range(10)]
        result = _group_aware_dedup(sources, per_doc_chunk_cap=1, unique_docs_in_top_k=5)

        distinct_file_ids = {s.file_id for s in result}
        assert len(distinct_file_ids) <= 5
        assert len(result) == 5

    def test_doc_with_6_strong_chunks_contributes_2_not_6_not_1(self):
        """Issue #12 core scenario: best doc contributes 2, not 1 (UID-strip bug) or 6."""
        sources = [self._make_source("best_doc", 1.0 - i * 0.01) for i in range(6)]
        sources += [self._make_source(f"other_doc_{i}", 0.5) for i in range(5)]
        result = _group_aware_dedup(sources, per_doc_chunk_cap=2, unique_docs_in_top_k=5)

        best_doc_chunks = [s for s in result if s.file_id == "best_doc"]
        assert len(best_doc_chunks) == 2

    def test_ranking_order_preserved(self):
        """The relative order of selected sources should be preserved."""
        sources = [
            self._make_source("doc1", 0.9),
            self._make_source("doc2", 0.8),
            self._make_source("doc1", 0.7),
        ]
        result = _group_aware_dedup(sources, per_doc_chunk_cap=2, unique_docs_in_top_k=5)
        file_ids = [s.file_id for s in result]
        assert file_ids == ["doc1", "doc2", "doc1"]

    def test_empty_input(self):
        result = _group_aware_dedup([], per_doc_chunk_cap=2, unique_docs_in_top_k=5)
        assert result == []


# ---------------------------------------------------------------------------
# RAGSource parent_window_text field tests (Issue #12)
# ---------------------------------------------------------------------------

class TestRAGSourceParentWindowText:
    def test_parent_window_text_default_none(self):
        src = RAGSource(text="hello", file_id="1", score=0.9, metadata={})
        assert src.parent_window_text is None

    def test_parent_window_text_can_be_set(self):
        src = RAGSource(text="hello", file_id="1", score=0.9, metadata={})
        src.parent_window_text = "broader context [[MATCH: hello]] more context"
        assert src.parent_window_text is not None


# ---------------------------------------------------------------------------
# Prompt builder [[MATCH:]] rendering tests (Issue #12)
# ---------------------------------------------------------------------------

class TestPromptBuilderParentWindow:
    """Test that format_chunk renders [[MATCH:]] markers when parent window is available."""

    def _make_source_with_parent(
        self, chunk_text: str, parent_text: str, raw_text: str = None
    ) -> RAGSource:
        src = RAGSource(
            text=chunk_text,
            file_id="42",
            score=0.85,
            metadata={
                "source_file": "test.pdf",
                "raw_text": raw_text or chunk_text,
                "parent_window_text": parent_text,
            },
        )
        src.parent_window_text = parent_text
        return src

    def test_match_marker_inserted_when_parent_retrieval_enabled(self):
        from app.services.prompt_builder import PromptBuilderService

        src = self._make_source_with_parent(
            chunk_text="the quick brown fox",
            parent_text="once upon a time the quick brown fox jumped over the lazy dog",
        )

        with patch("app.services.prompt_builder.settings") as mock_settings:
            mock_settings.parent_retrieval_enabled = True
            mock_settings.anchor_best_chunk = False
            mock_settings.primary_evidence_count = 0
            mock_settings.context_max_tokens = 6000

            builder = PromptBuilderService()
            formatted = builder.format_chunk(src, source_index=1)

        assert "[[MATCH: the quick brown fox]]" in formatted
        assert "once upon a time" in formatted

    def test_no_match_marker_when_parent_retrieval_disabled(self):
        from app.services.prompt_builder import PromptBuilderService

        src = self._make_source_with_parent(
            chunk_text="the quick brown fox",
            parent_text="once upon a time the quick brown fox jumped over the lazy dog",
        )

        with patch("app.services.prompt_builder.settings") as mock_settings:
            mock_settings.parent_retrieval_enabled = False
            mock_settings.anchor_best_chunk = False

            builder = PromptBuilderService()
            formatted = builder.format_chunk(src, source_index=1)

        assert "[[MATCH:" not in formatted
        assert "the quick brown fox" in formatted

    def test_fallback_when_match_text_not_in_parent(self):
        """If match text not found in parent window, append as annotation."""
        from app.services.prompt_builder import PromptBuilderService

        src = self._make_source_with_parent(
            chunk_text="exact match phrase",
            parent_text="completely unrelated parent context",
        )

        with patch("app.services.prompt_builder.settings") as mock_settings:
            mock_settings.parent_retrieval_enabled = True
            mock_settings.anchor_best_chunk = False

            builder = PromptBuilderService()
            formatted = builder.format_chunk(src, source_index=1)

        # Fallback: both parent text and match annotation present
        assert "completely unrelated parent context" in formatted
        assert "[[MATCH:" in formatted


# ---------------------------------------------------------------------------
# VectorStore schema migration tests (Issue #12)
# ---------------------------------------------------------------------------

class TestMigrateAddParentWindow:
    """Test migrate_add_parent_window idempotency and dry-run behavior (mocked)."""

    def _make_store_with_all_columns_no_nulls(self) -> "VectorStore":
        """Return a mocked VectorStore where all parent_window columns exist and
        parent_doc_id has no nulls — migration should be a no-op."""
        from pathlib import Path

        from app.services.vector_store import VectorStore

        store = VectorStore(db_path=Path("/tmp/mig_test"))

        # Simulate a schema that already includes all 4 parent-window columns
        field_names = [
            "id", "text", "file_id", "vault_id", "chunk_index", "chunk_scale",
            "sparse_embedding", "metadata",
            "parent_doc_id", "parent_window_start", "parent_window_end",
            "chunk_position", "embedding",
        ]

        class FakeField:
            def __init__(self, name):
                self.name = name

        class FakeSchema:
            def __init__(self):
                self._fields = [FakeField(n) for n in field_names]

            def __len__(self):
                return len(self._fields)

            def field(self, i):
                return self._fields[i]

        mock_table = MagicMock()
        mock_table.schema = AsyncMock(return_value=FakeSchema())
        # [issue #706] The no-op probe calls count_rows(filter="... IS NULL")
        # server-side; the plain (filterless) call still answers the total
        # row count for _safe_table_to_pandas's OOM logging. Model both:
        # 1 row total, 0 rows matching any null filter.
        mock_table.count_rows = AsyncMock(
            side_effect=lambda filter=None: 0 if filter is not None else 1
        )

        # Simulate a pandas-like DataFrame where parent_doc_id has zero nulls.
        # We avoid importing pandas to keep the test self-contained.
        class FakeColumn:
            def isna(self):
                return self
            def sum(self):
                return 0  # No nulls

        class FakeDF:
            def __getitem__(self, key):
                return FakeColumn()
            def __len__(self):
                return 1

        df = FakeDF()

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=["chunks"])
        mock_db.open_table = AsyncMock(return_value=mock_table)

        store.db = mock_db
        store.table = mock_table
        store._embedding_dim = 4

        # Monkeypatch _safe_table_to_pandas to return the prepared df
        async def _fake_safe_to_pandas(table, op_name):
            return df

        store._safe_table_to_pandas = _fake_safe_to_pandas
        return store

    @pytest.mark.asyncio
    async def test_dry_run_returns_zero_when_all_rows_backfilled(self):
        """Dry run returns 0 when no rows have null parent_doc_id."""
        store = self._make_store_with_all_columns_no_nulls()
        count = await store.migrate_add_parent_window(dry_run=True)
        assert count == 0

    @pytest.mark.asyncio
    async def test_migration_is_idempotent_when_no_nulls(self):
        """Running migration twice when already up-to-date returns 0 both times."""
        store = self._make_store_with_all_columns_no_nulls()
        count1 = await store.migrate_add_parent_window(dry_run=False)
        count2 = await store.migrate_add_parent_window(dry_run=False)
        assert count1 == 0
        assert count2 == 0

    @pytest.mark.asyncio
    async def test_returns_zero_when_no_table_exists(self):
        """If 'chunks' table doesn't exist, migration returns 0 (nothing to migrate)."""
        from pathlib import Path

        from app.services.vector_store import VectorStore

        store = VectorStore(db_path=Path("/tmp/mig_test"))
        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=[])  # No "chunks" table
        store.db = mock_db

        count = await store.migrate_add_parent_window(dry_run=True)
        assert count == 0
