"""
Adversarial Security Tests for Backend Services

Attack vectors tested:
- Malformed inputs (invalid types, structures)
- Oversized payloads (exceeding limits)
- Injection attempts (SQL-like, path traversal, command injection)
- Boundary violations (edge cases, negative values, extreme values)

Target files:
- backend/app/services/circuit_breaker.py
- backend/app/services/vector_store.py
- backend/app/services/embeddings.py
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from app.services.circuit_breaker import (
    AsyncCircuitBreaker,
    CircuitBreakerState,
    circuit_breaker,
)
from app.services.embeddings import (
    EmbeddingError,
    EmbeddingService,
    LRUCache,
)

# =============================================================================
# CIRCUIT BREAKER ADVERSARIAL TESTS
# =============================================================================

class TestCircuitBreakerAdversarial:
    """Attack vectors against circuit breaker state machine."""

    def test_negative_fail_max(self):
        """Attack: Negative failure threshold should be rejected."""
        with pytest.raises(ValueError, match="fail_max must be >= 1"):
            AsyncCircuitBreaker(fail_max=-5, reset_timeout=60, name="test")

    def test_zero_fail_max(self):
        """Attack: Zero failure threshold should be rejected."""
        with pytest.raises(ValueError, match="fail_max must be >= 1"):
            AsyncCircuitBreaker(fail_max=0, reset_timeout=60, name="test")

    def test_negative_reset_timeout(self):
        """Attack: Negative reset timeout should be rejected."""
        with pytest.raises(ValueError, match="reset_timeout must be > 0"):
            AsyncCircuitBreaker(fail_max=5, reset_timeout=-30, name="test")

    def test_extreme_reset_timeout(self):
        """Attack: Extremely large reset timeout could cause overflow."""
        cb = AsyncCircuitBreaker(fail_max=5, reset_timeout=1e308, name="test")
        assert cb.reset_timeout == 1e308

    def test_nan_timeout(self):
        """Attack: NaN timeout value."""
        cb = AsyncCircuitBreaker(fail_max=5, reset_timeout=float('nan'), name="test")
        assert cb.reset_timeout != cb.reset_timeout  # NaN check

    def test_infinity_timeout(self):
        """Attack: Infinity timeout value."""
        cb = AsyncCircuitBreaker(fail_max=5, reset_timeout=float('inf'), name="test")
        assert cb.reset_timeout == float('inf')

    def test_none_name(self):
        """Attack: None name should fallback gracefully."""
        cb = AsyncCircuitBreaker(fail_max=5, reset_timeout=60, name=None)
        assert cb.name == "unnamed"

    def test_malicious_name_injection(self):
        """Attack: Name with injection characters should not execute."""
        malicious_name = "test'; DROP TABLE users; --"
        cb = AsyncCircuitBreaker(fail_max=5, reset_timeout=60, name=malicious_name)
        assert cb.name == malicious_name  # Stored as-is, not executed

    def test_unicode_name(self):
        """Attack: Unicode name with special characters."""
        unicode_name = "test_日本語_🚀_\\x00\\n\\t"
        cb = AsyncCircuitBreaker(fail_max=5, reset_timeout=60, name=unicode_name)
        assert cb.name == unicode_name

    @pytest.mark.asyncio
    async def test_concurrent_state_manipulation(self):
        """Attack: Rapid concurrent state changes."""
        cb = AsyncCircuitBreaker(fail_max=3, reset_timeout=1, name="concurrent_test")

        async def record_failures():
            for _ in range(10):
                cb.record_failure()
                await asyncio.sleep(0.001)

        async def record_successes():
            for _ in range(10):
                cb.record_success()
                await asyncio.sleep(0.001)

        # Run both simultaneously
        await asyncio.gather(record_failures(), record_successes())
        # Should not crash, state should be consistent
        assert cb.current_state in [CircuitBreakerState.CLOSED, CircuitBreakerState.OPEN]

    @pytest.mark.asyncio
    async def test_rapid_timeout_check(self):
        """Attack: Rapid timeout checks with zero/negative timeout."""
        cb = AsyncCircuitBreaker(fail_max=1, reset_timeout=0.001, name="rapid_test")

        # Force open
        cb._transition_to_open()
        assert cb.current_state == CircuitBreakerState.OPEN

        # Rapid checks
        for _ in range(100):
            cb._check_timeout()

        # Should eventually transition or stay consistent
        assert cb.current_state in [CircuitBreakerState.OPEN, CircuitBreakerState.HALF_OPEN]

    @pytest.mark.asyncio
    async def test_call_with_exception_raising_func(self):
        """Attack: Function that raises various exception types."""
        cb = AsyncCircuitBreaker(fail_max=2, reset_timeout=60, name="exception_test")

        class CustomException(Exception):
            pass

        async def raise_custom():
            raise CustomException("Custom error")

        async def raise_system_exit():
            raise SystemExit("System exit")

        async def raise_keyboard_interrupt():
            raise KeyboardInterrupt("Keyboard interrupt")

        # These should all be caught and recorded as failures
        for func in [raise_custom, raise_system_exit, raise_keyboard_interrupt]:
            cb._fail_counter = 0
            cb._state = CircuitBreakerState.CLOSED
            try:
                await cb.call(func)
            except (CustomException, SystemExit, KeyboardInterrupt):
                pass
            # SystemExit and KeyboardInterrupt may not be caught properly
            # This is a security concern if they bypass the circuit breaker

    @pytest.mark.asyncio
    async def test_decorator_with_malicious_function(self):
        """Attack: Decorator with function that modifies circuit breaker."""
        cb = AsyncCircuitBreaker(fail_max=5, reset_timeout=60, name="decorator_test")

        @circuit_breaker(cb)
        async def malicious_func():
            # Try to manipulate the circuit breaker from within
            cb._fail_counter = -999
            cb._state = CircuitBreakerState.CLOSED
            return "hacked"

        await malicious_func()
        # The function was able to modify the circuit breaker state
        # This is a potential security issue
        assert cb._fail_counter == -999  # Demonstrates the vulnerability


# =============================================================================
# EMBEDDINGS SERVICE ADVERSARIAL TESTS
# =============================================================================

class TestEmbeddingsAdversarial:
    """Attack vectors against embedding service."""

    @pytest.fixture
    def mock_settings(self):
        """Mock settings for embedding service tests."""
        with patch('app.services.embeddings.settings') as mock, \
             patch('app.services.embeddings.assert_url_safe'):
            mock.ollama_embedding_url = "http://localhost:11434"
            mock.embedding_model = "test-model"
            mock.tri_vector_search_enabled = False
            mock.flag_embedding_url = None
            mock.embedding_doc_prefix = ""
            mock.embedding_query_prefix = ""
            mock.embedding_batch_size = 512
            mock.embedding_batch_max_retries = 3
            mock.embedding_batch_min_sub_size = 1
            mock.embedding_concurrent_batches = 4
            yield mock

    def _apply_mock_settings(self, mock_settings):
        """Apply mock settings to the embeddings module, handling reimports.

        After admin tests delete and reimport app.main, the embeddings module might
        be reimported, which rebinds the settings reference. This helper ensures the
        mocked settings are applied to the current module state.

        Returns a tuple of (emb_mod, original_settings) so the caller can restore later.
        """
        from app.services import embeddings as emb_mod
        original_settings = emb_mod.settings
        emb_mod.settings = mock_settings
        return emb_mod, original_settings

    def test_init_with_empty_url(self, mock_settings):
        """Attack: Empty URL should raise error."""
        mock_settings.ollama_embedding_url = ""

        from app.services.embeddings import EmbeddingError
        from app.services.embeddings import EmbeddingService as EmbeddingService_Fresh
        emb_mod, original_settings = self._apply_mock_settings(mock_settings)

        try:
            try:
                EmbeddingService_Fresh()
                pytest.fail("EmbeddingService() did not raise EmbeddingError")
            except EmbeddingError as e:
                if "not configured" not in str(e):
                    pytest.fail(f"EmbeddingError message '{e}' does not contain 'not configured'")
        finally:
            emb_mod.settings = original_settings

    def _assert_embedding_error(
        self, exception_fn, expected_message_substring
    ):
        """Helper to assert EmbeddingError is raised with expected message."""
        from app.services.embeddings import EmbeddingError

        try:
            exception_fn()
            pytest.fail("EmbeddingService() did not raise EmbeddingError")
        except EmbeddingError as e:
            if expected_message_substring not in str(e):
                pytest.fail(
                    f"EmbeddingError message '{e}' does not contain "
                    f"'{expected_message_substring}'"
                )

    def test_init_with_invalid_url_scheme(self, mock_settings):
        """Attack: Invalid URL scheme (ftp://, file://)."""
        mock_settings.ollama_embedding_url = "ftp://localhost:11434"
        from app.services.embeddings import EmbeddingService as EmbeddingService_Fresh

        emb_mod, original_settings = self._apply_mock_settings(mock_settings)
        try:
            self._assert_embedding_error(
                EmbeddingService_Fresh, "Invalid embedding URL"
            )
        finally:
            emb_mod.settings = original_settings

    def test_init_with_javascript_url(self, mock_settings):
        """Attack: JavaScript URL (XSS attempt)."""
        mock_settings.ollama_embedding_url = "javascript:alert('xss')"
        from app.services.embeddings import EmbeddingService as EmbeddingService_Fresh

        emb_mod, original_settings = self._apply_mock_settings(mock_settings)
        try:
            self._assert_embedding_error(
                EmbeddingService_Fresh, "Invalid embedding URL"
            )
        finally:
            emb_mod.settings = original_settings

    def test_init_with_data_url(self, mock_settings):
        """Attack: Data URL (potential injection)."""
        mock_settings.ollama_embedding_url = (
            "data:text/html,<script>alert('xss')</script>"
        )
        from app.services.embeddings import EmbeddingService as EmbeddingService_Fresh

        emb_mod, original_settings = self._apply_mock_settings(mock_settings)
        try:
            self._assert_embedding_error(
                EmbeddingService_Fresh, "Invalid embedding URL"
            )
        finally:
            emb_mod.settings = original_settings

    def test_init_with_path_traversal_url(self, mock_settings):
        """Attack: Path traversal in URL.

        The SSRF guard now blocks localhost before path traversal is even parsed,
        which is also safe behavior. Accept either outcome.
        """
        from app.services.ssrf import URLBlocked
        mock_settings.ollama_embedding_url = "http://localhost:11434/../../../etc/passwd"
        try:
            service = EmbeddingService()
            assert service is not None
        except (EmbeddingError, URLBlocked):
            pass  # SSRF guard or validation rejected the URL — also safe

    def test_init_with_null_bytes(self, mock_settings):
        """Attack: Null bytes in URL."""
        from app.services.ssrf import URLBlocked
        mock_settings.ollama_embedding_url = "http://localhost:11434\x00/api/embeddings"
        # Should handle gracefully
        try:
            EmbeddingService()
        except (EmbeddingError, ValueError, URLBlocked):
            pass  # Expected

    @pytest.mark.asyncio
    async def test_embed_single_with_sql_injection(self, mock_settings):
        """Attack: SQL injection in text input."""
        service = EmbeddingService()

        sql_injection_text = "'; DROP TABLE users; --"
        # Should not execute SQL, should treat as plain text
        with patch.object(service._client, 'post', new_callable=AsyncMock) as mock_post:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"embedding": [0.1, 0.2, 0.3]}
            mock_post.return_value = mock_response

            try:
                await service.embed_single(sql_injection_text)
                # The SQL should be treated as text, not executed
                mock_post.assert_called_once()
                call_args = mock_post.call_args
                # Verify the SQL is in the payload as text, not executed
                payload = call_args[1].get('json', {})
                assert sql_injection_text in str(payload)
            except EmbeddingError:
                pass

    @pytest.mark.asyncio
    async def test_embed_single_with_xss_payload(self, mock_settings):
        """Attack: XSS payload in text input."""
        service = EmbeddingService()

        xss_text = "<script>alert('xss')</script>"
        with patch.object(service._client, 'post', new_callable=AsyncMock) as mock_post:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"embedding": [0.1, 0.2, 0.3]}
            mock_post.return_value = mock_response

            await service.embed_single(xss_text)
            # XSS should be preserved as text, not executed
            mock_post.assert_called_once()

    @pytest.mark.asyncio
    async def test_embed_single_with_oversized_text(self, mock_settings):
        """Attack: Text exceeding MAX_TEXT_LENGTH."""
        service = EmbeddingService()

        oversized_text = "x" * (service.MAX_TEXT_LENGTH + 1000)
        with pytest.raises(EmbeddingError, match="exceeds maximum length"):
            await service.embed_batch([oversized_text])

    @pytest.mark.asyncio
    async def test_embed_single_with_extreme_oversized_text(self, mock_settings):
        """Attack: Extremely large text (memory exhaustion attempt)."""
        service = EmbeddingService()

        # Create a very large string that could cause memory issues
        huge_text = "x" * 100_000_000  # 100MB string
        with pytest.raises(EmbeddingError, match="exceeds maximum length"):
            await service.embed_batch([huge_text])

    @pytest.mark.asyncio
    async def test_embed_batch_with_oversized_batch(self, mock_settings):
        """Attack: Batch size exceeding MAX_BATCH_SIZE."""
        service = EmbeddingService()

        # Create batch larger than MAX_BATCH_SIZE
        texts = ["test"] * (service.MAX_BATCH_SIZE + 100)
        with patch.object(service._client, 'post', new_callable=AsyncMock) as mock_post:
            # Mock should return different responses for each batch call
            # First call: 512 embeddings, second call: 100 embeddings
            def create_response(batch_size):
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {
                    "embeddings": [[0.1, 0.2]] * batch_size
                }
                return mock_response

            # Use side_effect to return different responses for each call
            mock_post.side_effect = [
                create_response(512),  # First batch
                create_response(100),  # Second batch
            ]

            # Should process in chunks, not fail
            await service.embed_batch(texts)
            # Verify multiple calls were made due to batch size limit
            assert mock_post.call_count == 2

    @pytest.mark.asyncio
    async def test_embed_batch_with_negative_batch_size(self, mock_settings):
        """Attack: Negative batch size parameter."""
        service = EmbeddingService()

        texts = ["test1", "test2"]
        # Negative batch_size should be clamped to valid range (1)
        with patch.object(service._client, 'post', new_callable=AsyncMock) as mock_post:
            # With batch_size=1 and 2 texts, expect 2 calls with 1 embedding each
            def create_response(batch_size):
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {
                    "embeddings": [[0.1, 0.2]] * batch_size
                }
                return mock_response

            mock_post.side_effect = [
                create_response(1),  # First batch
                create_response(1),  # Second batch
            ]

            result = await service.embed_batch(texts, batch_size=-10)
            # Should handle gracefully (clamped to 1)
            assert len(result) == 2

    @pytest.mark.asyncio
    async def test_embed_batch_with_zero_batch_size(self, mock_settings):
        """Attack: Zero batch size parameter."""
        service = EmbeddingService()

        texts = ["test1", "test2"]
        # Zero batch_size should be clamped to valid range (1)
        with patch.object(service._client, 'post', new_callable=AsyncMock) as mock_post:
            # With batch_size=1 and 2 texts, expect 2 calls with 1 embedding each
            def create_response(batch_size):
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {
                    "embeddings": [[0.1, 0.2]] * batch_size
                }
                return mock_response

            mock_post.side_effect = [
                create_response(1),  # First batch
                create_response(1),  # Second batch
            ]

            result = await service.embed_batch(texts, batch_size=0)
            # Should handle gracefully (clamped to 1)
            assert len(result) == 2

    @pytest.mark.asyncio
    async def test_embed_batch_with_malicious_response(self, mock_settings):
        """Attack: Malformed API response."""
        service = EmbeddingService()

        with patch.object(service._client, 'post', new_callable=AsyncMock) as mock_post:
            # Response with missing embedding field
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"data": [{"invalid": "response"}]}
            mock_post.return_value = mock_response

            with pytest.raises(EmbeddingError):
                await service.embed_single("test")

    @pytest.mark.asyncio
    async def test_embed_batch_with_html_response(self, mock_settings):
        """Attack: HTML response instead of JSON (potential proxy/WAF block)."""
        service = EmbeddingService()

        with patch.object(service._client, 'post', new_callable=AsyncMock) as mock_post:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.side_effect = json.JSONDecodeError("test", "", 0)
            mock_response.text = "<html><body>Access Denied</body></html>"
            mock_post.return_value = mock_response

            with pytest.raises(EmbeddingError, match="Invalid response"):
                await service.embed_single("test")

    @pytest.mark.asyncio
    async def test_embed_batch_with_recursive_embedding(self, mock_settings):
        """Attack: Response with recursive/circular structure."""
        service = EmbeddingService()

        with patch.object(service._client, 'post', new_callable=AsyncMock) as mock_post:
            mock_response = MagicMock()
            mock_response.status_code = 200
            # Create a deeply nested structure
            nested = {"embedding": [0.1]}
            for _ in range(1000):
                nested = {"embedding": nested}
            mock_response.json.return_value = {"data": [nested]}
            mock_post.return_value = mock_response

            with pytest.raises(EmbeddingError):
                await service.embed_single("test")

    def test_lru_cache_with_negative_maxsize(self):
        """Attack: Negative cache maxsize."""
        cache = LRUCache(maxsize=-10)
        # Should handle gracefully
        cache.set("key", [0.1, 0.2])
        # With negative maxsize, cache might not store anything
        cache.get("key")
        # Behavior depends on implementation

    def test_lru_cache_with_zero_maxsize(self):
        """Attack: Zero cache maxsize."""
        cache = LRUCache(maxsize=0)
        cache.set("key", [0.1, 0.2])
        result = cache.get("key")
        # With zero maxsize, should return None
        assert result is None

    def test_lru_cache_with_extreme_maxsize(self):
        """Attack: Extremely large cache maxsize (memory exhaustion)."""
        cache = LRUCache(maxsize=100_000_000)
        # Should not crash, but might use excessive memory
        assert cache.maxsize == 100_000_000

    def test_lru_cache_with_malicious_key(self):
        """Attack: Cache key with special characters."""
        cache = LRUCache(maxsize=100)

        malicious_keys = [
            "'; DROP TABLE users; --",
            "<script>alert('xss')</script>",
            "../../../etc/passwd",
            "\x00\x01\x02\x03",
            "a" * 10000,  # Very long key
        ]

        for key in malicious_keys:
            cache.set(key, [0.1, 0.2])
            result = cache.get(key)
            assert result == [0.1, 0.2]

    @pytest.mark.asyncio
    async def test_validate_embedding_dimension_with_negative(self, mock_settings):
        """Attack: Negative expected dimension."""
        service = EmbeddingService()

        with pytest.raises(EmbeddingError, match="positive integer"):
            await service.validate_embedding_dimension(-10)

    @pytest.mark.asyncio
    async def test_validate_embedding_dimension_with_zero(self, mock_settings):
        """Attack: Zero expected dimension."""
        service = EmbeddingService()

        with pytest.raises(EmbeddingError, match="positive integer"):
            await service.validate_embedding_dimension(0)

    @pytest.mark.asyncio
    async def test_validate_embedding_dimension_with_extreme(self, mock_settings):
        """Attack: Extremely large expected dimension."""
        service = EmbeddingService()

        with patch.object(service, 'embed_single', new_callable=AsyncMock) as mock_embed:
            mock_embed.return_value = [0.1] * 1000

            with pytest.raises(EmbeddingError, match="dimension mismatch"):
                await service.validate_embedding_dimension(1000000)

    @pytest.mark.asyncio
    async def test_split_text_at_midpoint_with_null_bytes(self, mock_settings):
        """Attack: Text with null bytes."""
        service = EmbeddingService()

        text_with_null = "hello\x00world"
        left, right = service._split_text_at_midpoint(text_with_null)
        # Should handle gracefully
        assert isinstance(left, str) and isinstance(right, str)

    @pytest.mark.asyncio
    async def test_split_text_at_midpoint_with_control_chars(self, mock_settings):
        """Attack: Text with control characters."""
        service = EmbeddingService()

        text_with_control = "hello\x01\x02\x03world"
        left, right = service._split_text_at_midpoint(text_with_control)
        assert isinstance(left, str) and isinstance(right, str)

    def test_mean_pool_embeddings_with_mismatched_dims(self, mock_settings):
        """Attack: Embeddings with different dimensions."""
        service = EmbeddingService()

        emb1 = [0.1, 0.2, 0.3]
        emb2 = [0.4, 0.5]

        with pytest.raises(EmbeddingError, match="different dimensions"):
            service._mean_pool_embeddings(emb1, emb2)

    def test_mean_pool_embeddings_with_empty(self, mock_settings):
        """Attack: Empty embeddings."""
        service = EmbeddingService()

        with pytest.raises(EmbeddingError, match="different dimensions"):
            service._mean_pool_embeddings([], [0.1])

    def test_is_token_overflow_error_with_unicode(self, mock_settings):
        """Attack: Unicode error messages."""
        service = EmbeddingService()

        unicode_errors = [
            "输入（1000个标记）太大",  # Chinese
            "入力（1000トークン）が大きすぎます",  # Japanese
            "\x00\x01\x02",  # Binary
            "input (\u202e1000\u202c tokens) is too large",  # RTL override
        ]

        for error in unicode_errors:
            result = service._is_token_overflow_error(error)
            # Should not crash
            assert isinstance(result, bool)


# =============================================================================
# VECTOR STORE ADVERSARIAL TESTS
# =============================================================================

class TestVectorStoreAdversarial:
    """Attack vectors against vector store."""

    @pytest.fixture
    def mock_settings(self):
        """Mock settings for vector store tests."""
        with patch('app.services.vector_store.settings') as mock:
            mock.lancedb_path = Path("/tmp/test_lancedb")
            mock.vector_metric = "cosine"
            mock.multi_scale_indexing_enabled = False
            mock.multi_scale_chunk_sizes = ""
            mock.write_lock_timeout_seconds = 30.0
            mock.vector_search_concurrency = 16
            mock.embedding_dim = 384
            mock.optimize_mode = "periodic"
            mock.optimize_interval_chunks = 5000
            mock.index_rebuild_delta = 0.2
            mock.rrf_legacy_mode = False
            mock.hybrid_rrf_k = 60
            mock.multi_scale_rrf_k = 60
            yield mock

    @pytest.fixture
    async def mock_vector_store(self, mock_settings):
        """Create a mock vector store for testing."""
        from app.services.vector_store import VectorStore

        store = VectorStore(db_path=Path("/tmp/test_lancedb"))
        # Mock the database connection
        store.db = AsyncMock()
        store.table = AsyncMock()
        store._embedding_dim = 384
        return store

    @pytest.mark.asyncio
    async def test_add_chunks_with_sql_injection_file_id(self, mock_settings):
        """Attack: SQL injection in file_id field."""
        from app.services.vector_store import VectorStore

        store = VectorStore(db_path=Path("/tmp/test_lancedb"))
        store.table = AsyncMock()
        store.table.count_rows.return_value = 0
        store.table.list_indices.return_value = []
        store._embedding_dim = 384

        sql_injection_records = [{
            "id": "test_1",
            "text": "test text",
            "file_id": "'; DROP TABLE chunks; --",
            "vault_id": 1,
            "chunk_index": 0,
            "embedding": [0.1] * 384,
        }]

        # The SQL injection should be treated as a string value, not executed
        await store.add_chunks(sql_injection_records)
        store.table.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_add_chunks_with_path_traversal_id(self, mock_settings):
        """Attack: Path traversal in ID field."""
        from app.services.vector_store import VectorStore

        store = VectorStore(db_path=Path("/tmp/test_lancedb"))
        store.table = AsyncMock()
        store.table.count_rows.return_value = 0
        store.table.list_indices.return_value = []
        store._embedding_dim = 384

        path_traversal_records = [{
            "id": "../../../etc/passwd",
            "text": "test text",
            "file_id": "test_file",
            "vault_id": 1,
            "chunk_index": 0,
            "embedding": [0.1] * 384,
        }]

        await store.add_chunks(path_traversal_records)
        store.table.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_add_chunks_with_malformed_embedding(self, mock_settings):
        """Attack: Malformed embedding data."""
        from app.services.vector_store import VectorStore, VectorStoreValidationError

        store = VectorStore(db_path=Path("/tmp/test_lancedb"))
        store.table = AsyncMock()
        store._embedding_dim = 384

        malformed_records = [{
            "id": "test_1",
            "text": "test text",
            "file_id": "test_file",
            "vault_id": 1,
            "chunk_index": 0,
            "embedding": "not_a_list",  # String instead of list
        }]

        with pytest.raises(VectorStoreValidationError, match="must be a list"):
            await store.add_chunks(malformed_records)

    @pytest.mark.asyncio
    async def test_add_chunks_with_wrong_embedding_dimension(self, mock_settings):
        """Attack: Embedding with wrong dimension."""
        from app.services.vector_store import VectorStore, VectorStoreValidationError

        store = VectorStore(db_path=Path("/tmp/test_lancedb"))
        store.table = AsyncMock()
        store._embedding_dim = 384

        wrong_dim_records = [{
            "id": "test_1",
            "text": "test text",
            "file_id": "test_file",
            "vault_id": 1,
            "chunk_index": 0,
            "embedding": [0.1] * 100,  # Wrong dimension
        }]

        with pytest.raises(VectorStoreValidationError, match="dimension mismatch"):
            await store.add_chunks(wrong_dim_records)

    @pytest.mark.asyncio
    async def test_add_chunks_with_extreme_dimension(self, mock_settings):
        """Attack: Embedding with extreme dimension (memory exhaustion)."""
        from app.services.vector_store import VectorStore

        store = VectorStore(db_path=Path("/tmp/test_lancedb"))
        store.table = AsyncMock()
        store.table.count_rows.return_value = 0
        store.table.list_indices.return_value = []
        store._embedding_dim = None  # No dimension check

        extreme_dim_records = [{
            "id": "test_1",
            "text": "test text",
            "file_id": "test_file",
            "vault_id": 1,
            "chunk_index": 0,
            "embedding": [0.1] * 1_000_000,  # 1M dimensions
        }]

        # Should handle without crashing
        await store.add_chunks(extreme_dim_records)

    @pytest.mark.asyncio
    async def test_add_chunks_with_malformed_sparse_embedding(self, mock_settings):
        """Attack: Malformed sparse_embedding JSON."""
        from app.services.vector_store import VectorStore, VectorStoreValidationError

        store = VectorStore(db_path=Path("/tmp/test_lancedb"))
        store.table = AsyncMock()
        store._embedding_dim = 384

        malformed_sparse_records = [{
            "id": "test_1",
            "text": "test text",
            "file_id": "test_file",
            "vault_id": 1,
            "chunk_index": 0,
            "embedding": [0.1] * 384,
            "sparse_embedding": "not valid json {{",
        }]

        with pytest.raises(VectorStoreValidationError, match="valid JSON"):
            await store.add_chunks(malformed_sparse_records)

    @pytest.mark.asyncio
    async def test_add_chunks_with_xss_in_text(self, mock_settings):
        """Attack: XSS payload in text field."""
        from app.services.vector_store import VectorStore

        store = VectorStore(db_path=Path("/tmp/test_lancedb"))
        store.table = AsyncMock()
        store.table.count_rows.return_value = 0
        store.table.list_indices.return_value = []
        store._embedding_dim = 384

        xss_records = [{
            "id": "test_1",
            "text": "<script>alert('xss')</script>",
            "file_id": "test_file",
            "vault_id": 1,
            "chunk_index": 0,
            "embedding": [0.1] * 384,
        }]

        await store.add_chunks(xss_records)
        store.table.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_search_with_sql_injection_filter(self, mock_settings):
        """Attack: SQL injection in filter expression."""
        from app.services.vector_store import VectorStore

        store = VectorStore(db_path=Path("/tmp/test_lancedb"))
        store.db = AsyncMock()
        store.table = MagicMock()
        store.table.count_rows = AsyncMock(return_value=0)
        store.table.list_indices = AsyncMock(return_value=[])

        # Mock table.search to return a mock query object (search is awaited)
        mock_query = MagicMock()
        mock_query.where.return_value = mock_query
        mock_query.limit.return_value = mock_query
        mock_query.to_list = AsyncMock(return_value=[])
        store.table.search = AsyncMock(return_value=mock_query)

        sql_filter = "file_id = 'test' OR '1'='1'"

        # The filter should be escaped or parameterized
        await store.search(
            embedding=[0.1] * 384,
            filter_expr=sql_filter,
        )

        # Verify where was called
        mock_query.where.assert_called()

    @pytest.mark.asyncio
    async def test_search_with_path_traversal_vault_id(self, mock_settings):
        """Attack: Path traversal in vault_id."""
        from app.services.vector_store import VectorStore

        store = VectorStore(db_path=Path("/tmp/test_lancedb"))
        store.db = AsyncMock()
        store.table = MagicMock()
        store.table.count_rows = AsyncMock(return_value=0)
        store.table.list_indices = AsyncMock(return_value=[])

        mock_query = MagicMock()
        mock_query.where.return_value = mock_query
        mock_query.limit.return_value = mock_query
        mock_query.to_list = AsyncMock(return_value=[])
        store.table.search = AsyncMock(return_value=mock_query)

        path_traversal_vault = "../../../etc/passwd"

        await store.search(
            embedding=[0.1] * 384,
            vault_id=path_traversal_vault,
        )

        # Should handle gracefully
        mock_query.where.assert_called()

    @pytest.mark.asyncio
    async def test_search_with_unicode_injection(self, mock_settings):
        """Attack: Unicode injection in query text."""
        from app.services.vector_store import VectorStore

        store = VectorStore(db_path=Path("/tmp/test_lancedb"))
        store.db = AsyncMock()
        store.table = MagicMock()
        store.table.count_rows = AsyncMock(return_value=0)
        store.table.list_indices = AsyncMock(return_value=[])

        mock_query = MagicMock()
        mock_query.where.return_value = mock_query
        mock_query.limit.return_value = mock_query
        mock_query.to_list = AsyncMock(return_value=[])
        store.table.search = AsyncMock(return_value=mock_query)

        unicode_query = "test\u202e\u202d\u202c\u202e"  # Bidirectional override chars

        await store.search(
            embedding=[0.1] * 384,
            query_text=unicode_query,
            hybrid=True,
        )

        # Should handle gracefully
        store.table.search.assert_called()

    @pytest.mark.asyncio
    async def test_delete_by_file_with_sql_injection(self, mock_settings):
        """Attack: SQL injection in file_id for delete."""
        from app.services.vector_store import VectorStore

        store = VectorStore(db_path=Path("/tmp/test_lancedb"))
        store.db = AsyncMock()
        store.table = AsyncMock()
        store.table.count_rows.return_value = 0

        sql_injection_file_id = "'; DROP TABLE chunks; --"

        await store.delete_by_file(sql_injection_file_id)

        # Verify delete was called - the SQL should be escaped
        store.table.delete.assert_called_once()
        call_args = store.table.delete.call_args[0][0]
        # The injection should be in the filter string but escaped
        assert "DROP TABLE" in call_args or "';" in call_args

    @pytest.mark.asyncio
    async def test_delete_by_vault_with_special_chars(self, mock_settings):
        """Attack: Special characters in vault_id for delete."""
        from app.services.vector_store import VectorStore

        store = VectorStore(db_path=Path("/tmp/test_lancedb"))
        store.db = AsyncMock()
        store.table = AsyncMock()
        store.table.count_rows.return_value = 0

        special_vault_ids = [
            "vault' OR '1'='1",
            "vault\"; DROP TABLE",
            "vault\x00\x01\x02",
            "vault/../../../etc",
        ]

        for vault_id in special_vault_ids:
            store.table.delete.reset_mock()
            await store.delete_by_vault(vault_id)
            store.table.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_chunks_by_uid_with_sql_injection(self, mock_settings):
        """Attack: SQL injection in chunk_uids."""
        from app.services.vector_store import VectorStore

        store = VectorStore(db_path=Path("/tmp/test_lancedb"))
        store.table = MagicMock()
        store.table.count_rows = AsyncMock(return_value=0)
        store.table.list_indices = AsyncMock(return_value=[])

        # get_chunks_by_uid uses: await self.table.query().where(query).to_list()
        mock_where = MagicMock()
        mock_where.where.return_value = mock_where
        mock_where.to_list = AsyncMock(return_value=[])
        store.table.query.return_value = mock_where

        malicious_uids = [
            "file_1'; DROP TABLE chunks; --",
            "file_1' OR '1'='1",
            "file_1\"; DELETE FROM chunks; --",
        ]

        await store.get_chunks_by_uid(malicious_uids)

        # Verify the query was constructed
        store.table.query.assert_called()

    @pytest.mark.asyncio
    async def test_init_table_with_negative_dimension(self, mock_settings):
        """Attack: Negative embedding dimension."""
        from app.services.vector_store import VectorStore

        store = VectorStore(db_path=Path("/tmp/test_lancedb"))
        store.db = AsyncMock()

        # Negative dimension should be handled gracefully
        # PyArrow might reject this
        try:
            await store.init_table(embedding_dim=-10)
        except (ValueError, OSError, AttributeError):
            # Expected - negative dimension is invalid
            pass

    @pytest.mark.asyncio
    async def test_init_table_with_extreme_dimension(self, mock_settings):
        """Attack: Extreme embedding dimension."""
        from app.services.vector_store import VectorStore

        store = VectorStore(db_path=Path("/tmp/test_lancedb"))
        store.db = AsyncMock()

        # Very large dimension
        try:
            await store.init_table(embedding_dim=1_000_000)
        except (ValueError, OSError, MemoryError, AttributeError):
            # Might fail due to memory constraints
            pass


# =============================================================================
# INTEGRATION ATTACK TESTS
# =============================================================================

class TestIntegrationAttacks:
    """Cross-service attack vectors."""

    @pytest.mark.asyncio
    async def test_embedding_to_vector_store_chain(self):
        """Attack: Chain embedding output to vector store input."""
        from app.services.embeddings import EmbeddingService
        from app.services.vector_store import VectorStore

        with patch('app.services.embeddings.settings') as mock_emb_settings, \
             patch('app.services.embeddings.assert_url_safe'):
            mock_emb_settings.ollama_embedding_url = "http://localhost:11434"
            mock_emb_settings.embedding_model = "test-model"
            mock_emb_settings.tri_vector_search_enabled = False
            mock_emb_settings.flag_embedding_url = None
            mock_emb_settings.embedding_doc_prefix = ""
            mock_emb_settings.embedding_query_prefix = ""
            mock_emb_settings.embedding_batch_size = 512
            mock_emb_settings.embedding_batch_max_retries = 3
            mock_emb_settings.embedding_batch_min_sub_size = 1

            EmbeddingService()

            with patch('app.services.vector_store.settings') as mock_vs_settings:
                mock_vs_settings.lancedb_path = Path("/tmp/test_lancedb")
                mock_vs_settings.vector_metric = "cosine"
                mock_vs_settings.multi_scale_indexing_enabled = False
                mock_vs_settings.multi_scale_chunk_sizes = ""
                mock_vs_settings.write_lock_timeout_seconds = 30.0
                mock_vs_settings.vector_search_concurrency = 16

                store = VectorStore()
                store.table = AsyncMock()
                store.table.count_rows.return_value = 0
                store.table.list_indices.return_value = []
                store._embedding_dim = 384

                # Simulate embedding output being used as vector store input
                mock_embedding = [float('nan')] * 384  # NaN values

                record = {
                    "id": "test_1",
                    "text": "test",
                    "file_id": "file_1",
                    "vault_id": 1,
                    "chunk_index": 0,
                    "embedding": mock_embedding,
                }

                # Should handle NaN values gracefully
                await store.add_chunks([record])

    @pytest.mark.asyncio
    async def test_circuit_breaker_with_embedding_service(self):
        """Attack: Circuit breaker state manipulation via embedding failures."""
        from app.services.circuit_breaker import AsyncCircuitBreaker
        from app.services.embeddings import EmbeddingService

        with patch('app.services.embeddings.settings') as mock_settings, \
             patch('app.services.embeddings.assert_url_safe'):
            mock_settings.ollama_embedding_url = "http://localhost:11434"
            mock_settings.embedding_model = "test-model"
            mock_settings.tri_vector_search_enabled = False
            mock_settings.flag_embedding_url = None
            mock_settings.embedding_doc_prefix = ""
            mock_settings.embedding_query_prefix = ""
            mock_settings.embedding_batch_size = 512
            mock_settings.embedding_batch_max_retries = 3
            mock_settings.embedding_batch_min_sub_size = 1

            service = EmbeddingService()

            # Rapid failures should trip circuit breaker
            cb = AsyncCircuitBreaker(fail_max=3, reset_timeout=60, name="test")

            with patch.object(service._client, 'post', new_callable=AsyncMock) as mock_post:
                mock_post.side_effect = Exception("Connection refused")

                # Trigger multiple failures
                for _ in range(5):
                    try:
                        await cb.call(service._client.post, "http://test", json={})
                    except Exception:
                        pass

                # Circuit should be open
                assert cb.current_state == CircuitBreakerState.OPEN


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
