"""Tests for the lock-free vector-index freshness probe on the search path.

``_vector_index_needs_creation`` lets the hot search path skip acquiring the
write lock when the ANN index is confidently fresh, so chat queries no longer
block behind in-flight ingestion writes (which hold the same lock for up to
``write_lock_timeout_seconds``). The authoritative, write-locked
``_maybe_create_vector_index`` still re-validates freshness (double-checked
locking), so a concurrent builder cannot cause a duplicate rebuild.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.vector_store import VECTOR_INDEX_MIN_ROWS, VectorStore


def _make_store():
    store = VectorStore.__new__(VectorStore)
    store._index_mutation_generation = 0
    store._last_index_build_row_count = -1
    store._last_index_build_generation = -1
    store.table = MagicMock()
    return store


def _index(name: str, columns=None, index_type=None):
    # Shape-complete fake: detection reads columns/index_type (issue #557),
    # never the name.
    idx = MagicMock()
    idx.name = name
    if columns is not None:
        idx.columns = columns
    if index_type is not None:
        idx.index_type = index_type
    return idx


class TestVectorIndexFreshnessProbe:
    @pytest.mark.asyncio
    async def test_no_table_returns_false(self):
        store = _make_store()
        store.table = None
        assert await store._vector_index_needs_creation() is False

    @pytest.mark.asyncio
    async def test_below_min_rows_returns_false(self):
        store = _make_store()
        store.table.count_rows = AsyncMock(return_value=VECTOR_INDEX_MIN_ROWS - 1)
        store.table.list_indices = AsyncMock(return_value=[])
        # Too few rows to build an index → no creation needed, no lock taken.
        assert await store._vector_index_needs_creation() is False

    @pytest.mark.asyncio
    async def test_fresh_index_returns_false(self):
        store = _make_store()
        rows = VECTOR_INDEX_MIN_ROWS + 100
        store.table.count_rows = AsyncMock(return_value=rows)
        store.table.list_indices = AsyncMock(return_value=[_index("embedding_idx", ["embedding"], "IvfPq")])
        # Mark the index as built for exactly this row count + generation.
        store._last_index_build_row_count = rows
        store._last_index_build_generation = store._index_mutation_generation
        # Confidently fresh → search may skip the write lock.
        assert await store._vector_index_needs_creation() is False

    @pytest.mark.asyncio
    async def test_missing_index_returns_true(self):
        store = _make_store()
        rows = VECTOR_INDEX_MIN_ROWS + 100
        store.table.count_rows = AsyncMock(return_value=rows)
        store.table.list_indices = AsyncMock(return_value=[])  # no embedding_idx
        assert await store._vector_index_needs_creation() is True

    @pytest.mark.asyncio
    async def test_stale_generation_returns_true(self):
        store = _make_store()
        rows = VECTOR_INDEX_MIN_ROWS + 100
        store.table.count_rows = AsyncMock(return_value=rows)
        store.table.list_indices = AsyncMock(return_value=[_index("embedding_idx", ["embedding"], "IvfPq")])
        store._last_index_build_row_count = rows
        # Generation advanced since last build (e.g. ingestion happened).
        store._last_index_build_generation = store._index_mutation_generation - 1
        assert await store._vector_index_needs_creation() is True

    @pytest.mark.asyncio
    async def test_probe_error_returns_true_to_defer_to_lock(self):
        store = _make_store()
        store.table.count_rows = AsyncMock(side_effect=RuntimeError("io"))
        # On uncertainty, return True so the locked path makes the real call.
        assert await store._vector_index_needs_creation() is True
