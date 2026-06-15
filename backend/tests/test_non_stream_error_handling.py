"""
Tests for non_stream_chat_response error hardening (Task 7.2).

Verifies that RAGEngineError and ValueError raised inside the query generator
are caught and replaced with generic client-facing messages, preventing
sensitive server-side error details from leaking into HTTP responses.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.api.routes.chat import non_stream_chat_response
from app.services.rag_engine import RAGEngineError


class TestNonStreamErrorHandling:
    """Test error hardening for non_stream_chat_response."""

    @pytest.fixture
    def mock_rag_engine(self):
        """Build a mock RAGEngine with an async query generator."""
        engine = MagicMock()
        engine.query = MagicMock()
        return engine

    # -------------------------------------------------------------------------
    # RAGEngineError → 503 "Chat processing failed"
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_rag_engine_error_returns_503_with_generic_message(self, mock_rag_engine):
        """
        When RAGEngineError is raised inside the query generator,
        the HTTP response must be 503 with detail='Chat processing failed'.
        The raw exception message must NOT appear anywhere in the response.
        """
        sentinel = "Internal vault corrupted at /data/secret/path"
        mock_rag_engine.query.return_value = _raise_rag_engine_error(sentinel)

        with pytest.raises(HTTPException) as exc_info:
            await non_stream_chat_response(
                message="hello",
                history=[],
                rag_engine=mock_rag_engine,
            )

        assert exc_info.value.status_code == 503
        assert exc_info.value.detail == "Chat processing failed"
        # Critical: raw exception string must NOT leak into the response
        assert sentinel not in str(exc_info.value.detail)
        assert "vault" not in str(exc_info.value.detail).lower()
        assert "corrupted" not in str(exc_info.value.detail).lower()
        assert "/data/secret/path" not in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_rag_engine_error_omits_all_raw_exception_text(self, mock_rag_engine):
        """
        Ensure no token of the original RAGEngineError message appears
        in the HTTPException raised to the caller.
        """
        sentinel = "RAGEngineError: QueryTimeout: Vector DB unreachable after 30s"
        mock_rag_engine.query.return_value = _raise_rag_engine_error(sentinel)

        with pytest.raises(HTTPException) as exc_info:
            await non_stream_chat_response(
                message="hello",
                history=[],
                rag_engine=mock_rag_engine,
            )

        response_text = str(exc_info.value.detail)
        # None of the distinctive substrings from the raw error should appear
        assert "QueryTimeout" not in response_text
        assert "Vector DB" not in response_text
        assert "30s" not in response_text

    # -------------------------------------------------------------------------
    # ValueError → 400 "Invalid request parameters"
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_value_error_returns_400_with_generic_message(self, mock_rag_engine):
        """
        When ValueError is raised inside the query generator,
        the HTTP response must be 400 with detail='Invalid request parameters'.
        The raw exception message must NOT appear anywhere in the response.
        """
        sentinel = "Invalid model config: api_key=sk-12345"
        mock_rag_engine.query.return_value = _raise_value_error(sentinel)

        with pytest.raises(HTTPException) as exc_info:
            await non_stream_chat_response(
                message="hello",
                history=[],
                rag_engine=mock_rag_engine,
            )

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "Invalid request parameters"
        # Critical: raw exception string must NOT leak into the response
        assert sentinel not in str(exc_info.value.detail)
        assert "api_key" not in str(exc_info.value.detail)
        assert "sk-12345" not in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_value_error_omits_config_secrets(self, mock_rag_engine):
        """
        Even if a ValueError contains what looks like a secret or
        credentials fragment, it must not appear in the client response.
        """
        sentinel = "Invalid model config: api_key=sk-abcdefghijklmnopqrstuvwxyz"
        mock_rag_engine.query.return_value = _raise_value_error(sentinel)

        with pytest.raises(HTTPException) as exc_info:
            await non_stream_chat_response(
                message="hello",
                history=[],
                rag_engine=mock_rag_engine,
            )

        response_text = str(exc_info.value.detail)
        assert "sk-abcdefghijklmnopqrstuvwxyz" not in response_text
        assert "api_key" not in response_text

    # -------------------------------------------------------------------------
    # Sanity: normal flow is unaffected
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_no_error_returns_chat_response(self, mock_rag_engine):
        """
        When no exception is raised, the function returns a proper ChatResponse.
        This ensures the error hardening does not break the happy path.
        """
        # Simulate a single chunk: content + done
        mock_rag_engine.query.return_value = _content_then_done(
            content="The capital of France is Paris.",
            sources=[],
            memories_used=[],
            wiki_used=[],
            kms_used=[],
        )

        result = await non_stream_chat_response(
            message="What is the capital of France?",
            history=[],
            rag_engine=mock_rag_engine,
        )

        assert result.content == "The capital of France is Paris."


# -------------------------------------------------------------------------
# Helpers: async generators that raise or yield
# -------------------------------------------------------------------------

async def _raise_rag_engine_error(message: str):
    """Async generator that immediately raises RAGEngineError."""
    raise RAGEngineError(message)
    yield  # make this a generator


async def _raise_value_error(message: str):
    """Async generator that immediately raises ValueError."""
    raise ValueError(message)
    yield  # make this a generator


async def _content_then_done(content, sources, memories_used, wiki_used, kms_used):
    """Yield a content chunk then a done chunk."""
    yield {"type": "content", "content": content}
    yield {
        "type": "done",
        "sources": sources,
        "memories_used": memories_used,
        "wiki_used": wiki_used,
        "kms_used": kms_used,
    }
