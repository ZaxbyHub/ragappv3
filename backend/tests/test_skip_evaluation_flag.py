"""Tests for the skip_evaluation flag in _execute_retrieval and multi-sub-query orchestration.

Verifies:
1. _execute_retrieval accepts skip_evaluation=True and suppresses CRAG evaluation.
2. _execute_retrieval with skip_evaluation=False (default) runs evaluation normally.
3. The multi-sub-query orchestrator passes skip_evaluation=True for sub-queries.
4. The fallback path in multi-sub-query orchestrator uses skip_evaluation=False (default).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.chat_mode import ChatMode
from app.services.rag_engine import RAGEngine


def _make_engine_for_retrieval():
    """Construct a RAGEngine with the minimum wiring for _execute_retrieval."""
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
    return engine


class TestExecuteRetrievalSkipEvaluationFlag:
    """Tests for the skip_evaluation parameter on _execute_retrieval."""

    @pytest.fixture
    def mock_settings(self):
        with patch("app.services.rag_engine.settings") as mock:
            mock.retrieval_evaluation_enabled = True
            mock.instant_skip_retrieval_evaluation = False  # don't skip due to mode
            mock.context_max_tokens = 0
            mock.retrieval_recency_weight = 0.0
            mock.rrf_legacy_mode = False
            mock.exact_match_promote = False
            mock.reranking_enabled = False
            mock.hybrid_search_enabled = False
            yield mock

    @pytest.mark.asyncio
    async def test_skip_evaluation_true_suppresses_evaluation(self, mock_settings):
        """When skip_evaluation=True, RetrievalEvaluator must NOT be constructed."""
        engine = _make_engine_for_retrieval()
        client = MagicMock()

        with patch(
            "app.services.rag_engine.RetrievalEvaluator"
        ) as evaluator_cls:
            result = await engine._execute_retrieval(
                [("original", [0.1, 0.2, 0.3])],
                "question",
                vault_id=1,
                active_client=client,
                mode=ChatMode.THINKING,
                skip_evaluation=True,
            )

        # Evaluator must never be constructed when skip_evaluation=True
        evaluator_cls.assert_not_called()
        # eval_result stays at the default "CONFIDENT"
        assert result[2] == "CONFIDENT"

    @pytest.mark.asyncio
    async def test_skip_evaluation_false_runs_evaluation(self, mock_settings):
        """When skip_evaluation=False (default), RetrievalEvaluator IS constructed and called."""
        engine = _make_engine_for_retrieval()
        client = MagicMock()
        fake_evaluator = MagicMock()
        fake_evaluator.evaluate = AsyncMock(return_value="CONFIDENT")

        with patch(
            "app.services.rag_engine.RetrievalEvaluator",
            return_value=fake_evaluator,
        ) as evaluator_cls:
            result = await engine._execute_retrieval(
                [("original", [0.1, 0.2, 0.3])],
                "question",
                vault_id=1,
                active_client=client,
                mode=ChatMode.THINKING,
                skip_evaluation=False,
            )

        evaluator_cls.assert_called_once_with(client)
        fake_evaluator.evaluate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skip_evaluation_default_is_false(self, mock_settings):
        """Omitting skip_evaluation is equivalent to skip_evaluation=False."""
        engine = _make_engine_for_retrieval()
        client = MagicMock()
        fake_evaluator = MagicMock()
        fake_evaluator.evaluate = AsyncMock(return_value="CONFIDENT")

        with patch(
            "app.services.rag_engine.RetrievalEvaluator",
            return_value=fake_evaluator,
        ) as evaluator_cls:
            # No skip_evaluation argument at all
            result = await engine._execute_retrieval(
                [("original", [0.1, 0.2, 0.3])],
                "question",
                vault_id=1,
                active_client=client,
                mode=ChatMode.THINKING,
            )

        evaluator_cls.assert_called_once_with(client)
        fake_evaluator.evaluate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skip_evaluation_true_with_instant_mode(self, mock_settings):
        """skip_evaluation=True and INSTANT mode together — evaluation must still be suppressed."""
        engine = _make_engine_for_retrieval()
        client = MagicMock()
        mock_settings.instant_skip_retrieval_evaluation = True

        with patch(
            "app.services.rag_engine.RetrievalEvaluator"
        ) as evaluator_cls:
            await engine._execute_retrieval(
                [("original", [0.1, 0.2, 0.3])],
                "question",
                vault_id=1,
                active_client=client,
                mode=ChatMode.INSTANT,
                skip_evaluation=True,
            )

        # Even if instant_skip_retrieval_evaluation were False, skip_evaluation=True blocks it
        evaluator_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_skip_evaluation_works_with_no_active_client(self, mock_settings):
        """skip_evaluation=True suppresses evaluation even when active_client is None."""
        engine = _make_engine_for_retrieval()

        with patch(
            "app.services.rag_engine.RetrievalEvaluator"
        ) as evaluator_cls:
            result = await engine._execute_retrieval(
                [("original", [0.1, 0.2, 0.3])],
                "question",
                vault_id=1,
                active_client=None,
                mode=ChatMode.THINKING,
                skip_evaluation=True,
            )

        # No client → no evaluator normally, but skip_evaluation=True makes this explicit
        evaluator_cls.assert_not_called()
        assert result[2] == "CONFIDENT"


class TestOrchestrateSubQueryRetrievalSkipEvaluation:
    """Tests that multi-sub-query orchestration passes skip_evaluation=True for sub-queries."""

    @pytest.fixture
    def mock_settings(self):
        with patch("app.services.rag_engine.settings") as mock:
            mock.retrieval_evaluation_enabled = True
            mock.instant_skip_retrieval_evaluation = False
            mock.context_max_tokens = 0
            mock.retrieval_recency_weight = 0.0
            mock.rrf_legacy_mode = False
            mock.exact_match_promote = False
            mock.reranking_enabled = False
            mock.hybrid_search_enabled = False
            mock.query_transformation_enabled = False
            yield mock

    def _make_engine(self):
        """Build a minimal RAGEngine for sub-query orchestration testing."""
        engine = RAGEngine.__new__(RAGEngine)
        engine.vector_store = MagicMock()
        engine.vector_store.get_fts_exceptions.return_value = 0
        engine.reranking_service = None
        engine._retrieval_evaluators = {}
        engine.embedding_service = MagicMock()
        engine.embedding_service.embed_single = AsyncMock(
            return_value=[0.1, 0.2, 0.3]
        )
        return engine

    @pytest.mark.asyncio
    async def test_sub_query_calls_skip_evaluation_true(self, mock_settings):
        """Sub-queries in multi-sub-query orchestration must pass skip_evaluation=True."""
        engine = self._make_engine()

        # Two sub-queries in the plan
        plan = ["sub-query-one", "sub-query-two"]

        # Mock vector_store.search to return distinct results per sub-query
        search_results_1 = [{"id": "chunk-1", "file_id": "f1", "text": "result one", "_distance": 0.1, "metadata": {}}]
        search_results_2 = [{"id": "chunk-2", "file_id": "f2", "text": "result two", "_distance": 0.15, "metadata": {}}]

        call_count = 0
        async def fake_search(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return search_results_1 if call_count == 1 else search_results_2

        engine.vector_store.search = fake_search

        # Patch RetrievalEvaluator to detect if it gets called (it shouldn't)
        with patch(
            "app.services.rag_engine.RetrievalEvaluator"
        ) as evaluator_cls:
            result = await engine._orchestrate_sub_query_retrieval(
                plan=plan,
                vault_id=1,
                user_input="original question",
                effective_alpha=0.6,
                variants_dropped=[],
                effective_initial_top_k=10,
                effective_reranker_top_n=10,
                active_client=MagicMock(),
                mode=ChatMode.THINKING,
            )

            fused_results, fusion_applied, failed, score_type, relevance_hint, rerank_success, hybrid_status, rerank_status = result

        # Evaluator must never be called for sub-queries
        evaluator_cls.assert_not_called()
        assert fusion_applied is True
        assert len(fused_results) == 2

    @pytest.mark.asyncio
    async def test_fallback_path_uses_skip_evaluation_false(self, mock_settings):
        """When all sub-query embeddings fail, fallback calls _execute_retrieval without skip_evaluation."""
        engine = self._make_engine()

        # Make embedding fail for sub-queries
        engine.embedding_service.embed_single = AsyncMock(
            side_effect=Exception("embedding failure")
        )

        # But single fallback query succeeds
        call_args_list = []
        original_embed_single = engine.embedding_service.embed_single

        async def fake_embed(text):
            if text == "original question":
                return [0.1, 0.2, 0.3]
            raise Exception("sub-query embedding failed")

        engine.embedding_service.embed_single = fake_embed

        # Fake search for fallback
        search_results = [{"id": "chunk-1", "file_id": "f1", "text": "fallback result", "_distance": 0.1, "metadata": {}}]

        async def fake_search(*args, **kwargs):
            return search_results

        engine.vector_store.search = fake_search

        with patch(
            "app.services.rag_engine.RetrievalEvaluator"
        ) as evaluator_cls:
            fake_evaluator = MagicMock()
            fake_evaluator.evaluate = AsyncMock(return_value="CONFIDENT")
            evaluator_cls.return_value = fake_evaluator

            result = await engine._orchestrate_sub_query_retrieval(
                plan=["sq-one", "sq-two"],
                vault_id=1,
                user_input="original question",
                effective_alpha=0.6,
                variants_dropped=[],
                effective_initial_top_k=10,
                effective_reranker_top_n=10,
                active_client=MagicMock(),
                mode=ChatMode.THINKING,
            )

        # The fallback path uses skip_evaluation=False (default),
        # so with retrieval_evaluation_enabled=True and active_client set,
        # RetrievalEvaluator MUST be constructed.
        evaluator_cls.assert_called()

    @pytest.mark.asyncio
    async def test_sub_query_path_skip_evaluation_blocks_no_match_synthesis(self, mock_settings):
        """When skip_evaluation=True, NO_MATCH result from evaluator is impossible by design.

        The skip_evaluation flag is the mechanism that prevents the multi-sub-query
        orchestrator from generating spurious NO_MATCH relevance hints via CRAG.
        """
        engine = self._make_engine()

        plan = ["sub-query-one"]

        search_results = [{"id": "chunk-1", "file_id": "f1", "text": "result", "_distance": 0.1, "metadata": {}}]
        engine.vector_store.search = AsyncMock(return_value=search_results)

        with patch(
            "app.services.rag_engine.RetrievalEvaluator"
        ) as evaluator_cls:
            result = await engine._orchestrate_sub_query_retrieval(
                plan=plan,
                vault_id=1,
                user_input="original question",
                effective_alpha=0.6,
                variants_dropped=[],
                effective_initial_top_k=10,
                effective_reranker_top_n=10,
                active_client=MagicMock(),
                mode=ChatMode.THINKING,
            )

        # Evaluator must not be invoked — skip_evaluation=True blocks it
        evaluator_cls.assert_not_called()
        _, fusion_applied, _, _, _, _, _, _ = result
        assert fusion_applied is True
