"""Fail-open contract for RetrievalEvaluator.evaluate().

CRAG self-evaluation must NOT degrade an answer when the evaluator itself
fails. The documented contract (and every in-function fallback) is to return
``CONFIDENT`` — the no-op verdict that injects no relevance hint and does not
trigger distillation synthesis. A regression had the exception path returning
``AMBIGUOUS``, which silently altered prompt framing on evaluator outages.
"""

import unittest
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.retrieval_evaluator import RetrievalEvaluator


class TestRetrievalEvaluatorFailOpen(unittest.IsolatedAsyncioTestCase):
    @pytest.mark.asyncio
    async def test_exception_fails_open_to_confident(self):
        """LLM error during evaluation → CONFIDENT (not AMBIGUOUS)."""
        client = MagicMock()
        client.chat_completion = AsyncMock(side_effect=RuntimeError("LLM down"))
        evaluator = RetrievalEvaluator(client)

        verdict = await evaluator.evaluate("q", [{"text": "a relevant chunk"}])

        self.assertEqual(verdict, "CONFIDENT")

    @pytest.mark.asyncio
    async def test_empty_response_fails_open_to_confident(self):
        """Empty model response → CONFIDENT, no exception."""
        client = MagicMock()
        client.chat_completion = AsyncMock(return_value="")
        evaluator = RetrievalEvaluator(client)

        verdict = await evaluator.evaluate("q", [{"text": "chunk"}])

        self.assertEqual(verdict, "CONFIDENT")

    @pytest.mark.asyncio
    async def test_no_chunks_returns_confident_without_calling_llm(self):
        """No chunks to evaluate → CONFIDENT, LLM not invoked."""
        client = MagicMock()
        client.chat_completion = AsyncMock()
        evaluator = RetrievalEvaluator(client)

        verdict = await evaluator.evaluate("q", [])

        self.assertEqual(verdict, "CONFIDENT")
        client.chat_completion.assert_not_awaited()


class TestRetrievalEvaluatorTokenBudget(unittest.IsolatedAsyncioTestCase):
    """Regression guard: reasoning models emit chain-of-thought into a
    separate field that still consumes the completion token budget before
    any content appears. A cap too tight starves the evaluator into an
    empty response, which fail-opens to CONFIDENT and silently disables
    CRAG correction. See retrieval_evaluator.py:82.
    """

    @pytest.mark.asyncio
    async def test_evaluate_requests_2000_token_budget(self):
        client = MagicMock()
        client.chat_completion = AsyncMock(return_value="CONFIDENT")
        evaluator = RetrievalEvaluator(client)

        await evaluator.evaluate("q", [{"text": "a relevant chunk"}])

        assert client.chat_completion.call_args.kwargs["max_tokens"] == 2000


if __name__ == "__main__":
    unittest.main()
