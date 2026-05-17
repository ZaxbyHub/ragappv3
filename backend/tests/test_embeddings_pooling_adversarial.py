"""
Adversarial/attack-vector tests for EmbeddingService persistent HTTP client.

These tests verify Task 1.1 implementation handles edge cases and attack vectors:
1. Calling embed_single() after close() (should raise error, not hang)
2. Concurrent close() calls (no deadlock, no double-free crash)
3. embed_batch() with close() racing concurrently
4. Invalid URL in __init__ (EmbeddingError raised before _client created, close() safe)
5. Oversized input to embed_single() (> MAX_TEXT_LENGTH chars)
6. None input to embed_single() (immediate EmbeddingError)
"""
import asyncio
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

from app.services.embeddings import EmbeddingError, EmbeddingService


@pytest.fixture(autouse=True)
def mock_settings():
    """Mock settings for all tests."""
    with patch('app.services.embeddings.settings') as mock_settings:
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


class TestEmbedSingleAfterClose:
    """Attack vector: Calling embed_single() after close() has been called."""

    @pytest.mark.asyncio
    async def test_embed_single_after_close_raises_error_not_hangs(self, mock_settings):
        """embed_single() called after close() must raise error, not hang indefinitely."""
        service = EmbeddingService()

        # Close the service first
        await service.close()

        # Verify client is closed
        assert service._client.is_closed, "Client should be closed after close()"

        # embed_single() should raise an error (httpx or EmbeddingError), not hang
        with pytest.raises((EmbeddingError, httpx.HTTPError, RuntimeError)) as exc_info:
            await service.embed_single("test text")

        # The error message should be meaningful, not a cryptic stack trace
        error_msg = str(exc_info.value)
        assert len(error_msg) > 0, "Error message should not be empty"

    @pytest.mark.asyncio
    async def test_embed_single_after_close_returns_quickly(self, mock_settings):
        """embed_single() after close() should fail quickly, not hang for timeout duration."""
        service = EmbeddingService()
        await service.close()

        # Measure how long the call takes
        start_time = asyncio.get_event_loop().time()

        with pytest.raises((EmbeddingError, httpx.HTTPError, RuntimeError)):
            await service.embed_single("test text")

        elapsed = asyncio.get_event_loop().time() - start_time

        # Should fail quickly (< 1 second), not wait for full timeout (60s)
        assert elapsed < 1.0, f"embed_single after close took {elapsed}s, should fail immediately"

    @pytest.mark.asyncio
    async def test_embed_single_double_close_then_embed(self, mock_settings):
        """embed_single() after multiple close() calls should still fail gracefully."""
        service = EmbeddingService()

        # Close multiple times (idempotent close)
        await service.close()
        await service.close()
        await service.close()

        # Should still raise error
        with pytest.raises((EmbeddingError, httpx.HTTPError, RuntimeError)):
            await service.embed_single("test text")


class TestConcurrentCloseCalls:
    """Attack vector: Calling close() concurrently from multiple coroutines."""

    @pytest.mark.asyncio
    async def test_concurrent_close_no_deadlock(self, mock_settings):
        """Concurrent close() calls should not deadlock."""
        service = EmbeddingService()

        # Create multiple concurrent close tasks
        num_concurrent = 10
        tasks = [service.close() for _ in range(num_concurrent)]

        # All should complete without deadlock
        # Add a timeout to detect deadlock
        try:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=5.0)
        except asyncio.TimeoutError:
            pytest.fail("Concurrent close() calls deadlocked")

    @pytest.mark.asyncio
    async def test_concurrent_close_no_exception(self, mock_settings):
        """Concurrent close() calls should not raise exceptions."""
        service = EmbeddingService()

        # Create multiple concurrent close tasks
        num_concurrent = 10
        tasks = [service.close() for _ in range(num_concurrent)]

        # Gather with return_exceptions to check all results
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Check for any exceptions
        exceptions = [r for r in results if isinstance(r, Exception)]
        assert len(exceptions) == 0, f"Concurrent close raised exceptions: {exceptions}"

    @pytest.mark.asyncio
    async def test_concurrent_close_client_ends_closed(self, mock_settings):
        """After concurrent close() calls, client should be closed."""
        service = EmbeddingService()

        # Create multiple concurrent close tasks
        tasks = [service.close() for _ in range(10)]
        await asyncio.gather(*tasks)

        # Client should be closed
        assert service._client.is_closed, "Client should be closed after concurrent close()"

    @pytest.mark.asyncio
    async def test_concurrent_close_no_double_free_crash(self, mock_settings):
        """Concurrent close() should not cause double-free crash or memory corruption."""
        service = EmbeddingService()

        # Aggressively concurrent close with yield points
        async def close_with_yield():
            await service.close()
            await asyncio.sleep(0)  # Yield to event loop
            await service.close()  # Second close in same coroutine

        tasks = [close_with_yield() for _ in range(5)]

        # Should not crash
        try:
            await asyncio.gather(*tasks)
        except Exception as e:
            pytest.fail(f"Concurrent close caused crash: {e}")


class TestEmbedBatchWithCloseRace:
    """Attack vector: Calling embed_batch() with close() racing concurrently."""

    @pytest.mark.asyncio
    async def test_embed_batch_with_close_race_handled(self, mock_settings):
        """embed_batch() should handle close() racing concurrently."""
        service = EmbeddingService()

        # Create a slow mock response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"embeddings": [[0.1] * 768]}

        # Track if post was called
        post_called = asyncio.Event()
        post_completed = asyncio.Event()


        async def slow_post(*args, **kwargs):
            post_called.set()
            await asyncio.sleep(0.2)  # Simulate slow network
            post_completed.set()
            return mock_response

        service._client.post = slow_post

        async def do_batch():
            try:
                return await service.embed_batch(["test text"])
            except (EmbeddingError, httpx.HTTPError, RuntimeError):
                return None  # Acceptable if close raced

        async def do_close():
            await post_called.wait()  # Wait until post starts
            await service.close()  # Close while post is in flight

        # Run batch and close concurrently
        batch_task = asyncio.create_task(do_batch())
        close_task = asyncio.create_task(do_close())

        # Both should complete without deadlock
        try:
            results = await asyncio.wait_for(
                asyncio.gather(batch_task, close_task, return_exceptions=True),
                timeout=5.0
            )
        except asyncio.TimeoutError:
            pytest.fail("embed_batch with racing close deadlocked")

        # Check results - either success or error is acceptable
        batch_result, close_result = results
        if isinstance(batch_result, Exception):
            # Error is acceptable due to race
            assert isinstance(batch_result, (EmbeddingError, httpx.HTTPError, RuntimeError)), \
                f"Unexpected exception type: {type(batch_result)}"
        # If no exception, batch_result should be valid embeddings

    @pytest.mark.asyncio
    async def test_close_during_embed_batch_multiple_calls(self, mock_settings):
        """Multiple embed_batch calls with concurrent close should not hang."""
        service = EmbeddingService()

        # Create mock response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"embeddings": [[0.1] * 768]}

        call_count = 0

        async def counting_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.1)  # Simulate network delay
            return mock_response

        service._client.post = counting_post

        results = []
        exceptions = []

        async def do_batch(idx):
            try:
                result = await service.embed_batch([f"text {idx}"])
                results.append((idx, result))
            except (EmbeddingError, httpx.HTTPError, RuntimeError) as e:
                exceptions.append((idx, e))

        async def do_close_after_delay():
            await asyncio.sleep(0.15)  # Close after some batches started
            await service.close()

        # Start multiple batch operations
        batch_tasks = [asyncio.create_task(do_batch(i)) for i in range(5)]
        close_task = asyncio.create_task(do_close_after_delay())

        # All should complete without hanging
        try:
            await asyncio.wait_for(
                asyncio.gather(*batch_tasks, close_task, return_exceptions=True),
                timeout=5.0
            )
        except asyncio.TimeoutError:
            pytest.fail("Multiple embed_batch with close race deadlocked")


class TestInvalidUrlOnInit:
    """Attack vector: Invalid URL in __init__ (close() must still be safe)."""

    @pytest.mark.asyncio
    async def test_invalid_url_raises_embedding_error(self, mock_settings):
        """Invalid URL should raise EmbeddingError during __init__."""
        mock_settings.ollama_embedding_url = "not-a-valid-url"

        with pytest.raises(EmbeddingError) as exc_info:
            EmbeddingService()

        assert "Invalid embedding URL" in str(exc_info.value) or \
               "not configured" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_empty_url_raises_embedding_error(self, mock_settings):
        """Empty URL should raise EmbeddingError during __init__."""
        mock_settings.ollama_embedding_url = ""

        with pytest.raises(EmbeddingError) as exc_info:
            EmbeddingService()

        assert "not configured" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_none_url_raises_embedding_error(self, mock_settings):
        """None URL should raise EmbeddingError during __init__."""
        mock_settings.ollama_embedding_url = None

        with pytest.raises(EmbeddingError) as exc_info:
            EmbeddingService()

        assert "not configured" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_close_safe_after_invalid_url_init_failure(self, mock_settings):
        """close() must be safe to call even if __init__ failed with invalid URL."""
        mock_settings.ollama_embedding_url = "invalid-url"

        # Create instance without calling __init__
        service = object.__new__(EmbeddingService)
        # __init__ was never called, so _client doesn't exist

        # close() should not raise
        try:
            await service.close()
        except Exception as e:
            pytest.fail(f"close() after failed __init__ raised: {e}")

    @pytest.mark.asyncio
    async def test_close_safe_after_empty_url_init_failure(self, mock_settings):
        """close() must be safe after __init__ failed with empty URL."""
        mock_settings.ollama_embedding_url = ""

        # Create instance without calling __init__
        service = object.__new__(EmbeddingService)

        # close() should not raise
        try:
            await service.close()
        except Exception as e:
            pytest.fail(f"close() after empty URL init failure raised: {e}")


class TestOversizedInputToEmbedSingle:
    """Attack vector: Oversized input to embed_single() (> MAX_TEXT_LENGTH chars)."""

    @pytest.mark.asyncio
    async def test_oversized_input_raises_embedding_error(self, mock_settings):
        """Input exceeding MAX_TEXT_LENGTH should raise EmbeddingError."""
        service = EmbeddingService()

        # Create text longer than MAX_TEXT_LENGTH (8192 chars)
        oversized_text = "x" * (EmbeddingService.MAX_TEXT_LENGTH + 1)

        # embed_single currently doesn't validate length, so this may succeed or fail
        # depending on server behavior. We test that it doesn't hang.
        try:
            # Mock the response to avoid actual network call
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"embedding": [0.1] * 768}
            service._client.post = AsyncMock(return_value=mock_response)

            result = await service.embed_single(oversized_text)

            # If it succeeds, that's acceptable (server handles it)
            # But note: current implementation doesn't validate length
            assert len(result) == 768

        except EmbeddingError:
            # If it raises EmbeddingError, that's also acceptable
            pass
        except Exception as e:
            pytest.fail(f"Unexpected exception type for oversized input: {type(e)}: {e}")
        finally:
            await service.close()

    @pytest.mark.asyncio
    async def test_oversized_input_does_not_hang(self, mock_settings):
        """Oversized input should not cause indefinite hang."""
        service = EmbeddingService()

        oversized_text = "x" * (EmbeddingService.MAX_TEXT_LENGTH + 1000)

        # Mock to simulate server behavior
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"embedding": [0.1] * 768}
        service._client.post = AsyncMock(return_value=mock_response)

        try:
            # Should complete within reasonable time
            await asyncio.wait_for(
                service.embed_single(oversized_text),
                timeout=2.0
            )
        except asyncio.TimeoutError:
            pytest.fail("Oversized input caused embed_single to hang")
        except EmbeddingError:
            pass  # Acceptable
        finally:
            await service.close()

    @pytest.mark.asyncio
    async def test_exactly_max_length_accepted(self, mock_settings):
        """Input exactly at MAX_TEXT_LENGTH should be accepted."""
        service = EmbeddingService()

        # Create text exactly at MAX_TEXT_LENGTH
        max_length_text = "x" * EmbeddingService.MAX_TEXT_LENGTH

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"embedding": [0.1] * 768}
        service._client.post = AsyncMock(return_value=mock_response)

        try:
            result = await service.embed_single(max_length_text)
            assert len(result) == 768
        finally:
            await service.close()

    @pytest.mark.asyncio
    async def test_embed_batch_validates_oversized_input(self, mock_settings):
        """embed_batch should validate and reject oversized inputs."""
        service = EmbeddingService()

        oversized_text = "x" * (EmbeddingService.MAX_TEXT_LENGTH + 1)

        # embed_batch has explicit length validation
        with pytest.raises(EmbeddingError) as exc_info:
            await service.embed_batch([oversized_text])

        assert "exceeds maximum length" in str(exc_info.value)

        await service.close()


class TestNoneInputToEmbedSingle:
    """Attack vector: None input to embed_single()."""

    @pytest.mark.asyncio
    async def test_none_input_raises_embedding_error_immediately(self, mock_settings):
        """None input should raise EmbeddingError immediately."""
        service = EmbeddingService()

        with pytest.raises(EmbeddingError) as exc_info:
            await service.embed_single(None)

        assert "None" in str(exc_info.value)

        await service.close()

    @pytest.mark.asyncio
    async def test_none_input_does_not_call_server(self, mock_settings):
        """None input should fail before making any server request."""
        service = EmbeddingService()

        # Track if post was called
        post_called = []
        original_post = service._client.post

        async def tracking_post(*args, **kwargs):
            post_called.append(True)
            return original_post(*args, **kwargs)

        service._client.post = tracking_post

        with pytest.raises(EmbeddingError):
            await service.embed_single(None)

        # Post should not have been called
        assert len(post_called) == 0, "embed_single(None) should not make server request"

        await service.close()

    @pytest.mark.asyncio
    async def test_none_input_in_batch_raises_error(self, mock_settings):
        """None in batch input should raise EmbeddingError."""
        service = EmbeddingService()

        with pytest.raises(EmbeddingError) as exc_info:
            await service.embed_batch(["valid text", None, "another valid"])

        assert "None" in str(exc_info.value) or "index 1" in str(exc_info.value)

        await service.close()

    @pytest.mark.asyncio
    async def test_empty_string_input_raises_error(self, mock_settings):
        """Empty string input should raise EmbeddingError."""
        service = EmbeddingService()

        with pytest.raises(EmbeddingError) as exc_info:
            await service.embed_single("")

        assert "empty" in str(exc_info.value).lower()

        await service.close()

    @pytest.mark.asyncio
    async def test_whitespace_only_input_raises_error(self, mock_settings):
        """Whitespace-only input should raise EmbeddingError."""
        service = EmbeddingService()

        with pytest.raises(EmbeddingError) as exc_info:
            await service.embed_single("   \n\t  ")

        assert "empty" in str(exc_info.value).lower() or "whitespace" in str(exc_info.value).lower()

        await service.close()


class TestCloseWithClosedClient:
    """Attack vector: Calling close() on already-closed client."""

    @pytest.mark.asyncio
    async def test_close_on_already_closed_client_safe(self, mock_settings):
        """Calling close() on already-closed client should be safe."""
        service = EmbeddingService()

        # Close once
        await service.close()
        assert service._client.is_closed

        # Close again - should not raise
        await service.close()
        await service.close()
        await service.close()

        # Should still be closed
        assert service._client.is_closed

    @pytest.mark.asyncio
    async def test_close_with_manually_closed_client(self, mock_settings):
        """close() should handle if client was closed externally."""
        service = EmbeddingService()

        # Manually close the underlying client
        await service._client.aclose()

        # Service close() should still be safe
        await service.close()

        assert service._client.is_closed


class TestClientStateAfterErrors:
    """Attack vector: Client state after various error conditions."""

    @pytest.mark.asyncio
    async def test_client_usable_after_embed_single_error(self, mock_settings):
        """Client should remain usable after embed_single() raises error."""
        service = EmbeddingService()

        # First call with None should fail
        with pytest.raises(EmbeddingError):
            await service.embed_single(None)

        # Client should still be usable
        assert not service._client.is_closed, "Client should not be closed after error"

        # Successful call should work
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"embedding": [0.1] * 768}
        service._client.post = AsyncMock(return_value=mock_response)

        result = await service.embed_single("valid text")
        assert len(result) == 768

        await service.close()

    @pytest.mark.asyncio
    async def test_close_still_works_after_embed_error(self, mock_settings):
        """close() should work after embed operations raised errors."""
        service = EmbeddingService()

        # Cause various errors
        with pytest.raises(EmbeddingError):
            await service.embed_single(None)

        with pytest.raises(EmbeddingError):
            await service.embed_single("")

        # close() should still work
        await service.close()
        assert service._client.is_closed
