"""Phase 2 coverage gap tests for expand_window ordering and hybrid_alpha clamping.

This module contains mandatory tests to fill coverage gaps identified during Phase 2:
1. expand_window document ordering (relevance group order)
2. rrf_fuse receiving clamped hybrid_alpha weights
3. hybrid_alpha clamping logic at extremes
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.document_retrieval import DocumentRetrievalService, RAGSource


class TestExpandWindowRelevanceGroupOrder:
    """Tests for expand_window document group ordering.

    Verify that expand_window preserves cross-document relevance ranking while
    sorting chunks within each document by reading order (chunk_index).
    """

    @pytest.fixture
    def mock_vector_store(self):
        """Create a mock vector store that returns adjacent chunks."""
        store = MagicMock()
        store.get_chunks_by_uid = AsyncMock(return_value=[
            # Doc A chunk 2 (adjacent to chunk 3)
            {"id": "A_2", "text": "Doc A chunk 2", "file_id": "A",
             "metadata": {"chunk_index": 2}, "_distance": 0.5},
            # Doc A chunk 4 (adjacent to chunk 3)
            {"id": "A_4", "text": "Doc A chunk 4", "file_id": "A",
             "metadata": {"chunk_index": 4}, "_distance": 0.6},
            # Doc A chunk 6 (adjacent to chunk 7)
            {"id": "A_6", "text": "Doc A chunk 6", "file_id": "A",
             "metadata": {"chunk_index": 6}, "_distance": 0.7},
            # Doc A chunk 8 (adjacent to chunk 7)
            {"id": "A_8", "text": "Doc A chunk 8", "file_id": "A",
             "metadata": {"chunk_index": 8}, "_distance": 0.8},
            # Doc B chunk 4 (adjacent to chunk 5)
            {"id": "B_4", "text": "Doc B chunk 4", "file_id": "B",
             "metadata": {"chunk_index": 4}, "_distance": 0.3},
        ])
        return store

    @pytest.fixture
    def retrieval_service(self, mock_vector_store):
        """Create a DocumentRetrievalService with mock vector store."""
        service = DocumentRetrievalService(
            vector_store=mock_vector_store,
            retrieval_window=1,
            retrieval_top_k=10,
        )
        return service

    @pytest.mark.asyncio
    async def test_expand_window_preserves_relevance_group_order(self, retrieval_service):
        """Doc B should come first (best chunk ranked highest), then Doc A chunks.

        Input order: Doc B chunk 5 (rank 1), Doc A chunk 3 (rank 2), Doc A chunk 7 (rank 3)
        Expected output order:
          1. Doc B (its best chunk ranks first overall)
          2. Doc A chunks sorted by chunk_index (reading order: 3, 4, 6, 7, 8)
        """
        # Create input sources in ranked order
        sources = [
            RAGSource(
                text="Doc B chunk 5",
                file_id="B",
                score=0.2,
                metadata={"chunk_index": 5, "chunk_scale": "default"},
            ),
            RAGSource(
                text="Doc A chunk 3",
                file_id="A",
                score=0.4,
                metadata={"chunk_index": 3, "chunk_scale": "default"},
            ),
            RAGSource(
                text="Doc A chunk 7",
                file_id="A",
                score=0.6,
                metadata={"chunk_index": 7, "chunk_scale": "default"},
            ),
        ]

        # Execute expand_window
        expanded = await retrieval_service.expand_window(sources)

        # Extract file_ids in order
        file_ids = [s.file_id for s in expanded]

        # Doc B should be first (best relevance rank)
        assert file_ids[0] == "B", f"Expected first file_id to be 'B', got '{file_ids[0]}'"

        # All Doc A chunks should follow, in chunk_index order
        doc_a_indices = [
            s.metadata.get("chunk_index")
            for s in expanded
            if s.file_id == "A"
        ]
        assert doc_a_indices == sorted(doc_a_indices), (
            f"Doc A chunks not in reading order: {doc_a_indices}"
        )

        # Verify the first chunk for each doc is the best-ranked one
        # Doc B first occurrence at position 0
        b_positions = [i for i, s in enumerate(expanded) if s.file_id == "B"]
        a_positions = [i for i, s in enumerate(expanded) if s.file_id == "A"]

        assert b_positions[0] < a_positions[0], (
            f"Doc B should rank before Doc A. B at {b_positions[0]}, A at {a_positions[0]}"
        )


class TestRRFFuseReceivesClampedWeights:
    """Tests that rrf_fuse is called with properly clamped hybrid_alpha weights.

    Verify that when hybrid_alpha is outside [0.0, 1.0], the weights passed to
    rrf_fuse are clamped to [0.0, 1.0].
    """

    @pytest.mark.asyncio
    async def test_rrf_fuse_called_with_0_6_alpha_weights(self):
        """hybrid_alpha=0.6 should result in weights=[0.6, 0.4] passed to rrf_fuse."""
        from app.services.vector_store import VectorStore

        vs = VectorStore(db_path=None)
        vs._fts_exceptions = 0

        # to_list must be async — chain: search() -> query -> limit() -> to_list()
        mock_query = MagicMock()
        mock_query.limit.return_value = mock_query
        mock_query.where.return_value = mock_query
        mock_query.to_list = AsyncMock(return_value=[])  # empty dense results

        vs.table = MagicMock()
        vs.table.search = AsyncMock(return_value=mock_query)

        # Patch rrf_fuse with a mock that returns empty list
        mock_rrf_fuse = MagicMock(return_value=[])
        with patch('app.services.vector_store.rrf_fuse', mock_rrf_fuse):
            await vs._search_single_scale(
                embedding=[0.1] * 128,
                scale="default",
                fetch_k=10,
                filter_expr=None,
                vault_id=None,
                query_text="test query",
                hybrid=True,
                hybrid_alpha=0.6,
            )

        assert mock_rrf_fuse.called, "rrf_fuse was not called"
        call_kwargs = mock_rrf_fuse.call_args.kwargs
        assert call_kwargs.get('weights') == [0.6, 0.4], (
            f"Expected weights=[0.6, 0.4] for alpha=0.6, got {call_kwargs.get('weights')}"
        )

    @pytest.mark.asyncio
    async def test_rrf_fuse_called_with_clamped_high_alpha(self):
        """hybrid_alpha=1.5 should be clamped to weights=[1.0, 0.0]."""
        from app.services.vector_store import VectorStore

        vs = VectorStore(db_path=None)
        vs._fts_exceptions = 0

        # to_list must be async — chain: search() -> query -> limit() -> to_list()
        mock_query = MagicMock()
        mock_query.limit.return_value = mock_query
        mock_query.where.return_value = mock_query
        mock_query.to_list = AsyncMock(return_value=[])  # empty dense results

        vs.table = MagicMock()
        vs.table.search = AsyncMock(return_value=mock_query)

        # Patch rrf_fuse with a mock that returns empty list
        mock_rrf_fuse = MagicMock(return_value=[])
        with patch('app.services.vector_store.rrf_fuse', mock_rrf_fuse):
            await vs._search_single_scale(
                embedding=[0.1] * 128,
                scale="default",
                fetch_k=10,
                filter_expr=None,
                vault_id=None,
                query_text="test query",
                hybrid=True,
                hybrid_alpha=1.5,
            )

        assert mock_rrf_fuse.called, "rrf_fuse was not called"
        call_kwargs = mock_rrf_fuse.call_args.kwargs
        assert call_kwargs.get('weights') == [1.0, 0.0], (
            f"Expected weights=[1.0, 0.0] for alpha=1.5 (clamped), got {call_kwargs.get('weights')}"
        )

    @pytest.mark.asyncio
    async def test_rrf_fuse_called_with_clamped_negative_alpha(self):
        """hybrid_alpha=-0.3 should be clamped to weights=[0.0, 1.0]."""
        from app.services.vector_store import VectorStore

        vs = VectorStore(db_path=None)
        vs._fts_exceptions = 0

        # to_list must be async — chain: search() -> query -> limit() -> to_list()
        mock_query = MagicMock()
        mock_query.limit.return_value = mock_query
        mock_query.where.return_value = mock_query
        mock_query.to_list = AsyncMock(return_value=[])  # empty dense results

        vs.table = MagicMock()
        vs.table.search = AsyncMock(return_value=mock_query)

        # Patch rrf_fuse with a mock that returns empty list
        mock_rrf_fuse = MagicMock(return_value=[])
        with patch('app.services.vector_store.rrf_fuse', mock_rrf_fuse):
            await vs._search_single_scale(
                embedding=[0.1] * 128,
                scale="default",
                fetch_k=10,
                filter_expr=None,
                vault_id=None,
                query_text="test query",
                hybrid=True,
                hybrid_alpha=-0.3,
            )

        assert mock_rrf_fuse.called, "rrf_fuse was not called"
        call_kwargs = mock_rrf_fuse.call_args.kwargs
        assert call_kwargs.get('weights') == [0.0, 1.0], (
            f"Expected weights=[0.0, 1.0] for alpha=-0.3 (clamped), got {call_kwargs.get('weights')}"
        )
