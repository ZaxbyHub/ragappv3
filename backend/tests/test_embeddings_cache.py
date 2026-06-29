"""
Tests for embedding LRU cache implementation.

Covers cache hit/miss, eviction, clear functionality, and statistics.
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

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.embeddings import EmbeddingError, EmbeddingService, LRUCache


@pytest.fixture(autouse=True)
def allow_local_services_for_mocked_embedding_urls():
    """Embedding cache tests use mocked localhost clients; keep the opt-in local."""
    previous = os.environ.get("ALLOW_LOCAL_SERVICES")
    os.environ["ALLOW_LOCAL_SERVICES"] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("ALLOW_LOCAL_SERVICES", None)
        else:
            os.environ["ALLOW_LOCAL_SERVICES"] = previous


class TestLRUCache:
    """Test suite for LRUCache class."""

    def test_cache_initialization(self):
        """Test cache initializes with correct default values."""
        cache = LRUCache(maxsize=100)

        assert cache.maxsize == 100
        assert cache.size == 0
        assert cache.hits == 0
        assert cache.misses == 0

        stats = cache.get_stats()
        assert stats['hits'] == 0
        assert stats['misses'] == 0
        assert stats['size'] == 0
        assert stats['maxsize'] == 100
        assert stats['hit_rate'] == 0.0

    def test_cache_set_and_get(self):
        """Test basic set and get operations."""
        cache = LRUCache(maxsize=10)

        # Set a value
        cache.set('key1', [0.1, 0.2, 0.3])

        # Get the value
        result = cache.get('key1')
        assert result == [0.1, 0.2, 0.3]

        # Check stats - first get is a miss (wasn't there before set)
        # Actually, we set first then get, so this should be a hit
        assert cache.hits == 1
        assert cache.misses == 0

    def test_cache_miss(self):
        """Test cache miss for non-existent key."""
        cache = LRUCache(maxsize=10)

        # Try to get non-existent key
        result = cache.get('nonexistent')

        assert result is None
        assert cache.hits == 0
        assert cache.misses == 1

    def test_cache_hit_updates_lru_order(self):
        """Test that accessing a key moves it to the end (most recently used)."""
        cache = LRUCache(maxsize=3)

        # Add three items
        cache.set('key1', [1.0])
        cache.set('key2', [2.0])
        cache.set('key3', [3.0])

        # Access key1 to make it most recently used
        cache.get('key1')

        # Add key4 - should evict key2 (least recently used)
        cache.set('key4', [4.0])

        # key1 should still be there (was accessed recently)
        assert cache.get('key1') == [1.0]
        # key2 should be evicted
        assert cache.get('key2') is None
        # key3 and key4 should be there
        assert cache.get('key3') == [3.0]
        assert cache.get('key4') == [4.0]

    def test_cache_eviction_when_full(self):
        """Test that oldest item is evicted when cache is full."""
        cache = LRUCache(maxsize=2)

        # Add two items
        cache.set('key1', [1.0])
        cache.set('key2', [2.0])

        assert cache.size == 2

        # Add third item - should evict key1
        cache.set('key3', [3.0])

        assert cache.size == 2  # Still at maxsize
        assert cache.get('key1') is None  # Evicted
        assert cache.get('key2') == [2.0]  # Still there
        assert cache.get('key3') == [3.0]  # New item

    def test_cache_update_existing_key(self):
        """Test updating an existing key moves it to end."""
        cache = LRUCache(maxsize=2)

        cache.set('key1', [1.0])
        cache.set('key2', [2.0])

        # Update key1 - should move it to end
        cache.set('key1', [1.5])

        # Add key3 - should evict key2 (now least recently used)
        cache.set('key3', [3.0])

        assert cache.get('key1') == [1.5]  # Still there, updated
        assert cache.get('key2') is None  # Evicted
        assert cache.get('key3') == [3.0]  # New item

    def test_cache_clear(self):
        """Test cache clear functionality."""
        cache = LRUCache(maxsize=10)

        # Add some items
        cache.set('key1', [1.0])
        cache.set('key2', [2.0])
        cache.get('key1')  # Generate a hit
        cache.get('nonexistent')  # Generate a miss

        assert cache.size == 2
        assert cache.hits == 1
        assert cache.misses == 1

        # Clear cache
        cache.clear()

        assert cache.size == 0
        assert cache.hits == 0
        assert cache.misses == 0
        assert cache.get('key1') is None
        assert cache.get('key2') is None

    def test_cache_stats_hit_rate(self):
        """Test hit rate calculation in stats."""
        cache = LRUCache(maxsize=10)

        # No operations yet
        stats = cache.get_stats()
        assert stats['hit_rate'] == 0.0

        # Add and retrieve
        cache.set('key1', [1.0])
        cache.get('key1')  # Hit
        cache.get('key1')  # Hit
        cache.get('key2')  # Miss

        stats = cache.get_stats()
        assert stats['hits'] == 2
        assert stats['misses'] == 1
        assert stats['hit_rate'] == 66.67  # 2/3 * 100, rounded to 2 decimals

    def test_cache_disabled_with_zero_maxsize(self):
        """Test that cache with maxsize=0 doesn't store anything."""
        cache = LRUCache(maxsize=0)

        cache.set('key1', [1.0])

        assert cache.size == 0
        assert cache.get('key1') is None

    def test_cache_disabled_with_negative_maxsize(self):
        """Test that cache with negative maxsize doesn't store anything."""
        cache = LRUCache(maxsize=-1)

        cache.set('key1', [1.0])

        assert cache.size == 0
        assert cache.get('key1') is None

    def test_cache_large_values(self):
        """Test cache can handle large embedding vectors."""
        cache = LRUCache(maxsize=100)

        # Create a large embedding vector (typical size)
        large_embedding = [0.1] * 768

        cache.set('large_key', large_embedding)
        result = cache.get('large_key')

        assert result == large_embedding
        assert len(result) == 768

    def test_cache_multiple_types(self):
        """Test cache handles different value types correctly."""
        cache = LRUCache(maxsize=10)

        # Float list
        cache.set('floats', [0.1, 0.2, 0.3])
        # Empty list
        cache.set('empty', [])
        # Single value
        cache.set('single', [1.0])

        assert cache.get('floats') == [0.1, 0.2, 0.3]
        assert cache.get('empty') == []
        assert cache.get('single') == [1.0]


@pytest.mark.asyncio
class TestEmbeddingServiceCache:
    """Test suite for EmbeddingService cache integration."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up test fixtures."""
        self.mock_settings_patcher = patch('app.services.embeddings.settings')
        self.mock_settings = self.mock_settings_patcher.start()

        # Configure mock settings
        self.mock_settings.ollama_embedding_url = "http://localhost:11434/api/embeddings"
        self.mock_settings.embedding_model = "nomic-embed-text"
        self.mock_settings.embedding_doc_prefix = ""
        self.mock_settings.embedding_query_prefix = ""
        self.mock_settings.embedding_batch_size = 512
        self.mock_settings.embedding_batch_max_retries = 3
        self.mock_settings.embedding_batch_min_sub_size = 1
        self.mock_settings.chunk_size_chars = 1200
        self.mock_settings.chunk_overlap_chars = 120
        self.mock_settings.tri_vector_search_enabled = False
        self.mock_settings.flag_embedding_url = None

        yield

        self.mock_settings_patcher.stop()

    async def test_embed_single_uses_cache(self):
        """Test that embed_single uses and populates the cache."""
        service = EmbeddingService()

        # Mock the HTTP client
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "embedding": [0.1, 0.2, 0.3]
        }

        with patch.object(service._client, 'post', new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            # First call - should hit the API
            result1 = await service.embed_single("test text")
            assert result1 == [0.1, 0.2, 0.3]
            assert mock_post.call_count == 1

            # Second call with same text - should use cache
            result2 = await service.embed_single("test text")
            assert result2 == [0.1, 0.2, 0.3]
            # API should not be called again
            assert mock_post.call_count == 1

    async def test_embed_single_cache_with_prefix(self):
        """Test that cache key includes the query prefix."""
        self.mock_settings.embedding_query_prefix = "Query: "
        service = EmbeddingService()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "embedding": [0.1, 0.2, 0.3]
        }

        with patch.object(service._client, 'post', new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            # Call with prefix applied
            await service.embed_single("test")
            assert mock_post.call_count == 1

            # Same text should be cached
            await service.embed_single("test")
            assert mock_post.call_count == 1  # Still 1, cached

            # Different text should call API
            await service.embed_single("different")
            assert mock_post.call_count == 2

    async def test_embed_single_different_texts_not_cached(self):
        """Test that different texts don't share cache entries."""
        service = EmbeddingService()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "embedding": [0.1, 0.2, 0.3]
        }

        with patch.object(service._client, 'post', new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            # First text
            await service.embed_single("text one")
            assert mock_post.call_count == 1

            # Different text - should call API
            await service.embed_single("text two")
            assert mock_post.call_count == 2

            # Third different text
            await service.embed_single("text three")
            assert mock_post.call_count == 3

    async def test_get_cache_stats(self):
        """Test that EmbeddingService exposes cache statistics."""
        service = EmbeddingService()

        # Initially empty
        stats = service.get_cache_stats()
        assert stats['l1']['hits'] == 0
        assert stats['l1']['misses'] == 0
        assert stats['l1']['size'] == 0
        assert stats['l1']['maxsize'] == 1000
        assert stats['l2']['available'] is False
        assert stats['l2']['hits'] == 0
        assert stats['l2']['misses'] == 0

    async def test_cache_stats_after_operations(self):
        """Test cache stats reflect actual operations."""
        service = EmbeddingService()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "embedding": [0.1, 0.2, 0.3]
        }

        with patch.object(service._client, 'post', new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            # First call - miss
            await service.embed_single("test")
            stats = service.get_cache_stats()
            assert stats['l1']['misses'] == 1
            assert stats['l1']['hits'] == 0
            assert stats['l1']['size'] == 1

            # Second call - hit
            await service.embed_single("test")
            stats = service.get_cache_stats()
            assert stats['l1']['misses'] == 1
            assert stats['l1']['hits'] == 1
            assert stats['l1']['size'] == 1

    async def test_embed_single_whitespace_text_raises_error(self):
        """Test that empty/whitespace text raises EmbeddingError."""
        service = EmbeddingService()

        with pytest.raises(EmbeddingError, match="Text cannot be empty"):
            await service.embed_single("   ")

        with pytest.raises(EmbeddingError, match="Text cannot be empty"):
            await service.embed_single("")

    async def test_cache_persists_across_calls(self):
        """Test that cache persists across multiple embed_single calls."""
        service = EmbeddingService()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "embedding": [0.5] * 768
        }

        with patch.object(service._client, 'post', new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            # Multiple different texts
            texts = [f"text {i}" for i in range(5)]

            # First round - all API calls
            for text in texts:
                await service.embed_single(text)

            assert mock_post.call_count == 5

            # Second round - all cache hits
            for text in texts:
                await service.embed_single(text)

            # No additional API calls
            assert mock_post.call_count == 5

            stats = service.get_cache_stats()
            assert stats['l1']['hits'] == 5
            assert stats['l1']['misses'] == 5
            assert stats['l1']['size'] == 5


class TestLRUCacheEdgeCases:
    """Test edge cases for LRUCache."""

    def test_cache_with_single_item_capacity(self):
        """Test cache with maxsize=1."""
        cache = LRUCache(maxsize=1)

        cache.set('key1', [1.0])
        assert cache.get('key1') == [1.0]

        # Adding second item evicts first
        cache.set('key2', [2.0])
        assert cache.get('key1') is None
        assert cache.get('key2') == [2.0]
        assert cache.size == 1

    def test_cache_repeated_access_preserves_order(self):
        """Test repeated access to same key maintains LRU order."""
        cache = LRUCache(maxsize=3)

        cache.set('a', [1.0])
        cache.set('b', [2.0])
        cache.set('c', [3.0])

        # Access 'a' multiple times
        for _ in range(10):
            cache.get('a')

        # Add new item - should evict 'b' (least recently used)
        cache.set('d', [4.0])

        assert cache.get('a') == [1.0]  # Still there
        assert cache.get('b') is None  # Evicted
        assert cache.get('c') == [3.0]  # Still there
        assert cache.get('d') == [4.0]  # New

    def test_cache_empty_string_key(self):
        """Test cache with empty string key."""
        cache = LRUCache(maxsize=10)

        cache.set('', [1.0])
        assert cache.get('') == [1.0]

    def test_cache_special_characters_in_key(self):
        """Test cache with special characters in key."""
        cache = LRUCache(maxsize=10)

        special_keys = [
            'key with spaces',
            'key\nwith\nnewlines',
            'key\twith\ttabs',
            'unicode: 🎉',
            'very' * 100 + 'longkey',
        ]

        for i, key in enumerate(special_keys):
            cache.set(key, [float(i)])

        for i, key in enumerate(special_keys):
            assert cache.get(key) == [float(i)]

    def test_cache_stats_with_no_requests(self):
        """Test stats when no get operations performed."""
        cache = LRUCache(maxsize=10)

        # Only sets, no gets
        cache.set('key1', [1.0])
        cache.set('key2', [2.0])

        stats = cache.get_stats()
        assert stats['hits'] == 0
        assert stats['misses'] == 0
        assert stats['size'] == 2
        assert stats['hit_rate'] == 0.0

    def test_cache_overwrite_updates_value(self):
        """Test that overwriting a key updates its value."""
        cache = LRUCache(maxsize=10)

        cache.set('key', [1.0, 2.0])
        assert cache.get('key') == [1.0, 2.0]

        cache.set('key', [3.0, 4.0])
        assert cache.get('key') == [3.0, 4.0]

        # Should still be a hit
        assert cache.hits == 2

    def test_cache_many_items(self):
        """Test cache with many items approaching maxsize."""
        cache = LRUCache(maxsize=100)

        # Add exactly maxsize items
        for i in range(100):
            cache.set(f'key{i}', [float(i)])

        assert cache.size == 100

        # All should be retrievable
        for i in range(100):
            assert cache.get(f'key{i}') == [float(i)]

        # Add one more - should evict the first
        cache.set('newkey', [999.0])
        assert cache.size == 100
        assert cache.get('key0') is None  # First one evicted
        assert cache.get('newkey') == [999.0]


@pytest.mark.asyncio
class TestEmbedPassageExists:
    """Tests for embed_passage method existence and basic behavior."""

    async def test_embed_passage_method_exists(self):
        """embed_passage method should exist on EmbeddingService."""
        assert hasattr(EmbeddingService, 'embed_passage'), "embed_passage method should exist"

    async def test_embed_passage_is_async(self):
        """embed_passage should be an async method."""
        import inspect
        assert inspect.iscoroutinefunction(EmbeddingService.embed_passage), "embed_passage should be async"


@pytest.mark.asyncio
class TestEmbedPassagePrefix:
    """Tests for embed_passage and embed_single prefix application."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up test fixtures."""
        self.mock_settings_patcher = patch('app.services.embeddings.settings')
        self.mock_settings = self.mock_settings_patcher.start()

        # Configure mock settings
        self.mock_settings.ollama_embedding_url = "http://localhost:11434/api/embeddings"
        self.mock_settings.embedding_model = "nomic-embed-text"
        self.mock_settings.embedding_doc_prefix = ""
        self.mock_settings.embedding_query_prefix = ""
        self.mock_settings.embedding_batch_size = 512
        self.mock_settings.embedding_batch_max_retries = 3
        self.mock_settings.embedding_batch_min_sub_size = 1
        self.mock_settings.chunk_size_chars = 1200
        self.mock_settings.chunk_overlap_chars = 120
        self.mock_settings.tri_vector_search_enabled = False
        self.mock_settings.flag_embedding_url = None

        yield

        self.mock_settings_patcher.stop()

    async def test_embed_passage_uses_doc_prefix(self):
        """Test that embed_passage applies embedding_doc_prefix to the payload."""
        # Use OpenAI mode URL so payload uses "input" key
        self.mock_settings.ollama_embedding_url = "http://localhost:1234/v1/embeddings"
        self.mock_settings.embedding_doc_prefix = "passage: "
        service = EmbeddingService()

        mock_response = MagicMock()
        mock_response.status_code = 200
        # OpenAI mode expects {"data": [{"embedding": [...]}]}
        mock_response.json.return_value = {"data": [{"embedding": [0.1, 0.2, 0.3]}]}

        with patch.object(service._client, 'post', new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            await service.embed_passage("foo")

            # Verify the payload's "input" field contains the doc prefix
            call_args = mock_post.call_args
            payload = call_args[1]['json']  # Second positional arg is **kwargs with 'json'
            assert "passage: foo" in payload['input'], f"Expected 'passage: foo' in input, got: {payload['input']}"

    async def test_embed_single_uses_query_prefix(self):
        """Test that embed_single applies embedding_query_prefix to the payload."""
        # Use OpenAI mode URL so payload uses "input" key
        self.mock_settings.ollama_embedding_url = "http://localhost:1234/v1/embeddings"
        self.mock_settings.embedding_query_prefix = "query: "
        service = EmbeddingService()

        mock_response = MagicMock()
        mock_response.status_code = 200
        # OpenAI mode expects {"data": [{"embedding": [...]}]}
        mock_response.json.return_value = {"data": [{"embedding": [0.1, 0.2, 0.3]}]}

        with patch.object(service._client, 'post', new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            await service.embed_single("bar")

            # Verify the payload's "input" field contains the query prefix
            call_args = mock_post.call_args
            payload = call_args[1]['json']  # Second positional arg is **kwargs with 'json'
            assert "query: bar" in payload['input'], f"Expected 'query: bar' in input, got: {payload['input']}"

    async def test_embed_passage_uses_doc_prefix_ollama(self):
        """Test that embed_passage applies embedding_doc_prefix with Ollama mode 'prompt' key."""
        # Use Ollama mode URL so payload uses "prompt" key
        self.mock_settings.ollama_embedding_url = "http://localhost:11434/api/embeddings"
        self.mock_settings.embedding_doc_prefix = "passage: "
        service = EmbeddingService()

        mock_response = MagicMock()
        mock_response.status_code = 200
        # Ollama mode expects {"embedding": [...]}
        mock_response.json.return_value = {"embedding": [0.1, 0.2, 0.3]}

        with patch.object(service._client, 'post', new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            await service.embed_passage("foo")

            # Verify the payload's "prompt" field contains the doc prefix
            call_args = mock_post.call_args
            payload = call_args[1]['json']  # Second positional arg is **kwargs with 'json'
            assert "passage: foo" in payload['prompt'], f"Expected 'passage: foo' in prompt, got: {payload['prompt']}"

    async def test_embed_single_uses_query_prefix_ollama(self):
        """Test that embed_single applies embedding_query_prefix with Ollama mode 'prompt' key."""
        # Use Ollama mode URL so payload uses "prompt" key
        self.mock_settings.ollama_embedding_url = "http://localhost:11434/api/embeddings"
        self.mock_settings.embedding_query_prefix = "query: "
        service = EmbeddingService()

        mock_response = MagicMock()
        mock_response.status_code = 200
        # Ollama mode expects {"embedding": [...]}
        mock_response.json.return_value = {"embedding": [0.1, 0.2, 0.3]}

        with patch.object(service._client, 'post', new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            await service.embed_single("bar")

            # Verify the payload's "prompt" field contains the query prefix
            call_args = mock_post.call_args
            payload = call_args[1]['json']  # Second positional arg is **kwargs with 'json'
            assert "query: bar" in payload['prompt'], f"Expected 'query: bar' in prompt, got: {payload['prompt']}"


@pytest.mark.asyncio
class TestCacheKeyFormat:
    """Tests for cache key format including model fingerprint."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up test fixtures."""
        self.mock_settings_patcher = patch('app.services.embeddings.settings')
        self.mock_settings = self.mock_settings_patcher.start()

        # Configure mock settings
        self.mock_settings.ollama_embedding_url = "http://localhost:11434/api/embeddings"
        self.mock_settings.embedding_model = "nomic-embed-text"
        self.mock_settings.embedding_doc_prefix = ""
        self.mock_settings.embedding_query_prefix = ""
        self.mock_settings.embedding_batch_size = 512
        self.mock_settings.embedding_batch_max_retries = 3
        self.mock_settings.embedding_batch_min_sub_size = 1
        self.mock_settings.chunk_size_chars = 1200
        self.mock_settings.chunk_overlap_chars = 120
        self.mock_settings.tri_vector_search_enabled = False
        self.mock_settings.flag_embedding_url = None

        yield

        self.mock_settings_patcher.stop()

    async def test_cache_key_includes_model_when_set(self):
        """Test that cache key includes model fingerprint - same text with different model should be cache miss."""
        # Configure model1 in settings
        self.mock_settings.embedding_model = "model1"
        service1 = EmbeddingService()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "embedding": [0.1, 0.2, 0.3]
        }

        with patch.object(service1._client, 'post', new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            # First call with model1 - should hit the API
            result1 = await service1.embed_single("test text")
            assert result1 == [0.1, 0.2, 0.3]
            assert mock_post.call_count == 1

            # Second call with same text - should use cache (same model)
            result2 = await service1.embed_single("test text")
            assert result2 == [0.1, 0.2, 0.3]
            # API should not be called again
            assert mock_post.call_count == 1

            # Now change the model via settings — this should invalidate
            # the cache because embedding_model is now read live.
            self.mock_settings.embedding_model = "different_model"

            # Third call with same text but different model - should be cache MISS
            result3 = await service1.embed_single("test text")
            assert result3 == [0.1, 0.2, 0.3]
            # API should be called again because model changed
            assert mock_post.call_count == 2


class TestResolvedUrlCache:
    """_resolved_cache memoizes provider-mode detection per URL (ARCH-007).

    Detection runs once per configured URL and is invalidated when the live
    ollama_embedding_url changes, so the ~19 property reads per embedding call
    don't each re-parse the URL.
    """

    def test_detection_memoized_and_invalidated_on_url_change(self):
        mock_settings = MagicMock()
        mock_settings.ollama_embedding_url = "http://localhost:11434/api/embeddings"
        mock_settings.embedding_model = "nomic-embed-text"

        # Patch the SSRF guard to a no-op so a loopback test URL doesn't trip it
        # at construction (the guard is covered in test_settings_ssrf.py).
        with patch("app.services.embeddings.settings", mock_settings), patch(
            "app.services.embeddings.assert_url_safe", lambda *a, **k: None
        ):
            service = EmbeddingService()
            with patch.object(
                service,
                "_detect_provider_mode",
                wraps=service._detect_provider_mode,
            ) as spy:
                # Repeated reads of the same URL → detection runs exactly once.
                _ = service.embeddings_url
                _ = service.provider_mode
                _ = service.embeddings_url
                assert spy.call_count == 1

                # Changing the live URL invalidates the cache → detection re-runs.
                mock_settings.ollama_embedding_url = "http://localhost:1234/v1/embeddings"
                assert service.provider_mode == "openai"
                assert spy.call_count == 2


@pytest.mark.asyncio
class TestEmbeddingServiceRedisL2Cache:
    """Test suite for EmbeddingService Redis L2 shared cache."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up test fixtures."""
        self.mock_settings_patcher = patch('app.services.embeddings.settings')
        self.mock_settings = self.mock_settings_patcher.start()

        # Configure mock settings
        self.mock_settings.ollama_embedding_url = "http://localhost:11434/api/embeddings"
        self.mock_settings.embedding_model = "nomic-embed-text"
        self.mock_settings.embedding_doc_prefix = ""
        self.mock_settings.embedding_query_prefix = ""
        self.mock_settings.embedding_batch_size = 512
        self.mock_settings.embedding_batch_max_retries = 3
        self.mock_settings.embedding_batch_min_sub_size = 1
        self.mock_settings.chunk_size_chars = 1200
        self.mock_settings.chunk_overlap_chars = 120
        self.mock_settings.tri_vector_search_enabled = False
        self.mock_settings.flag_embedding_url = None
        self.mock_settings.redis_url = "redis://localhost:6379/0"
        self.mock_settings.embedding_cache_ttl_seconds = 604800

        yield

        self.mock_settings_patcher.stop()

    async def test_redis_l2_hit_backfills_l1(self):
        """Test that a Redis L2 hit also populates the L1 LRU cache."""
        # Mock Redis client
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        # Return a cached embedding on get (Redis hit)
        mock_redis.get.return_value = '[0.1, 0.2, 0.3]'

        with patch('app.services.embeddings.redis') as mock_redis_module:
            mock_redis_module.from_url.return_value = mock_redis

            service = EmbeddingService()

            assert service._redis_available is True
            assert service._redis_client is not None

            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"embedding": [0.1, 0.2, 0.3]}

            # Track whether the HTTP client is called
            with patch.object(service._client, 'post', new_callable=AsyncMock) as mock_post:
                mock_post.return_value = mock_response

                # First call - Redis hit, backfills L1
                result = await service.embed_single("test text")
                assert result == [0.1, 0.2, 0.3]
                # HTTP should NOT be called (served from Redis)
                assert mock_post.call_count == 0
                # Redis get was called
                assert mock_redis.get.called
                # L1 should now have the entry (backfilled from Redis hit)
                cache_stats = service.get_cache_stats()
                assert cache_stats['l1']['size'] >= 1

    async def test_redis_l2_miss_falls_through_to_provider(self):
        """Test that a Redis miss falls through to the embedding provider."""
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.get.return_value = None  # Redis miss

        with patch('app.services.embeddings.redis') as mock_redis_module:
            mock_redis_module.from_url.return_value = mock_redis

            service = EmbeddingService()

            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"embedding": [0.5, 0.6, 0.7]}

            with patch.object(service._client, 'post', new_callable=AsyncMock) as mock_post:
                mock_post.return_value = mock_response

                # First call - Redis miss, call provider
                result = await service.embed_single("miss text")
                assert result == [0.5, 0.6, 0.7]
                assert mock_post.call_count == 1

    async def test_redis_setex_with_ttl_on_provider_hit(self):
        """Test that after computing an embedding, it is stored in Redis with TTL."""
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.get.return_value = None  # Force miss to hit provider

        with patch('app.services.embeddings.redis') as mock_redis_module:
            mock_redis_module.from_url.return_value = mock_redis

            service = EmbeddingService()

            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"embedding": [0.9, 0.8, 0.7]}

            with patch.object(service._client, 'post', new_callable=AsyncMock) as mock_post:
                mock_post.return_value = mock_response

                await service.embed_single("new text")

                # Verify setex was called with TTL (604800 seconds = 7 days)
                assert mock_redis.setex.called
                call_args = mock_redis.setex.call_args
                # setex(key, ttl, value)
                assert call_args[0][0].startswith("emb:")
                assert call_args[0][1] == 604800  # TTL
                assert json.loads(call_args[0][2]) == [0.9, 0.8, 0.7]

    async def test_redis_key_namespaced_with_emb_prefix(self):
        """Test that Redis keys are prefixed with 'emb:' for namespacing."""
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.get.return_value = None  # Force miss to hit provider

        with patch('app.services.embeddings.redis') as mock_redis_module:
            mock_redis_module.from_url.return_value = mock_redis

            service = EmbeddingService()

            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"embedding": [0.1, 0.2]}

            with patch.object(service._client, 'post', new_callable=AsyncMock) as mock_post:
                mock_post.return_value = mock_response

                await service.embed_single("namespaced test")

                # Verify all Redis keys start with 'emb:' prefix
                for call in mock_redis.method_calls:
                    if call[0] == 'get':
                        assert call[1][0].startswith("emb:")
                    elif call[0] == 'setex':
                        assert call[1][0].startswith("emb:")

    async def test_redis_unavailable_service_still_works_via_lru(self):
        """Test that when Redis is unavailable (client=None), embeddings still work via LRU + provider."""
        with patch('app.services.embeddings.redis') as mock_redis_module:
            # Simulate Redis connection failure at init
            mock_redis_module.from_url.side_effect = Exception("Connection refused")

            service = EmbeddingService()

            # Redis should be unavailable
            assert service._redis_client is None
            assert service._redis_available is False

            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"embedding": [0.1, 0.2, 0.3]}

            with patch.object(service._client, 'post', new_callable=AsyncMock) as mock_post:
                mock_post.return_value = mock_response

                # Embeddings should work fine
                result = await service.embed_single("works without redis")
                assert result == [0.1, 0.2, 0.3]

                # Second call should hit L1 cache
                result2 = await service.embed_single("works without redis")
                assert result2 == [0.1, 0.2, 0.3]
                assert mock_post.call_count == 1  # Only called once

    async def test_redis_unavailable_at_runtime_still_works(self):
        """Test that when Redis goes down at runtime, service continues via LRU + provider."""
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.get.return_value = '[0.1, 0.2, 0.3]'

        with patch('app.services.embeddings.redis') as mock_redis_module:
            mock_redis_module.from_url.return_value = mock_redis

            service = EmbeddingService()

            # Simulate Redis going down at runtime
            def redis_get_raises(*args, **kwargs):
                raise Exception("Redis connection lost")

            mock_redis.get.side_effect = redis_get_raises

            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"embedding": [0.5, 0.6, 0.7]}

            with patch.object(service._client, 'post', new_callable=AsyncMock) as mock_post:
                mock_post.return_value = mock_response

                # Should fall through to provider when Redis get fails
                result = await service.embed_single("runtime failure text")
                assert result == [0.5, 0.6, 0.7]
                assert mock_post.call_count == 1

    async def test_redis_set_failure_does_not_raise(self):
        """Test that Redis setex failure does not raise - L1 still gets the value."""
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.get.return_value = None  # Force provider call

        def redis_setex_raises(*args, **kwargs):
            raise Exception("Redis write error")

        mock_redis.setex.side_effect = redis_setex_raises

        with patch('app.services.embeddings.redis') as mock_redis_module:
            mock_redis_module.from_url.return_value = mock_redis

            service = EmbeddingService()

            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"embedding": [0.1, 0.2, 0.3]}

            with patch.object(service._client, 'post', new_callable=AsyncMock) as mock_post:
                mock_post.return_value = mock_response

                # Should NOT raise despite Redis setex failure
                result = await service.embed_single("set failure test")
                assert result == [0.1, 0.2, 0.3]
                # L1 should still have the value
                cache_stats = service.get_cache_stats()
                assert cache_stats['l1']['size'] >= 1

    async def test_redis_json_serialization_round_trip_exact_float_precision(self):
        """Test that JSON dumps/loads preserves embedding floats exactly (FR-005 serialization invariant).

        Uses a vector of floats that have known binary representation issues with
        binary float formats, plus edge-case values, to verify the JSON round-trip
        is exact — no precision is lost or altered.
        """
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        # Return None to force provider path, capturing the setex call
        mock_redis.get.return_value = None

        with patch('app.services.embeddings.redis') as mock_redis_module:
            mock_redis_module.from_url.return_value = mock_redis

            service = EmbeddingService()

            # Vector with values known to cause floating-point representation issues
            # and edge-case values across the representable range
            original = [
                0.0,
                1.0,
                -1.0,
                0.1,   # 0.1 cannot be represented exactly in binary float
                0.2,   # 0.2 same
                0.3,   # 0.3 same
                1.234567890123456789,
                1e-100,
                1e100,
                -0.0,  # negative zero
            ]

            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"embedding": original}

            with patch.object(service._client, 'post', new_callable=AsyncMock) as mock_post:
                mock_post.return_value = mock_response

                await service.embed_single("precision test")

                # Capture what was written to Redis
                assert mock_redis.setex.called, "Should have written to Redis"
                setex_call = mock_redis.setex.call_args[0]
                redis_key, redis_ttl, redis_value = setex_call[0], setex_call[1], setex_call[2]

                # Verify key format
                assert redis_key.startswith("emb:")

                # Verify TTL is the configured value
                assert redis_ttl == 604800, "TTL should be embedding_cache_ttl_seconds (7 days)"

                # Deserialize from Redis
                loaded = json.loads(redis_value)

                # Exact equality — JSON round-trip must preserve every float value
                assert loaded == original, \
                    f"JSON round-trip changed values: {original} vs {loaded}"

    async def test_stats_include_redis_hits_and_misses(self):
        """Test that get_cache_stats includes Redis L2 hits/misses/availability."""
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        # First call: miss (not in Redis), then hit after backfill
        mock_redis.get.return_value = None

        with patch('app.services.embeddings.redis') as mock_redis_module:
            mock_redis_module.from_url.return_value = mock_redis

            service = EmbeddingService()

            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"embedding": [0.1, 0.2, 0.3]}

            with patch.object(service._client, 'post', new_callable=AsyncMock) as mock_post:
                mock_post.return_value = mock_response

                # Provider path
                await service.embed_single("stats test")

                stats = service.get_cache_stats()
                assert stats['l2']['available'] is True
                assert stats['l2']['misses'] == 1
                assert stats['l2']['hits'] == 0

    async def test_cluster_wide_sharing_write_in_one_hits_in_other(self):
        """Test that write via service_a is a Redis hit for service_b (cluster-wide sharing).

        This is the primary cluster-sharing invariant (FR-005): two EmbeddingService
        instances sharing a Redis client must see each other's writes — a value
        embedded via service_a must be a Redis L2 hit when service_b embeds the same text.
        """
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        # Redis initially empty (no cached value)
        mock_redis.get.return_value = None

        with patch('app.services.embeddings.redis') as mock_redis_module:
            mock_redis_module.from_url.return_value = mock_redis

            service_a = EmbeddingService()
            service_b = EmbeddingService()

            # Both must share the same Redis client
            assert service_a._redis_client is service_b._redis_client

            # Track what HTTP endpoint returns for the provider path
            http_embedding = [0.42, 0.123, 0.999]
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"embedding": http_embedding}

            # service_a posts → provider returns http_embedding → setex writes to Redis
            with patch.object(service_a._client, 'post', new_callable=AsyncMock) as mock_post_a:
                mock_post_a.return_value = mock_response

                result_a = await service_a.embed_single("cluster sharing text")
                assert result_a == http_embedding
                assert mock_post_a.call_count == 1
                # Verify setex was called (service_a wrote to Redis)
                assert mock_redis.setex.called, "service_a should have written embedding to Redis via setex"
                setex_key = mock_redis.setex.call_args[0][0]
                assert setex_key.startswith("emb:")

            # Now service_b embeds the SAME text — must hit Redis L2, not call provider
            mock_redis.get.reset_mock()
            mock_redis.setex.reset_mock()

            with patch.object(service_b._client, 'post', new_callable=AsyncMock) as mock_post_b:
                # Redis has the value from service_a's setex
                mock_redis.get.return_value = json.dumps(http_embedding)

                result_b = await service_b.embed_single("cluster sharing text")
                assert result_b == http_embedding
                # HTTP should NOT be called — Redis L2 hit
                assert mock_post_b.call_count == 0, \
                    "service_b should have hit Redis L2, not called the provider"
                # Redis get was called (L2 lookup)
                assert mock_redis.get.called, "service_b should have looked up the key in Redis"

    async def test_shared_redis_client_across_instances(self):
        """Test that two services sharing a Redis client see the same cache (identity check)."""
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.get.return_value = None  # Force provider path

        with patch('app.services.embeddings.redis') as mock_redis_module:
            mock_redis_module.from_url.return_value = mock_redis

            service_a = EmbeddingService()
            service_b = EmbeddingService()

            # Both should share the same Redis client (same object reference)
            assert service_a._redis_client is service_b._redis_client
            assert service_a._redis_client is not None


# ─── Tests for no-Redis path (client = None) ─────────────────────────────────

@pytest.mark.asyncio
class TestEmbeddingServiceNoRedisFallback:
    """Test that EmbeddingService works when Redis is entirely absent."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up test fixtures with no Redis."""
        self.mock_settings_patcher = patch('app.services.embeddings.settings')
        self.mock_settings = self.mock_settings_patcher.start()

        # Configure mock settings with no Redis
        self.mock_settings.ollama_embedding_url = "http://localhost:11434/api/embeddings"
        self.mock_settings.embedding_model = "nomic-embed-text"
        self.mock_settings.embedding_doc_prefix = ""
        self.mock_settings.embedding_query_prefix = ""
        self.mock_settings.embedding_batch_size = 512
        self.mock_settings.embedding_batch_max_retries = 3
        self.mock_settings.embedding_batch_min_sub_size = 1
        self.mock_settings.chunk_size_chars = 1200
        self.mock_settings.chunk_overlap_chars = 120
        self.mock_settings.tri_vector_search_enabled = False
        self.mock_settings.flag_embedding_url = None
        self.mock_settings.redis_url = ""  # Empty URL means no Redis
        self.mock_settings.embedding_cache_ttl_seconds = 604800

        yield

        self.mock_settings_patcher.stop()

    async def test_no_redis_url_still_works_via_lru(self):
        """Test that when redis_url is empty, service falls back to LRU only."""
        service = EmbeddingService()

        # Redis should be unavailable
        assert service._redis_client is None
        assert service._redis_available is False

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"embedding": [0.1, 0.2, 0.3]}

        with patch.object(service._client, 'post', new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            # Should work fine
            result1 = await service.embed_single("lru only test")
            assert result1 == [0.1, 0.2, 0.3]
            assert mock_post.call_count == 1

            # Second call should use L1 cache
            result2 = await service.embed_single("lru only test")
            assert result2 == [0.1, 0.2, 0.3]
            assert mock_post.call_count == 1

            # Stats should show L1 activity
            stats = service.get_cache_stats()
            assert stats['l2']['available'] is False
            assert stats['l1']['hits'] == 1
            assert stats['l1']['misses'] == 1
