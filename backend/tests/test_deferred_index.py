"""
Tests for deferred vector index creation and FTS guard in VectorStore.

This module tests:
1. VECTOR_INDEX_MIN_ROWS constant equals 256
2. init_table defers vector index creation (no immediate create_index for embedding column)
3. FTS index created when not exists
4. FTS index skipped when already exists (list_indices returns a
   shape-complete FTS index)
5. _maybe_create_vector_index returns early when table is None
6. _maybe_create_vector_index skips when embedding_idx already exists
7. _maybe_create_vector_index skips when row count < 256
8. _maybe_create_vector_index creates index when row count >= 256
9. _maybe_create_vector_index handles list_indices failure gracefully
10. _maybe_create_vector_index handles count_rows failure gracefully
"""

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.vector_store import (
    VECTOR_INDEX_MIN_ROWS,
    VectorStore,
)


class TestVectorIndexMinRowsConstant(unittest.TestCase):
    """Test cases for VECTOR_INDEX_MIN_ROWS constant."""

    def test_vector_index_min_rows_equals_256(self):
        """Test that VECTOR_INDEX_MIN_ROWS constant equals 256."""
        self.assertEqual(VECTOR_INDEX_MIN_ROWS, 256)


class TestInitTableDefersVectorIndex(unittest.IsolatedAsyncioTestCase):
    """Test cases for init_table deferring vector index creation."""

    async def test_init_table_defers_vector_index_creation(self):
        """
        Test that init_table does NOT call create_index for the embedding column.

        Vector index creation should be deferred until >= 256 rows.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        # Mock the database connection
        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=[])

        # Mock table creation - track create_index calls
        mock_table = MagicMock()
        create_index_calls = []

        async def mock_create_index(column, config=None, replace=False):
            create_index_calls.append(
                {"column": column, "config": config, "replace": replace}
            )

        mock_table.create_index = mock_create_index
        mock_table.list_indices = AsyncMock(return_value=[])  # No indices yet

        mock_db.create_table = AsyncMock(return_value=mock_table)

        store.db = mock_db

        # Mock pyarrow schema creation to avoid real LanceDB
        with patch("app.services.vector_store.pa") as mock_pa:
            mock_pa.schema.return_value = MagicMock()

            # Mock settings
            with patch("app.services.vector_store.settings") as mock_settings:
                mock_settings.vector_metric = "cosine"
                mock_settings.write_lock_timeout_seconds = 30

                # Mock FTS import
                with patch("app.services.vector_store.FTS") as mock_fts:
                    mock_fts.return_value = MagicMock()

                    await store.init_table(embedding_dim=384)

        # Verify table was created
        self.assertIsNotNone(store.table)

        # Verify no create_index call was for embedding column (vector index deferred)
        for call_record in create_index_calls:
            self.assertNotEqual(
                call_record["column"],
                "embedding",
                "init_table should NOT create vector index on embedding column",
            )


class TestFTSIndexGuard(unittest.IsolatedAsyncioTestCase):
    """Test cases for FTS index creation guard."""

    async def test_fts_index_created_when_not_exists(self):
        """
        Test that FTS index is created when it doesn't exist in list_indices.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        # Mock the database connection
        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=[])

        # Mock table with no existing FTS index
        mock_table = MagicMock()
        fts_created = []

        async def mock_create_index(column, config=None, replace=False):
            fts_created.append(column)

        mock_table.create_index = mock_create_index
        mock_table.list_indices = AsyncMock(return_value=[])  # No indices

        mock_db.create_table = AsyncMock(return_value=mock_table)

        store.db = mock_db

        # Mock pyarrow schema
        with patch("app.services.vector_store.pa") as mock_pa:
            mock_pa.schema.return_value = MagicMock()

            with patch("app.services.vector_store.settings") as mock_settings:
                mock_settings.vector_metric = "cosine"
                mock_settings.write_lock_timeout_seconds = 30

                with patch("app.services.vector_store.FTS") as mock_fts:
                    mock_fts.return_value = MagicMock()

                    await store.init_table(embedding_dim=384)

        # Verify FTS index was created on text column
        self.assertIn(
            "text", fts_created, "FTS index should be created on 'text' column"
        )

    async def test_fts_index_skipped_when_already_exists(self):
        """
        Test that FTS index creation is skipped when a real-shape FTS index
        already exists (columns=['text'], index_type='FTS' — issue #557).
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        # Mock the database connection
        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=[])

        # Mock existing FTS index with the REAL engine-reported shape
        # (auto-named 'text_idx'; detection is column+type based).
        existing_fts_index = MagicMock()
        existing_fts_index.name = "text_idx"
        existing_fts_index.columns = ["text"]
        existing_fts_index.index_type = "FTS"

        mock_table = MagicMock()
        mock_table.list_indices = AsyncMock(return_value=[existing_fts_index])
        mock_table.create_index = AsyncMock()  # Track if called

        mock_db.create_table = AsyncMock(return_value=mock_table)

        store.db = mock_db

        with patch("app.services.vector_store.pa") as mock_pa:
            mock_pa.schema.return_value = MagicMock()

            with patch("app.services.vector_store.settings") as mock_settings:
                mock_settings.vector_metric = "cosine"
                mock_settings.write_lock_timeout_seconds = 30

                await store.init_table(embedding_dim=384)

        # Verify create_index was NOT called (FTS already exists)
        mock_table.create_index.assert_not_called()


class TestMaybeCreateVectorIndex(unittest.IsolatedAsyncioTestCase):
    """Test cases for _maybe_create_vector_index method."""

    def setUp(self):
        """Set up test environment."""
        self.store = VectorStore(db_path=Path("/tmp/test_lancedb"))
        self.embedding_dim = 384

    async def test_returns_early_when_table_is_none(self):
        """
        Test that _maybe_create_vector_index returns early when self.table is None.

        This is the first guard clause in the method.
        """
        self.store.table = None

        # Should not raise and should return immediately
        await self.store._maybe_create_vector_index()

        # No exception means success (early return)

    async def test_skips_when_embedding_idx_already_exists(self):
        """
        Test that _maybe_create_vector_index skips when 'embedding_idx' is fresh.
        """
        # Mock table with an existing embedding index (real engine shape)
        existing_index = MagicMock()
        existing_index.name = "embedding_idx"
        existing_index.columns = ["embedding"]
        existing_index.index_type = "IvfPq"

        mock_table = MagicMock()
        mock_table.list_indices = AsyncMock(return_value=[existing_index])
        mock_table.count_rows = AsyncMock(return_value=300)
        mock_table.create_index = AsyncMock()

        self.store.table = mock_table
        self.store._last_index_build_row_count = 300

        await self.store._maybe_create_vector_index()

        # Verify row count was checked before treating the index as fresh.
        mock_table.count_rows.assert_awaited_once()

        # Verify create_index was NOT called
        mock_table.create_index.assert_not_called()

    async def test_skips_when_row_count_less_than_256(self):
        """
        Test that _maybe_create_vector_index skips when row count < 256.
        """
        # Mock table with no existing embedding_idx and < 256 rows
        mock_table = MagicMock()
        mock_table.list_indices = AsyncMock(return_value=[])  # No embedding_idx
        mock_table.count_rows = AsyncMock(return_value=100)  # < 256
        mock_table.create_index = AsyncMock()

        self.store.table = mock_table

        await self.store._maybe_create_vector_index()

        # Verify create_index was NOT called (row count < 256)
        mock_table.create_index.assert_not_called()

    async def test_creates_index_when_row_count_at_least_256(self):
        """
        Test that _maybe_create_vector_index creates index when row count >= 256.
        """
        # Mock table with no existing embedding_idx and >= 256 rows
        mock_table = MagicMock()
        mock_table.list_indices = AsyncMock(return_value=[])  # No embedding_idx
        mock_table.count_rows = AsyncMock(return_value=300)  # >= 256

        # Track the create_index call arguments
        create_index_kwargs = {}

        async def mock_create_index(**kwargs):
            create_index_kwargs.update(kwargs)

        mock_table.create_index = mock_create_index

        self.store.table = mock_table

        # Mock IvfPq class
        mock_ivf_pq = MagicMock()
        mock_ivf_pq.num_partitions = 256
        mock_ivf_pq.num_sub_vectors = 96

        with patch("app.services.vector_store.IvfPq") as MockIvfPq:
            MockIvfPq.return_value = mock_ivf_pq

            with patch("app.services.vector_store.settings") as mock_settings:
                mock_settings.vector_metric = "cosine"
                mock_settings.write_lock_timeout_seconds = 30
                mock_settings.embedding_dim = 768

                await self.store._maybe_create_vector_index()

        # Verify IvfPq was called with correct parameters
        MockIvfPq.assert_called_once()
        call_kwargs = MockIvfPq.call_args.kwargs
        self.assertEqual(call_kwargs["num_partitions"], 256)
        self.assertEqual(call_kwargs["num_sub_vectors"], 96)  # 768 // 8 = 96

        # Verify create_index was called with embedding column
        self.assertEqual(create_index_kwargs.get("column"), "embedding")
        self.assertEqual(create_index_kwargs.get("replace"), True)

    async def test_creates_index_exactly_at_256_rows(self):
        """
        Test that index is created exactly at the 256 threshold.
        """
        mock_table = MagicMock()
        mock_table.list_indices = AsyncMock(return_value=[])
        mock_table.count_rows = AsyncMock(return_value=256)  # Exactly 256

        create_index_called = False

        async def mock_create_index(**kwargs):
            nonlocal create_index_called
            create_index_called = True

        mock_table.create_index = mock_create_index

        self.store.table = mock_table

        with patch("app.services.vector_store.IvfPq") as MockIvfPq:
            MockIvfPq.return_value = MagicMock()

            with patch("app.services.vector_store.settings") as mock_settings:
                mock_settings.vector_metric = "cosine"
                mock_settings.write_lock_timeout_seconds = 30
                mock_settings.embedding_dim = 768

                await self.store._maybe_create_vector_index()

        # Should create index at exactly 256 rows
        self.assertTrue(
            create_index_called, "Index should be created at exactly 256 rows"
        )

    async def test_handles_list_indices_failure_gracefully(self):
        """
        Test that _maybe_create_vector_index handles list_indices failure gracefully.
        """
        mock_table = MagicMock()
        mock_table.list_indices = AsyncMock(
            side_effect=RuntimeError("LanceDB connection error")
        )
        mock_table.count_rows = AsyncMock(return_value=100)  # < 256, won't create
        mock_table.create_index = AsyncMock()

        self.store.table = mock_table

        # Should NOT raise - should handle gracefully
        await self.store._maybe_create_vector_index()

        # After list_indices failure, count_rows is called to check threshold
        mock_table.count_rows.assert_called()

    async def test_handles_count_rows_failure_gracefully(self):
        """
        Test that _maybe_create_vector_index handles count_rows failure gracefully.
        """
        mock_table = MagicMock()
        mock_table.list_indices = AsyncMock(return_value=[])  # No embedding_idx
        mock_table.count_rows = AsyncMock(side_effect=RuntimeError("LanceDB error"))
        mock_table.create_index = AsyncMock()

        self.store.table = mock_table

        # Should NOT raise - should return early
        await self.store._maybe_create_vector_index()

        # create_index should NOT be called (count_rows failed)
        mock_table.create_index.assert_not_called()

    async def test_uses_settings_vector_metric(self):
        """
        Test that _maybe_create_vector_index uses settings.vector_metric for index config.
        """
        mock_table = MagicMock()
        mock_table.list_indices = AsyncMock(return_value=[])
        mock_table.count_rows = AsyncMock(return_value=300)
        mock_table.create_index = AsyncMock()

        self.store.table = mock_table

        with patch("app.services.vector_store.IvfPq") as MockIvfPq:
            MockIvfPq.return_value = MagicMock()

            with patch("app.services.vector_store.settings") as mock_settings:
                mock_settings.vector_metric = "dot"  # Non-default metric
                mock_settings.embedding_dim = 768

                await self.store._maybe_create_vector_index()

        # Verify the config used the specified metric
        call_kwargs = MockIvfPq.call_args.kwargs
        self.assertEqual(call_kwargs["distance_type"], "dot")


class TestMaybeCreateVectorIndexLogging(unittest.IsolatedAsyncioTestCase):
    """Test cases for logging behavior in _maybe_create_vector_index."""

    def setUp(self):
        """Set up test environment."""
        self.store = VectorStore(db_path=Path("/tmp/test_lancedb"))

    async def test_logs_debug_when_index_already_exists(self):
        """
        Test that debug log is emitted when the embedding index is already
        fresh (real engine shape: IvfPq on ['embedding'] — issue #557).
        """
        existing_index = MagicMock()
        existing_index.name = "embedding_idx"
        existing_index.columns = ["embedding"]
        existing_index.index_type = "IvfPq"

        mock_table = MagicMock()
        mock_table.list_indices = AsyncMock(return_value=[existing_index])
        mock_table.count_rows = AsyncMock(return_value=300)
        mock_table.create_index = AsyncMock()

        self.store.table = mock_table
        self.store._last_index_build_row_count = 300

        with patch("app.services.vector_store.logger") as mock_logger:
            await self.store._maybe_create_vector_index()

            mock_logger.debug.assert_any_call(
                "Vector index already fresh for %d rows at generation %d, skipping creation",
                300,
                0,
            )

    async def test_logs_info_when_index_created(self):
        """
        Test that info log is emitted when vector index is created.
        """
        mock_table = MagicMock()
        mock_table.list_indices = AsyncMock(return_value=[])
        mock_table.count_rows = AsyncMock(return_value=500)
        mock_table.create_index = AsyncMock()

        self.store.table = mock_table

        with patch("app.services.vector_store.IvfPq") as MockIvfPq:
            MockIvfPq.return_value = MagicMock()

            with patch("app.services.vector_store.settings") as mock_settings:
                mock_settings.vector_metric = "cosine"
                mock_settings.write_lock_timeout_seconds = 30
                mock_settings.embedding_dim = 768

                with patch("app.services.vector_store.logger") as mock_logger:
                    await self.store._maybe_create_vector_index()

                    # Verify info log was called
                    mock_logger.info.assert_called()


class TestMaybeCreateVectorIndexEdgeCases(unittest.IsolatedAsyncioTestCase):
    """Test edge cases for _maybe_create_vector_index."""

    def setUp(self):
        """Set up test environment."""
        self.store = VectorStore(db_path=Path("/tmp/test_lancedb"))

    async def test_handles_empty_indices_list(self):
        """
        Test handling of empty indices list (no existing indices).
        """
        mock_table = MagicMock()
        mock_table.list_indices = AsyncMock(return_value=[])  # Empty list
        mock_table.count_rows = AsyncMock(return_value=100)
        mock_table.create_index = AsyncMock()

        self.store.table = mock_table

        # Should not raise
        await self.store._maybe_create_vector_index()

        # count_rows should be called since no embedding_idx found
        mock_table.count_rows.assert_called_once()

    async def test_ignores_other_index_names(self):
        """
        Test that an index of a different KIND does not satisfy the embedding
        check (a real-shape FTS index on 'text' is not an IvfPq on
        'embedding' — issue #557).
        """
        other_index = MagicMock()
        other_index.name = "text_idx"
        other_index.columns = ["text"]
        other_index.index_type = "FTS"

        mock_table = MagicMock()
        mock_table.list_indices = AsyncMock(return_value=[other_index])
        mock_table.count_rows = AsyncMock(return_value=100)
        mock_table.create_index = AsyncMock()

        self.store.table = mock_table

        await self.store._maybe_create_vector_index()

        # count_rows should be called (no embedding-column index found)
        mock_table.count_rows.assert_called_once()

    async def test_create_index_failure_logs_warning(self):
        """
        Test that create_index failure logs a warning (doesn't raise).
        """
        mock_table = MagicMock()
        mock_table.list_indices = AsyncMock(return_value=[])
        mock_table.count_rows = AsyncMock(return_value=300)
        mock_table.create_index = AsyncMock(
            side_effect=RuntimeError("Index creation failed")
        )

        self.store.table = mock_table

        with patch("app.services.vector_store.IvfPq") as MockIvfPq:
            MockIvfPq.return_value = MagicMock()

            with patch("app.services.vector_store.settings") as mock_settings:
                mock_settings.vector_metric = "cosine"
                mock_settings.write_lock_timeout_seconds = 30
                mock_settings.embedding_dim = 768

                with patch("app.services.vector_store.logger") as mock_logger:
                    # Should NOT raise - should log warning
                    await self.store._maybe_create_vector_index()

                    # Verify warning was logged
                    mock_logger.warning.assert_called()


if __name__ == "__main__":
    unittest.main()
