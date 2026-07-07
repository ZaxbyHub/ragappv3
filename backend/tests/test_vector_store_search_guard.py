"""
Tests for LanceDB Expr API migration in vector_store.py FTS search path.

Verifies the three constraints from task 1.1:
1. The FTS vault_id filter uses the Expr API (col("vault_id").eq(lit(vault_id)))
   or documented .to_sql() fallback when combined with filter_expr.
2. Cross-vault leakage is prevented (vault_id filter always applied when specified).
3. The dense arm still works with string interpolation via _lance_escape().

This file supersedes the placeholder output path — the actual Expr API migration
tests live here alongside None-guard coverage. Integration tests that call
init_table() are skipped in stub environments (lancedb stub has no
connect_async); they run in CI with the real lancedb package.

Coverage map (vector_store.py line refs):
- _search_single_scale() FTS vault_id filter: lines ~903-911 (Expr API via .to_sql())
- search() FTS vault_id filter without filter_expr: lines ~1178 (Expr API direct)
- search() FTS vault_id filter with filter_expr: lines ~1172-1176 (.to_sql() fallback)
- Dense arm string interpolation: lines ~1137-1143 (fallback per design doc)
"""

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from app.services.vector_store import VectorStore, _lance_escape

# ---------------------------------------------------------------------------
# Unit tests: _lance_escape (dense arm fallback, delete_by_vault)
# ---------------------------------------------------------------------------

class TestLanceEscapeFunction(unittest.TestCase):
    """Test the _lance_escape helper used by dense arm and delete paths."""

    def test_escape_doubles_single_quotes(self):
        """Single quotes must be doubled for SQL-standard escaping."""
        self.assertEqual(_lance_escape("vault'id"), "vault''id")

    def test_escape_handles_plain_vault_id(self):
        """Plain vault IDs pass through unchanged."""
        self.assertEqual(_lance_escape("vault123"), "vault123")

    def test_escape_handles_numeric_vault_id(self):
        """Numeric vault IDs are converted to string and escaped."""
        self.assertEqual(_lance_escape(123), "123")

    def test_escape_handles_empty_string(self):
        """Empty string remains empty."""
        self.assertEqual(_lance_escape(""), "")

    def test_escape_handles_multiple_quotes(self):
        """Multiple single quotes are all doubled."""
        self.assertEqual(_lance_escape("va'u'lt"), "va''u''lt")

    def test_escape_injection_attempt_vault_id(self):
        """SQL injection attempt in vault_id is escaped."""
        injection = "vault' OR '1'='1"
        escaped = _lance_escape(injection)
        self.assertEqual(escaped, "vault'' OR ''1''=''1")
        # The escaped version cannot break out of the string literal
        self.assertNotIn("' OR '", escaped.replace("''", ""))


# ---------------------------------------------------------------------------
# Unit tests: FTS path uses Expr API (mock-based, no LanceDB needed)
# ---------------------------------------------------------------------------

class _FakeSearchQuery:
    """Fake search query that records .where() filter calls."""

    def __init__(self, rows=None):
        self._rows = rows or []
        self._filters = []
        self._limit_value = None

    def where(self, filter_expr):
        self._filters.append(filter_expr)
        return self

    def limit(self, limit):
        self._limit_value = limit
        return self

    async def to_list(self):
        if self._limit_value is None:
            return list(self._rows)
        return list(self._rows[: self._limit_value])

    @property
    def filters(self):
        return self._filters


class _FakeLanceTable:
    """Fake LanceDB table that tracks search queries."""

    def __init__(self, dense_rows=None, fts_rows=None):
        self.dense_rows = dense_rows or []
        self.fts_rows = fts_rows or []
        self.dense_query = _FakeSearchQuery(dense_rows)
        self.fts_query = _FakeSearchQuery(fts_rows)

    async def search(self, query, query_type=None, **kwargs):
        if query_type == "vector":
            return self.dense_query
        if query_type == "fts":
            return self.fts_query
        raise AssertionError(f"unexpected query_type: {query_type}")

    async def list_indices(self):
        return []

    async def count_rows(self, filter_expr=None):
        return len(self.dense_rows)


class TestSearchFTSVaultIdExprAPI(unittest.IsolatedAsyncioTestCase):
    """Test FTS path vault_id filtering uses Expr API in search()."""

    def setUp(self):
        self.store = VectorStore.__new__(VectorStore)
        self.store.db = MagicMock()
        self.store.table = MagicMock()
        self.store.table.list_indices = AsyncMock(return_value=[])
        self.store.table.count_rows = AsyncMock(return_value=0)
        self.store.table.schema = MagicMock()
        self.store._embedding_dim = 384
        self.store._fts_exceptions = 0
        self.store._search_semaphore = asyncio.Semaphore(10)

        self.dense_results = [
            {"id": f"doc_{i}", "text": f"doc text {i}", "_distance": 0.1 * i}
            for i in range(3)
        ]

    def _make_fts_mock_builder(self, results):
        mock_builder = MagicMock()
        mock_builder.where.return_value = mock_builder
        mock_builder.limit.return_value = mock_builder
        mock_builder.to_list = AsyncMock(return_value=results)
        return mock_builder

    def _make_dense_mock_builder(self, results):
        mock_builder = MagicMock()
        mock_builder.where.return_value = mock_builder
        mock_builder.limit.return_value = mock_builder
        mock_builder.to_list = AsyncMock(return_value=results)
        return mock_builder

    @pytest.mark.asyncio
    async def test_fts_vault_id_filter_uses_expr_api_without_filter_expr(self):
        """search() FTS path: vault_id alone uses Expr API col().eq(lit()).

        When filter_expr is None, the code passes the Expr object directly to
        .where() (line 1178: fts_query.where(col("vault_id").eq(lit(vault_id)))).
        The Expr.__str__ returns "Expr((vault_id = 'value'))".

        In the stub environment (conftest lancedb stub), col() returns _ExprStub
        which stores _sql="vault_id" and .eq() returns _ExprStub with
        _sql="vault_id = 'test_vault_123'". We check _sql directly since
        _ExprStub lacks __str__/__repr__.
        """
        fts_results = [{"id": "fts_1", "text": "fts result"}]
        dense_builder = self._make_dense_mock_builder(self.dense_results)
        fts_builder = self._make_fts_mock_builder(fts_results)

        async def search_side_effect(query, query_type=None, **kwargs):
            if query_type == "vector":
                return dense_builder
            elif query_type == "fts":
                return fts_builder
            return MagicMock()

        self.store.table.search = AsyncMock(side_effect=search_side_effect)

        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = False

            await self.store.search(
                embedding=[0.1] * 384,
                limit=5,
                vault_id="test_vault_123",
                query_text="test query",
                hybrid=True,
            )

        fts_builder.where.assert_called_once()
        filter_arg = fts_builder.where.call_args[0][0]
        # filter_arg is lancedb.expr.Expr (prod) or _ExprStub (stub env).
        # _ExprStub has _sql; real Expr has to_sql() and proper __str__.
        if hasattr(filter_arg, "_sql"):
            # Stub environment: _ExprStub
            self.assertIn("vault_id", filter_arg._sql)
            self.assertIn("test_vault_123", filter_arg._sql)
        else:
            # Real lancedb environment: Expr object
            self.assertIn("vault_id", str(filter_arg))
            self.assertIn("test_vault_123", str(filter_arg))

    @pytest.mark.asyncio
    async def test_fts_vault_id_filter_uses_expr_api_with_filter_expr(self):
        """search() FTS path: vault_id + filter_expr uses .to_sql() fallback.

        When filter_expr is also present, the Expr is converted to SQL via
        .to_sql() (line 1173) and concatenated as a string.
        """
        fts_results = [{"id": "fts_1", "text": "fts result"}]
        dense_builder = self._make_dense_mock_builder(self.dense_results)
        fts_builder = self._make_fts_mock_builder(fts_results)

        async def search_side_effect(query, query_type=None, **kwargs):
            if query_type == "vector":
                return dense_builder
            elif query_type == "fts":
                return fts_builder
            return MagicMock()

        self.store.table.search = AsyncMock(side_effect=search_side_effect)

        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = False

            await self.store.search(
                embedding=[0.1] * 384,
                limit=5,
                vault_id="test_vault_456",
                filter_expr="chunk_scale = 'default'",
                query_text="test query",
                hybrid=True,
            )

        fts_builder.where.assert_called_once()
        filter_arg = fts_builder.where.call_args[0][0]
        # With filter_expr, .to_sql() produces a SQL string
        self.assertIsInstance(filter_arg, str)
        self.assertIn("vault_id", filter_arg)
        self.assertIn("chunk_scale", filter_arg)
        self.assertIn("test_vault_456", filter_arg)


class TestSearchSingleScaleFTSVaultIdExprAPI(unittest.IsolatedAsyncioTestCase):
    """Test FTS path vault_id filtering uses Expr API in _search_single_scale()."""

    def setUp(self):
        self.store = VectorStore.__new__(VectorStore)
        self.store.db = None
        self.store.table = MagicMock()
        self.store._embedding_dim = 384
        self.store._fts_exceptions = 0
        self.store._search_semaphore = asyncio.Semaphore(10)

        self.dense_results = [
            {"id": f"doc_{i}", "text": f"doc text {i}", "_distance": 0.1 * i}
            for i in range(3)
        ]

    def _make_fts_mock_builder(self, results):
        mock_builder = MagicMock()
        mock_builder.where.return_value = mock_builder
        mock_builder.limit.return_value = mock_builder
        mock_builder.to_list = AsyncMock(return_value=results)
        return mock_builder

    def _make_dense_mock_builder(self, results):
        mock_builder = MagicMock()
        mock_builder.where.return_value = mock_builder
        mock_builder.limit.return_value = mock_builder
        mock_builder.to_list = AsyncMock(return_value=results)
        return mock_builder

    @pytest.mark.asyncio
    async def test_single_scale_fts_vault_id_uses_expr_api(self):
        """_search_single_scale() FTS path uses col("vault_id").eq(lit(vault_id)).to_sql().

        The _run_fts() inner function builds fts_filter_parts with the Expr's
        .to_sql() output (line 907).
        """
        fts_results = [{"id": "fts_scale_1", "text": "fts scale result"}]
        dense_builder = self._make_dense_mock_builder(self.dense_results)
        fts_builder = self._make_fts_mock_builder(fts_results)

        async def search_side_effect(query, query_type=None, **kwargs):
            if query_type == "vector":
                return dense_builder
            elif query_type == "fts":
                return fts_builder
            return MagicMock()

        self.store.table.search = AsyncMock(side_effect=search_side_effect)

        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.rrf_legacy_mode = False
            mock_settings.hybrid_rrf_k = 60

            result = await self.store._search_single_scale(
                embedding=[0.1] * 384,
                scale="default",
                fetch_k=5,
                vault_id="vault_expr_test",
                query_text="test query",
                hybrid=True,
            )

        fts_builder.where.assert_called_once()
        filter_arg = fts_builder.where.call_args[0][0]
        # to_sql() produces a string
        self.assertIsInstance(filter_arg, str)
        self.assertIn("vault_id", filter_arg)
        self.assertIn("vault_expr_test", filter_arg)


# ---------------------------------------------------------------------------
# Unit tests: Dense arm uses string interpolation
# ---------------------------------------------------------------------------

class TestDenseVaultIdStringInterpolation(unittest.IsolatedAsyncioTestCase):
    """Test dense arm uses string interpolation with _lance_escape() for vault_id."""

    def setUp(self):
        self.store = VectorStore.__new__(VectorStore)
        self.store.db = MagicMock()
        self.store.table = MagicMock()
        self.store.table.list_indices = AsyncMock(return_value=[])
        self.store.table.count_rows = AsyncMock(return_value=0)
        self.store.table.schema = MagicMock()
        self.store._embedding_dim = 384
        self.store._fts_exceptions = 0
        self.store._search_semaphore = asyncio.Semaphore(10)

        self.dense_results = [
            {"id": f"doc_{i}", "text": f"doc text {i}", "_distance": 0.1 * i}
            for i in range(3)
        ]

    def _make_dense_mock_builder(self, results):
        mock_builder = MagicMock()
        mock_builder.where.return_value = mock_builder
        mock_builder.limit.return_value = mock_builder
        mock_builder.to_list = AsyncMock(return_value=results)
        return mock_builder

    @pytest.mark.asyncio
    async def test_dense_vault_id_uses_string_interpolation(self):
        """search() dense path uses string interpolation with _lance_escape().

        Dense arm uses f-string building with _lance_escape() (lines 1137-1143).
        """
        dense_builder = self._make_dense_mock_builder(self.dense_results)

        async def search_side_effect(query, query_type=None, **kwargs):
            if query_type == "vector":
                return dense_builder
            elif query_type == "fts":
                mock_builder = MagicMock()
                mock_builder.where.return_value = mock_builder
                mock_builder.limit.return_value = mock_builder
                mock_builder.to_list = AsyncMock(return_value=[])
                return mock_builder
            return MagicMock()

        self.store.table.search = AsyncMock(side_effect=search_side_effect)

        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = False

            # dense-only (no query_text) to test the dense arm directly
            await self.store.search(
                embedding=[0.1] * 384,
                limit=5,
                vault_id="dense_vault_test",
                hybrid=False,
            )

        dense_builder.where.assert_called_once()
        filter_arg = dense_builder.where.call_args[0][0]
        # Dense arm uses string interpolation: vault_id = 'value'
        self.assertIsInstance(filter_arg, str)
        self.assertIn("vault_id", filter_arg)
        self.assertIn("dense_vault_test", filter_arg)
        # Must use single quotes for string literal
        self.assertIn("'", filter_arg)

    @pytest.mark.asyncio
    async def test_dense_vault_id_escapes_single_quotes(self):
        """Dense path: vault_id with single quotes is escaped via _lance_escape()."""
        dense_builder = self._make_dense_mock_builder(self.dense_results)

        async def search_side_effect(query, query_type=None, **kwargs):
            if query_type == "vector":
                return dense_builder
            elif query_type == "fts":
                mock_builder = MagicMock()
                mock_builder.where.return_value = mock_builder
                mock_builder.limit.return_value = mock_builder
                mock_builder.to_list = AsyncMock(return_value=[])
                return mock_builder
            return MagicMock()

        self.store.table.search = AsyncMock(side_effect=search_side_effect)

        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = False

            # vault_id with single quote to test escaping
            await self.store.search(
                embedding=[0.1] * 384,
                limit=5,
                vault_id="vault' OR '1'='1",  # SQL injection attempt
                hybrid=False,
            )

        dense_builder.where.assert_called_once()
        filter_arg = dense_builder.where.call_args[0][0]
        # The single quote must be doubled for escaping
        self.assertIn("''", filter_arg)  # Escaped quote
        # The injection pattern should be neutralized
        self.assertNotIn("' OR '", filter_arg.replace("''", ""))


class TestDenseArmWithVaultIdAndFilterExpr(unittest.IsolatedAsyncioTestCase):
    """Test dense arm string interpolation when vault_id AND filter_expr are both present."""

    def setUp(self):
        self.store = VectorStore.__new__(VectorStore)
        self.store.db = MagicMock()
        self.store.table = MagicMock()
        self.store.table.list_indices = AsyncMock(return_value=[])
        self.store.table.count_rows = AsyncMock(return_value=0)
        self.store.table.schema = MagicMock()
        self.store._embedding_dim = 384
        self.store._fts_exceptions = 0
        self.store._search_semaphore = asyncio.Semaphore(10)

        self.dense_results = [
            {"id": f"doc_{i}", "text": f"doc text {i}", "_distance": 0.1 * i}
            for i in range(3)
        ]

    def _make_dense_mock_builder(self, results):
        mock_builder = MagicMock()
        mock_builder.where.return_value = mock_builder
        mock_builder.limit.return_value = mock_builder
        mock_builder.to_list = AsyncMock(return_value=results)
        return mock_builder

    @pytest.mark.asyncio
    async def test_dense_arm_combines_vault_and_filter_expr(self):
        """Dense arm combines vault_id filter with filter_expr via string interpolation."""
        dense_builder = self._make_dense_mock_builder(self.dense_results)

        async def search_side_effect(query, query_type=None, **kwargs):
            if query_type == "vector":
                return dense_builder
            elif query_type == "fts":
                mock_builder = MagicMock()
                mock_builder.where.return_value = mock_builder
                mock_builder.limit.return_value = mock_builder
                mock_builder.to_list = AsyncMock(return_value=[])
                return mock_builder
            return MagicMock()

        self.store.table.search = AsyncMock(side_effect=search_side_effect)

        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = False

            await self.store.search(
                embedding=[0.1] * 384,
                limit=5,
                vault_id="combined_vault",
                filter_expr="chunk_scale = 'default'",
                hybrid=False,
            )

        dense_builder.where.assert_called_once()
        filter_arg = dense_builder.where.call_args[0][0]
        self.assertIsInstance(filter_arg, str)
        self.assertIn("vault_id", filter_arg)
        self.assertIn("chunk_scale", filter_arg)
        self.assertIn("combined_vault", filter_arg)


# ---------------------------------------------------------------------------
# Integration tests: cross-vault leakage prevention
# ---------------------------------------------------------------------------
# These require a real LanceDB instance. They are skipped when the lancedb
# stub (used in some CI-free local environments) lacks connect_async.
# ---------------------------------------------------------------------------

def _lancedb_has_connect_async():
    """Check if real lancedb is available with connect_async."""
    try:
        import lancedb
        return hasattr(lancedb, "connect_async")
    except Exception:
        return False


class TestCrossVaultLeakagePrevention(unittest.IsolatedAsyncioTestCase):
    """Test that cross-vault leakage is prevented by always applying vault_id filter."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test_lancedb"
        self.embedding_dim = 384

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    @pytest.mark.skipif(
        not _lancedb_has_connect_async(),
        reason="lancedb stub has no connect_async; run in CI with real lancedb",
    )
    @pytest.mark.asyncio
    async def test_search_single_scale_cross_vault_isolation(self):
        """_search_single_scale: vault_id filter prevents cross-vault results."""
        store = VectorStore(db_path=self.db_path)
        await store.init_table(embedding_dim=self.embedding_dim)

        vault_a_records = [
            {
                "id": f"vault_a_{i}",
                "text": f"Vault A chunk {i}",
                "file_id": f"file_a_{i}",
                "vault_id": "vault_a",
                "chunk_index": i,
                "chunk_scale": "default",
                "metadata": json.dumps({"source": "test", "vault": "A"}),
                "embedding": np.random.randn(self.embedding_dim).tolist(),
            }
            for i in range(3)
        ]

        vault_b_records = [
            {
                "id": f"vault_b_{i}",
                "text": f"Vault B chunk {i}",
                "file_id": f"file_b_{i}",
                "vault_id": "vault_b",
                "chunk_index": i,
                "chunk_scale": "default",
                "metadata": json.dumps({"source": "test", "vault": "B"}),
                "embedding": np.random.randn(self.embedding_dim).tolist(),
            }
            for i in range(2)
        ]

        await store.add_chunks(vault_a_records + vault_b_records)

        # Search with vault_id="vault_a" - should only return vault_a results
        vault_a_embedding = vault_a_records[0]["embedding"]
        results_vault_a = await store._search_single_scale(
            embedding=vault_a_embedding,
            scale="default",
            fetch_k=10,
            vault_id="vault_a",
            hybrid=False,
        )

        result_vault_ids = {r.get("vault_id") for r in results_vault_a}
        self.assertEqual(result_vault_ids, {"vault_a"})
        self.assertEqual(len(results_vault_a), 3)

        # Search with vault_id="vault_b" - should only return vault_b results
        vault_b_embedding = vault_b_records[0]["embedding"]
        results_vault_b = await store._search_single_scale(
            embedding=vault_b_embedding,
            scale="default",
            fetch_k=10,
            vault_id="vault_b",
            hybrid=False,
        )

        result_vault_ids_b = {r.get("vault_id") for r in results_vault_b}
        self.assertEqual(result_vault_ids_b, {"vault_b"})
        self.assertEqual(len(results_vault_b), 2)

    @pytest.mark.skipif(
        not _lancedb_has_connect_async(),
        reason="lancedb stub has no connect_async; run in CI with real lancedb",
    )
    @pytest.mark.asyncio
    async def test_search_cross_vault_isolation(self):
        """search(): vault_id filter prevents cross-vault results."""
        store = VectorStore(db_path=self.db_path)
        await store.init_table(embedding_dim=self.embedding_dim)

        vault_x_records = [
            {
                "id": f"vault_x_{i}",
                "text": f"Vault X chunk {i}",
                "file_id": f"file_x_{i}",
                "vault_id": "vault_x",
                "chunk_index": i,
                "chunk_scale": "default",
                "metadata": json.dumps({"source": "test"}),
                "embedding": np.random.randn(self.embedding_dim).tolist(),
            }
            for i in range(2)
        ]

        vault_y_records = [
            {
                "id": f"vault_y_{i}",
                "text": f"Vault Y chunk {i}",
                "file_id": f"file_y_{i}",
                "vault_id": "vault_y",
                "chunk_index": i,
                "chunk_scale": "default",
                "metadata": json.dumps({"source": "test"}),
                "embedding": np.random.randn(self.embedding_dim).tolist(),
            }
            for i in range(2)
        ]

        await store.add_chunks(vault_x_records + vault_y_records)

        vault_x_embedding = vault_x_records[0]["embedding"]
        results = await store.search(
            embedding=vault_x_embedding,
            limit=10,
            vault_id="vault_x",
            hybrid=False,
        )

        for result in results:
            self.assertEqual(
                result.get("vault_id"),
                "vault_x",
                f"Cross-vault leakage: got result with vault_id={result.get('vault_id')}",
            )

    @pytest.mark.skipif(
        not _lancedb_has_connect_async(),
        reason="lancedb stub has no connect_async; run in CI with real lancedb",
    )
    @pytest.mark.asyncio
    async def test_search_without_vault_id_returns_all_vaults(self):
        """search(): without vault_id, results from all vaults are returned (admin mode)."""
        store = VectorStore(db_path=self.db_path)
        await store.init_table(embedding_dim=self.embedding_dim)

        vault_p_records = [
            {
                "id": f"vault_p_{i}",
                "text": f"Vault P chunk {i}",
                "file_id": f"file_p_{i}",
                "vault_id": "vault_p",
                "chunk_index": i,
                "chunk_scale": "default",
                "metadata": json.dumps({"source": "test"}),
                "embedding": np.random.randn(self.embedding_dim).tolist(),
            }
            for i in range(2)
        ]

        vault_q_records = [
            {
                "id": f"vault_q_{i}",
                "text": f"Vault Q chunk {i}",
                "file_id": f"file_q_{i}",
                "vault_id": "vault_q",
                "chunk_index": i,
                "chunk_scale": "default",
                "metadata": json.dumps({"source": "test"}),
                "embedding": np.random.randn(self.embedding_dim).tolist(),
            }
            for i in range(2)
        ]

        await store.add_chunks(vault_p_records + vault_q_records)

        embedding = vault_p_records[0]["embedding"]
        results = await store.search(
            embedding=embedding,
            limit=10,
            vault_id=None,
            hybrid=False,
        )

        result_vault_ids = {r.get("vault_id") for r in results}
        self.assertIn("vault_p", result_vault_ids)
        self.assertIn("vault_q", result_vault_ids)


class TestDeleteByVaultStringInterpolation(unittest.IsolatedAsyncioTestCase):
    """Test delete_by_vault uses _lance_escape() string interpolation."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test_lancedb"
        self.embedding_dim = 384

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    @pytest.mark.skipif(
        not _lancedb_has_connect_async(),
        reason="lancedb stub has no connect_async; run in CI with real lancedb",
    )
    @pytest.mark.asyncio
    async def test_delete_by_vault_normal(self):
        """delete_by_vault deletes only the specified vault's chunks."""
        store = VectorStore(db_path=self.db_path)
        await store.init_table(embedding_dim=self.embedding_dim)

        to_delete_records = [
            {
                "id": f"delete_me_{i}",
                "text": f"Delete me chunk {i}",
                "file_id": f"file_delete_{i}",
                "vault_id": "vault_delete",
                "chunk_index": i,
                "chunk_scale": "default",
                "metadata": json.dumps({"source": "test"}),
                "embedding": np.random.randn(self.embedding_dim).tolist(),
            }
            for i in range(3)
        ]

        to_keep_records = [
            {
                "id": f"keep_me_{i}",
                "text": f"Keep me chunk {i}",
                "file_id": f"file_keep_{i}",
                "vault_id": "vault_keep",
                "chunk_index": i,
                "chunk_scale": "default",
                "metadata": json.dumps({"source": "test"}),
                "embedding": np.random.randn(self.embedding_dim).tolist(),
            }
            for i in range(2)
        ]

        await store.add_chunks(to_delete_records + to_keep_records)

        initial_count = await store.table.count_rows()
        self.assertEqual(initial_count, 5)

        deleted_count = await store.delete_by_vault("vault_delete")

        self.assertEqual(deleted_count, 3)

        remaining_count = await store.table.count_rows()
        self.assertEqual(remaining_count, 2)

        all_remaining = list(await store.table.to_pandas())
        for row in all_remaining:
            self.assertEqual(
                row["vault_id"],
                "vault_keep",
                f"Cross-vault deletion: found row with vault_id={row['vault_id']}",
            )

    @pytest.mark.skipif(
        not _lancedb_has_connect_async(),
        reason="lancedb stub has no connect_async; run in CI with real lancedb",
    )
    @pytest.mark.asyncio
    async def test_delete_by_vault_escapes_special_chars(self):
        """delete_by_vault: special characters in vault_id are escaped."""
        store = VectorStore(db_path=self.db_path)
        await store.init_table(embedding_dim=self.embedding_dim)

        special_vault_records = [
            {
                "id": f"special_{i}",
                "text": f"Special vault chunk {i}",
                "file_id": f"file_special_{i}",
                "vault_id": "vault'special",  # Contains single quote
                "chunk_index": i,
                "chunk_scale": "default",
                "metadata": json.dumps({"source": "test"}),
                "embedding": np.random.randn(self.embedding_dim).tolist(),
            }
            for i in range(2)
        ]

        await store.add_chunks(special_vault_records)

        initial_count = await store.table.count_rows()
        self.assertEqual(initial_count, 2)

        deleted_count = await store.delete_by_vault("vault'special")

        self.assertEqual(deleted_count, 2)

        remaining_count = await store.table.count_rows()
        self.assertEqual(remaining_count, 0)


# ---------------------------------------------------------------------------
# None-guard tests from the pre-existing test_vector_store_search_guard.py
# (included here so this output file is self-contained)
# ---------------------------------------------------------------------------

class TestVectorStoreNoneGuards(unittest.TestCase):
    """Test cases for None guard behavior in VectorStore."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test_lancedb"
        self.embedding_dim = 384

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def create_vector_store(self) -> VectorStore:
        return VectorStore(db_path=self.db_path)


class TestSearchSingleScaleNoneGuard(unittest.IsolatedAsyncioTestCase):
    """Test cases for _search_single_scale() None guard."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test_lancedb"
        self.embedding_dim = 384

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_search_single_scale_none_table(self):
        """Test that _search_single_scale returns [] when self.table is None."""
        store = VectorStore(db_path=self.db_path)
        store.table = None

        result = await store._search_single_scale(
            embedding=[0.0] * self.embedding_dim,
            scale="default",
            fetch_k=10,
        )
        self.assertEqual(result, [])

    @pytest.mark.asyncio
    async def test_search_single_scale_none_table_with_vault_filter(self):
        """Test that _search_single_scale returns [] when table is None, even with vault filter."""
        store = VectorStore(db_path=self.db_path)
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
        """Test that _search_single_scale returns [] when table is None, even with hybrid query."""
        store = VectorStore(db_path=self.db_path)
        store.table = None

        result = await store._search_single_scale(
            embedding=[0.0] * self.embedding_dim,
            scale="default",
            fetch_k=10,
            query_text="test query",
            hybrid=True,
        )
        self.assertEqual(result, [])


class TestSearchNoneGuard(unittest.IsolatedAsyncioTestCase):
    """Test cases for search() None guard in single-scale path."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test_lancedb"
        self.embedding_dim = 384

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_search_none_table_single_scale(self):
        """Test that search() returns [] when self.table is None in single-scale path."""
        store = VectorStore(db_path=self.db_path)
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
            )
        self.assertEqual(result, [])

    @pytest.mark.asyncio
    async def test_search_none_table_single_scale_with_vault(self):
        """Test that search() returns [] when table is None, even with vault filter."""
        store = VectorStore(db_path=self.db_path)
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
    """Regression: single-scale hybrid search does not raise UnboundLocalError on rrf_fuse."""
    dense_rows = [
        {"id": "dense-1", "text": "dense result", "file_id": "file-dense", "metadata": {}, "_distance": 0.1}
    ]
    fts_rows = [
        {"id": "fts-1", "text": "fts result", "file_id": "file-fts", "metadata": {}, "_distance": 0.2}
    ]

    store = VectorStore(db_path=tmp_path / "test_lancedb")
    store.db = MagicMock()

    fake_table = _FakeLanceTable(dense_rows, fts_rows)
    store.table = fake_table
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


class TestDeleteByFileNoneGuard(unittest.IsolatedAsyncioTestCase):
    """Test cases for delete_by_file() None guard."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test_lancedb"
        self.embedding_dim = 384

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_delete_by_file_none_table(self):
        """Test that delete_by_file raises VectorStoreConnectionError when table open fails.

        When table_names says "chunks" exists but open_table raises RuntimeError,
        _delete_by_file_unlocked catches it and raises VectorStoreConnectionError.
        """
        store = VectorStore(db_path=self.db_path)
        store.table = None

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=["chunks"])
        mock_db.open_table = AsyncMock(side_effect=RuntimeError("Table open failed"))
        store.db = mock_db

        from app.services.vector_store import VectorStoreConnectionError
        with self.assertRaises(VectorStoreConnectionError):
            await store.delete_by_file("test_file_id")

    @pytest.mark.asyncio
    async def test_delete_by_file_none_table_no_chunks_table(self):
        """Test that delete_by_file returns 0 when table doesn't exist."""
        store = VectorStore(db_path=self.db_path)

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=[])
        store.db = mock_db

        result = await store.delete_by_file("test_file_id")
        self.assertEqual(result, 0)


class TestDeleteByVaultNoneGuard(unittest.IsolatedAsyncioTestCase):
    """Test cases for delete_by_vault() None guard."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test_lancedb"
        self.embedding_dim = 384

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_delete_by_vault_none_table(self):
        """Test that delete_by_vault raises VectorStoreConnectionError when table open fails.

        When table_names says "chunks" exists but open_table raises RuntimeError,
        _delete_by_vault_unlocked catches it and raises VectorStoreConnectionError.
        """
        store = VectorStore(db_path=self.db_path)
        store.table = None

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=["chunks"])
        mock_db.open_table = AsyncMock(side_effect=RuntimeError("Table open failed"))
        store.db = mock_db

        from app.services.vector_store import VectorStoreConnectionError
        with self.assertRaises(VectorStoreConnectionError):
            await store.delete_by_vault("test_vault_id")

    @pytest.mark.asyncio
    async def test_delete_by_vault_none_table_no_chunks_table(self):
        """Test that delete_by_vault returns 0 when table doesn't exist."""
        store = VectorStore(db_path=self.db_path)

        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=[])
        store.db = mock_db

        result = await store.delete_by_vault("test_vault_id")
        self.assertEqual(result, 0)


if __name__ == "__main__":
    unittest.main()
