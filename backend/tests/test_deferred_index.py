"""
Tests for deferred vector index creation and FTS guard in VectorStore.

This module tests:
1. VECTOR_INDEX_MIN_ROWS constant equals 256
2. init_table defers vector index creation (no immediate create_index for embedding column)
3. FTS index created when not exists
4. FTS index skipped when already exists (list_indices returns fts_text)
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
        Test that FTS index creation is skipped when an FTS index already
        exists on the 'text' column.

        NOTE: superseded assumption — this test previously set
        ``existing_fts_index.name = "fts_text"`` and relied on a by-name
        check. Verified against lancedb==0.34.0 (the pinned version): a
        FTS index created via ``create_index(column="text", config=FTS())``
        (no explicit ``name=``, which is what this codebase has always
        passed) is auto-named "text_idx" by LanceDB's default
        "{column}_idx" convention, never "fts_text". The by-name check
        could therefore never match a real index this method created. The
        production code now detects existence via ``index_type`` +
        ``columns`` instead (matching how LanceDB itself resolves which
        index to use at query time), so this fixture models a realistic
        existing index: ``name="text_idx", index_type="FTS",
        columns=["text"]``.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        # Mock the database connection
        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=[])

        # Mock existing FTS index (real LanceDB default name/type/columns)
        existing_fts_index = MagicMock()
        existing_fts_index.name = "text_idx"
        existing_fts_index.index_type = "FTS"
        existing_fts_index.columns = ["text"]

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

    async def test_fts_index_creation_ignores_non_fts_indices(self):
        """
        Test that a non-FTS index on an unrelated column (e.g. the vector
        index) is not mistaken for an existing FTS index on 'text'.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=[])

        other_index = MagicMock()
        other_index.name = "embedding_idx"
        other_index.index_type = "IvfPq"
        other_index.columns = ["embedding"]

        mock_table = MagicMock()
        mock_table.list_indices = AsyncMock(return_value=[other_index])
        mock_table.create_index = AsyncMock()

        mock_db.create_table = AsyncMock(return_value=mock_table)

        store.db = mock_db

        with patch("app.services.vector_store.pa") as mock_pa:
            mock_pa.schema.return_value = MagicMock()

            with patch("app.services.vector_store.settings") as mock_settings:
                mock_settings.vector_metric = "cosine"
                mock_settings.write_lock_timeout_seconds = 30

                with patch("app.services.vector_store.FTS") as mock_fts:
                    mock_fts.return_value = MagicMock()

                    await store.init_table(embedding_dim=384)

        # embedding_idx is not an FTS index on 'text', so creation should
        # still be attempted.
        mock_table.create_index.assert_called_once()

    async def test_fts_index_conflict_on_create_is_tolerated(self):
        """
        Test that an "already exists" error from create_index (e.g. a
        legacy FTS index on 'text' that this method's existence check
        missed) is treated as recoverable: logged at INFO, NOT as the
        "hybrid search will be unavailable" WARNING, since LanceDB
        resolves FTS/hybrid queries by column rather than index name and
        the already-exists error itself proves a usable index is present.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=[])

        mock_table = MagicMock()
        mock_table.list_indices = AsyncMock(return_value=[])  # check misses it
        mock_table.create_index = AsyncMock(
            side_effect=RuntimeError(
                "Index name 'text_idx' already exists, please specify a "
                "different name or use replace=True"
            )
        )

        mock_db.create_table = AsyncMock(return_value=mock_table)

        store.db = mock_db

        with patch("app.services.vector_store.pa") as mock_pa:
            mock_pa.schema.return_value = MagicMock()

            with patch("app.services.vector_store.settings") as mock_settings:
                mock_settings.vector_metric = "cosine"
                mock_settings.write_lock_timeout_seconds = 30

                with patch("app.services.vector_store.FTS") as mock_fts:
                    mock_fts.return_value = MagicMock()

                    with patch("app.services.vector_store.logger") as mock_logger:
                        await store.init_table(embedding_dim=384)

        mock_table.create_index.assert_called_once()
        # Tolerant INFO log, not the "unavailable" warning.
        info_messages = [call.args[0] for call in mock_logger.info.call_args_list]
        self.assertTrue(
            any("already" in msg and "exists" in msg for msg in info_messages),
            f"Expected an INFO log about the tolerated conflict, got: {info_messages}",
        )
        for call in mock_logger.warning.call_args_list:
            self.assertNotIn("hybrid search will be unavailable", call.args[0])

    async def test_fts_index_genuine_creation_failure_still_warns(self):
        """
        Test that a genuine (non-conflict) create_index failure still logs
        the original "hybrid search will be unavailable" WARNING —
        the conflict-tolerance branch must not swallow real failures.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=[])

        mock_table = MagicMock()
        mock_table.list_indices = AsyncMock(return_value=[])
        mock_table.create_index = AsyncMock(
            side_effect=RuntimeError("disk I/O error while building index")
        )

        mock_db.create_table = AsyncMock(return_value=mock_table)

        store.db = mock_db

        with patch("app.services.vector_store.pa") as mock_pa:
            mock_pa.schema.return_value = MagicMock()

            with patch("app.services.vector_store.settings") as mock_settings:
                mock_settings.vector_metric = "cosine"
                mock_settings.write_lock_timeout_seconds = 30

                with patch("app.services.vector_store.FTS") as mock_fts:
                    mock_fts.return_value = MagicMock()

                    with patch("app.services.vector_store.logger") as mock_logger:
                        await store.init_table(embedding_dim=384)

        mock_table.create_index.assert_called_once()
        warning_messages = [
            call.args[0] for call in mock_logger.warning.call_args_list
        ]
        self.assertTrue(
            any("hybrid search will be unavailable" in msg for msg in warning_messages),
            f"Expected the unchanged WARNING path, got: {warning_messages}",
        )

    async def test_fts_index_different_fields_conflict_still_warns(self):
        """
        Test that LanceDB's OTHER "already exists" message —
        "Index name '<name>' already exists with different fields, please
        specify a different name" (no "or use replace=True" suffix) — is
        NOT treated as the benign tolerate-and-log case.

        This message means an index with that name exists but is NOT
        equivalent to the one just requested (e.g. different columns/config),
        which is a genuine misconfiguration, not proof that a working FTS
        index on 'text' is already present. It must fall through to the
        unchanged "hybrid search will be unavailable" WARNING path.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=[])

        mock_table = MagicMock()
        mock_table.list_indices = AsyncMock(return_value=[])
        mock_table.create_index = AsyncMock(
            side_effect=RuntimeError(
                "Index name 'text_idx' already exists with different "
                "fields, please specify a different name"
            )
        )

        mock_db.create_table = AsyncMock(return_value=mock_table)

        store.db = mock_db

        with patch("app.services.vector_store.pa") as mock_pa:
            mock_pa.schema.return_value = MagicMock()

            with patch("app.services.vector_store.settings") as mock_settings:
                mock_settings.vector_metric = "cosine"
                mock_settings.write_lock_timeout_seconds = 30

                with patch("app.services.vector_store.FTS") as mock_fts:
                    mock_fts.return_value = MagicMock()

                    with patch("app.services.vector_store.logger") as mock_logger:
                        await store.init_table(embedding_dim=384)

        mock_table.create_index.assert_called_once()
        warning_messages = [
            call.args[0] for call in mock_logger.warning.call_args_list
        ]
        self.assertTrue(
            any("hybrid search will be unavailable" in msg for msg in warning_messages),
            f"Expected the 'different fields' conflict to still warn, got: {warning_messages}",
        )
        # And must NOT be logged as the tolerant INFO message.
        info_messages = [call.args[0] for call in mock_logger.info.call_args_list]
        self.assertFalse(
            any("already" in msg and "exists" in msg for msg in info_messages),
            f"'different fields' conflict should not be tolerated, got INFO: {info_messages}",
        )


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
        # Mock table with existing embedding_idx
        existing_index = MagicMock()
        existing_index.name = "embedding_idx"

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
        Test that debug log is emitted when embedding_idx is already fresh.
        """
        existing_index = MagicMock()
        existing_index.name = "embedding_idx"

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
        Test that other index names (not embedding_idx) are ignored.
        """
        other_index = MagicMock()
        other_index.name = "some_other_index"

        mock_table = MagicMock()
        mock_table.list_indices = AsyncMock(return_value=[other_index])
        mock_table.count_rows = AsyncMock(return_value=100)
        mock_table.create_index = AsyncMock()

        self.store.table = mock_table

        await self.store._maybe_create_vector_index()

        # count_rows should be called (embedding_idx not found)
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
