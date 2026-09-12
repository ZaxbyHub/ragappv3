"""
Integration tests for performance optimization changes:
- Task 3.1: fail_fast backward compatibility
- Task 3.3: Parallel overflow retry ordering (issue #258 TEST-007: injected at
  the HTTP boundary so the REAL _embed_batch_with_retry / _handle_overflow_retry
  split executes — no patching of the method under test)
- Task 2.5: Optimize mode conditional execution
"""
import json
import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Stub missing optional dependencies
try:
    import lancedb
except ImportError:
    import types
    sys.modules['lancedb'] = types.ModuleType('lancedb')

try:
    import pyarrow
except ImportError:
    import types
    sys.modules['pyarrow'] = types.ModuleType('pyarrow')

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.services.embeddings import EmbeddingError, EmbeddingService


# Helper for async mock
class AsyncMock(MagicMock):
    async def __call__(self, *args, **kwargs):
        return super(AsyncMock, self).__call__(*args, **kwargs)


@pytest.fixture(autouse=True)
def mock_settings():
    """Mock settings for all tests."""
    with patch('app.services.embeddings.settings') as mock_settings, \
         patch('app.services.embeddings.assert_url_safe'):
        # TEI-mode endpoint: /embed path resolves to mode="tei" so
        # _embed_batch_api takes the native batch route through the REAL
        # _embed_batch_with_retry (the legacy /api/embeddings URL would
        # fan out per-item instead — issue #258 TEST-007 rewrite).
        mock_settings.ollama_embedding_url = "http://127.0.0.1:8080/embed"
        mock_settings.embedding_model = "nomic-embed-text"
        mock_settings.embedding_doc_prefix = ""
        mock_settings.embedding_query_prefix = ""
        mock_settings.embedding_batch_size = 64
        mock_settings.embedding_batch_max_retries = 3
        mock_settings.embedding_batch_min_sub_size = 1
        mock_settings.embedding_concurrent_batches = 4
        # Issue #513 W23: embed_batch now also bounds batches by a total-char
        # budget read live from settings — production default supplied here so
        # the count-bound semantics these tests exercise are unchanged.
        mock_settings.embedding_batch_max_chars = 131072
        mock_settings.chunk_size_chars = 1200
        mock_settings.chunk_overlap_chars = 120
        yield mock_settings


class TestFailFast:
    """Tests for embed_batch fail_fast parameter (Task 3.1)."""

    @pytest.mark.asyncio
    async def test_fail_fast_true_raises_on_failure(self):
        """fail_fast=True (default): raises EmbeddingError on batch failure."""
        service = EmbeddingService()
        texts = ["text1", "text2"]

        # Mock the httpx client to return an error for the batch call
        with patch('app.services.embeddings.EmbeddingService._embed_batch_api') as mock_api:
            mock_api.side_effect = EmbeddingError("API error")

            with pytest.raises(EmbeddingError, match="Embedding batch failed"):
                await service.embed_batch(texts, batch_size=2)

    @pytest.mark.asyncio
    async def test_fail_fast_false_returns_tuple_on_failure(self):
        """fail_fast=False: returns (embeddings, failed_indices) on batch failure."""
        service = EmbeddingService()
        texts = ["text1", "text2"]

        with patch('app.services.embeddings.EmbeddingService._embed_batch_api') as mock_api:
            mock_api.side_effect = EmbeddingError("API error")

            result = await service.embed_batch(texts, batch_size=2, fail_fast=False)
            assert isinstance(result, tuple)
            assert len(result) == 2
            embeddings, failed_indices = result
            assert len(embeddings) == 2  # full-length list with None placeholders
            assert embeddings == [None, None]  # both positions are None (batch failed)
            assert 0 in failed_indices

    @pytest.mark.asyncio
    async def test_fail_fast_false_partial_success(self):
        """fail_fast=False: partial success returns succeeded embeddings + failed indices."""
        service = EmbeddingService()
        texts = ["good1", "good2", "bad1", "good3"]

        # Return error for one batch, success for another
        call_count = 0
        async def mock_batch_api(batch_texts, config=None):
            nonlocal call_count
            call_count += 1
            if call_count == 2:  # Second batch fails
                raise EmbeddingError("Batch 2 failed")
            return [[0.1] * 1024] * len(batch_texts)

        with patch('app.services.embeddings.EmbeddingService._embed_batch_api') as mock_api:
            mock_api.side_effect = mock_batch_api

            result = await service.embed_batch(texts, batch_size=2, fail_fast=False)
            embeddings, failed_indices = result
            assert len(embeddings) > 0
            assert 1 in failed_indices  # batch index 1 failed

    @pytest.mark.asyncio
    async def test_fail_fast_false_empty_texts(self):
        """fail_fast=False with empty texts returns ([], [])."""
        service = EmbeddingService()
        result = await service.embed_batch([], fail_fast=False)
        embeddings, failed_indices = result
        assert embeddings == []
        assert failed_indices == []


class _OverflowEmbeddingServer:
    """Stateful mock embedding server for the HTTP boundary (issue #258 TEST-007).

    Requests carrying MORE than ``max_texts_per_request`` texts get a 500 whose
    body matches the llama.cpp token-overflow pattern ("input (N tokens) is too
    large") so production treats it as overflow; smaller requests get a 200
    with deterministic PER-TEXT vectors in the TEI raw-array shape.
    """

    def __init__(self, max_texts_per_request: int) -> None:
        self.max_texts_per_request = max_texts_per_request
        self.overflow_responses = 0
        self.ok_responses = 0
        self.served_batch_sizes: list = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        texts = payload.get("inputs", [])
        self.served_batch_sizes.append(len(texts))
        if len(texts) > self.max_texts_per_request:
            self.overflow_responses += 1
            tokens = len(texts) * 1024
            body = (
                f"{{\"error\":{{\"message\":\"input ({tokens} tokens) is too large, "
                f"current batch size exceeds the context window\","
                f"\"code\":500}}}}"
            )
            return httpx.Response(
                500, text=body, headers={"content-type": "application/json"}
            )
        self.ok_responses += 1
        return httpx.Response(
            200,
            text=json.dumps([_vector_for_text(t) for t in texts]),
            headers={"content-type": "application/json"},
        )


def _vector_for_text(text: str) -> list:
    """Deterministic per-text vector: distinct texts get distinct vectors."""
    signature = [ord(c) for c in text]
    return [float(len(text))] + [
        float((i * 131 + s * 31) % 997) for i, s in enumerate(signature)
    ]


class TestParallelOverflowRetry:
    """Tests for parallel overflow retry (Task 3.3), issue #258 TEST-007.

    The overflow is injected at the HTTP boundary (httpx.MockTransport on the
    service client), NOT by patching ``_embed_batch_with_retry`` — the legacy
    form mocked the method whose recovery is the named contract and then
    asserted tautologies. Here the REAL ``embed_batch`` → ``_embed_batch_api``
    → ``_embed_batch_with_retry`` → ``_handle_overflow_retry`` recursion
    executes against the mock wire.
    """

    def _service_with_mock_wire(self, server: _OverflowEmbeddingServer):
        """Build an EmbeddingService whose HTTP client rides the MockTransport."""
        service = EmbeddingService()
        # HTTP-boundary seam: swap the persistent client so every production
        # request (payload build, POST, status/parse handling) runs for real
        # against the mock wire.
        service._client = httpx.AsyncClient(
            transport=httpx.MockTransport(server.handler)
        )
        return service

    @pytest.mark.asyncio
    async def test_overflow_retry_order_preserved(self, mock_settings):
        """A successful recursive overflow preserves all 20 unique inputs, in order.

        20 texts -> 500 -> split -> 10+10 -> 500 -> split -> 5+5 -> 200 (with a
        5-text server limit): every returned embedding must be per-text EXACT
        (order + content) and pairwise unique — an all-None result or a
        reversed/duplicated split fails.
        """
        server = _OverflowEmbeddingServer(max_texts_per_request=5)
        service = self._service_with_mock_wire(server)
        try:
            texts = [
                f"issue-258 overflow order text #{i:02d} — unique payload"
                for i in range(20)
            ]

            result = await service.embed_batch(texts, batch_size=20, fail_fast=False)
            assert isinstance(result, tuple)
            embeddings, failed_indices = result

            # The real split executed at the HTTP layer: the 20-text request
            # overflowed, then smaller sub-batches were served (and succeeded).
            assert server.overflow_responses >= 1
            assert server.ok_responses >= 4  # four 5-text leaf batches
            assert any(
                0 < size < 20 for size in server.served_batch_sizes
            ), "no sub-batch was ever served — the split did not execute"

            # No failures, and exactly one embedding per input.
            assert failed_indices == []
            assert len(embeddings) == 20
            for i, (text, vector) in enumerate(zip(texts, embeddings)):
                assert isinstance(vector, list), f"vector {i} is not a list"
                assert vector == _vector_for_text(text), (
                    f"vector {i} does not match its input text "
                    f"(order or content corrupted)"
                )

            # All 20 embeddings are pairwise unique.
            assert len({tuple(v) for v in embeddings}) == 20, (
                "duplicate embeddings returned by the split"
            )
        finally:
            await service.close()

    @pytest.mark.asyncio
    async def test_overflow_retry_exhaustion_reports_failed_batch(self, mock_settings):
        """Overflow that cannot split far enough fails the batch — visibly.

        An always-overflowing server drives the real recursion down to the
        minimum split size (the parallel gather fails and the sequential
        fallback branch executes inside _handle_overflow_retry); with
        fail_fast=False the batch is reported in failed_indices with None
        placeholders — never a silent short or reordered success.
        """
        # Stop the split at 8 texts so the bounded-retry tree stays shallow.
        mock_settings.embedding_batch_min_sub_size = 8
        # Threshold 0 = EVERY request overflows: the recursion can never
        # succeed, so it must terminate via the minimum-split-size guard.
        server = _OverflowEmbeddingServer(max_texts_per_request=0)
        service = self._service_with_mock_wire(server)
        try:
            texts = [f"issue-258 exhaust text #{i:02d}" for i in range(20)]

            result = await service.embed_batch(texts, batch_size=20, fail_fast=False)
            assert isinstance(result, tuple)
            embeddings, failed_indices = result

            # The retry tree really ran over the wire: the oversized batch
            # overflowed and progressively smaller sub-batches were attempted.
            assert server.overflow_responses >= 3
            assert server.ok_responses == 0
            assert any(
                size < 20 for size in server.served_batch_sizes
            ), "no smaller sub-batch was attempted — the split did not execute"

            # The single 20-text batch is reported failed with a None
            # placeholder per text.
            assert failed_indices == [0]
            assert len(embeddings) == 20
            assert all(e is None for e in embeddings)
        finally:
            await service.close()


class TestOptimizeMode:
    """Tests for conditional optimize mode (Task 2.5)."""

    def _create_mock_table(self):
        """Create a mock LanceDB table with all required async methods."""
        mock_table = MagicMock()
        mock_table.schema = AsyncMock(return_value=MagicMock(
            field=MagicMock(return_value=MagicMock(type=MagicMock(list_size=1024)))
        ))
        mock_table.count_rows = AsyncMock(return_value=0)
        mock_table.add = AsyncMock(return_value=None)
        mock_table.list_indices = AsyncMock(return_value=[])
        mock_table.create_index = AsyncMock(return_value=None)
        return mock_table

    @pytest.mark.asyncio
    async def test_periodic_optimize_counter_increments(self):
        """periodic optimize mode increments counter on add_chunks."""
        from app.services.vector_store import VectorStore

        vs = VectorStore()
        vs.table = self._create_mock_table()

        # Set mode to periodic
        with patch('app.services.vector_store.settings') as mock_s:
            mock_s.optimize_mode = "periodic"
            mock_s.optimize_interval_chunks = 100
            mock_s.vector_metric = "cosine"
            mock_s.embedding_dim = 1024
            mock_s.index_rebuild_delta = 0.1
            mock_s.write_lock_timeout_seconds = 30.0
            mock_s.vector_search_concurrency = 4
            mock_s.rrf_legacy_mode = False
            mock_s.hybrid_rrf_k = 60
            mock_s.multi_scale_rrf_k = 60

            # Add a record
            await vs.add_chunks([{
                "id": "test1",
                "text": "test",
                "file_id": "1",
                "vault_id": "v1",
                "chunk_index": 0,
                "embedding": [0.1] * 1024
            }])
            assert vs._optimize_counter == 1

    @pytest.mark.asyncio
    async def test_periodic_optimize_triggers_at_interval(self):
        """periodic optimize mode triggers optimize() when counter reaches interval."""
        from app.services.vector_store import VectorStore

        vs = VectorStore()
        vs.table = self._create_mock_table()

        optimize_called = []

        async def mock_optimize():
            optimize_called.append(True)

        vs.table.optimize = mock_optimize

        # Set mode to periodic with interval of 2
        with patch('app.services.vector_store.settings') as mock_s:
            mock_s.optimize_mode = "periodic"
            mock_s.optimize_interval_chunks = 2
            mock_s.vector_metric = "cosine"
            mock_s.embedding_dim = 1024
            mock_s.index_rebuild_delta = 0.1
            mock_s.write_lock_timeout_seconds = 30.0
            mock_s.vector_search_concurrency = 4
            mock_s.rrf_legacy_mode = False
            mock_s.hybrid_rrf_k = 60
            mock_s.multi_scale_rrf_k = 60

            # Add first record - counter becomes 1, no optimize yet
            await vs.add_chunks([{
                "id": "test1",
                "text": "test1",
                "file_id": "1",
                "vault_id": "v1",
                "chunk_index": 0,
                "embedding": [0.1] * 1024
            }])
            assert vs._optimize_counter == 1
            assert len(optimize_called) == 0

            # Add second record - counter becomes 2, triggers optimize
            await vs.add_chunks([{
                "id": "test2",
                "text": "test2",
                "file_id": "1",
                "vault_id": "v1",
                "chunk_index": 1,
                "embedding": [0.1] * 1024
            }])
            assert vs._optimize_counter == 0  # Reset after optimize
            assert len(optimize_called) == 1

    @pytest.mark.asyncio
    async def test_after_every_write_optimize_called(self):
        """after_every_write mode calls optimize after each add_chunks."""
        from app.services.vector_store import VectorStore

        vs = VectorStore()
        vs.table = self._create_mock_table()

        optimize_called = []

        async def mock_optimize():
            optimize_called.append(True)

        vs.table.optimize = mock_optimize

        with patch('app.services.vector_store.settings') as mock_s:
            mock_s.optimize_mode = "after_every_write"
            mock_s.vector_metric = "cosine"
            mock_s.embedding_dim = 1024
            mock_s.index_rebuild_delta = 0.1
            mock_s.write_lock_timeout_seconds = 30.0
            mock_s.vector_search_concurrency = 4
            mock_s.rrf_legacy_mode = False
            mock_s.hybrid_rrf_k = 60
            mock_s.multi_scale_rrf_k = 60

            # Add a record
            await vs.add_chunks([{
                "id": "test1",
                "text": "test",
                "file_id": "1",
                "vault_id": "v1",
                "chunk_index": 0,
                "embedding": [0.1] * 1024
            }])
            assert len(optimize_called) == 1

    @pytest.mark.asyncio
    async def test_manual_optimize_not_called(self):
        """manual mode does not call optimize during add_chunks."""
        from app.services.vector_store import VectorStore

        vs = VectorStore()
        vs.table = self._create_mock_table()

        optimize_called = []

        async def mock_optimize():
            optimize_called.append(True)

        vs.table.optimize = mock_optimize

        with patch('app.services.vector_store.settings') as mock_s:
            mock_s.optimize_mode = "manual"
            mock_s.vector_metric = "cosine"
            mock_s.embedding_dim = 1024
            mock_s.index_rebuild_delta = 0.1
            mock_s.write_lock_timeout_seconds = 30.0
            mock_s.vector_search_concurrency = 4
            mock_s.rrf_legacy_mode = False
            mock_s.hybrid_rrf_k = 60
            mock_s.multi_scale_rrf_k = 60

            # Add a record
            await vs.add_chunks([{
                "id": "test1",
                "text": "test",
                "file_id": "1",
                "vault_id": "v1",
                "chunk_index": 0,
                "embedding": [0.1] * 1024
            }])
            assert len(optimize_called) == 0
            assert vs._optimize_counter == 0  # Counter doesn't increment in manual mode
