"""
Tests for embedding service with adaptive batching.
"""
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
from httpx import HTTPError

from app.services.embeddings import EmbeddingError, EmbeddingService


@pytest.mark.asyncio
class TestEmbeddingBatching:
    """Test suite for adaptive embedding batching."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up test fixtures."""
        self.mock_settings_patcher = patch('app.services.embeddings.settings')
        self.mock_ssrf_patcher = patch('app.services.embeddings.assert_url_safe')
        self.mock_settings = self.mock_settings_patcher.start()
        self.mock_ssrf_patcher.start()

        # Configure mock settings
        self.mock_settings.ollama_embedding_url = "http://localhost:11434/api/embeddings"
        self.mock_settings.embedding_model = "nomic-embed-text"
        self.mock_settings.embedding_doc_prefix = ""
        self.mock_settings.embedding_query_prefix = ""
        self.mock_settings.embedding_batch_size = 512
        self.mock_settings.embedding_batch_max_retries = 3
        self.mock_settings.embedding_batch_min_sub_size = 1
        self.mock_settings.embedding_concurrent_batches = 4

        # Mock the settings for chunk_size_chars
        self.mock_settings.chunk_size_chars = 1200
        self.mock_settings.chunk_overlap_chars = 120

    @pytest.fixture(autouse=True)
    def teardown(self):
        """Tear down test fixtures."""
        yield
        self.mock_settings_patcher.stop()
        self.mock_ssrf_patcher.stop()

    def _create_mock_response(self, embeddings):
        """Create a mock HTTP response with embeddings (Ollama format)."""
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "embeddings": embeddings
        }
        return response

    def _create_overflow_error(self, message="input (4096 tokens) is too large to process, current batch size: 1024"):
        """Create an HTTPError simulating llama.cpp token overflow."""
        error = HTTPError(message)
        return error

    async def test_multi_item_batch_overflow_split_and_retry(self):
        """Test that overflow in multi-item batch triggers split/halving and succeeds."""
        service = EmbeddingService()
        texts = [f"test text {i}" for i in range(20)]
        mock_client = MagicMock()

        # Use batch-size-based mock: batches >= 10 overflow, smaller batches succeed.
        # This is robust for concurrent sub-task execution (asyncio.gather) because
        # the response does not depend on call order.
        async def mock_post(url, json=None, **kwargs):
            n = len((json or {}).get("input", []))
            if n >= 10:
                raise self._create_overflow_error()
            return self._create_mock_response([[0.1] * 768] * n)

        service._client = mock_client
        mock_client.post = mock_post

        # Should succeed after recursive splitting: 20 -> 10+10 -> 5+5+5+5
        embeddings = await service.embed_batch(texts, batch_size=20)

        assert len(embeddings) == 20
        assert all(e == [0.1] * 768 for e in embeddings)

    async def test_single_item_batch_overflow_recovery_with_split(self):
        """Test that overflow with single-item batch triggers split/mean-pool recovery."""
        # Create service
        service = EmbeddingService()

        # Long text that can be split - above MIN_SPLIT_CHARS (200 chars)
        texts = ["This is a very long text that will overflow the token limit. " * 50]

        mock_client = MagicMock()

        # First call (single text) will overflow (retry_count=0)
        # Second call (split left) will overflow (retry_count=1)
        # Third call (split right) will overflow (retry_count=2)
        # Fourth call (split left's left) succeeds
        # Fifth call (split left's right) succeeds
        # The recursion continues until we get successful embeddings

        # We'll simulate: overflow -> split -> each part overflows -> each sub-part succeeds
        responses = [
            self._create_overflow_error(),  # Initial single text overflows
            self._create_overflow_error(),  # First split part overflows
            self._create_mock_response([[0.1] * 768]),  # First sub-part succeeds
            self._create_mock_response([[0.2] * 768]),  # Second sub-part succeeds
            self._create_overflow_error(),  # Second split part overflows
            self._create_mock_response([[0.3] * 768]),  # Third sub-part succeeds
            self._create_mock_response([[0.4] * 768]),  # Fourth sub-part succeeds
        ]

        call_count = [0]

        async def mock_post(*args, **kwargs):
            resp = responses.pop(0)
            call_count[0] += 1
            if isinstance(resp, HTTPError):
                raise resp
            return resp

        service._client = mock_client
        mock_client.post = mock_post

        # This should succeed after splitting and mean-pooling
        embeddings = await service.embed_batch(texts, batch_size=1)

        # Verify we got exactly 1 embedding (one-embedding-per-input contract)
        assert len(embeddings) == 1

        # Verify the embedding is a mean-pooled result
        # Mean of [0.1, 0.2, 0.3, 0.4] = 0.25
        expected = [0.25] * 768
        assert embeddings[0] == expected, f"Expected {expected[:5]}..., got {embeddings[0][:5]}..."

        # Verify bounded number of API calls (no runaway recursion)
        assert call_count[0] == 7, f"Expected 7 API calls for happy path split recovery, got {call_count[0]}"

    async def test_single_item_batch_overflow_hard_failure_for_small_text(self):
        """Test that overflow with single small text raises actionable EmbeddingError."""
        # Create service
        service = EmbeddingService()

        # Short text that is below MIN_SPLIT_CHARS (200 chars)
        # This should fail without attempting recovery
        short_text = "Short text that overflows but is too short to split."
        texts = [short_text]

        assert len(short_text) < service.MIN_SPLIT_CHARS, "Test text should be below MIN_SPLIT_CHARS"

        mock_client = MagicMock()

        # Always return overflow error
        overflow_error = self._create_overflow_error()

        service._client = mock_client
        mock_client.post = AsyncMock(side_effect=overflow_error)

        # Should raise EmbeddingError with actionable message mentioning the minimum
        with pytest.raises(EmbeddingError) as context:
            await service.embed_batch(texts, batch_size=1)

        # Verify error message is actionable and mentions the threshold
        error_msg = str(context.value)
        assert "single input" in error_msg.lower()
        assert str(service.MIN_SPLIT_CHARS) in error_msg

    async def test_batch_preserves_order_after_split(self):
        """Test that batch order is preserved after adaptive splitting."""
        service = EmbeddingService()

        # 15 items that will split into batches of 7 and 8 (midpoint split)
        texts = [f"text_{i}" for i in range(15)]

        mock_client = MagicMock()

        # First batch of 15 overflows, splits to 7 and 8
        # Both sub-batches succeed
        responses = [
            self._create_overflow_error(),  # Initial batch overflows
            self._create_mock_response([[float(i)] * 768 for i in range(7)]),  # First 7
            self._create_mock_response([[float(i + 7)] * 768 for i in range(8)]),  # Next 8
        ]

        async def mock_post(*args, **kwargs):
            resp = responses.pop(0)
            if isinstance(resp, HTTPError):
                raise resp
            return resp

        service._client = mock_client
        mock_client.post = mock_post

        embeddings = await service.embed_batch(texts, batch_size=15)

        # Verify order is preserved
        assert len(embeddings) == 15
        for i in range(7):
            assert embeddings[i] == [float(i)] * 768
        for i in range(8):
            assert embeddings[7 + i] == [float(7 + i)] * 768

    async def test_no_overflow_returns_immediately(self):
        """Test that successful batch returns without retry."""
        service = EmbeddingService()

        texts = ["text1", "text2", "text3"]

        mock_client = MagicMock()

        # Success on first try
        mock_client.post = AsyncMock(return_value=self._create_mock_response(
            [[0.1] * 768, [0.2] * 768, [0.3] * 768]
        ))

        service._client = mock_client

        embeddings = await service.embed_batch(texts, batch_size=5)

        # Should return immediately without retry
        assert len(embeddings) == 3
        assert embeddings[0] == [0.1] * 768
        assert embeddings[1] == [0.2] * 768
        assert embeddings[2] == [0.3] * 768

    async def test_non_overflow_error_raises_immediately(self):
        """Test that non-token-overflow errors raise immediately."""
        service = EmbeddingService()

        texts = ["text1"]

        mock_client = MagicMock()

        # Non-overflow error (e.g., 500 internal server error)
        error = HTTPError("Internal server error")

        service._client = mock_client
        mock_client.post = AsyncMock(side_effect=error)

        with pytest.raises(EmbeddingError) as context:
            await service.embed_batch(texts, batch_size=5)

        # Error should not mention token overflow
        assert "single input" not in str(context.value).lower()

    async def test_ollama_mode_overflow_handling(self):
        """Test that Ollama mode also handles overflow correctly."""
        # Configure for Ollama mode
        self.mock_settings.ollama_embedding_url = "http://localhost:11434/api/embeddings"
        service = EmbeddingService()

        assert service.provider_mode == 'ollama'

        texts = ["text1", "text2"]

        mock_client = MagicMock()

        # Ollama overflow error format
        overflow_error = HTTPError("input (8192 tokens) is too large to process")

        service._client = mock_client
        mock_client.post = AsyncMock(side_effect=overflow_error)

        with pytest.raises(EmbeddingError) as context:
            await service.embed_batch(texts, batch_size=5)

        # Should raise actionable error
        assert "single input" in str(context.value).lower()


class TestIsTokenOverflowError:
    """Test the _is_token_overflow_error detection method."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.mock_settings_patcher = patch('app.services.embeddings.settings')
        self.mock_ssrf_patcher = patch('app.services.embeddings.assert_url_safe')
        self.mock_settings = self.mock_settings_patcher.start()
        self.mock_ssrf_patcher.start()
        self.mock_settings.ollama_embedding_url = "http://localhost:11434/api/embeddings"
        self.mock_settings.embedding_model = "nomic-embed-text"
        self.mock_settings.embedding_doc_prefix = ""
        self.mock_settings.embedding_query_prefix = ""
        self.mock_settings.embedding_batch_size = 512
        self.mock_settings.embedding_batch_max_retries = 3
        self.mock_settings.embedding_batch_min_sub_size = 1
        self.mock_settings.embedding_concurrent_batches = 4
        self.mock_settings.chunk_size_chars = 1200
        self.mock_settings.chunk_overlap_chars = 120

    @pytest.fixture(autouse=True)
    def teardown(self):
        yield
        self.mock_settings_patcher.stop()
        self.mock_ssrf_patcher.stop()

    def _create_service(self):
        return EmbeddingService()

    def test_llama_cpp_overflow_pattern(self):
        """Test detection of llama.cpp overflow pattern."""
        service = self._create_service()
        error_msg = "input (4096 tokens) is too large to process, current batch size: 1024"
        assert service._is_token_overflow_error(error_msg) is True

    def test_openai_overflow_pattern(self):
        """Test detection of OpenAI overflow pattern."""
        service = self._create_service()
        error_msg = "too large to process, current batch size: 1024"
        assert service._is_token_overflow_error(error_msg) is True

    def test_token_limit_exceeded_pattern(self):
        """Test detection of token limit exceeded pattern."""
        service = self._create_service()
        error_msg = "token limit exceeded for request"
        assert service._is_token_overflow_error(error_msg) is True

    def test_batch_size_too_small_pattern(self):
        """Test detection of batch size too small pattern."""
        service = self._create_service()
        error_msg = "batch size too small for processing"
        assert service._is_token_overflow_error(error_msg) is True

    def test_non_overflow_error(self):
        """Test that non-overflow errors are not detected."""
        service = self._create_service()
        error_msg = "Internal server error"
        assert service._is_token_overflow_error(error_msg) is False

    def test_case_insensitive_detection(self):
        """Test that detection is case-insensitive."""
        service = self._create_service()
        error_msg = "INPUT (4096 TOKENS) IS TOO LARGE TO PROCESS"
        assert service._is_token_overflow_error(error_msg) is True
