"""Regression tests: settings mutations propagate to running services live.

Validates the contract that an admin clicking "Save" in the Settings UI —
which calls ``setattr(settings, field, value)`` on the singleton — takes
effect on the next service call without restarting the process.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.document_processor import DocumentProcessor
from app.services.embeddings import EmbeddingService
from app.services.reranking import RerankingService

# ---------------------------------------------------------------------------
# EmbeddingService
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_embedding_settings():
    with patch("app.services.embeddings.settings") as mock, \
         patch("app.services.embeddings.assert_url_safe"):
        mock.ollama_embedding_url = "http://initial.example:11434/api/embeddings"
        mock.embedding_model = "initial-model"
        mock.embedding_doc_prefix = ""
        mock.embedding_query_prefix = ""
        yield mock


class TestEmbeddingServiceLiveReads:
    def test_model_change_visible_without_reinit(self, mock_embedding_settings):
        service = EmbeddingService()
        assert service.embedding_model == "initial-model"

        mock_embedding_settings.embedding_model = "swapped-model"

        assert service.embedding_model == "swapped-model"

    def test_url_change_visible_without_reinit(self, mock_embedding_settings):
        service = EmbeddingService()
        first_url = service.embeddings_url

        mock_embedding_settings.ollama_embedding_url = (
            "http://swapped.example:11434/api/embeddings"
        )

        assert service.embeddings_url != first_url
        assert "swapped.example" in service.embeddings_url

    def test_provider_mode_change_visible_without_reinit(
        self, mock_embedding_settings
    ):
        service = EmbeddingService()
        assert service.provider_mode == "ollama"

        # LM Studio default port → OpenAI mode
        mock_embedding_settings.ollama_embedding_url = "http://swapped.example:1234"

        assert service.provider_mode == "openai"

    def test_prefix_change_visible_without_reinit(self, mock_embedding_settings):
        service = EmbeddingService()
        assert service.embedding_doc_prefix == ""

        mock_embedding_settings.embedding_doc_prefix = "Passage: "

        assert service.embedding_doc_prefix == "Passage: "

    def test_qwen_auto_prefix_follows_live_model(self, mock_embedding_settings):
        service = EmbeddingService()
        # Initial model is not qwen — no auto-prefix
        assert service.embedding_doc_prefix == ""

        # Swap to a qwen model — auto-prefix kicks in
        mock_embedding_settings.embedding_model = "qwen3-embedding-0.6b"

        assert "Document:" in service.embedding_doc_prefix
        assert "Query:" in service.embedding_query_prefix


# ---------------------------------------------------------------------------
# RerankingService
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_reranking_settings():
    with patch("app.services.reranking.settings") as mock:
        mock.reranker_url = "http://initial.example:8080"
        mock.reranker_model = "initial-reranker"
        mock.reranker_top_n = 5
        yield mock


class TestRerankingServiceLiveReads:
    def test_no_args_init_reads_live_url(self, mock_reranking_settings):
        service = RerankingService()
        assert service.reranker_url == "http://initial.example:8080"

        mock_reranking_settings.reranker_url = "http://swapped.example:8080"

        assert service.reranker_url == "http://swapped.example:8080"

    def test_no_args_init_reads_live_model(self, mock_reranking_settings):
        service = RerankingService()
        assert service.reranker_model == "initial-reranker"

        mock_reranking_settings.reranker_model = "swapped-reranker"

        assert service.reranker_model == "swapped-reranker"

    def test_no_args_init_reads_live_top_n(self, mock_reranking_settings):
        service = RerankingService()
        assert service.top_n == 5

        mock_reranking_settings.reranker_top_n = 10

        assert service.top_n == 10

    def test_explicit_init_args_pin_value(self, mock_reranking_settings):
        """Test/backward-compat path: explicit args shadow live settings."""
        service = RerankingService(
            reranker_url="http://pinned.example:8080",
            reranker_model="pinned-model",
            top_n=3,
        )

        # Now mutate the underlying settings — the service should keep
        # using the pinned values from the constructor.
        mock_reranking_settings.reranker_url = "http://changed.example:8080"
        mock_reranking_settings.reranker_model = "changed-model"
        mock_reranking_settings.reranker_top_n = 99

        assert service.reranker_url == "http://pinned.example:8080"
        assert service.reranker_model == "pinned-model"
        assert service.top_n == 3

    def test_url_strip_trailing_slash(self, mock_reranking_settings):
        mock_reranking_settings.reranker_url = "http://example.com/"
        service = RerankingService()
        assert service.reranker_url == "http://example.com"

    def test_empty_settings_url_means_local_mode(self, mock_reranking_settings):
        mock_reranking_settings.reranker_url = ""
        service = RerankingService()
        assert service.reranker_url == ""


# ---------------------------------------------------------------------------
# RAGEngine
# ---------------------------------------------------------------------------

class TestRAGEngineLiveReads:
    """RAGEngine property-based live reads.

    These tests skip __init__ and check the property layer directly because
    constructing a full RAGEngine requires lancedb which is not installable in
    every CI environment.
    """

    def _make_engine_skeleton(self, mock_settings):
        """Create an RAGEngine without invoking __init__ to isolate property behavior."""
        # Import deferred so the mock is in place before any module-level reads.
        from app.services.rag_engine import RAGEngine
        engine = RAGEngine.__new__(RAGEngine)
        return engine

    @pytest.fixture
    def mock_settings(self):
        with patch("app.services.rag_engine.settings") as mock:
            mock.chunk_size_chars = 2000
            mock.chunk_overlap_chars = 200
            mock.retrieval_top_k = 5
            mock.vector_metric = "cosine"
            mock.max_distance_threshold = 0.7
            mock.embedding_doc_prefix = ""
            mock.embedding_query_prefix = ""
            mock.retrieval_window = 1
            mock.reranking_enabled = False
            mock.reranker_top_n = 5
            mock.initial_retrieval_top_k = 20
            mock.hybrid_search_enabled = False
            mock.hybrid_alpha = 0.5
            mock.rag_relevance_threshold = None
            mock.vector_top_k = None
            mock.maintenance_mode = False
            yield mock

    def test_retrieval_top_k_reads_live(self, mock_settings):
        engine = self._make_engine_skeleton(mock_settings)
        assert engine.retrieval_top_k == 5
        mock_settings.retrieval_top_k = 50
        assert engine.retrieval_top_k == 50

    def test_max_distance_threshold_reads_live(self, mock_settings):
        engine = self._make_engine_skeleton(mock_settings)
        assert engine.max_distance_threshold == 0.7
        mock_settings.max_distance_threshold = 0.3
        assert engine.max_distance_threshold == 0.3

    def test_hybrid_search_enabled_reads_live(self, mock_settings):
        engine = self._make_engine_skeleton(mock_settings)
        assert engine.hybrid_search_enabled is False
        mock_settings.hybrid_search_enabled = True
        assert engine.hybrid_search_enabled is True

    def test_hybrid_alpha_reads_live(self, mock_settings):
        engine = self._make_engine_skeleton(mock_settings)
        assert engine.hybrid_alpha == 0.5
        mock_settings.hybrid_alpha = 0.8
        assert engine.hybrid_alpha == 0.8

    def test_reranking_enabled_reads_live(self, mock_settings):
        engine = self._make_engine_skeleton(mock_settings)
        assert engine.reranking_enabled is False
        mock_settings.reranking_enabled = True
        assert engine.reranking_enabled is True

    def test_initial_retrieval_top_k_reads_live(self, mock_settings):
        engine = self._make_engine_skeleton(mock_settings)
        assert engine.initial_retrieval_top_k == 20
        mock_settings.initial_retrieval_top_k = 50
        assert engine.initial_retrieval_top_k == 50

    def test_instance_override_shadows_settings(self, mock_settings):
        """Tests rely on ``engine.field = X`` pinning the value."""
        engine = self._make_engine_skeleton(mock_settings)
        engine.retrieval_top_k = 99
        # Settings change does NOT override the per-instance pin
        mock_settings.retrieval_top_k = 7
        assert engine.retrieval_top_k == 99

    def test_instance_override_none_is_intentional(self, mock_settings):
        """Setting ``max_distance_threshold = None`` is a deliberate override,
        not a fallback to settings."""
        engine = self._make_engine_skeleton(mock_settings)
        engine.max_distance_threshold = None
        mock_settings.max_distance_threshold = 0.9
        assert engine.max_distance_threshold is None

    def test_maintenance_mode_reads_live(self, mock_settings):
        engine = self._make_engine_skeleton(mock_settings)
        assert engine.maintenance_mode is False
        mock_settings.maintenance_mode = True
        assert engine.maintenance_mode is True

    def test_sync_propagates_live_settings_to_document_retrieval(
        self, mock_settings
    ):
        """The production query path syncs live values into the DocumentRetrievalService
        before each filter_relevant call. Without this sync, the service keeps
        the snapshot it captured at engine construction.
        """
        from app.services.rag_engine import RAGEngine

        # Build an engine and a fake document_retrieval. Avoid __init__ so we
        # don't pull in lancedb-backed services in this constrained CI env.
        engine = RAGEngine.__new__(RAGEngine)
        fake_dr = MagicMock()
        fake_dr.max_distance_threshold = 0.99  # stale snapshot
        fake_dr.relevance_threshold = 0.99
        fake_dr.retrieval_top_k = 99
        fake_dr.retrieval_window = 99
        engine.document_retrieval = fake_dr
        engine.prompt_builder = MagicMock()

        # Admin changes settings via the UI
        mock_settings.max_distance_threshold = 0.3
        mock_settings.retrieval_top_k = 7
        mock_settings.retrieval_window = 2
        mock_settings.rag_relevance_threshold = 0.5

        engine._sync_document_retrieval_settings()

        assert fake_dr.max_distance_threshold == 0.3
        assert fake_dr.retrieval_top_k == 7
        assert fake_dr.retrieval_window == 2
        assert fake_dr.relevance_threshold == 0.5

    def test_sync_propagates_per_instance_overrides(self, mock_settings):
        """Pinned per-instance values (engine.X = Y) win over live settings."""
        from app.services.rag_engine import RAGEngine

        engine = RAGEngine.__new__(RAGEngine)
        fake_dr = MagicMock()
        engine.document_retrieval = fake_dr
        engine.prompt_builder = MagicMock()

        # Test pins values on the engine
        engine.retrieval_top_k = 42
        engine.max_distance_threshold = 0.1

        # Settings change after the pin — should NOT win
        mock_settings.retrieval_top_k = 99
        mock_settings.max_distance_threshold = 0.99

        engine._sync_document_retrieval_settings()

        assert fake_dr.retrieval_top_k == 42
        assert fake_dr.max_distance_threshold == 0.1

    @pytest.mark.asyncio
    async def test_retrieval_evaluator_uses_active_client(self, mock_settings):
        """Instant-mode retrieval evaluation should use the mode-selected LLM client."""
        from app.services.rag_engine import RAGEngine

        engine = RAGEngine.__new__(RAGEngine)
        engine.vector_store = MagicMock()
        engine.vector_store.search = AsyncMock(
            return_value=[
                {
                    "id": "chunk-1",
                    "file_id": "file-1",
                    "text": "Relevant text",
                    "_distance": 0.1,
                    "metadata": {},
                }
            ]
        )
        engine.vector_store.get_fts_exceptions.return_value = 0
        engine.reranking_service = None
        engine._retrieval_evaluators = {}

        mock_settings.retrieval_evaluation_enabled = True
        mock_settings.context_max_tokens = 0
        mock_settings.retrieval_recency_weight = 0.0
        mock_settings.rrf_legacy_mode = False
        mock_settings.exact_match_promote = False
        mock_settings.reranking_enabled = False
        mock_settings.hybrid_search_enabled = False

        instant_client = MagicMock()
        fake_evaluator = MagicMock()
        fake_evaluator.evaluate = AsyncMock(return_value="CONFIDENT")

        with patch(
            "app.services.rag_engine.RetrievalEvaluator",
            return_value=fake_evaluator,
        ) as evaluator_cls:
            await engine._execute_retrieval(
                [("original", [0.1, 0.2, 0.3])],
                "question",
                vault_id=1,
                active_client=instant_client,
            )

        evaluator_cls.assert_called_once_with(instant_client)
        fake_evaluator.evaluate.assert_awaited_once()


# ---------------------------------------------------------------------------
# DocumentProcessor chunker rebuild
# ---------------------------------------------------------------------------

class TestDocumentProcessorChunkerLiveReads:
    """Validates that admin-changed chunk_size_chars / chunk_overlap_chars
    take effect on the next ingested document without restart."""

    @pytest.fixture
    def mock_settings(self):
        with patch("app.services.document_processor.settings") as mock:
            mock.chunk_size_chars = 2000
            mock.chunk_overlap_chars = 200
            mock.sqlite_path = ":memory:"
            yield mock

    def test_get_chunker_returns_chunker_with_settings_values(self, mock_settings):
        processor = DocumentProcessor(chunk_size_chars=2000, chunk_overlap_chars=200)
        chunker = processor._get_chunker()
        assert chunker.chunk_size == 2000
        assert chunker.chunk_overlap == 200

    def test_get_chunker_rebuilds_on_settings_change(self, mock_settings):
        processor = DocumentProcessor(chunk_size_chars=2000, chunk_overlap_chars=200)
        original_chunker = processor._get_chunker()

        mock_settings.chunk_size_chars = 4096
        mock_settings.chunk_overlap_chars = 400

        new_chunker = processor._get_chunker()
        assert new_chunker is not original_chunker
        assert new_chunker.chunk_size == 4096
        assert new_chunker.chunk_overlap == 400

    def test_get_chunker_reuses_when_values_unchanged(self, mock_settings):
        processor = DocumentProcessor(chunk_size_chars=2000, chunk_overlap_chars=200)
        first = processor._get_chunker()
        second = processor._get_chunker()
        assert first is second

    def test_get_chunker_falls_back_to_init_values_when_settings_unset(
        self, mock_settings
    ):
        # Drift review (issue #513 W4 / frozen C3): "unset" is None, which
        # still falls back to the constructor values. An explicitly configured
        # 0 is a VALUE and is now honored (previously the falsy-zero bug
        # treated 0 as unset and silently fell back — that behavior is pinned
        # as wrong by frozen check C3).
        mock_settings.chunk_size_chars = None
        mock_settings.chunk_overlap_chars = None
        processor = DocumentProcessor(chunk_size_chars=1500, chunk_overlap_chars=150)
        chunker = processor._get_chunker()
        assert chunker.chunk_size == 1500
        assert chunker.chunk_overlap == 150

        # Explicit zeros are honored, not treated as unset (issue #513 W4).
        mock_settings.chunk_size_chars = 0
        mock_settings.chunk_overlap_chars = 0
        zero_chunker = processor._get_chunker()
        assert zero_chunker.chunk_size == 0
        assert zero_chunker.chunk_overlap == 0

    def test_get_chunker_default_strategy_returns_title_chunker(self, mock_settings):
        from app.services.chunking import SemanticChunker

        mock_settings.semantic_chunking_strategy = "title"
        processor = DocumentProcessor(chunk_size_chars=2000, chunk_overlap_chars=200)
        processor.embedding_service = MagicMock()
        chunker = processor._get_chunker()
        assert isinstance(chunker, SemanticChunker)

    def test_get_chunker_embedding_strategy_returns_embedding_chunker(
        self, mock_settings
    ):
        from app.services.chunking import EmbeddingSemanticChunker

        mock_settings.semantic_chunking_strategy = "embedding"
        processor = DocumentProcessor(chunk_size_chars=2000, chunk_overlap_chars=200)
        embedding_service = MagicMock()
        processor.embedding_service = embedding_service
        chunker = processor._get_chunker()
        assert isinstance(chunker, EmbeddingSemanticChunker)
        assert chunker.embedding_service is embedding_service
        assert chunker.max_chunk_size == 2000

    def test_get_chunker_embedding_strategy_without_service_falls_back(
        self, mock_settings
    ):
        from app.services.chunking import SemanticChunker

        mock_settings.semantic_chunking_strategy = "embedding"
        processor = DocumentProcessor(chunk_size_chars=2000, chunk_overlap_chars=200)
        processor.embedding_service = None
        chunker = processor._get_chunker()
        assert isinstance(chunker, SemanticChunker)
