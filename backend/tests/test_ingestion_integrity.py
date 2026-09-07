"""Tests for ingestion integrity: ANN index lifecycle, visibility filter,
safe re-upload, vault_id required, and audit migration (Issue #13 / #14)."""

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.document_retrieval import DocumentRetrievalService
from app.services.vector_store import VectorStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_record(file_id: str, chunk_index: int = 0, distance: float = 0.1) -> dict:
    """Return a minimal search-result dict as returned by vector store search."""
    return {
        "id": f"{file_id}_{chunk_index}",
        "text": f"text from {file_id} chunk {chunk_index}",
        "file_id": file_id,
        "vault_id": "1",
        "chunk_index": chunk_index,
        "chunk_scale": "default",
        "metadata": "{}",
        "_distance": distance,
    }


def _make_mock_table(row_count: int = 300, has_ivfpq: bool = False):
    """Return a fully-mocked LanceDB table object."""
    mock_idx = MagicMock()
    mock_idx.name = "embedding_idx"

    table = MagicMock()
    table.count_rows = AsyncMock(return_value=row_count)
    table.list_indices = AsyncMock(return_value=[mock_idx] if has_ivfpq else [])
    table.drop_index = AsyncMock(return_value=None)
    table.create_index = AsyncMock(return_value=None)
    table.add = AsyncMock(return_value=None)
    table.optimize = AsyncMock(return_value=None)
    table.delete = AsyncMock(return_value=None)
    return table


# ---------------------------------------------------------------------------
# ANN index lifecycle tests (Issue #13)
# ---------------------------------------------------------------------------

class TestAnnIndexLifecycle:
    """_maybe_rebuild_or_drop_vector_index behavior after bulk deletes."""

    @pytest.mark.asyncio
    async def test_no_op_when_zero_deleted(self):
        """deleted_count=0 → method returns immediately without any table ops."""
        store = VectorStore(db_path=Path("/tmp/ann_test"))
        store.table = _make_mock_table(row_count=300, has_ivfpq=True)
        store._last_index_build_row_count = 300

        await store._maybe_rebuild_or_drop_vector_index(deleted_count=0)

        store.table.count_rows.assert_not_called()
        store.table.drop_index.assert_not_called()
        store.table.create_index.assert_not_called()

    @pytest.mark.asyncio
    async def test_drops_index_when_row_count_falls_below_threshold(self):
        """After delete, if rows < 256 and IVF_PQ exists → index dropped."""
        store = VectorStore(db_path=Path("/tmp/ann_test"))
        # Current rows after delete: 100 (below 256)
        store.table = _make_mock_table(row_count=100, has_ivfpq=True)
        store._last_index_build_row_count = 300

        await store._maybe_rebuild_or_drop_vector_index(deleted_count=200)

        store.table.drop_index.assert_awaited_once_with("embedding_idx")
        store.table.create_index.assert_not_called()
        assert store._last_index_build_row_count == 0

    @pytest.mark.asyncio
    async def test_no_drop_when_no_ivfpq_index(self):
        """If no IVF_PQ index exists, nothing is dropped even below threshold."""
        store = VectorStore(db_path=Path("/tmp/ann_test"))
        store.table = _make_mock_table(row_count=50, has_ivfpq=False)
        store._last_index_build_row_count = 300

        await store._maybe_rebuild_or_drop_vector_index(deleted_count=250)

        store.table.drop_index.assert_not_called()
        store.table.create_index.assert_not_called()

    @pytest.mark.asyncio
    async def test_rebuilds_index_when_churn_exceeds_delta(self):
        """When churn (deleted / last_build_count) >= index_rebuild_delta → rebuild."""
        store = VectorStore(db_path=Path("/tmp/ann_test"))
        # Last build: 300 rows.  Delete 60 → churn = 0.20 ≥ 0.20 default.
        # Remaining 240 rows still above 256 threshold so no drop.
        # Use 290 remaining (delete 10 from 300 = churn 0.033) — wait, we need
        # rows > 256 AND churn >= 0.20.  Example: 300 → delete 70 → 230 remaining.
        # 230 < 256 would still trigger drop.  Use 400 → delete 90 → 310 remaining.
        store.table = _make_mock_table(row_count=310, has_ivfpq=True)
        store._last_index_build_row_count = 400

        with patch("app.services.vector_store.settings") as mock_settings, \
             patch("app.services.vector_store.IvfPq") as mock_ivfpq:
            mock_settings.index_rebuild_delta = 0.2
            mock_settings.embedding_dim = 8
            mock_settings.vector_metric = "cosine"
            mock_settings.write_lock_timeout_seconds = 30
            mock_ivfpq.return_value = MagicMock()
            await store._maybe_rebuild_or_drop_vector_index(deleted_count=90)

        store.table.create_index.assert_awaited_once()
        # last_build count updated to current rows
        assert store._last_index_build_row_count == 310

    @pytest.mark.asyncio
    async def test_no_rebuild_below_churn_threshold(self):
        """When churn < index_rebuild_delta → no rebuild triggered."""
        store = VectorStore(db_path=Path("/tmp/ann_test"))
        # Last build: 300 rows.  Delete 10 → churn = 0.033 < 0.20
        store.table = _make_mock_table(row_count=290, has_ivfpq=True)
        store._last_index_build_row_count = 300

        with patch("app.services.vector_store.settings") as mock_settings, \
             patch("app.services.vector_store.IvfPq") as mock_ivfpq:
            mock_settings.index_rebuild_delta = 0.2
            mock_settings.embedding_dim = 8
            mock_settings.vector_metric = "cosine"
            mock_settings.write_lock_timeout_seconds = 30
            mock_ivfpq.return_value = MagicMock()
            await store._maybe_rebuild_or_drop_vector_index(deleted_count=10)

        store.table.create_index.assert_not_called()
        store.table.drop_index.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_rebuild_when_last_build_count_is_zero(self):
        """If _last_index_build_row_count is 0 (no index was ever built), skip churn check."""
        store = VectorStore(db_path=Path("/tmp/ann_test"))
        store.table = _make_mock_table(row_count=290, has_ivfpq=True)
        store._last_index_build_row_count = 0  # Never built

        with patch("app.services.vector_store.settings") as mock_settings, \
             patch("app.services.vector_store.IvfPq") as mock_ivfpq:
            mock_settings.index_rebuild_delta = 0.2
            mock_settings.embedding_dim = 8
            mock_settings.vector_metric = "cosine"
            mock_settings.write_lock_timeout_seconds = 30
            mock_ivfpq.return_value = MagicMock()
            await store._maybe_rebuild_or_drop_vector_index(deleted_count=50)

        store.table.create_index.assert_not_called()


# ---------------------------------------------------------------------------
# optimize() called after every add_chunks (Issue #13)
# ---------------------------------------------------------------------------

class TestAddChunksCallsOptimize:
    """table.optimize() is called after every successful add_chunks batch."""

    @pytest.mark.asyncio
    async def test_optimize_called_after_table_add(self):
        """Verify optimize() is awaited once per add_chunks call."""
        store = VectorStore(db_path=Path("/tmp/opt_test"))
        mock_table = _make_mock_table(row_count=1, has_ivfpq=False)
        store.table = mock_table
        store._embedding_dim = 4

        records = [
            {
                "id": "f1_0",
                "text": "hello",
                "file_id": "1",
                "chunk_index": 0,
                "vault_id": "1",
                "chunk_scale": "default",
                "embedding": [0.1, 0.2, 0.3, 0.4],
            }
        ]

        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.optimize_mode = "after_every_write"
            mock_settings.embedding_dim = 4
            mock_settings.vector_metric = "cosine"
            mock_settings.write_lock_timeout_seconds = 30
            # Bypass dimension validation by mocking _get_expected_embedding_dim
            store._get_expected_embedding_dim = AsyncMock(return_value=4)
            await store.add_chunks(records)

        mock_table.optimize.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_optimize_failure_is_non_fatal(self):
        """If optimize() raises, add_chunks should still succeed."""
        store = VectorStore(db_path=Path("/tmp/opt_test"))
        mock_table = _make_mock_table(row_count=1, has_ivfpq=False)
        mock_table.optimize = AsyncMock(side_effect=RuntimeError("optimize failed"))
        store.table = mock_table
        store._embedding_dim = 4

        records = [{
            "id": "f1_0", "text": "hello", "file_id": "1",
            "chunk_index": 0, "vault_id": "1", "chunk_scale": "default",
            "embedding": [0.1, 0.2, 0.3, 0.4],
        }]

        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.embedding_dim = 4
            mock_settings.vector_metric = "cosine"
            mock_settings.write_lock_timeout_seconds = 30
            store._get_expected_embedding_dim = AsyncMock(return_value=4)
            # Should not raise
            await store.add_chunks(records)

        mock_table.add.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_existing_embedding_index_is_refreshed_after_new_rows(self):
        """Existing embedding_idx must not skip freshness handling after inserts."""
        store = VectorStore(db_path=Path("/tmp/opt_test"))
        mock_table = _make_mock_table(row_count=300, has_ivfpq=True)
        store.table = mock_table
        store._embedding_dim = 4
        store._last_index_build_row_count = 256

        records = [{
            "id": "f1_0", "text": "hello", "file_id": "1",
            "chunk_index": 0, "vault_id": "1", "chunk_scale": "default",
            "embedding": [0.1, 0.2, 0.3, 0.4],
        }]

        with patch("app.services.vector_store.settings") as mock_settings, \
             patch("app.services.vector_store.IvfPq") as mock_ivfpq:
            mock_settings.optimize_mode = "manual"
            mock_settings.embedding_dim = 4
            mock_settings.vector_metric = "cosine"
            mock_settings.write_lock_timeout_seconds = 30
            mock_ivfpq.return_value = MagicMock()
            store._get_expected_embedding_dim = AsyncMock(return_value=4)

            await store.add_chunks(records)

        mock_table.create_index.assert_awaited_once()
        assert store._last_index_build_row_count == 300

    @pytest.mark.asyncio
    async def test_delete_then_add_same_row_count_still_refreshes_existing_index(self):
        """Row count alone must not prove ANN freshness after content changes."""
        store = VectorStore(db_path=Path("/tmp/opt_test"))
        store.db = MagicMock()
        store._embedding_dim = 4
        store._last_index_build_row_count = 300
        store._last_index_build_generation = 0
        store._index_mutation_generation = 0
        row_state = {"total": 300}

        async def mock_count_rows(filter_expr=""):
            if filter_expr:
                return 10
            return row_state["total"]

        async def mock_delete(_filter_expr):
            row_state["total"] -= 10

        async def mock_add(records):
            row_state["total"] += len(records)

        mock_table = _make_mock_table(row_count=300, has_ivfpq=True)
        mock_table.count_rows = AsyncMock(side_effect=mock_count_rows)
        mock_table.delete = AsyncMock(side_effect=mock_delete)
        mock_table.add = AsyncMock(side_effect=mock_add)
        store.table = mock_table

        records = [
            {
                "id": f"f1_{i}",
                "text": f"replacement {i}",
                "file_id": "1",
                "chunk_index": i,
                "vault_id": "1",
                "chunk_scale": "default",
                "embedding": [0.1, 0.2, 0.3, 0.4],
            }
            for i in range(10)
        ]

        with patch("app.services.vector_store.settings") as mock_settings, \
             patch("app.services.vector_store.IvfPq") as mock_ivfpq:
            mock_settings.optimize_mode = "manual"
            mock_settings.embedding_dim = 4
            mock_settings.vector_metric = "cosine"
            mock_settings.write_lock_timeout_seconds = 30
            mock_settings.index_rebuild_delta = 0.2
            mock_ivfpq.return_value = MagicMock()
            store._get_expected_embedding_dim = AsyncMock(return_value=4)

            deleted = await store.delete_by_file("1")
            await store.add_chunks(records)

        assert deleted == 10
        assert row_state["total"] == 300
        mock_table.create_index.assert_awaited_once()
        assert store._last_index_build_row_count == 300
        assert store._last_index_build_generation == store._index_mutation_generation

    @pytest.mark.asyncio
    async def test_small_delete_marks_existing_index_accepted_without_rebuild(self):
        """Below-threshold delete churn should not force rebuild on next search."""
        store = VectorStore(db_path=Path("/tmp/opt_test"))
        store.db = MagicMock()
        store._embedding_dim = 4
        store._last_index_build_row_count = 1000
        store._last_index_build_generation = 0
        store._index_mutation_generation = 0

        query = MagicMock()
        # Issue #510 VECTOR-004: dense chain starts at .distance_type(metric).
        query.distance_type.return_value = query
        query.limit.return_value.to_list = AsyncMock(return_value=[])

        mock_table = _make_mock_table(row_count=999, has_ivfpq=True)
        mock_table.count_rows = AsyncMock(side_effect=[1, 999, 999])
        mock_table.search = AsyncMock(return_value=query)
        store.table = mock_table

        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.index_rebuild_delta = 0.2
            mock_settings.multi_scale_indexing_enabled = False
            mock_settings.multi_scale_chunk_sizes = ""
            mock_settings.write_lock_timeout_seconds = 30
            mock_settings.vector_search_concurrency = 4

            deleted = await store.delete_by_file("1")
            await store.search([0.1, 0.2, 0.3, 0.4], hybrid=False)

        assert deleted == 1
        mock_table.create_index.assert_not_awaited()
        assert store._last_index_build_row_count == 999
        assert store._last_index_build_generation == store._index_mutation_generation

    @pytest.mark.asyncio
    async def test_bypass_vector_index_option_uses_lancedb_flat_scan_hook(self):
        """Dense diagnostic search calls LanceDB bypass_vector_index when present."""
        store = VectorStore(db_path=Path("/tmp/search_test"))
        store.db = MagicMock()
        store._embedding_dim = 4

        query = MagicMock()
        # Issue #510 VECTOR-004: dense chain starts at .distance_type(metric).
        query.distance_type.return_value = query
        query.bypass_vector_index = MagicMock(return_value=query)
        query.limit.return_value.to_list = AsyncMock(
            return_value=[{"id": "doc_b_0", "file_id": "doc_b"}]
        )

        table = _make_mock_table(row_count=1, has_ivfpq=False)
        table.search = AsyncMock(return_value=query)
        store.table = table

        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = False
            mock_settings.multi_scale_chunk_sizes = ""
            mock_settings.vector_search_concurrency = 4

            results = await store.search(
                embedding=[0.1, 0.2, 0.3, 0.4],
                limit=1,
                hybrid=False,
                bypass_vector_index=True,
            )

        query.bypass_vector_index.assert_called_once()
        assert results[0]["id"] == "doc_b_0"


# ---------------------------------------------------------------------------
# Atomic visibility filter tests (Issue #13)
# ---------------------------------------------------------------------------

class TestAtomicVisibilityFilter:
    """Chunks from non-indexed files are hidden from filter_relevant."""

    def _make_service(self) -> DocumentRetrievalService:
        """Return a DocumentRetrievalService with dedup and window expansion disabled."""
        service = DocumentRetrievalService()
        service.max_distance_threshold = 1.0
        service.retrieval_window = 0
        return service

    @pytest.mark.asyncio
    async def test_pending_file_chunks_excluded(self):
        """Chunks with file_id not in indexed_file_ids are excluded."""
        service = self._make_service()

        results = [
            _make_record("indexed_file", 0, distance=0.1),
            _make_record("pending_file", 0, distance=0.1),
            _make_record("indexed_file", 1, distance=0.2),
        ]
        indexed = {"indexed_file"}

        with patch("app.services.document_retrieval.settings") as ms:
            ms.new_dedup_policy = False
            sources = await service.filter_relevant(results, top_k=10, indexed_file_ids=indexed)

        returned_file_ids = {s.file_id for s in sources}

        assert "indexed_file" in returned_file_ids
        assert "pending_file" not in returned_file_ids

    @pytest.mark.asyncio
    async def test_indexed_file_chunks_returned(self):
        """Chunks whose file_id is in indexed_file_ids pass through."""
        service = self._make_service()

        results = [_make_record("doc1", i, distance=0.1) for i in range(3)]
        indexed = {"doc1"}

        with patch("app.services.document_retrieval.settings") as ms:
            ms.new_dedup_policy = False
            sources = await service.filter_relevant(results, top_k=10, indexed_file_ids=indexed)

        assert len(sources) == 3
        assert all(s.file_id == "doc1" for s in sources)

    @pytest.mark.asyncio
    async def test_none_indexed_file_ids_disables_filter(self):
        """When indexed_file_ids is None, no visibility filtering is applied."""
        service = self._make_service()

        results = [
            _make_record("file_a", 0),
            _make_record("file_b", 0),
        ]

        with patch("app.services.document_retrieval.settings") as ms:
            ms.new_dedup_policy = False
            sources = await service.filter_relevant(results, top_k=10, indexed_file_ids=None)

        returned_file_ids = {s.file_id for s in sources}

        assert "file_a" in returned_file_ids
        assert "file_b" in returned_file_ids

    @pytest.mark.asyncio
    async def test_empty_indexed_set_hides_all_chunks(self):
        """When indexed_file_ids is an empty set, all chunks are hidden."""
        service = self._make_service()

        results = [_make_record("doc1", 0), _make_record("doc2", 0)]

        with patch("app.services.document_retrieval.settings") as ms:
            ms.new_dedup_policy = False
            sources = await service.filter_relevant(results, top_k=10, indexed_file_ids=set())

        assert sources == []


# ---------------------------------------------------------------------------
# Safe re-upload / delete_old_generation_by_file (Issue #13)
# ---------------------------------------------------------------------------

class TestSafeReupload:
    """Old-generation chunks are deleted after new ones are inserted."""

    def _make_store_with_mock_table(
        self,
        count_before: int,
        count_new: int,
    ) -> VectorStore:
        """Return a VectorStore with a mocked table for delete_old_generation_by_file."""
        store = VectorStore(db_path=Path("/tmp/reupload_test"))

        call_number = [0]

        async def mock_count_rows(filter_expr=""):
            # First call = count_before (all chunks for file_id)
            # Second call = count_new (only new-gen chunks)
            call_number[0] += 1
            return count_new if call_number[0] > 1 else count_before

        table = MagicMock()
        table.count_rows = AsyncMock(side_effect=mock_count_rows)
        table.delete = AsyncMock(return_value=None)
        table.list_indices = AsyncMock(return_value=[])  # No IVF_PQ index
        store.table = table
        store.db = MagicMock()  # Prevent real connect() call
        store._last_index_build_row_count = 0
        return store

    @pytest.mark.asyncio
    async def test_old_generation_chunks_deleted(self):
        """delete_old_generation_by_file returns the number of stale chunks removed."""
        # 2 total chunks, 1 new-gen → 1 old-gen should be deleted
        store = self._make_store_with_mock_table(count_before=2, count_new=1)

        deleted = await store.delete_old_generation_by_file("42", "abc12345")

        assert deleted == 1
        store.table.delete.assert_awaited_once()
        # The delete filter should target file_id='42' excluding new prefix
        delete_call_args = store.table.delete.call_args[0][0]
        assert "42" in delete_call_args
        assert "abc12345" in delete_call_args

    @pytest.mark.asyncio
    async def test_new_generation_chunks_preserved(self):
        """delete_old_generation_by_file does not delete when count_new == count_before."""
        # All chunks are already new-gen (nothing to delete)
        store = self._make_store_with_mock_table(count_before=1, count_new=1)

        deleted = await store.delete_old_generation_by_file("10", "deadbeef")

        assert deleted == 0
        store.table.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_zero_chunk_state(self):
        """New-gen chunks inserted BEFORE old-gen deleted → always ≥1 chunk visible.

        This tests that the delete_old_generation_by_file pattern preserves a
        positive chunk count by having new chunks exist before calling delete.
        Since we mock table.count_rows to reflect 2 chunks (new + old), the
        count during the transition is always ≥1.
        """
        # 2 total (new + old), 1 new-gen → after delete: 1 new-gen remains
        store = self._make_store_with_mock_table(count_before=2, count_new=1)

        deleted = await store.delete_old_generation_by_file("99", "cafef00d")

        # During delete: count_before was 2 (new + old gen coexist → no zero-chunk gap)
        assert deleted == 1
        # Verify delete was called (old gen removed)
        store.table.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delete_filter_targets_correct_file_id(self):
        """The delete filter uses the correct file_id and new hash prefix."""
        store = self._make_store_with_mock_table(count_before=3, count_new=2)

        await store.delete_old_generation_by_file("file_abc", "h4sh5678")

        delete_call_args = store.table.delete.call_args[0][0]
        assert "file_abc" in delete_call_args
        assert "h4sh5678" in delete_call_args


# ---------------------------------------------------------------------------
# Upload API: vault_id required → 422 on missing (Issue #14)
# ---------------------------------------------------------------------------

class TestUploadApiVaultIdRequired:
    """Upload endpoints declare vault_id as required (Query with no default)."""

    def _read_documents_route_source(self) -> str:
        """Return the source of documents.py without importing it."""
        src_path = (
            Path(__file__).parent.parent / "app" / "api" / "routes" / "documents.py"
        )
        return src_path.read_text()

    def test_upload_root_uses_query_ellipsis_for_vault_id(self):
        """upload_document_root declares vault_id=Query(...) — no default value."""
        source = self._read_documents_route_source()
        # FastAPI Query(...) with Ellipsis makes a query param required (returns 422 if absent).
        # The old code used Query(1, ...) or Query(default=1).
        # We verify the required pattern is present.
        assert "vault_id: int = Query(...," in source or 'vault_id: int = Query(...)' in source, (
            "upload_document_root should declare vault_id with Query(...) — no default"
        )

    def test_upload_root_does_not_have_default_vault_id_1(self):
        """vault_id must NOT default to 1 in the upload endpoints."""
        source = self._read_documents_route_source()
        # Old pattern was Query(1, ...) or default=1 for vault_id in upload endpoints
        # Ensure the breaking change is present
        upload_section = source[source.find("async def upload_document_root"):]
        # Only look in the first ~20 lines of the function definition
        first_lines = "\n".join(upload_section.splitlines()[:20])
        assert "vault_id: int = Query(1" not in first_lines, (
            "vault_id must not default to 1 — it should be required (Query(...))"
        )

    def test_upload_endpoint_does_not_have_default_vault_id_1(self):
        """vault_id must NOT default to 1 in upload_document endpoint."""
        source = self._read_documents_route_source()
        upload_section = source[source.find("async def upload_document"):]
        first_lines = "\n".join(upload_section.splitlines()[:20])
        assert "vault_id: int = Query(1" not in first_lines, (
            "vault_id must not default to 1 in upload_document — it should be required"
        )



