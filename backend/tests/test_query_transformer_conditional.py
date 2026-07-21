"""Tests for conditional query transformation.

Validates that:
- Exact/quoted queries bypass step-back/HyDE
- Filename-specific queries bypass transformation
- Short exact lookups bypass transformation
- Abstract conceptual queries still get transformed
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.query_transformer import (
    QueryPlanner,
    QueryTransformer,
    _is_exact_or_document_query,
)


class TestQueryTypeDetection(unittest.TestCase):
    """Test _is_exact_or_document_query detection logic."""

    def test_quoted_phrase_is_exact(self):
        self.assertTrue(_is_exact_or_document_query('What is "machine learning"?'))

    def test_filename_pdf_is_exact(self):
        self.assertTrue(_is_exact_or_document_query("What does report.pdf say?"))

    def test_filename_docx_is_exact(self):
        self.assertTrue(_is_exact_or_document_query("summary from config.yaml"))

    def test_filename_md_is_exact(self):
        self.assertTrue(_is_exact_or_document_query("README.md contents"))

    def test_short_lookup_is_exact(self):
        self.assertTrue(_is_exact_or_document_query("API key"))

    def test_single_word_is_exact(self):
        self.assertTrue(_is_exact_or_document_query("authentication"))

    def test_conceptual_question_is_not_exact(self):
        self.assertFalse(
            _is_exact_or_document_query(
                "How does the authentication system handle token refresh?"
            )
        )

    def test_abstract_query_is_not_exact(self):
        self.assertFalse(
            _is_exact_or_document_query(
                "Explain the architecture of the retrieval pipeline"
            )
        )

    def test_what_question_is_not_exact(self):
        self.assertFalse(
            _is_exact_or_document_query("What are the main security risks?")
        )

    def test_why_question_is_not_exact(self):
        self.assertFalse(
            _is_exact_or_document_query("Why does the system use reranking?")
        )


class TestQueryTransformerTokenBudgets(unittest.IsolatedAsyncioTestCase):
    """Regression guard for the max_tokens budgets on query_transformer.py's
    utility LLM calls (step-back, follow-up rewrite, HyDE, query planning).

    Reasoning models emit chain-of-thought into a separate field that still
    consumes the completion token budget before any content appears. Tiny
    max_tokens caps starve them into empty responses, so these calls must
    request enough headroom (2000). See query_transformer.py:298-299.
    """

    async def test_all_utility_llm_calls_request_2000_token_budget(self):
        mock_llm = MagicMock()
        mock_llm.chat_completion = AsyncMock(
            return_value="a sufficiently long mocked response"
        )

        with patch("app.services.query_transformer.settings") as mock_settings:
            mock_settings.stepback_enabled = True
            mock_settings.hyde_enabled = False
            mock_settings.redis_url = None
            mock_settings.chat_model = "test-model"
            mock_settings.query_transform_temperature = 0.0
            mock_settings.hyde_temperature = 0.0

            qt = QueryTransformer(llm_client=mock_llm)

            # Step-back (query_transformer.py:298)
            await qt.transform("What is gradient descent in neural networks?")
            assert mock_llm.chat_completion.call_args.kwargs["max_tokens"] == 2000

            # Follow-up rewrite (query_transformer.py:442)
            mock_llm.chat_completion.reset_mock()
            chat_history = [
                {"role": "user", "content": "What is the refund policy?"},
                {"role": "assistant", "content": "Refunds are available within 30 days."},
            ]
            await qt.rewrite_followup("tell me more", chat_history=chat_history)
            assert mock_llm.chat_completion.call_args.kwargs["max_tokens"] == 2000

            # HyDE (query_transformer.py:481)
            mock_llm.chat_completion.reset_mock()
            await qt.generate_hyde("What is gradient descent?")
            assert mock_llm.chat_completion.call_args.kwargs["max_tokens"] == 2000

            # Query planning (query_transformer.py:728) — needs a non-simple
            # query to hit the LLM path instead of the cheap heuristic.
            mock_llm.chat_completion = AsyncMock(
                return_value='["part A of the question", "part B of the question"]'
            )
            planner = QueryPlanner(llm_client=mock_llm)
            await planner.plan(
                "Compare the pricing tiers and the feature differences between plan A and plan B"
            )
            assert mock_llm.chat_completion.call_args.kwargs["max_tokens"] == 2000


if __name__ == "__main__":
    unittest.main()
