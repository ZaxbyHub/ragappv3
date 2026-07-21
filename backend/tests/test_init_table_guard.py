"""
Tests for table_just_created tracking and deferred-index logging in init_table().

This module tests:
1. table_just_created = False initially
2. table_just_created = True when brand new table is created
3. table_just_created = True when table is recreated via overwrite path
4. table_just_created remains False when opening existing table
5. Deferred-index log fires only when table_just_created is True
6. Deferred-index log does NOT fire when opening existing table
7. FTS index created with replace=False when fts_index_exists=False
8. FTS index NOT recreated when fts_index_exists=True
"""

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.vector_store import (
    VECTOR_INDEX_MIN_ROWS,
    VectorStore,
)


class TestTableJustCreatedTracking(unittest.IsolatedAsyncioTestCase):
    """Test cases for table_just_created boolean tracking in init_table()."""

    async def test_deferred_log_fires_when_brand_new_table_created(self):
        """
        Test that deferred-index log fires when a brand new table is created.

        This tests the code path where table_names does NOT contain 'chunks',
        so create_table is called without 'overwrite' mode.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        # Mock database - no existing tables
        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=[])  # No existing tables

        # Mock table
        mock_table = MagicMock()
        mock_table.list_indices = AsyncMock(return_value=[])
        mock_table.create_index = AsyncMock()

        mock_db.create_table = AsyncMock(return_value=mock_table)

        store.db = mock_db

        # Capture logger calls
        with patch("app.services.vector_store.pa") as mock_pa:
            mock_pa.schema.return_value = MagicMock()

            with patch("app.services.vector_store.settings") as mock_settings:
                mock_settings.vector_metric = "cosine"
                mock_settings.write_lock_timeout_seconds = 30

                with patch("app.services.vector_store.FTS") as mock_fts:
                    mock_fts.return_value = MagicMock()

                    with patch("app.services.vector_store.logger") as mock_logger:
                        await store.init_table(embedding_dim=384)

        # Verify deferred-index log was called (table_just_created was True)
        log_calls = [str(call) for call in mock_logger.info.call_args_list]
        deferred_log_found = any(
            "vector index deferred" in str(call).lower() for call in log_calls
        )
        self.assertTrue(
            deferred_log_found,
            "Deferred-index log should fire when brand new table is created",
        )

    async def test_deferred_log_fires_when_table_recreated_via_overwrite(self):
        """
        Test that deferred-index log fires when table is recreated via overwrite path.

        This tests the code path where 'chunks' table exists but open_table fails,
        triggering drop_table and create_table with mode='overwrite'.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        # Mock database - chunks table exists
        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=["chunks"])

        # open_table fails, triggering overwrite path
        mock_db.open_table = AsyncMock(side_effect=RuntimeError("Stale table"))
        mock_db.drop_table = AsyncMock()

        # Mock table for overwrite creation
        mock_table = MagicMock()
        mock_table.list_indices = AsyncMock(return_value=[])
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

                    with patch("app.services.vector_store.logger") as mock_logger:
                        await store.init_table(embedding_dim=384)

        # Verify deferred-index log was called (table_just_created was True)
        log_calls = [str(call) for call in mock_logger.info.call_args_list]
        deferred_log_found = any(
            "vector index deferred" in str(call).lower() for call in log_calls
        )
        self.assertTrue(
            deferred_log_found,
            "Deferred-index log should fire when table is recreated via overwrite",
        )

    async def test_deferred_log_does_not_fire_when_opening_existing_table(self):
        """
        Test that deferred-index log does NOT fire when opening existing table.

        This tests the code path where 'chunks' table exists and open_table succeeds,
        so table_just_created remains False.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        # Mock database - chunks table exists
        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=["chunks"])

        # Mock table with existing FTS index. Real LanceDB default name is
        # "text_idx" (verified against lancedb==0.34.0), not "fts_text";
        # index_type/columns are what the FTS-exists check now matches on.
        mock_table = MagicMock()
        existing_fts = MagicMock()
        existing_fts.name = "text_idx"
        existing_fts.index_type = "FTS"
        existing_fts.columns = ["text"]
        mock_table.list_indices = AsyncMock(return_value=[existing_fts])
        mock_table.create_index = AsyncMock()

        # open_table succeeds
        mock_db.open_table = AsyncMock(return_value=mock_table)

        store.db = mock_db

        with patch("app.services.vector_store.pa") as mock_pa:
            mock_pa.schema.return_value = MagicMock()

            with patch("app.services.vector_store.settings") as mock_settings:
                mock_settings.vector_metric = "cosine"
                mock_settings.write_lock_timeout_seconds = 30

                with patch("app.services.vector_store.logger") as mock_logger:
                    await store.init_table(embedding_dim=384)

        # Verify deferred-index log was NOT called (table_just_created was False)
        log_calls = [str(call) for call in mock_logger.info.call_args_list]
        deferred_log_found = any(
            "vector index deferred" in str(call).lower() for call in log_calls
        )
        self.assertFalse(
            deferred_log_found,
            "Deferred-index log should NOT fire when opening existing table",
        )


class TestFTSIndexReplaceFalse(unittest.IsolatedAsyncioTestCase):
    """Test cases for FTS index creation with replace=False."""

    async def test_fts_index_created_with_replace_false_when_not_exists(self):
        """
        Test that FTS index is created with replace=False when it doesn't exist.

        This verifies the FTS guard: create_index should use replace=False,
        relying on the fts_index_exists guard to skip if index already exists.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        # Mock database
        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=[])

        # Track create_index call arguments
        create_index_calls = []

        async def mock_create_index(column, config=None, replace=False):
            create_index_calls.append(
                {"column": column, "config": config, "replace": replace}
            )

        mock_table = MagicMock()
        mock_table.create_index = mock_create_index
        mock_table.list_indices = AsyncMock(return_value=[])  # No FTS index

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

        # Find the FTS create_index call
        fts_call = None
        for call in create_index_calls:
            if call["column"] == "text":
                fts_call = call
                break

        self.assertIsNotNone(fts_call, "FTS index should be created on 'text' column")
        self.assertFalse(
            fts_call["replace"], "FTS index should be created with replace=False"
        )

    async def test_fts_index_not_recreated_when_already_exists(self):
        """
        Test that FTS index creation is skipped when an FTS index already
        exists on the 'text' column.

        NOTE: superseded assumption — previously modeled the existing index
        as ``name="fts_text"``. Verified against lancedb==0.34.0: LanceDB
        auto-names an FTS index created via create_index(column="text",
        config=FTS()) (no explicit name=, which is all this codebase has
        ever passed) as "text_idx" ("{column}_idx" convention), never
        "fts_text" — a by-name check against "fts_text" could never match a
        real index. The guard now matches on index_type + columns instead
        (mirroring how LanceDB itself resolves which index to use for
        FTS/hybrid queries), so this fixture models the real shape:
        ``name="text_idx", index_type="FTS", columns=["text"]``.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        # Mock database
        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=[])

        # Mock existing FTS index (real LanceDB default name/type/columns)
        existing_fts = MagicMock()
        existing_fts.name = "text_idx"
        existing_fts.index_type = "FTS"
        existing_fts.columns = ["text"]

        mock_table = MagicMock()
        mock_table.list_indices = AsyncMock(return_value=[existing_fts])
        mock_table.create_index = AsyncMock()  # Track if called

        mock_db.create_table = AsyncMock(return_value=mock_table)

        store.db = mock_db

        with patch("app.services.vector_store.pa") as mock_pa:
            mock_pa.schema.return_value = MagicMock()

            with patch("app.services.vector_store.settings") as mock_settings:
                mock_settings.vector_metric = "cosine"
                mock_settings.write_lock_timeout_seconds = 30

                await store.init_table(embedding_dim=384)

        # Verify create_index was NOT called for FTS
        mock_table.create_index.assert_not_called()

    async def test_fts_guard_logs_debug_when_skipping_existing_index(self):
        """
        Test that debug log is emitted when FTS index already exists.

        This verifies the 'FTS index already exists, skipping creation' log path.

        NOTE: superseded assumption — see
        test_fts_index_not_recreated_when_already_exists above for why the
        existing-index fixture now sets index_type/columns rather than
        relying on name="fts_text".
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        # Mock database
        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=[])

        # Mock existing FTS index (real LanceDB default name/type/columns)
        existing_fts = MagicMock()
        existing_fts.name = "text_idx"
        existing_fts.index_type = "FTS"
        existing_fts.columns = ["text"]

        mock_table = MagicMock()
        mock_table.list_indices = AsyncMock(return_value=[existing_fts])
        mock_table.create_index = AsyncMock()

        mock_db.create_table = AsyncMock(return_value=mock_table)

        store.db = mock_db

        with patch("app.services.vector_store.pa") as mock_pa:
            mock_pa.schema.return_value = MagicMock()

            with patch("app.services.vector_store.settings") as mock_settings:
                mock_settings.vector_metric = "cosine"
                mock_settings.write_lock_timeout_seconds = 30

                with patch("app.services.vector_store.logger") as mock_logger:
                    await store.init_table(embedding_dim=384)

        # Verify debug log was called for FTS skip
        debug_calls = [str(call) for call in mock_logger.debug.call_args_list]
        fts_skip_found = any(
            "fts index already exists" in str(call).lower() for call in debug_calls
        )
        self.assertTrue(
            fts_skip_found, "Debug log should indicate FTS index already exists"
        )


class TestFTSIndexCreationFailure(unittest.IsolatedAsyncioTestCase):
    """Test FTS index creation failure handling."""

    async def test_fts_failure_logs_warning(self):
        """
        Test that FTS index creation failure logs a warning, not an error.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        # Mock database
        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=[])

        mock_table = MagicMock()
        mock_table.list_indices = AsyncMock(return_value=[])  # No existing FTS

        # create_index fails
        mock_table.create_index = AsyncMock(
            side_effect=RuntimeError("FTS creation failed")
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
                        # Should not raise
                        await store.init_table(embedding_dim=384)

        # Verify warning was logged
        warning_calls = [str(call) for call in mock_logger.warning.call_args_list]
        fts_warning_found = any(
            "fts" in str(call).lower() and "failed" in str(call).lower()
            for call in warning_calls
        )
        self.assertTrue(fts_warning_found, "FTS creation failure should log a warning")


class TestDeferredIndexLogMessage(unittest.IsolatedAsyncioTestCase):
    """Test the deferred-index log message content."""

    async def test_deferred_log_contains_threshold_value(self):
        """
        Test that deferred-index log contains the VECTOR_INDEX_MIN_ROWS threshold (256).
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        # Mock database - no existing tables
        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=[])

        mock_table = MagicMock()
        mock_table.list_indices = AsyncMock(return_value=[])
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

                    with patch("app.services.vector_store.logger") as mock_logger:
                        await store.init_table(embedding_dim=384)

        # Find the deferred log call and verify threshold
        info_calls = mock_logger.info.call_args_list
        deferred_call = None
        for call in info_calls:
            call_str = str(call)
            if "vector index deferred" in call_str.lower():
                deferred_call = call
                break

        self.assertIsNotNone(deferred_call, "Deferred-index log should be emitted")

        # Verify the call contains the threshold value
        # The log format is: "Table created; vector index deferred until ≥%d rows"
        call_args = deferred_call[0] if deferred_call[0] else []
        deferred_call[1] if deferred_call[1] else {}

        # The threshold should be passed as an argument
        threshold_found = False
        for arg in call_args:
            if arg == VECTOR_INDEX_MIN_ROWS or arg == 256:
                threshold_found = True
                break

        if not threshold_found:
            # Check if it's in the format string arguments
            if 256 in str(call_args) or str(VECTOR_INDEX_MIN_ROWS) in str(call_args):
                threshold_found = True

        self.assertTrue(
            threshold_found,
            f"Deferred log should reference threshold {VECTOR_INDEX_MIN_ROWS}",
        )


class TestVectorIndexMinRowsConstant(unittest.TestCase):
    """Test that VECTOR_INDEX_MIN_ROWS is 256."""

    def test_threshold_is_256(self):
        """Verify the deferred index threshold is 256 rows."""
        self.assertEqual(VECTOR_INDEX_MIN_ROWS, 256)


if __name__ == "__main__":
    unittest.main()
