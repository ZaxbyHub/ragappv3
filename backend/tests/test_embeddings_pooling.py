"""
Tests for verifying persistent HTTP client connection pooling in EmbeddingService.

These tests verify Task 1.1 implementation:
- EmbeddingService creates a persistent httpx.AsyncClient
- Connection limits are properly configured
- Client is reused across calls (not created per-call)
- close() method is async and idempotent
"""
import inspect
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

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.embeddings import EmbeddingService


@pytest.fixture(autouse=True)
def mock_settings():
    """Mock settings for all tests."""
    with patch('app.services.embeddings.settings') as mock_settings, \
         patch('app.services.embeddings.assert_url_safe'):
        mock_settings.ollama_embedding_url = "http://localhost:11434/api/embeddings"
        mock_settings.embedding_model = "nomic-embed-text"
        mock_settings.embedding_doc_prefix = ""
        mock_settings.embedding_query_prefix = ""
        mock_settings.embedding_batch_size = 512
        mock_settings.embedding_batch_max_retries = 3
        mock_settings.embedding_batch_min_sub_size = 1
        mock_settings.embedding_concurrent_batches = 4
        mock_settings.chunk_size_chars = 1200
        mock_settings.chunk_overlap_chars = 120
        yield mock_settings


class TestEmbeddingServiceClientAttribute:
    """Tests verifying EmbeddingService has a persistent _client attribute."""

    @pytest.mark.asyncio
    async def test_service_has_client_attribute(self, mock_settings):
        """EmbeddingService instance should have a _client attribute."""
        service = EmbeddingService()
        assert hasattr(service, '_client'), "EmbeddingService should have _client attribute"
        await service.close()

    @pytest.mark.asyncio
    async def test_client_is_httpx_async_client(self, mock_settings):
        """The _client attribute should be an httpx.AsyncClient instance."""
        service = EmbeddingService()
        assert isinstance(service._client, httpx.AsyncClient), \
            f"_client should be httpx.AsyncClient, got {type(service._client)}"
        await service.close()


class TestEmbeddingServiceConnectionLimits:
    """Tests verifying correct connection pool limits are configured."""

    def test_client_created_with_limits(self, mock_settings):
        """The _client should be created with httpx.Limits."""
        # Save reference to real httpx classes before patching
        real_async_client = httpx.AsyncClient

        # Patch httpx.AsyncClient to capture the limits parameter
        with patch('app.services.embeddings.httpx.AsyncClient') as mock_client_class:
            mock_client_instance = MagicMock(spec=real_async_client)
            mock_client_instance.is_closed = False
            mock_client_instance.aclose = AsyncMock()
            mock_client_class.return_value = mock_client_instance

            EmbeddingService()

            # Verify AsyncClient was called
            mock_client_class.assert_called_once()

            # Get the call kwargs
            call_kwargs = mock_client_class.call_args[1]

            # Verify limits was passed
            assert 'limits' in call_kwargs, "AsyncClient should be called with limits parameter"
            assert isinstance(call_kwargs['limits'], httpx.Limits), \
                f"limits should be httpx.Limits, got {type(call_kwargs['limits'])}"

    def test_max_connections_is_20(self, mock_settings):
        """The _client should be created with max_connections=20."""
        real_async_client = httpx.AsyncClient

        with patch('app.services.embeddings.httpx.AsyncClient') as mock_client_class:
            mock_client_instance = MagicMock(spec=real_async_client)
            mock_client_instance.is_closed = False
            mock_client_instance.aclose = AsyncMock()
            mock_client_class.return_value = mock_client_instance

            EmbeddingService()

            # Get the call kwargs
            call_kwargs = mock_client_class.call_args[1]

            # Verify max_connections
            assert call_kwargs['limits'].max_connections == 20, \
                f"max_connections should be 20, got {call_kwargs['limits'].max_connections}"

    def test_max_keepalive_connections_is_10(self, mock_settings):
        """The _client should be created with max_keepalive_connections=10."""
        real_async_client = httpx.AsyncClient

        with patch('app.services.embeddings.httpx.AsyncClient') as mock_client_class:
            mock_client_instance = MagicMock(spec=real_async_client)
            mock_client_instance.is_closed = False
            mock_client_instance.aclose = AsyncMock()
            mock_client_class.return_value = mock_client_instance

            EmbeddingService()

            # Get the call kwargs
            call_kwargs = mock_client_class.call_args[1]

            # Verify max_keepalive_connections
            assert call_kwargs['limits'].max_keepalive_connections == 10, \
                f"max_keepalive_connections should be 10, got {call_kwargs['limits'].max_keepalive_connections}"


class TestEmbeddingServiceCloseMethod:
    """Tests verifying the close() method exists and is idempotent."""

    def test_close_method_exists(self, mock_settings):
        """EmbeddingService should have a close method."""
        service = EmbeddingService()
        assert hasattr(service, 'close'), "EmbeddingService should have close method"
        import asyncio
        asyncio.run(service.close())

    def test_close_is_async_method(self, mock_settings):
        """The close method should be async (coroutine function)."""
        service = EmbeddingService()
        assert inspect.iscoroutinefunction(service.close), \
            "close() should be an async method (coroutine function)"
        import asyncio
        asyncio.run(service.close())

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self, mock_settings):
        """Calling close() multiple times should not raise an error."""
        service = EmbeddingService()

        # First close should succeed
        await service.close()

        # Second close should also succeed (idempotent)
        await service.close()

        # Third close should also succeed
        await service.close()

    @pytest.mark.asyncio
    async def test_close_actually_closes_client(self, mock_settings):
        """After close(), the client should be marked as closed."""
        service = EmbeddingService()

        # Initially not closed
        assert not service._client.is_closed, "Client should not be closed initially"

        # Close the service
        await service.close()

        # Now should be closed
        assert service._client.is_closed, "Client should be closed after close()"


class TestEmbedSingleUsesClient:
    """Tests verifying embed_single() uses self._client instead of creating new clients."""

    @pytest.mark.asyncio
    async def test_embed_single_uses_self_client(self, mock_settings):
        """embed_single should use self._client.post, not create a new client."""
        service = EmbeddingService()

        # Create a mock response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"embedding": [0.1] * 768}

        # Mock the _client.post method
        original_post = service._client.post
        post_called = []

        async def mock_post(*args, **kwargs):
            post_called.append((args, kwargs))
            return mock_response

        service._client.post = mock_post

        try:
            # Call embed_single
            await service.embed_single("test text")

            # Verify _client.post was called
            assert len(post_called) == 1, "embed_single should call _client.post exactly once"
            assert post_called[0][1].get('json') is not None, "post should be called with json payload"

            # Verify the URL matches the service's embeddings_url
            assert post_called[0][0][0] == service.embeddings_url, \
                "post should be called with service's embeddings_url"
        finally:
            service._client.post = original_post
            await service.close()

    @pytest.mark.asyncio
    async def test_embed_single_does_not_create_new_client(self, mock_settings):
        """embed_single should NOT create a new httpx.AsyncClient instance."""
        service = EmbeddingService()

        # Create a mock response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"embedding": [0.1] * 768}

        # Track the client instance before the call
        client_before = service._client

        # Mock the post method
        service._client.post = AsyncMock(return_value=mock_response)

        # Call embed_single
        await service.embed_single("test text")

        # Verify the client is the same instance
        assert service._client is client_before, \
            "embed_single should reuse the same _client instance, not create a new one"

        await service.close()


class TestEmbedBatchApiUsesClient:
    """Tests verifying _embed_batch_api() uses self._client."""

    @pytest.mark.asyncio
    async def test_embed_batch_api_passes_self_client_to_retry(self, mock_settings):
        """_embed_batch_api should pass self._client to _embed_batch_with_retry."""
        service = EmbeddingService()

        # Create a mock response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"embeddings": [[0.1] * 768]}

        # Mock the _client.post method
        service._client.post = AsyncMock(return_value=mock_response)

        # Call _embed_batch_api
        result = await service._embed_batch_api(["test text"])

        # Verify _client.post was called (meaning _client was used)
        service._client.post.assert_called_once()

        # Verify result
        assert len(result) == 1
        assert len(result[0]) == 768

        await service.close()

    @pytest.mark.asyncio
    async def test_embed_batch_uses_self_client(self, mock_settings):
        """embed_batch should use self._client for all API calls."""
        service = EmbeddingService()

        # Create mock responses for multiple batches
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "embeddings": [[0.1] * 768, [0.2] * 768]
        }

        # Track the client instance before the call
        client_before = service._client

        # Mock the post method
        service._client.post = AsyncMock(return_value=mock_response)

        # Call embed_batch with multiple texts
        await service.embed_batch(["text1", "text2"])

        # Verify the client is the same instance
        assert service._client is client_before, \
            "embed_batch should reuse the same _client instance"

        # Verify post was called
        service._client.post.assert_called()

        await service.close()

    @pytest.mark.asyncio
    async def test_embed_batch_multiple_calls_reuse_client(self, mock_settings):
        """Multiple embed_batch calls should reuse the same _client."""
        service = EmbeddingService()

        # Create mock response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"embeddings": [[0.1] * 768]}

        service._client.post = AsyncMock(return_value=mock_response)

        # Track client ID
        client_id_before = id(service._client)

        # Make multiple calls
        await service.embed_batch(["text1"])
        client_id_after_first = id(service._client)

        await service.embed_batch(["text2"])
        client_id_after_second = id(service._client)

        # All should be the same client instance
        assert client_id_before == client_id_after_first == client_id_after_second, \
            "Multiple embed_batch calls should reuse the same _client instance"

        await service.close()


class TestClientReuseAcrossMethods:
    """Tests verifying client reuse across different methods."""

    @pytest.mark.asyncio
    async def test_single_and_batch_use_same_client(self, mock_settings):
        """embed_single and embed_batch should use the same _client instance."""
        service = EmbeddingService()

        # Create mock responses
        mock_response_single = MagicMock()
        mock_response_single.status_code = 200
        mock_response_single.json.return_value = {"embedding": [0.1] * 768}

        mock_response_batch = MagicMock()
        mock_response_batch.status_code = 200
        mock_response_batch.json.return_value = {"embeddings": [[0.2] * 768]}

        # Track which client is used
        client_for_single = None
        client_for_batch = None


        async def track_single(*args, **kwargs):
            nonlocal client_for_single
            client_for_single = service._client
            return mock_response_single

        async def track_batch(*args, **kwargs):
            nonlocal client_for_batch
            client_for_batch = service._client
            return mock_response_batch

        # First call embed_single
        service._client.post = track_single
        await service.embed_single("single text")

        # Then call embed_batch
        service._client.post = track_batch
        await service.embed_batch(["batch text"])

        # Both should use the same client
        assert client_for_single is client_for_batch, \
            "embed_single and embed_batch should use the same _client instance"

        await service.close()


class TestCloseAfterFailedInit:
    """Tests verifying close() is safe even if __init__ failed."""

    @pytest.mark.asyncio
    async def test_close_safe_when_client_not_created(self, mock_settings):
        """close() should be safe to call even if _client doesn't exist."""
        # Create a service instance but manually remove _client
        service = object.__new__(EmbeddingService)
        # Don't call __init__ so _client is never created

        # close() should not raise
        await service.close()  # This should work without error

    @pytest.mark.asyncio
    async def test_close_safe_after_manual_client_removal(self, mock_settings):
        """close() should be safe if _client is deleted after creation."""
        service = EmbeddingService()

        # Manually delete _client
        delattr(service, '_client')

        # close() should not raise
        await service.close()  # This should work without error
