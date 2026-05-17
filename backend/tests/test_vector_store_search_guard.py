"""
Tests for None guard behavior in VectorStore search and delete operations.

This module specifically tests the None guard checks added to handle cases where
self.table is None, preventing LanceDB operation errors:
1. _search_single_scale() returns [] when self.table is None
2. search() single-scale path returns [] when self.table is None
3. delete_by_file() returns 0 when self.table is None
4. delete_by_vault() returns 0 when self.table is None
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from app.services.vector_store import VectorStore


class _FakeSearchQuery:
    def __init__(self, rows):
        self._rows = rows
        self.filters = []
        self.limit_value = None

    def where(self, filter_expr):
        self.filters.append(filter_expr)
        return self

    def limit(self, limit):
        self.limit_value = limit
        return self

    async def to_list(self):
        if self.limit_value is None:
            return list(self._rows)
        return list(self._rows[: self.limit_value])


class _FakeLanceTable:
    def __init__(self, dense_rows, fts_rows):
        self.dense_query = _FakeSearchQuery(dense_rows)
        self.fts_query = _FakeSearchQuery(fts_rows)

    async def search(self, query, query_type):
        if query_type == "vector":
            return self.dense_query
        if query_type == "fts":
            return self.fts_query
        raise AssertionError(f"unexpected query_type: {query_type}")

    async def list_indices(self):
        return []

    async def count_rows(self):
        return 1


class TestVectorStoreNoneGuards(unittest.TestCase):
    """Test cases for None guard behavior in VectorStore."""

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


class TestSearchSingleScaleNoneGuard(TestVectorStoreNoneGuards):
    """Test cases for _search_single_scale() None guard."""

    @pytest.mark.asyncio
    async def test_search_single_scale_none_table(self):
        """
        Test that _search_single_scale returns [] when self.table is None.

        This tests the None guard at the start of _search_single_scale method.
        """
        store = self.create_vector_store()

        # Explicitly set table to None (simulating uninitialized state)
        store.table = None

        # Call _search_single_scale with dummy params
        result = await store._search_single_scale(
            embedding=[0.0] * self.embedding_dim,
            scale="default",
            fetch_k=10,
        )

        # Should return empty list due to None guard
        self.assertEqual(result, [])

    @pytest.mark.asyncio
    async def test_search_single_scale_none_table_with_vault_filter(self):
        """
        Test that _search_single_scale returns [] when table is None, even with vault filter.
        """
        store = self.create_vector_store()
        store.table = None

        result = await store._search_single_scale(
            embedding=[0.0] * self.embedding_dim,
            scale="default",
            fetch_k=10,
            vault_id="test_vault",
        )

        self.assertEqual(result, [])

    @pytest.mark.asyncio
    async def test_search_single_scale_none_table_with_query_text(self):
        """
        Test that _search_single_scale returns [] when table is None, even with hybrid query.
        """
        store = self.create_vector_store()
        store.table = None

        result = await store._search_single_scale(
            embedding=[0.0] * self.embedding_dim,
            scale="default",
            fetch_k=10,
            query_text="test query",
            hybrid=True,
        )

        self.assertEqual(result, [])


class TestSearchNoneGuard(TestVectorStoreNoneGuards):
    """Test cases for search() None guard in single-scale path."""

    @pytest.mark.asyncio
    async def test_search_none_table_single_scale(self):
        """
        Test that search() returns [] when self.table is None in single-scale path.

        This tests the None guard at line 523-524 in vector_store.py.
        We need to disable multi_scale_indexing to hit the single-scale path.
        """
        store = self.create_vector_store()

        # Set table to None directly
        store.table = None

        # Mock the db to prevent connection attempts
        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=[])  # No chunks table
        store.db = mock_db

        # Disable multi-scale to hit single-scale path
        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = False
            mock_settings.multi_scale_chunk_sizes = "512"

            # Call search with dummy embedding
            result = await store.search(
                embedding=[0.0] * self.embedding_dim,
                limit=10,
            )

        # Should return empty list due to None guard
        self.assertEqual(result, [])

    @pytest.mark.asyncio
    async def test_search_none_table_single_scale_with_vault(self):
        """
        Test that search() returns [] when table is None, even with vault filter.
        """
        store = self.create_vector_store()
        store.table = None

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=[])
        store.db = mock_db

        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = False
            mock_settings.multi_scale_chunk_sizes = "512"

            result = await store.search(
                embedding=[0.0] * self.embedding_dim,
                limit=10,
                vault_id="test_vault",
            )

        self.assertEqual(result, [])


@pytest.mark.asyncio
async def test_single_scale_hybrid_search_no_rrf_unboundlocal(monkeypatch, tmp_path):
    """
    Regression: the multi-scale branch used to import rrf_fuse locally, so
    Python treated rrf_fuse as a search() local and the single-scale hybrid
    branch raised UnboundLocalError before fusion could return results.
    """
    dense_rows = [
        {
            "id": "dense-1",
            "text": "dense result",
            "file_id": "file-dense",
            "metadata": {},
            "_distance": 0.1,
        }
    ]
    fts_rows = [
        {
            "id": "fts-1",
            "text": "fts result",
            "file_id": "file-fts",
            "metadata": {},
            "_distance": 0.2,
        }
    ]

    store = VectorStore(db_path=tmp_path / "test_lancedb")
    store.db = MagicMock()
    store.table = _FakeLanceTable(dense_rows, fts_rows)
    monkeypatch.setattr(store, "_maybe_create_vector_index", AsyncMock())
    monkeypatch.setattr(
        "app.services.vector_store.settings.multi_scale_indexing_enabled",
        False,
    )
    monkeypatch.setattr(
        "app.services.vector_store.settings.hybrid_rrf_k",
        60,
    )
    monkeypatch.setattr(
        "app.services.vector_store.settings.rrf_legacy_mode",
        False,
    )

    results = await store.search(
        embedding=[0.1] * 384,
        limit=5,
        query_text="hybrid query",
        hybrid=True,
        hybrid_alpha=0.5,
    )

    assert [row["id"] for row in results] == ["dense-1", "fts-1"]
    assert all(row["_fts_status"] == "ok" for row in results)
    assert all("_rrf_score" in row for row in results)


class TestDeleteByFileNoneGuard(TestVectorStoreNoneGuards):
    """Test cases for delete_by_file() None guard."""

    @pytest.mark.asyncio
    async def test_delete_by_file_none_table(self):
        """
        Test that delete_by_file returns 0 when self.table is None.

        This tests the None guard at line 617-618 in vector_store.py.
        """
        store = self.create_vector_store()
        store.table = None

        # Mock db to prevent connection attempts
        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=["chunks"])  # Table exists in DB
        store.db = mock_db

        # Call delete_by_file
        result = await store.delete_by_file("test_file_id")

        # Should return 0 due to None guard
        self.assertEqual(result, 0)

    @pytest.mark.asyncio
    async def test_delete_by_file_none_table_no_chunks_table(self):
        """
        Test that delete_by_file returns 0 when table doesn't exist.
        """
        store = self.create_vector_store()

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=[])  # No chunks table
        store.db = mock_db

        result = await store.delete_by_file("test_file_id")

        # Should return 0 when table doesn't exist
        self.assertEqual(result, 0)


class TestDeleteByVaultNoneGuard(TestVectorStoreNoneGuards):
    """Test cases for delete_by_vault() None guard."""

    @pytest.mark.asyncio
    async def test_delete_by_vault_none_table(self):
        """
        Test that delete_by_vault returns 0 when self.table is None.

        This tests the None guard at line 666-667 in vector_store.py.
        """
        store = self.create_vector_store()
        store.table = None

        # Mock db to simulate table exists but couldn't be opened
        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=["chunks"])  # Table exists
        # Make open_table raise to simulate failure, leaving table as None
        mock_db.open_table = AsyncMock(side_effect=RuntimeError("Table open failed"))
        store.db = mock_db

        # Call delete_by_vault
        result = await store.delete_by_vault("test_vault_id")

        # Should return 0 due to None guard
        self.assertEqual(result, 0)

    @pytest.mark.asyncio
    async def test_delete_by_vault_none_table_no_chunks_table(self):
        """
        Test that delete_by_vault returns 0 when table doesn't exist.
        """
        store = self.create_vector_store()

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=[])  # No chunks table
        store.db = mock_db

        result = await store.delete_by_vault("test_vault_id")

        # Should return 0 when table doesn't exist
        self.assertEqual(result, 0)


class TestNormalOperationWithTable(TestVectorStoreNoneGuards):
    """Test that normal operations work correctly when table IS set."""

    @pytest.mark.asyncio
    async def test_search_single_scale_normal_operation(self):
        """
        Test that _search_single_scale works normally when table IS set.

        This verifies the None guard doesn't interfere with normal operation.
        """
        store = self.create_vector_store()
        await store.init_table(embedding_dim=self.embedding_dim)

        # Add a test record
        test_record = {
            "id": "test_0",
            "text": "Test chunk content",
            "file_id": "file1",
            "vault_id": "1",
            "chunk_index": 0,
            "chunk_scale": "default",
            "metadata": json.dumps({"source": "test"}),
            "embedding": np.random.randn(self.embedding_dim).tolist(),
        }
        await store.add_chunks([test_record])

        # Search with the same embedding
        results = await store._search_single_scale(
            embedding=test_record["embedding"],
            scale="default",
            fetch_k=10,
        )

        # Should return results (not empty due to None guard)
        self.assertIsInstance(results, list)
        # Should have at least one result (the record we added)
        self.assertGreater(len(results), 0)

    @pytest.mark.asyncio
    async def test_search_normal_operation(self):
        """
        Test that search() works normally when table IS set.
        """
        store = self.create_vector_store()
        await store.init_table(embedding_dim=self.embedding_dim)

        # Add test records
        records = []
        for i in range(3):
            records.append(
                {
                    "id": f"test_{i}",
                    "text": f"Test chunk {i}",
                    "file_id": "file1",
                    "vault_id": "1",
                    "chunk_index": i,
                    "chunk_scale": "default",
                    "metadata": json.dumps({"source": "test"}),
                    "embedding": np.random.randn(self.embedding_dim).tolist(),
                }
            )
        await store.add_chunks(records)

        # Search
        results = await store.search(
            embedding=records[0]["embedding"],
            limit=10,
        )

        # Should return results
        self.assertIsInstance(results, list)
        self.assertGreater(len(results), 0)

    @pytest.mark.asyncio
    async def test_delete_by_file_normal_operation(self):
        """
        Test that delete_by_file works normally when table IS set.
        """
        store = self.create_vector_store()
        await store.init_table(embedding_dim=self.embedding_dim)

        # Add test record
        test_record = {
            "id": "file1_0",
            "text": "Test chunk",
            "file_id": "file_to_delete",
            "vault_id": "1",
            "chunk_index": 0,
            "chunk_scale": "default",
            "metadata": json.dumps({"source": "test"}),
            "embedding": np.random.randn(self.embedding_dim).tolist(),
        }
        await store.add_chunks([test_record])

        # Verify record exists
        count_before = await store.table.count_rows()
        self.assertEqual(count_before, 1)

        # Delete by file
        deleted_count = await store.delete_by_file("file_to_delete")

        # Should return count of deleted records
        self.assertEqual(deleted_count, 1)

        # Verify deletion
        count_after = await store.table.count_rows()
        self.assertEqual(count_after, 0)

    @pytest.mark.asyncio
    async def test_delete_by_vault_normal_operation(self):
        """
        Test that delete_by_vault works normally when table IS set.
        """
        store = self.create_vector_store()
        await store.init_table(embedding_dim=self.embedding_dim)

        # Add test records for two vaults
        records_vault1 = [
            {
                "id": f"v1_{i}",
                "text": f"Vault 1 chunk {i}",
                "file_id": f"file{i}",
                "vault_id": "vault_to_delete",
                "chunk_index": i,
                "chunk_scale": "default",
                "metadata": json.dumps({"source": "test"}),
                "embedding": np.random.randn(self.embedding_dim).tolist(),
            }
            for i in range(3)
        ]

        records_vault2 = [
            {
                "id": f"v2_{i}",
                "text": f"Vault 2 chunk {i}",
                "file_id": f"file{i}",
                "vault_id": "vault_keep",
                "chunk_index": i,
                "chunk_scale": "default",
                "metadata": json.dumps({"source": "test"}),
                "embedding": np.random.randn(self.embedding_dim).tolist(),
            }
            for i in range(2)
        ]

        await store.add_chunks(records_vault1 + records_vault2)

        # Verify records exist
        count_before = await store.table.count_rows()
        self.assertEqual(count_before, 5)

        # Delete by vault
        deleted_count = await store.delete_by_vault("vault_to_delete")

        # Should return count of deleted records
        self.assertEqual(deleted_count, 3)

        # Verify only vault2 records remain
        count_after = await store.table.count_rows()
        self.assertEqual(count_after, 2)


if __name__ == "__main__":
    unittest.main()
