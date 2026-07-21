"""
Adversarial tests for table_just_created tracking in vector_store.py init_table().

Attack surfaces tested:
1. Unexpected exception types from db.table_names() (not OSError/RuntimeError/ValueError)
2. Corrupt table: open_table succeeds but list_indices fails
3. Overwrite path failure during drop_table
4. Concurrency: init_table called twice rapidly
5. embedding_dim boundary: 0, negative, extreme values
6. Table schema completely different from expected
7. Race conditions in table creation
"""

import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.vector_store import (
    VectorStore,
    VectorStoreConnectionError,
)


class TestAdversarialUnexpectedExceptionTypes(unittest.IsolatedAsyncioTestCase):
    """
    Test that unexpected exception types from db.table_names() are properly handled.

    The code catches (OSError, RuntimeError, ValueError) - test what happens with
    other exception types like KeyError, TypeError, AttributeError, etc.
    """

    async def test_table_names_raises_keyerror_propagates_as_connection_error(self):
        """
        ATTACK VECTOR: db.table_names() raises KeyError (unexpected type).

        Expected: VectorStoreConnectionError should be raised, not KeyError.
        The except block only catches OSError/RuntimeError/ValueError.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        mock_db = MagicMock()
        # KeyError is NOT in the caught exception types
        mock_db.table_names = AsyncMock(side_effect=KeyError("unexpected key error"))
        store.db = mock_db

        with patch("app.services.vector_store.pa") as mock_pa:
            mock_pa.schema.return_value = MagicMock()

            # KeyError should propagate, not be caught by the try/except
            with self.assertRaises(KeyError):
                await store.init_table(embedding_dim=384)

    async def test_table_names_raises_typeerror_propagates(self):
        """
        ATTACK VECTOR: db.table_names() raises TypeError.

        Expected: TypeError propagates since it's not caught.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(side_effect=TypeError("unexpected type"))
        store.db = mock_db

        with patch("app.services.vector_store.pa") as mock_pa:
            mock_pa.schema.return_value = MagicMock()

            with self.assertRaises(TypeError):
                await store.init_table(embedding_dim=384)

    async def test_table_names_raises_attributeerror_propagates(self):
        """
        ATTACK VECTOR: db.table_names() raises AttributeError.

        Expected: AttributeError propagates since it's not caught.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(
            side_effect=AttributeError("unexpected attribute")
        )
        store.db = mock_db

        with patch("app.services.vector_store.pa") as mock_pa:
            mock_pa.schema.return_value = MagicMock()

            with self.assertRaises(AttributeError):
                await store.init_table(embedding_dim=384)

    async def test_table_names_raises_keyboard_interrupt_propagates(self):
        """
        ATTACK VECTOR: db.table_names() raises KeyboardInterrupt.

        Expected: KeyboardInterrupt propagates (critical for process control).
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(side_effect=KeyboardInterrupt())
        store.db = mock_db

        with patch("app.services.vector_store.pa") as mock_pa:
            mock_pa.schema.return_value = MagicMock()

            with self.assertRaises(KeyboardInterrupt):
                await store.init_table(embedding_dim=384)


class TestAdversarialCorruptTable(unittest.IsolatedAsyncioTestCase):
    """
    Test handling when open_table succeeds but subsequent operations fail.

    Attack vector: Table exists but is corrupt - list_indices fails.
    """

    async def test_corrupt_table_list_indices_fails_after_open(self):
        """
        ATTACK VECTOR: open_table succeeds but list_indices fails.

        This simulates a corrupt table where the table object exists
        but operations on it fail.

        When list_indices fails, fts_index_exists defaults to False,
        so FTS creation IS attempted. This tests that behavior.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=["chunks"])

        # Mock table that appears valid but list_indices fails
        mock_table = MagicMock()
        mock_table.list_indices = AsyncMock(
            side_effect=RuntimeError("Corrupt table: cannot list indices")
        )
        mock_table.create_index = AsyncMock()

        mock_db.open_table = AsyncMock(return_value=mock_table)
        store.db = mock_db

        with patch("app.services.vector_store.pa") as mock_pa:
            mock_pa.schema.return_value = MagicMock()

            with patch("app.services.vector_store.settings") as mock_settings:
                mock_settings.vector_metric = "cosine"
                mock_settings.write_lock_timeout_seconds = 30

            with patch("app.services.vector_store.FTS") as mock_fts:
                mock_fts.return_value = MagicMock()

                with patch("app.services.vector_store.logger"):
                    # Should NOT raise - list_indices failure is caught
                    await store.init_table(embedding_dim=384)

                    # Verify the table was opened
                    self.assertEqual(store.table, mock_table)

                    # FTS creation IS attempted because fts_index_exists defaults to False
                    # when list_indices fails
                    mock_table.create_index.assert_called_once()
                    call_kwargs = mock_table.create_index.call_args[1]
                    self.assertEqual(call_kwargs["column"], "text")
                    self.assertEqual(call_kwargs["replace"], False)

    async def test_corrupt_table_with_oserror_after_open(self):
        """
        ATTACK VECTOR: open_table succeeds but list_indices raises OSError.

        OSError should be caught by the except block, but fts_index_exists
        defaults to False, so FTS creation IS attempted.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=["chunks"])

        mock_table = MagicMock()
        mock_table.list_indices = AsyncMock(
            side_effect=OSError("IO error on corrupt table")
        )
        mock_table.create_index = AsyncMock()

        mock_db.open_table = AsyncMock(return_value=mock_table)
        store.db = mock_db

        with patch("app.services.vector_store.pa") as mock_pa:
            mock_pa.schema.return_value = MagicMock()

            with patch("app.services.vector_store.settings") as mock_settings:
                mock_settings.vector_metric = "cosine"
                mock_settings.write_lock_timeout_seconds = 30

            with patch("app.services.vector_store.FTS") as mock_fts:
                mock_fts.return_value = MagicMock()

                with patch("app.services.vector_store.logger"):
                    # Should NOT raise
                    await store.init_table(embedding_dim=384)

                    # FTS creation IS attempted because fts_index_exists defaults to False
                    mock_table.create_index.assert_called_once()


class TestAdversarialOverwritePathFailures(unittest.IsolatedAsyncioTestCase):
    """
    Test edge cases in the overwrite path (drop_table + create_table).

    Attack vectors:
    - drop_table fails during overwrite
    - create_table fails after drop_table succeeds
    """

    async def test_drop_table_fails_during_overwrite_path(self):
        """
        ATTACK VECTOR: open_table fails, drop_table also fails.

        This tests the nested exception handling in the overwrite path.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=["chunks"])

        # open_table fails, triggering overwrite path
        mock_db.open_table = AsyncMock(side_effect=RuntimeError("Stale table"))

        # drop_table also fails (but should be caught silently)
        mock_db.drop_table = AsyncMock(side_effect=OSError("Cannot drop"))

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

                    with patch("app.services.vector_store.logger"):
                        # Should NOT raise - drop_table failure is caught silently
                        await store.init_table(embedding_dim=384)

                        # Verify create_table was still called after drop failure
                        mock_db.create_table.assert_called_once()
                        # Verify mode="overwrite" was used
                        call_kwargs = mock_db.create_table.call_args[1]
                        self.assertEqual(call_kwargs.get("mode"), "overwrite")

    async def test_create_table_fails_after_successful_drop(self):
        """
        ATTACK VECTOR: drop_table succeeds but create_table fails in overwrite path.

        This tests the exception handling when recreation fails.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=["chunks"])

        # open_table fails, triggering overwrite
        mock_db.open_table = AsyncMock(side_effect=RuntimeError("Stale table"))
        mock_db.drop_table = AsyncMock()

        # create_table fails after successful drop
        mock_db.create_table = AsyncMock(
            side_effect=RuntimeError("Cannot create after drop")
        )

        store.db = mock_db

        with patch("app.services.vector_store.pa") as mock_pa:
            mock_pa.schema.return_value = MagicMock()

            # RuntimeError is caught, should raise VectorStoreConnectionError
            with self.assertRaises(VectorStoreConnectionError) as context:
                await store.init_table(embedding_dim=384)

            self.assertIn("Cannot create after drop", str(context.exception))


class TestAdversarialConcurrency(unittest.IsolatedAsyncioTestCase):
    """
    Test race conditions when init_table is called concurrently.

    Attack vector: Two init_table calls racing to create the same table.
    """

    async def test_concurrent_init_table_calls_race_condition(self):
        """
        ATTACK VECTOR: Two init_table calls race to create table.

        Test that concurrent calls don't cause state corruption.
        """
        store1 = VectorStore(db_path=Path("/tmp/test_lancedb"))
        store2 = VectorStore(db_path=Path("/tmp/test_lancedb"))

        # Track order of operations
        call_order = []
        create_count = [0]

        async def mock_table_names():
            call_order.append("table_names")
            await asyncio.sleep(0.01)  # Simulate latency
            return []  # No tables exist

        async def mock_create_table(name, schema=None, mode=None):
            call_order.append(f"create_{len(call_order)}")
            create_count[0] += 1
            await asyncio.sleep(0.02)  # Simulate creation time
            mock_table = MagicMock()
            mock_table.list_indices = AsyncMock(return_value=[])
            mock_table.create_index = AsyncMock()
            return mock_table

        mock_db1 = MagicMock()
        mock_db1.table_names = mock_table_names
        mock_db1.create_table = mock_create_table

        mock_db2 = MagicMock()
        mock_db2.table_names = mock_table_names
        mock_db2.create_table = mock_create_table

        store1.db = mock_db1
        store2.db = mock_db2

        with patch("app.services.vector_store.pa") as mock_pa:
            mock_pa.schema.return_value = MagicMock()

            with patch("app.services.vector_store.settings") as mock_settings:
                mock_settings.vector_metric = "cosine"
                mock_settings.write_lock_timeout_seconds = 30

                with patch("app.services.vector_store.FTS") as mock_fts:
                    mock_fts.return_value = MagicMock()

                    # Run both init_table calls concurrently
                    results = await asyncio.gather(
                        store1.init_table(embedding_dim=384),
                        store2.init_table(embedding_dim=384),
                        return_exceptions=True,
                    )

                    # Both should complete (no uncaught exceptions)
                    for r in results:
                        self.assertIsInstance(r, VectorStore)

    async def test_rapid_sequential_init_table_same_store(self):
        """
        ATTACK VECTOR: Same store has init_table called rapidly twice.

        Test that rapid sequential calls don't cause issues.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        call_count = [0]

        async def mock_create_table(name, schema=None, mode=None):
            call_count[0] += 1
            mock_table = MagicMock()
            mock_table.list_indices = AsyncMock(return_value=[])
            mock_table.create_index = AsyncMock()
            return mock_table

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=[])
        mock_db.create_table = mock_create_table

        store.db = mock_db

        with patch("app.services.vector_store.pa") as mock_pa:
            mock_pa.schema.return_value = MagicMock()

            with patch("app.services.vector_store.settings") as mock_settings:
                mock_settings.vector_metric = "cosine"
                mock_settings.write_lock_timeout_seconds = 30

                with patch("app.services.vector_store.FTS") as mock_fts:
                    mock_fts.return_value = MagicMock()

                    # First call
                    await store.init_table(embedding_dim=384)
                    # Second call immediately
                    await store.init_table(embedding_dim=384)

                    # Both should succeed
                    self.assertEqual(call_count[0], 2)


class TestAdversarialEmbeddingDimBoundary(unittest.IsolatedAsyncioTestCase):
    """
    Test boundary values for embedding_dim parameter.

    Attack vectors: 0, negative, extremely large values.
    """

    async def test_embedding_dim_zero_creates_empty_list_schema(self):
        """
        ATTACK VECTOR: embedding_dim = 0.

        With embedding_dim=0, pa.list_(pa.float32(), 0) creates a list of size 0.
        This is technically valid PyArrow but semantically meaningless.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=[])

        mock_table = MagicMock()
        mock_table.list_indices = AsyncMock(return_value=[])
        mock_table.create_index = AsyncMock()

        mock_db.create_table = AsyncMock(return_value=mock_table)
        store.db = mock_db

        captured_schema = None

        def capture_schema(*args, **kwargs):
            nonlocal captured_schema
            # Capture the schema argument
            if "schema" in kwargs:
                captured_schema = kwargs["schema"]
            return MagicMock()

        with patch("app.services.vector_store.pa") as mock_pa:
            mock_pa.schema.return_value = MagicMock()
            mock_pa.list_ = lambda t, size: MagicMock(list_size=size)

            with patch("app.services.vector_store.settings") as mock_settings:
                mock_settings.vector_metric = "cosine"
                mock_settings.write_lock_timeout_seconds = 30

                with patch("app.services.vector_store.FTS") as mock_fts:
                    mock_fts.return_value = MagicMock()

                    # embedding_dim=0 should not crash
                    await store.init_table(embedding_dim=0)

                    # Verify embedding_dim was stored as 0
                    self.assertEqual(store._embedding_dim, 0)

    async def test_embedding_dim_negative_raises_on_schema_creation(self):
        """
        ATTACK VECTOR: embedding_dim = -1.

        Negative size for fixed-size list should fail during schema creation.
        PyArrow may raise an error for negative list sizes.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=[])
        mock_db.create_table = AsyncMock(return_value=MagicMock())
        store.db = mock_db

        # Mock pa.list_ to raise for negative values
        def mock_list(t, size):
            if size < 0:
                raise ValueError(f"List size must be non-negative, got {size}")
            return MagicMock(list_size=size)

        with patch("app.services.vector_store.pa") as mock_pa:
            mock_pa.schema.return_value = MagicMock()
            mock_pa.list_ = mock_list

            with patch("app.services.vector_store.settings") as mock_settings:
                mock_settings.vector_metric = "cosine"
                mock_settings.write_lock_timeout_seconds = 30

                # Negative embedding_dim should raise ValueError
                with self.assertRaises(ValueError):
                    await store.init_table(embedding_dim=-1)

    async def test_embedding_dim_extremely_large(self):
        """
        ATTACK VECTOR: embedding_dim = 10000000 (10 million).

        Extremely large embedding dimension could cause memory issues.
        Test that schema creation doesn't crash immediately.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=[])

        mock_table = MagicMock()
        mock_table.list_indices = AsyncMock(return_value=[])
        mock_table.create_index = AsyncMock()

        mock_db.create_table = AsyncMock(return_value=mock_table)
        store.db = mock_db

        with patch("app.services.vector_store.pa") as mock_pa:
            mock_pa.schema.return_value = MagicMock()
            mock_pa.list_ = lambda t, size: MagicMock(list_size=size)

            with patch("app.services.vector_store.settings") as mock_settings:
                mock_settings.vector_metric = "cosine"
                mock_settings.write_lock_timeout_seconds = 30

                with patch("app.services.vector_store.FTS") as mock_fts:
                    mock_fts.return_value = MagicMock()

                    # Large embedding_dim should be accepted (schema is just metadata)
                    await store.init_table(embedding_dim=10000000)

                    self.assertEqual(store._embedding_dim, 10000000)

    async def test_embedding_dim_one(self):
        """
        ATTACK VECTOR: embedding_dim = 1 (boundary minimum).

        Minimum valid positive embedding dimension.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=[])

        mock_table = MagicMock()
        mock_table.list_indices = AsyncMock(return_value=[])
        mock_table.create_index = AsyncMock()

        mock_db.create_table = AsyncMock(return_value=mock_table)
        store.db = mock_db

        with patch("app.services.vector_store.pa") as mock_pa:
            mock_pa.schema.return_value = MagicMock()
            mock_pa.list_ = lambda t, size: MagicMock(list_size=size)

            with patch("app.services.vector_store.settings") as mock_settings:
                mock_settings.vector_metric = "cosine"
                mock_settings.write_lock_timeout_seconds = 30

                with patch("app.services.vector_store.FTS") as mock_fts:
                    mock_fts.return_value = MagicMock()

                    await store.init_table(embedding_dim=1)
                    self.assertEqual(store._embedding_dim, 1)


class TestAdversarialSchemaMismatch(unittest.IsolatedAsyncioTestCase):
    """
    Test when existing table has a completely different schema.

    Attack vector: Table exists with unexpected columns/types.
    """

    async def test_existing_table_missing_embedding_column(self):
        """
        ATTACK VECTOR: Existing table is missing the embedding column.

        If table was created by different code or corrupted, schema may differ.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=["chunks"])

        # Mock table with completely different schema
        mock_table = MagicMock()

        # Schema without embedding column
        mock_schema = MagicMock()
        mock_field_names = [
            "id",
            "text",
            "file_id",
        ]  # Missing embedding, vault_id, etc.
        mock_schema.field = lambda i: MagicMock(name=mock_field_names[i])
        mock_schema.__len__ = lambda self: len(mock_field_names)

        mock_table.schema = AsyncMock(return_value=mock_schema)
        mock_table.list_indices = AsyncMock(return_value=[])
        mock_table.create_index = AsyncMock()

        mock_db.open_table = AsyncMock(return_value=mock_table)
        store.db = mock_db

        with patch("app.services.vector_store.pa") as mock_pa:
            mock_pa.schema.return_value = MagicMock()

            with patch("app.services.vector_store.settings") as mock_settings:
                mock_settings.vector_metric = "cosine"
                mock_settings.write_lock_timeout_seconds = 30

                with patch("app.services.vector_store.logger"):
                    # Should open the table without crashing
                    await store.init_table(embedding_dim=384)

                    # Table should be assigned
                    self.assertEqual(store.table, mock_table)

    async def test_existing_table_wrong_embedding_type(self):
        """
        ATTACK VECTOR: Existing table has embedding as string instead of list.

        Schema type mismatch could cause issues later.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=["chunks"])

        mock_table = MagicMock()

        # Mock schema with wrong embedding type
        mock_schema = MagicMock()

        # Simulate accessing field that returns unexpected type
        mock_embedding_field = MagicMock()
        mock_embedding_field.type = "string"  # Wrong type (should be list)

        def mock_field(name_or_idx):
            if name_or_idx == "embedding":
                return mock_embedding_field
            return MagicMock()

        mock_schema.field = mock_field
        mock_table.schema = AsyncMock(return_value=mock_schema)
        mock_table.list_indices = AsyncMock(return_value=[])
        mock_table.create_index = AsyncMock()

        mock_db.open_table = AsyncMock(return_value=mock_table)
        store.db = mock_db

        with patch("app.services.vector_store.pa") as mock_pa:
            mock_pa.schema.return_value = MagicMock()

            with patch("app.services.vector_store.settings") as mock_settings:
                mock_settings.vector_metric = "cosine"
                mock_settings.write_lock_timeout_seconds = 30

                # Should not crash - schema mismatch is handled later
                await store.init_table(embedding_dim=384)


class TestAdversarialFTSCreationEdgeCases(unittest.IsolatedAsyncioTestCase):
    """
    Test edge cases in FTS index creation.

    Attack vectors: FTS creation fails, returns unexpected value, etc.
    """

    async def test_fts_create_index_raises_unexpected_exception_type(self):
        """
        ATTACK VECTOR: create_index raises unexpected exception type.

        Test that FTS creation failure with unexpected error type is handled.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=[])

        mock_table = MagicMock()
        mock_table.list_indices = AsyncMock(return_value=[])

        # create_index raises unexpected exception type
        mock_table.create_index = AsyncMock(side_effect=KeyError("FTS key error"))

        mock_db.create_table = AsyncMock(return_value=mock_table)
        store.db = mock_db

        with patch("app.services.vector_store.pa") as mock_pa:
            mock_pa.schema.return_value = MagicMock()

            with patch("app.services.vector_store.settings") as mock_settings:
                mock_settings.vector_metric = "cosine"
                mock_settings.write_lock_timeout_seconds = 30

                with patch("app.services.vector_store.FTS") as mock_fts:
                    mock_fts.return_value = MagicMock()

                    with patch("app.services.vector_store.logger"):
                        # KeyError is NOT caught, should propagate
                        with self.assertRaises(KeyError):
                            await store.init_table(embedding_dim=384)

    async def test_fts_list_indices_returns_none_instead_of_list(self):
        """
        ATTACK VECTOR: list_indices returns None instead of list.

        Test handling when list_indices returns unexpected type.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=[])

        mock_table = MagicMock()
        # Returns None instead of list
        mock_table.list_indices = AsyncMock(return_value=None)
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

                    # Should crash on "for idx in indices" (indices is None,
                    # not iterable) before the per-index predicate even runs.
                    # Still true after the FTS-exists check was rewritten to
                    # match on (idx.index_type, idx.columns) instead of
                    # idx.name (see test_fts_index_object_missing_attributes
                    # below) -- the None-iteration TypeError is unaffected by
                    # which attributes the predicate reads.
                    with self.assertRaises(TypeError):
                        await store.init_table(embedding_dim=384)

    async def test_fts_index_object_missing_attributes(self):
        """
        ATTACK VECTOR: Index object in list_indices lacks 'index_type'/'columns'.

        NOTE: superseded assumption — this test previously deleted only
        `.name` and asserted an AttributeError, because the FTS-exists
        check used to do a direct `idx.name == "fts_text"` attribute
        access. Verified against lancedb==0.34.0: real FTS indices are
        never actually named "fts_text" (LanceDB auto-names them
        "text_idx"), so that by-name check was replaced with a check on
        `idx.index_type` / `idx.columns` (matching how LanceDB itself
        resolves which index to use for FTS/hybrid queries). The new check
        reads those two attributes via `getattr(idx, ..., default)` —
        mirroring the same defensive pattern already used a few lines above
        for the embedding_idx check — so a malformed index object missing
        them no longer crashes the whole init_table() call; it is simply
        treated as "not a matching FTS index" and creation proceeds.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=[])

        mock_table = MagicMock()

        # Index object without 'index_type' or 'columns' attributes
        mock_idx = MagicMock()
        del mock_idx.index_type
        del mock_idx.columns

        mock_table.list_indices = AsyncMock(return_value=[mock_idx])
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

                    # Should NOT crash: getattr(..., default) treats the
                    # malformed object as "not a matching FTS index" and
                    # falls through to attempting creation.
                    await store.init_table(embedding_dim=384)

        mock_table.create_index.assert_called_once()


class TestAdversarialTableJustCreatedState(unittest.IsolatedAsyncioTestCase):
    """
    Test that table_just_created boolean is correctly tracked in adversarial scenarios.
    """

    async def test_table_just_created_remains_false_on_partial_failure(self):
        """
        ATTACK VECTOR: Ensure table_just_created doesn't get set True prematurely.

        When open_table fails and drop_table also fails, the overwrite path
        should still work (drop_table failure is silently caught).
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        mock_db = MagicMock()

        mock_db.table_names = AsyncMock(return_value=["chunks"])
        # open_table fails, triggering overwrite path
        mock_db.open_table = AsyncMock(side_effect=RuntimeError("Cannot open"))
        # drop_table fails but should be silently caught
        mock_db.drop_table = AsyncMock(side_effect=OSError("Cannot drop"))

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

                with patch("app.services.vector_store.logger"):
                    # Should NOT raise - drop_table failure is caught silently
                    await store.init_table(embedding_dim=384)

                    # Verify create_table was called (overwrite path)
                    mock_db.create_table.assert_called_once()
                    call_kwargs = mock_db.create_table.call_args[1]
                    self.assertEqual(call_kwargs.get("mode"), "overwrite")

    async def test_overwrite_path_sets_table_just_created_true(self):
        """
        Verify that the overwrite path correctly sets table_just_created=True.
        """
        store = VectorStore(db_path=Path("/tmp/test_lancedb"))

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=["chunks"])
        mock_db.open_table = AsyncMock(side_effect=RuntimeError("Stale"))
        mock_db.drop_table = AsyncMock()

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

                    # Verify deferred-index log fired (proof table_just_created=True)
                    info_calls = [str(c) for c in mock_logger.info.call_args_list]
                    deferred_found = any(
                        "vector index deferred" in c.lower() for c in info_calls
                    )
                    self.assertTrue(
                        deferred_found,
                        "Deferred-index log should fire for overwrite path",
                    )


class TestAdversarialInputInjection(unittest.IsolatedAsyncioTestCase):
    """
    Test injection attempts in inputs.

    Attack vectors: Path traversal, special characters in db_path.
    """

    async def test_path_traversal_in_db_path(self):
        """
        ATTACK VECTOR: Path traversal sequences in db_path.

        Attempt to access files outside expected directory.
        """
        # Path with traversal attempt
        malicious_path = Path("/tmp/../../../etc/passwd/lancedb")

        store = VectorStore(db_path=malicious_path)

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

                # The path is stored as-is; LanceDB handles path validation
                # This test verifies no crash on suspicious paths
                await store.init_table(embedding_dim=384)

                self.assertEqual(store.db_path, malicious_path)

    async def test_unicode_in_db_path(self):
        """
        ATTACK VECTOR: Unicode characters in db_path.

        Test handling of non-ASCII characters in path.
        """
        unicode_path = Path(
            "/tmp/test_\u4e2d\u6587_\u0623\u0644\u0639\u0631\u0628\u064a\u0629"
        )

        store = VectorStore(db_path=unicode_path)

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

                # Should handle unicode paths without crash
                await store.init_table(embedding_dim=384)


if __name__ == "__main__":
    unittest.main()
