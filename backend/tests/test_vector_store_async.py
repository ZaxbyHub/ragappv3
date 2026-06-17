"""
Tests for async LanceDB vector store implementation.

This module tests the async methods of the VectorStore class:
- connect: Async connection to LanceDB
- init_table: Initialize or open the 'chunks' table
- search: Search for similar chunks by embedding
- add_chunks: Add chunk records to the vector store
- delete_by_file: Delete all chunks for a given file_id
- delete_by_vault: Delete all chunks for a given vault_id
"""

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

# Import the module under test
from app.services.vector_store import (
    VectorStore,
    VectorStoreValidationError,
)


class TestVectorStoreAsync(unittest.TestCase):
    """Test cases for async LanceDB VectorStore implementation."""

    def setUp(self):
        """Set up test environment with temporary directory."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test_lancedb"
        self.embedding_dim = 384  # Small dimension for testing

    def tearDown(self):
        """Clean up temporary directory."""
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def create_vector_store(self) -> VectorStore:
        """Helper to create a VectorStore instance with test path."""
        return VectorStore(db_path=self.db_path)

    def create_test_records(self, count: int = 3) -> List[Dict[str, Any]]:
        """Helper to create test chunk records."""
        records = []
        for i in range(count):
            records.append({
                "id": f"file1_{i}",
                "text": f"This is test chunk {i}",
                "file_id": "file1",
                "vault_id": "1",
                "chunk_index": i,
                "chunk_scale": "default",
                "metadata": json.dumps({"source": "test"}),
                "embedding": np.random.randn(self.embedding_dim).tolist(),
            })
        return records


class TestVectorStoreConnect(TestVectorStoreAsync):
    """Test cases for VectorStore.connect() method."""

    @pytest.mark.asyncio
    async def test_connect_success(self):
        """Test successful async connection to LanceDB."""
        store = self.create_vector_store()

        result = await store.connect()

        # Should return self for chaining
        self.assertEqual(result, store)
        # Should have db connection
        self.assertIsNotNone(store.db)

    @pytest.mark.asyncio
    async def test_connect_creates_directory(self):
        """Test that connect creates the database directory if it doesn't exist."""
        store = self.create_vector_store()

        await store.connect()

        # Directory should be created
        self.assertTrue(self.db_path.exists() or self.db_path.parent.exists())

    @pytest.mark.asyncio
    async def test_connect_idempotent(self):
        """Test that multiple connects don't cause errors."""
        store = self.create_vector_store()

        await store.connect()
        first_db = store.db

        await store.connect()
        second_db = store.db

        # Both should work
        self.assertIsNotNone(first_db)
        self.assertIsNotNone(second_db)


class TestVectorStoreInitTable(TestVectorStoreAsync):
    """Test cases for VectorStore.init_table() method."""

    @pytest.mark.asyncio
    async def test_init_table_creates_new_table(self):
        """Test that init_table creates a new 'chunks' table."""
        store = self.create_vector_store()

        result = await store.init_table(embedding_dim=self.embedding_dim)

        # Should return self for chaining
        self.assertEqual(result, store)
        # Should have table
        self.assertIsNotNone(store.table)
        # Table should be named 'chunks'
        table_names = await store.db.table_names()
        self.assertIn("chunks", table_names)

    @pytest.mark.asyncio
    async def test_init_table_opens_existing_table(self):
        """Test that init_table opens an existing 'chunks' table."""
        store = self.create_vector_store()

        # First initialization
        await store.init_table(embedding_dim=self.embedding_dim)

        # Second initialization should open existing table
        store2 = self.create_vector_store()
        await store2.init_table(embedding_dim=self.embedding_dim)

        self.assertIsNotNone(store2.table)

    @pytest.mark.asyncio
    async def test_init_table_with_auto_connect(self):
        """Test that init_table auto-connects if not connected."""
        store = self.create_vector_store()

        # Should not be connected initially
        self.assertIsNone(store.db)

        # init_table should auto-connect
        await store.init_table(embedding_dim=self.embedding_dim)

        self.assertIsNotNone(store.db)
        self.assertIsNotNone(store.table)

    @pytest.mark.asyncio
    async def test_init_table_stores_embedding_dim(self):
        """Test that init_table stores the embedding dimension."""
        store = self.create_vector_store()

        await store.init_table(embedding_dim=self.embedding_dim)

        self.assertEqual(store._embedding_dim, self.embedding_dim)


class TestVectorStoreAddChunks(TestVectorStoreAsync):
    """Test cases for VectorStore.add_chunks() method."""

    @pytest.mark.asyncio
    async def test_add_chunks_success(self):
        """Test successfully adding chunk records."""
        store = self.create_vector_store()
        await store.init_table(embedding_dim=self.embedding_dim)

        records = self.create_test_records(count=3)

        # Should not raise
        await store.add_chunks(records)

        # Verify records were added by counting
        count = await store.table.count_rows()
        self.assertEqual(count, 3)

    def test_add_chunks_count_by_file_sees_inserted_rows(self):
        """count_by_file must observe rows after a successful add_chunks call."""
        async def run_test():
            store = self.create_vector_store()
            rows = []
            table = MagicMock()

            async def add_records(records):
                rows.extend(records)

            async def count_rows(filter_expr=""):
                if "file1" in filter_expr:
                    return sum(row["file_id"] == "file1" for row in rows)
                if "file2" in filter_expr:
                    return sum(row["file_id"] == "file2" for row in rows)
                if "missing" in filter_expr:
                    return 0
                return len(rows)

            table.add = AsyncMock(side_effect=add_records)
            table.count_rows = AsyncMock(side_effect=count_rows)
            table.list_indices = AsyncMock(return_value=[])
            store.db = MagicMock()
            store.table = table
            store._get_expected_embedding_dim = AsyncMock(
                return_value=self.embedding_dim
            )

            records = self.create_test_records(count=3)
            records.append({
                "id": "file2_0",
                "text": "This is another file",
                "file_id": "file2",
                "vault_id": "1",
                "chunk_index": 0,
                "chunk_scale": "default",
                "metadata": json.dumps({"source": "test"}),
                "embedding": np.random.randn(self.embedding_dim).tolist(),
            })

            with patch("app.services.vector_store.settings") as mock_settings:
                mock_settings.optimize_mode = "manual"
                mock_settings.embedding_dim = self.embedding_dim
                mock_settings.vector_metric = "cosine"
                mock_settings.write_lock_timeout_seconds = 30
                mock_settings.search_semaphore_timeout_seconds = 30.0
                await store.add_chunks(records)

            self.assertEqual(await store.count_by_file("file1"), 3)
            self.assertEqual(await store.count_by_file("file2"), 1)
            self.assertEqual(await store.count_by_file("missing"), 0)

        asyncio.run(run_test())

    def test_periodic_optimize_mode_still_exposes_inserted_rows(self):
        """Periodic optimize must not hide newly added rows from count_by_file."""
        async def run_test():
            store = self.create_vector_store()
            rows = []
            table = MagicMock()

            async def add_records(records):
                rows.extend(records)

            async def count_rows(filter_expr=""):
                if "file1" in filter_expr:
                    return sum(row["file_id"] == "file1" for row in rows)
                return len(rows)

            table.add = AsyncMock(side_effect=add_records)
            table.count_rows = AsyncMock(side_effect=count_rows)
            table.list_indices = AsyncMock(return_value=[])
            table.optimize = AsyncMock(return_value=None)
            store.db = MagicMock()
            store.table = table
            store._get_expected_embedding_dim = AsyncMock(
                return_value=self.embedding_dim
            )

            with patch("app.services.vector_store.settings") as mock_settings:
                mock_settings.optimize_mode = "periodic"
                mock_settings.optimize_interval_chunks = 5000
                mock_settings.embedding_dim = self.embedding_dim
                mock_settings.vector_metric = "cosine"
                mock_settings.write_lock_timeout_seconds = 30

                await store.add_chunks(self.create_test_records(count=2))

            self.assertEqual(await store.count_by_file("file1"), 2)
            table.optimize.assert_not_awaited()

        asyncio.run(run_test())

    def test_existing_index_search_finds_new_insert(self):
        """A post-index insert remains discoverable by normal search."""
        async def run_test():
            embedding_dim = 8
            store = self.create_vector_store()
            rows = []
            table = MagicMock()

            async def add_records(records):
                rows.extend(records)

            async def count_rows(filter_expr=""):
                if "doc_b" in filter_expr:
                    return sum(row["file_id"] == "doc_b" for row in rows)
                return len(rows)

            def search_records(_embedding, query_type="vector"):
                query = MagicMock()
                query.where.return_value = query
                query.limit.return_value.to_list = AsyncMock(
                    return_value=[row for row in rows if row["file_id"] == "doc_b"]
                    or rows[:1]
                )
                return query

            table.add = AsyncMock(side_effect=add_records)
            table.count_rows = AsyncMock(side_effect=count_rows)
            table.list_indices = AsyncMock(
                side_effect=[
                    [],
                    [type("Idx", (), {"name": "embedding_idx"})()],
                    [type("Idx", (), {"name": "embedding_idx"})()],
                ]
            )
            table.create_index = AsyncMock(return_value=None)
            table.search = AsyncMock(side_effect=search_records)
            store.db = MagicMock()
            store.table = table
            store._get_expected_embedding_dim = AsyncMock(return_value=embedding_dim)

            base_embedding = [1.0] + [0.0] * (embedding_dim - 1)
            base_records = [
                {
                    "id": f"doc_a_{i}",
                    "text": f"Document A filler chunk {i}",
                    "file_id": "doc_a",
                    "vault_id": "1",
                    "chunk_index": i,
                    "chunk_scale": "default",
                    "metadata": json.dumps({"source": "test"}),
                    "embedding": base_embedding,
                }
                for i in range(256)
            ]
            doc_b_embedding = [0.0, 1.0] + [0.0] * (embedding_dim - 2)
            doc_b_record = {
                "id": "doc_b_0",
                "text": "Document B unique freshness term",
                "file_id": "doc_b",
                "vault_id": "1",
                "chunk_index": 0,
                "chunk_scale": "default",
                "metadata": json.dumps({"source": "test"}),
                "embedding": doc_b_embedding,
            }

            with patch("app.services.vector_store.settings") as mock_settings, \
                 patch("app.services.vector_store.IvfPq") as mock_ivfpq:
                mock_settings.optimize_mode = "manual"
                mock_settings.optimize_interval_chunks = 5000
                mock_settings.embedding_dim = embedding_dim
                mock_settings.vector_metric = "cosine"
                mock_settings.write_lock_timeout_seconds = 30
                mock_settings.search_semaphore_timeout_seconds = 30.0
                mock_settings.vector_search_concurrency = 4
                mock_settings.multi_scale_indexing_enabled = False
                mock_settings.multi_scale_chunk_sizes = ""
                mock_ivfpq.return_value = MagicMock()

                await store.add_chunks(base_records)
                await store.add_chunks([doc_b_record])
                results = await store.search(
                    embedding=doc_b_embedding,
                    limit=5,
                    hybrid=False,
                )

            self.assertIn("doc_b_0", [row["id"] for row in results])
            self.assertEqual(table.create_index.await_count, 2)

        asyncio.run(run_test())

    @pytest.mark.asyncio
    async def test_add_chunks_empty_list(self):
        """Test adding empty list of records."""
        store = self.create_vector_store()
        await store.init_table(embedding_dim=self.embedding_dim)

        # Should not raise
        await store.add_chunks([])

        # Count should be 0
        count = await store.table.count_rows()
        self.assertEqual(count, 0)

    @pytest.mark.asyncio
    async def test_add_chunks_missing_required_fields(self):
        """Test validation of required fields."""
        store = self.create_vector_store()
        await store.init_table(embedding_dim=self.embedding_dim)

        # Missing 'text' field
        invalid_record = {
            "id": "file1_0",
            "file_id": "file1",
            "chunk_index": 0,
            "embedding": np.random.randn(self.embedding_dim).tolist(),
        }

        with self.assertRaises(VectorStoreValidationError) as ctx:
            await store.add_chunks([invalid_record])

        self.assertIn("missing required fields", str(ctx.exception).lower())

    @pytest.mark.asyncio
    async def test_add_chunks_wrong_embedding_dimension(self):
        """Test validation of embedding dimension."""
        store = self.create_vector_store()
        await store.init_table(embedding_dim=self.embedding_dim)

        # Wrong embedding dimension
        invalid_record = {
            "id": "file1_0",
            "text": "Test text",
            "file_id": "file1",
            "chunk_index": 0,
            "embedding": np.random.randn(100).tolist(),  # Wrong dimension
        }

        with self.assertRaises(VectorStoreValidationError) as ctx:
            await store.add_chunks([invalid_record])

        self.assertIn("dimension mismatch", str(ctx.exception).lower())

    @pytest.mark.asyncio
    async def test_add_chunks_numpy_array_embedding(self):
        """Test that numpy array embeddings are converted to lists."""
        store = self.create_vector_store()
        await store.init_table(embedding_dim=self.embedding_dim)

        records = [{
            "id": "file1_0",
            "text": "Test text",
            "file_id": "file1",
            "chunk_index": 0,
            "embedding": np.random.randn(self.embedding_dim),  # numpy array
        }]

        # Should convert numpy array to list
        await store.add_chunks(records)

        count = await store.table.count_rows()
        self.assertEqual(count, 1)

    @pytest.mark.asyncio
    async def test_add_chunks_invalid_embedding_type(self):
        """Test validation of embedding type."""
        store = self.create_vector_store()
        await store.init_table(embedding_dim=self.embedding_dim)

        invalid_record = {
            "id": "file1_0",
            "text": "Test text",
            "file_id": "file1",
            "chunk_index": 0,
            "embedding": "not_a_valid_embedding",  # Invalid type
        }

        with self.assertRaises(VectorStoreValidationError) as ctx:
            await store.add_chunks([invalid_record])

        self.assertIn("must be a list or numpy array", str(ctx.exception))

    @pytest.mark.asyncio
    async def test_add_chunks_invalid_sparse_embedding(self):
        """Test validation of sparse_embedding JSON format."""
        store = self.create_vector_store()
        await store.init_table(embedding_dim=self.embedding_dim)

        invalid_record = {
            "id": "file1_0",
            "text": "Test text",
            "file_id": "file1",
            "chunk_index": 0,
            "embedding": np.random.randn(self.embedding_dim).tolist(),
            "sparse_embedding": "not valid json",
        }

        with self.assertRaises(VectorStoreValidationError) as ctx:
            await store.add_chunks([invalid_record])

        self.assertIn("valid json", str(ctx.exception).lower())

    # ─────────────────────────────────────────────────────────────────
    # 6.2 vault_id fallback removal — regression tests
    # ─────────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_add_chunks_missing_vault_id_raises_key_error(self):
        """Missing vault_id must raise KeyError, not default to '1'.

        Previously add_chunks used record.get('vault_id', '1') which silently
        masked missing vault_id.  After the fallback removal it uses direct
        dict access record['vault_id'], so a missing vault_id now raises
        KeyError.
        """
        store = self.create_vector_store()
        await store.init_table(embedding_dim=self.embedding_dim)

        # Record missing vault_id but otherwise valid
        record_without_vault_id = {
            "id": "file1_0",
            "text": "Test text",
            "file_id": "file1",
            "chunk_index": 0,
            "embedding": np.random.randn(self.embedding_dim).tolist(),
            # vault_id intentionally omitted
        }

        with self.assertRaises(KeyError):
            await store.add_chunks([record_without_vault_id])

    @pytest.mark.asyncio
    async def test_add_chunks_uses_direct_vault_id_access_not_get(self):
        """vault_id must be read via record['vault_id'], not record.get().

        This is a behavioural contract: the caller is responsible for
        providing vault_id.  No implicit default of '1' is applied inside
        add_chunks.
        """
        store = self.create_vector_store()
        await store.init_table(embedding_dim=self.embedding_dim)

        # Record with an explicit non-default vault_id value
        explicit_vault_id = "explicit-vault-42"
        record = {
            "id": "file1_0",
            "text": "Test text",
            "file_id": "file1",
            "vault_id": explicit_vault_id,  # intentionally non-default
            "chunk_index": 0,
            "metadata": "{}",
            "embedding": np.random.randn(self.embedding_dim).tolist(),
        }

        await store.add_chunks([record])

        # Verify the stored vault_id is exactly what we passed, not "1"
        rows = await store.table.search(
            np.random.randn(self.embedding_dim).tolist()
        ).limit(10).to_list()

        stored = next((r for r in rows if r["id"] == "file1_0"), None)
        self.assertIsNotNone(stored, "Record was not stored")
        self.assertEqual(
            stored["vault_id"],
            explicit_vault_id,
            "vault_id should be the explicit value, not defaulted to '1'",
        )

    @pytest.mark.asyncio
    async def test_add_chunks_before_init_table(self):
        """Test that add_chunks raises if table not initialized."""
        store = self.create_vector_store()
        await store.connect()  # Connect but don't init table

        records = self.create_test_records(count=1)

        with self.assertRaises(RuntimeError) as ctx:
            await store.add_chunks(records)

        self.assertIn("table not initialized", str(ctx.exception).lower())


class TestVectorStoreSearch(TestVectorStoreAsync):
    """Test cases for VectorStore.search() method."""

    @pytest.mark.asyncio
    async def test_search_returns_results(self):
        """Test basic search functionality."""
        store = self.create_vector_store()
        await store.init_table(embedding_dim=self.embedding_dim)

        # Add test records
        records = self.create_test_records(count=3)
        await store.add_chunks(records)

        # Search with query embedding
        query_embedding = records[0]["embedding"]
        results = await store.search(embedding=query_embedding, limit=2)

        # Should return results
        self.assertIsInstance(results, list)
        self.assertGreater(len(results), 0)

    @pytest.mark.asyncio
    async def test_search_empty_table(self):
        """Test search on empty table returns empty list."""
        store = self.create_vector_store()
        await store.init_table(embedding_dim=self.embedding_dim)

        query_embedding = np.random.randn(self.embedding_dim).tolist()
        results = await store.search(embedding=query_embedding)

        # Should return empty list, not error
        self.assertEqual(results, [])

    @pytest.mark.asyncio
    async def test_search_no_table_exists(self):
        """Test search when no table exists returns empty list."""
        store = self.create_vector_store()
        await store.connect()  # Connect but don't create table

        query_embedding = np.random.randn(self.embedding_dim).tolist()
        results = await store.search(embedding=query_embedding)

        # Should return empty list gracefully
        self.assertEqual(results, [])

    @pytest.mark.asyncio
    async def test_search_with_vault_filter(self):
        """Test search with vault_id filter."""
        store = self.create_vector_store()
        await store.init_table(embedding_dim=self.embedding_dim)

        # Add records with different vault_ids
        records_vault1 = [{
            "id": f"v1_{i}",
            "text": f"Vault 1 chunk {i}",
            "file_id": "file1",
            "vault_id": "1",
            "chunk_index": i,
            "embedding": np.random.randn(self.embedding_dim).tolist(),
        } for i in range(2)]

        records_vault2 = [{
            "id": f"v2_{i}",
            "text": f"Vault 2 chunk {i}",
            "file_id": "file2",
            "vault_id": "2",
            "chunk_index": i,
            "embedding": np.random.randn(self.embedding_dim).tolist(),
        } for i in range(2)]

        await store.add_chunks(records_vault1 + records_vault2)

        # Search with vault filter
        query_embedding = records_vault1[0]["embedding"]
        results = await store.search(
            embedding=query_embedding,
            vault_id="1",
            limit=10
        )

        # All results should be from vault 1
        for result in results:
            self.assertEqual(result.get("vault_id"), "1")

    @pytest.mark.asyncio
    async def test_search_respects_limit(self):
        """Test that search respects the limit parameter."""
        store = self.create_vector_store()
        await store.init_table(embedding_dim=self.embedding_dim)

        # Add many records
        records = self.create_test_records(count=10)
        await store.add_chunks(records)

        query_embedding = records[0]["embedding"]

        # Test different limits
        for limit in [1, 3, 5]:
            results = await store.search(
                embedding=query_embedding,
                limit=limit
            )
            self.assertLessEqual(len(results), limit)


class TestVectorStoreDelete(TestVectorStoreAsync):
    """Test cases for VectorStore delete operations."""

    @pytest.mark.asyncio
    async def test_delete_by_file_success(self):
        """Test deleting chunks by file_id."""
        store = self.create_vector_store()
        await store.init_table(embedding_dim=self.embedding_dim)

        # Add records for different files
        records_file1 = [{
            "id": f"file1_{i}",
            "text": f"File 1 chunk {i}",
            "file_id": "file1",
            "chunk_index": i,
            "embedding": np.random.randn(self.embedding_dim).tolist(),
        } for i in range(3)]

        records_file2 = [{
            "id": f"file2_{i}",
            "text": f"File 2 chunk {i}",
            "file_id": "file2",
            "chunk_index": i,
            "embedding": np.random.randn(self.embedding_dim).tolist(),
        } for i in range(2)]

        await store.add_chunks(records_file1 + records_file2)

        # Delete file1 chunks
        deleted_count = await store.delete_by_file("file1")

        # Should report correct count
        self.assertEqual(deleted_count, 3)

        # Verify deletion
        remaining = await store.table.count_rows()
        self.assertEqual(remaining, 2)

    @pytest.mark.asyncio
    async def test_delete_by_file_nonexistent(self):
        """Test deleting chunks for non-existent file_id."""
        store = self.create_vector_store()
        await store.init_table(embedding_dim=self.embedding_dim)

        # Add some records
        records = self.create_test_records(count=2)
        await store.add_chunks(records)

        # Delete non-existent file
        deleted_count = await store.delete_by_file("nonexistent")

        # Should return 0
        self.assertEqual(deleted_count, 0)

        # Original records should still exist
        remaining = await store.table.count_rows()
        self.assertEqual(remaining, 2)

    @pytest.mark.asyncio
    async def test_delete_by_file_no_table(self):
        """Test delete_by_file when no table exists."""
        store = self.create_vector_store()
        await store.connect()  # Connect but don't create table

        # Should return 0 gracefully
        deleted_count = await store.delete_by_file("file1")
        self.assertEqual(deleted_count, 0)

    @pytest.mark.asyncio
    async def test_delete_by_vault_success(self):
        """Test deleting chunks by vault_id."""
        store = self.create_vector_store()
        await store.init_table(embedding_dim=self.embedding_dim)

        # Add records for different vaults
        records_vault1 = [{
            "id": f"v1_{i}",
            "text": f"Vault 1 chunk {i}",
            "file_id": "file1",
            "vault_id": "1",
            "chunk_index": i,
            "embedding": np.random.randn(self.embedding_dim).tolist(),
        } for i in range(3)]

        records_vault2 = [{
            "id": f"v2_{i}",
            "text": f"Vault 2 chunk {i}",
            "file_id": "file2",
            "vault_id": "2",
            "chunk_index": i,
            "embedding": np.random.randn(self.embedding_dim).tolist(),
        } for i in range(2)]

        await store.add_chunks(records_vault1 + records_vault2)

        # Delete vault 1 chunks
        deleted_count = await store.delete_by_vault("1")

        # Should report correct count
        self.assertEqual(deleted_count, 3)

        # Verify deletion
        remaining = await store.table.count_rows()
        self.assertEqual(remaining, 2)

    @pytest.mark.asyncio
    async def test_delete_by_vault_nonexistent(self):
        """Test deleting chunks for non-existent vault_id."""
        store = self.create_vector_store()
        await store.init_table(embedding_dim=self.embedding_dim)

        # Add some records
        records = self.create_test_records(count=2)
        await store.add_chunks(records)

        # Delete non-existent vault
        deleted_count = await store.delete_by_vault("999")

        # Should return 0
        self.assertEqual(deleted_count, 0)

        # Original records should still exist
        remaining = await store.table.count_rows()
        self.assertEqual(remaining, 2)

    @pytest.mark.asyncio
    async def test_delete_by_vault_no_table(self):
        """Test delete_by_vault when no table exists."""
        store = self.create_vector_store()
        await store.connect()  # Connect but don't create table

        # Should return 0 gracefully
        deleted_count = await store.delete_by_vault("1")
        self.assertEqual(deleted_count, 0)


class TestVectorStoreEdgeCases(TestVectorStoreAsync):
    """Test edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_close_clears_connection(self):
        """Test that close() clears the connection."""
        store = self.create_vector_store()
        await store.init_table(embedding_dim=self.embedding_dim)

        # Should have connection
        self.assertIsNotNone(store.db)
        self.assertIsNotNone(store.table)

        # Close
        store.close()

        # Should be cleared
        self.assertIsNone(store.db)
        self.assertIsNone(store.table)

    @pytest.mark.asyncio
    async def test_get_stats_empty_table(self):
        """Test get_stats on empty table."""
        store = self.create_vector_store()
        await store.init_table(embedding_dim=self.embedding_dim)

        stats = await store.get_stats()

        self.assertEqual(stats["total_chunks"], 0)
        self.assertEqual(stats["embedding_dim"], self.embedding_dim)

    @pytest.mark.asyncio
    async def test_get_stats_with_data(self):
        """Test get_stats with data."""
        store = self.create_vector_store()
        await store.init_table(embedding_dim=self.embedding_dim)

        records = self.create_test_records(count=5)
        await store.add_chunks(records)

        stats = await store.get_stats()

        self.assertEqual(stats["total_chunks"], 5)
        self.assertEqual(stats["embedding_dim"], self.embedding_dim)

    @pytest.mark.asyncio
    async def test_get_stats_no_table(self):
        """Test get_stats when table not initialized."""
        store = self.create_vector_store()

        stats = await store.get_stats()

        self.assertEqual(stats["total_chunks"], 0)
        self.assertEqual(stats["embedding_dim"], None)

    @pytest.mark.asyncio
    async def test_get_chunks_by_uid_empty_list(self):
        """Test get_chunks_by_uid with empty list."""
        store = self.create_vector_store()
        await store.init_table(embedding_dim=self.embedding_dim)

        results = await store.get_chunks_by_uid([])

        self.assertEqual(results, [])

    @pytest.mark.asyncio
    async def test_get_chunks_by_uid_no_table(self):
        """Test get_chunks_by_uid when table not initialized."""
        store = self.create_vector_store()

        results = await store.get_chunks_by_uid(["file1_0"])

        self.assertEqual(results, [])


class TestVectorStoreMigrationMethods(TestVectorStoreAsync):
    """Test migration methods."""

    @pytest.mark.asyncio
    async def test_migrate_add_vault_id_no_table(self):
        """Test migrate_add_vault_id when no table exists."""
        store = self.create_vector_store()
        await store.connect()

        result = await store.migrate_add_vault_id()

        self.assertEqual(result, 0)

    @pytest.mark.asyncio
    async def test_migrate_add_chunk_scale_no_table(self):
        """Test migrate_add_chunk_scale when no table exists."""
        store = self.create_vector_store()
        await store.connect()

        result = await store.migrate_add_chunk_scale()

        self.assertEqual(result, 0)

    @pytest.mark.asyncio
    async def test_migrate_add_sparse_embedding_no_table(self):
        """Test migrate_add_sparse_embedding when no table exists."""
        store = self.create_vector_store()
        await store.connect()

        result = await store.migrate_add_sparse_embedding()

        self.assertEqual(result, 0)


class TestVectorStoreValidation(TestVectorStoreAsync):
    """Test validation methods."""

    @pytest.mark.asyncio
    async def test_validate_schema_no_table(self):
        """Test validate_schema when no table exists."""
        store = self.create_vector_store()
        await store.connect()

        result = await store.validate_schema(
            embedding_model_id="test-model",
            embedding_dim=self.embedding_dim
        )

        self.assertEqual(result["table_exists"], False)
        self.assertEqual(result["expected_dim"], self.embedding_dim)

    @pytest.mark.asyncio
    async def test_validate_schema_with_table(self):
        """Test validate_schema with existing table."""
        store = self.create_vector_store()
        await store.init_table(embedding_dim=self.embedding_dim)

        result = await store.validate_schema(
            embedding_model_id="test-model",
            embedding_dim=self.embedding_dim
        )

        self.assertEqual(result["table_exists"], True)
        self.assertEqual(result["expected_dim"], self.embedding_dim)
        self.assertEqual(result["actual_dim"], self.embedding_dim)

    @pytest.mark.asyncio
    async def test_validate_schema_dimension_mismatch(self):
        """Test validate_schema detects dimension mismatch."""
        store = self.create_vector_store()
        await store.init_table(embedding_dim=self.embedding_dim)

        with self.assertRaises(VectorStoreValidationError) as ctx:
            await store.validate_schema(
                embedding_model_id="test-model",
                embedding_dim=768  # Different dimension
            )

        self.assertIn("dimension changed", str(ctx.exception).lower())


class TestVectorStoreHelperMethods(TestVectorStoreAsync):
    """Test helper methods."""

    def test_generate_probe_embedding(self):
        """Test _generate_probe_embedding generates correct dimension."""
        store = self.create_vector_store()

        embedding = store._generate_probe_embedding("test", dim=128)

        self.assertEqual(len(embedding), 128)
        # Should be normalized (approximately)
        magnitude = sum(x*x for x in embedding) ** 0.5
        self.assertAlmostEqual(magnitude, 1.0, places=5)

    def test_generate_probe_embedding_deterministic(self):
        """Test that same text generates same embedding."""
        store = self.create_vector_store()

        embedding1 = store._generate_probe_embedding("test", dim=64)
        embedding2 = store._generate_probe_embedding("test", dim=64)

        self.assertEqual(embedding1, embedding2)

    def test_generate_probe_embedding_different_texts(self):
        """Test that different texts generate different embeddings."""
        store = self.create_vector_store()

        embedding1 = store._generate_probe_embedding("text1", dim=64)
        embedding2 = store._generate_probe_embedding("text2", dim=64)

        self.assertNotEqual(embedding1, embedding2)


if __name__ == "__main__":
    unittest.main()
