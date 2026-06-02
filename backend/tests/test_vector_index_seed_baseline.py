"""Regression: startup seeding of the IVF_PQ row-count baseline.

When ``_init_table_unlocked`` opens an existing ``chunks`` table, it must seed
``_last_index_build_row_count`` from the existing vector index so the first
search after startup does NOT trigger a full IVF_PQ rebuild (a multi-second to
multi-minute stall on large tables).

The detection must match how LanceDB actually reports an existing vector index:
by NAME (``"embedding_idx"``), consistent with every other index check in
``VectorStore``. A prior bug matched the literal ``"IVF_PQ"`` against
``idx.index_type`` — but LanceDB reports the type as ``"IvfPq"`` (mixed-case, no
underscore; verified against lancedb at runtime), so ``"IVF_PQ" in "IvfPq"`` was
always False and seeding never fired. These tests pin the real ``index_type``
value so the regression cannot return.
"""

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.vector_store import VectorStore


def _existing_index(name: str, index_type: str):
    idx = MagicMock()
    idx.name = name
    idx.index_type = index_type
    return idx


def _make_store_opening_existing(indices, row_count):
    """A VectorStore wired to open an existing 'chunks' table whose
    list_indices() returns ``indices`` and count_rows() returns ``row_count``."""
    store = VectorStore(db_path=Path("/tmp/test_lancedb"))
    store._index_mutation_generation = 0
    store._last_index_build_row_count = 0
    store._last_index_build_generation = 0

    mock_table = MagicMock()
    mock_table.list_indices = AsyncMock(return_value=indices)
    mock_table.count_rows = AsyncMock(return_value=row_count)
    mock_table.create_index = AsyncMock()

    mock_db = MagicMock()
    mock_db.table_names = AsyncMock(return_value=["chunks"])
    mock_db.open_table = AsyncMock(return_value=mock_table)
    store.db = mock_db
    return store


class TestVectorIndexSeedBaseline(unittest.IsolatedAsyncioTestCase):
    async def test_seeds_row_count_from_existing_embedding_idx(self):
        """Existing vector index (name='embedding_idx', index_type='IvfPq') →
        baseline seeded to the current row count so first search won't rebuild."""
        # index_type is the REAL LanceDB value "IvfPq" — the old "IVF_PQ"
        # substring check would miss this and leave the baseline at 0.
        store = _make_store_opening_existing(
            indices=[_existing_index("embedding_idx", "IvfPq")],
            row_count=40340,
        )

        with patch("app.services.vector_store.pa") as mock_pa:
            mock_pa.schema.return_value = MagicMock()
            with patch("app.services.vector_store.settings") as mock_settings:
                mock_settings.vector_metric = "cosine"
                mock_settings.write_lock_timeout_seconds = 5.0
                with patch("app.services.vector_store.FTS") as mock_fts:
                    mock_fts.return_value = MagicMock()
                    await store.init_table(embedding_dim=384)

        self.assertEqual(store._last_index_build_row_count, 40340)
        self.assertEqual(
            store._last_index_build_generation, store._index_mutation_generation
        )

    async def test_does_not_seed_when_only_fts_index_present(self):
        """No vector index → nothing to seed; baseline stays 0."""
        store = _make_store_opening_existing(
            indices=[_existing_index("fts_text", "FTS")],
            row_count=40340,
        )

        with patch("app.services.vector_store.pa") as mock_pa:
            mock_pa.schema.return_value = MagicMock()
            with patch("app.services.vector_store.settings") as mock_settings:
                mock_settings.vector_metric = "cosine"
                mock_settings.write_lock_timeout_seconds = 5.0
                with patch("app.services.vector_store.FTS") as mock_fts:
                    mock_fts.return_value = MagicMock()
                    await store.init_table(embedding_dim=384)

        self.assertEqual(store._last_index_build_row_count, 0)


if __name__ == "__main__":
    unittest.main()
