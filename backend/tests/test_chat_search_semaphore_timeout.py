"""
End-to-end test for SearchSemaphoreTimeoutError propagation through chat path.

Verifies that when the RAG engine raises SearchSemaphoreTimeoutError during
query execution, the non-streaming chat endpoint converts it into an
HTTP 503 response with detail 'Search temporarily unavailable'.
"""

import os
from unittest import IsolatedAsyncioTestCase
from unittest.mock import MagicMock

from fastapi import HTTPException

from app.api.routes.chat import non_stream_chat_response
from app.services.vector_store import SearchSemaphoreTimeoutError


class TestSearchSemaphoreTimeoutPropagation(IsolatedAsyncioTestCase):
    """Verify SearchSemaphoreTimeoutError → HTTP 503 through chat path."""

    def setUp(self):
        """Ensure required env vars are set for each test."""
        os.environ["ADMIN_SECRET_TOKEN"] = "test-admin-key"
        os.environ["USERS_ENABLED"] = "False"

    def _make_rag_engine(self):
        """Build a mock RAGEngine with an async query generator."""
        engine = MagicMock()
        engine.query = MagicMock()
        return engine

    async def test_search_semaphore_timeout_returns_503(self):
        """
        When SearchSemaphoreTimeoutError is raised inside the query generator,
        the non_stream_chat_response function must raise HTTPException
        with status_code=503 and detail='Search temporarily unavailable'.
        """
        mock_rag_engine = self._make_rag_engine()
        mock_rag_engine.query.return_value = _raise_search_semaphore_timeout()

        with self.assertRaises(HTTPException) as context:
            await non_stream_chat_response(
                message="What is in my vault?",
                history=[],
                mode="chat",
                vault_id=1,
                rag_engine=mock_rag_engine,
            )

        self.assertEqual(context.exception.status_code, 503)
        self.assertEqual(context.exception.detail, "Search temporarily unavailable")

    async def test_search_semaphore_timeout_does_not_leak_internal_details(self):
        """
        The raw SearchSemaphoreTimeoutError message must not appear in
        the client-facing HTTPException detail.
        """
        sentinel = "Semaphore timeout after 30.0s waiting for vector store slot"
        mock_rag_engine = self._make_rag_engine()
        mock_rag_engine.query.return_value = _raise_search_semaphore_timeout(sentinel)

        with self.assertRaises(HTTPException) as context:
            await non_stream_chat_response(
                message="What is in my vault?",
                history=[],
                mode="chat",
                vault_id=1,
                rag_engine=mock_rag_engine,
            )

        response_text = str(context.exception.detail)
        self.assertNotIn("Semaphore timeout", response_text)
        self.assertNotIn("30.0s", response_text)
        self.assertNotIn("vector store slot", response_text)


# -------------------------------------------------------------------------
# Helpers: async generators that raise or yield
# -------------------------------------------------------------------------


async def _raise_search_semaphore_timeout(message: str = "Search semaphore timeout"):
    """Async generator that immediately raises SearchSemaphoreTimeoutError."""
    raise SearchSemaphoreTimeoutError(message)
    yield  # make this a generator
