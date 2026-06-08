"""
Dual-provider embedding client service supporting Ollama and OpenAI-compatible APIs.
"""

import asyncio
import hashlib
import logging
from collections import OrderedDict
from typing import List, Optional
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.services.circuit_breaker import CircuitBreakerError, embeddings_cb
from app.services.ssrf import assert_url_safe

logger = logging.getLogger(__name__)


class LRUCache:
    """Simple LRU cache with size limit and hit/miss statistics."""

    def __init__(self, maxsize: int = 1000):
        self.maxsize = maxsize
        self._cache: OrderedDict[str, List[float]] = OrderedDict()
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Optional[List[float]]:
        """Get value from cache, moving to end if found (LRU)."""
        if key in self._cache:
            self._cache.move_to_end(key)
            self._hits += 1
            return self._cache[key]
        self._misses += 1
        return None

    def set(self, key: str, value: List[float]) -> None:
        """Set value in cache, evicting oldest if at capacity."""
        if self.maxsize <= 0:
            return
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = value
        if len(self._cache) > self.maxsize:
            self._cache.popitem(last=False)

    @property
    def hits(self) -> int:
        return self._hits

    @property
    def misses(self) -> int:
        return self._misses

    @property
    def size(self) -> int:
        return len(self._cache)

    def get_stats(self) -> dict:
        """Return cache statistics."""
        total = self._hits + self._misses
        hit_rate = (self._hits / total * 100) if total > 0 else 0
        return {
            "hits": self._hits,
            "misses": self._misses,
            "size": self.size,
            "maxsize": self.maxsize,
            "hit_rate": round(hit_rate, 2),
        }

    def clear(self) -> None:
        """Clear cache and reset statistics."""
        self._cache.clear()
        self._hits = 0
        self._misses = 0


class EmbeddingError(Exception):
    """Exception raised for embedding service errors."""

    pass


class EmbeddingDimensionMismatchError(EmbeddingError):
    """Raised when embedding dimensions don't match the expected schema."""

    def __init__(self, expected: int, got: int):
        self.expected = expected
        self.got = got
        super().__init__(
            f"Embedding dimension mismatch: expected {expected}, got {got}"
        )


class EmbeddingService:
    """Service for generating text embeddings via Ollama or OpenAI-compatible APIs."""

    # Hard caps for input validation
    MAX_BATCH_SIZE = 512  # Maximum number of texts per batch call
    MAX_TEXT_LENGTH = (
        8192  # Maximum characters per text (derived from chunk_size_chars=8192)
    )
    MIN_SPLIT_CHARS = 200  # Minimum text length to attempt single-text splitting

    # Qwen3 auto-prefixes applied when the configured model includes "qwen"
    # and the user hasn't supplied explicit prefixes in settings.
    _QWEN_DOC_PREFIX = (
        "Instruct: Represent this technical documentation passage for retrieval.\n"
        "Document: "
    )
    _QWEN_QUERY_PREFIX = (
        "Instruct: Retrieve relevant technical documentation passages.\n"
        "Query: "
    )
    _global_batch_semaphore: asyncio.Semaphore | None = None
    _global_batch_semaphore_limit: int | None = None
    _global_batch_semaphore_loop: asyncio.AbstractEventLoop | None = None

    def __init__(self):
        """Initialize the embedding service with HTTP client and provider detection.

        URL, model name, and prefixes are read live from ``settings`` on each
        call so admins can change them via the Settings UI without restarting.
        The initial URL is validated here to fail fast at startup when the
        service is unconfigured; subsequent reads tolerate an empty URL (the
        operation will fail at request time with a clear error).
        """
        base_url = settings.ollama_embedding_url

        # Validate base_url at startup so misconfiguration is loud
        if not base_url:
            raise EmbeddingError("Embedding service is not configured")
        if not base_url.startswith(("http://", "https://")):
            raise EmbeddingError("Invalid embedding URL configuration")

        assert_url_safe(base_url)

        self.timeout = 60.0

        # Persistent HTTP client — created once, reused for all embedding calls.
        # URL is passed per-request from `embeddings_url`, so this pool survives
        # endpoint changes.
        self._client = httpx.AsyncClient(
            timeout=self.timeout,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

        # LRU cache. Cache keys include the live model/url/prefix fingerprints,
        # so a settings change naturally invalidates cached entries.
        self._embed_cache = LRUCache(maxsize=1000)

        # Memoized (base_url, resolved_tuple) so provider-mode detection runs
        # once per configured URL instead of on every one of the ~19 property
        # reads per embedding call.
        self._resolved_cache: Optional[tuple] = None

    def _get_global_batch_semaphore(self) -> asyncio.Semaphore:
        """Return the process-wide embedding batch semaphore for the current loop."""
        limit = getattr(settings, "embedding_global_concurrent_batches", None)
        if not isinstance(limit, int):
            limit = settings.embedding_concurrent_batches
        loop = asyncio.get_running_loop()
        if (
            self.__class__._global_batch_semaphore is None
            or self.__class__._global_batch_semaphore_limit != limit
            or self.__class__._global_batch_semaphore_loop is not loop
        ):
            self.__class__._global_batch_semaphore = asyncio.Semaphore(limit)
            self.__class__._global_batch_semaphore_limit = limit
            self.__class__._global_batch_semaphore_loop = loop
        return self.__class__._global_batch_semaphore

    @property
    def embedding_model(self) -> str:
        """Live read of the configured embedding model name."""
        return settings.embedding_model

    @property
    def embedding_doc_prefix(self) -> str:
        """Live read of the document prefix; auto-applies Qwen3 default when unset."""
        prefix = settings.embedding_doc_prefix
        if not prefix and "qwen" in settings.embedding_model.lower():
            return self._QWEN_DOC_PREFIX
        return prefix

    @property
    def embedding_query_prefix(self) -> str:
        """Live read of the query prefix; auto-applies Qwen3 default when unset."""
        prefix = settings.embedding_query_prefix
        if not prefix and "qwen" in settings.embedding_model.lower():
            return self._QWEN_QUERY_PREFIX
        return prefix

    @property
    def provider_mode(self) -> str:
        """Live read of the provider mode derived from the configured URL."""
        return self._resolved_url_and_mode()[0]

    @property
    def embeddings_url(self) -> str:
        """Live read of the resolved embeddings endpoint URL."""
        return self._resolved_url_and_mode()[1]

    def _resolved_url_and_mode(self) -> tuple:
        """Resolve provider mode and embeddings URL from the current settings.

        Reads ``settings.ollama_embedding_url`` at call time so endpoint
        changes take effect without re-instantiating the service.
        """
        base_url = settings.ollama_embedding_url
        if not base_url:
            raise EmbeddingError("Embedding service is not configured")
        if not base_url.startswith(("http://", "https://")):
            raise EmbeddingError("Invalid embedding URL configuration")
        cached = self._resolved_cache
        if cached is not None and cached[0] == base_url:
            return cached[1]
        resolved = self._detect_provider_mode(base_url)
        self._resolved_cache = (base_url, resolved)
        return resolved

    def _detect_provider_mode(self, base_url: str) -> tuple:
        """
        Detect which embedding provider mode to use based on URL path.

        Detection strategy:
        - If URL path includes '/api/embeddings' -> Ollama mode
        - If URL path includes '/v1/embeddings' -> OpenAI mode
        - If no explicit embeddings path:
          - Port 1234 -> OpenAI mode (LM Studio default)
          - Otherwise -> Ollama mode

        Args:
            base_url: The configured embedding URL

        Returns:
            Tuple of (provider_mode, embeddings_url)
        """
        parsed = urlparse(base_url)
        path = parsed.path

        # Check for explicit paths
        if "/api/embeddings" in path:
            # Already has Ollama path, use as-is
            return ("ollama", base_url)
        elif "/v1/embeddings" in path:
            # Already has OpenAI path, use as-is
            return ("openai", base_url)

        # No explicit path - determine by port
        port = parsed.port
        if port == 1234:
            # LM Studio default port - use OpenAI mode
            base_url = base_url.rstrip("/") + "/v1/embeddings"
            return ("openai", base_url)
        elif port == 8080:
            # TEI default port - use OpenAI mode
            base_url = base_url.rstrip("/") + "/v1/embeddings"
            return ("openai", base_url)
        else:
            # Default to Ollama mode
            base_url = base_url.rstrip("/") + "/api/embeddings"
            return ("ollama", base_url)

    def _build_payload(self, text: str) -> dict:
        """
        Build the API request payload based on provider mode.

        Args:
            text: The text to embed

        Returns:
            Dictionary payload for the API request
        """
        if self.provider_mode == "openai":
            return {"model": self.embedding_model, "input": text}
        else:  # ollama mode
            return {"model": self.embedding_model, "prompt": text}

    def _extract_embedding(self, data: dict) -> List[float]:
        """
        Extract embedding vector from API response based on provider mode.

        Args:
            data: Parsed JSON response from the API

        Returns:
            List of float values representing the embedding vector

        Raises:
            EmbeddingError: If embedding cannot be extracted
        """
        if self.provider_mode == "openai":
            # OpenAI format: data[0].embedding
            if "data" not in data:
                logger.error(
                    "Embedding API response missing 'data' field in OpenAI mode"
                )
                raise EmbeddingError("Embedding API response is invalid")
            if not isinstance(data["data"], list) or len(data["data"]) == 0:
                logger.error(
                    "Embedding API response 'data' field is empty or invalid in OpenAI mode"
                )
                raise EmbeddingError("Embedding API response is invalid")
            embedding = data["data"][0].get("embedding")
            if embedding is None:
                logger.error(
                    "Embedding API response missing 'data[0].embedding' field in OpenAI mode"
                )
                raise EmbeddingError("Embedding API response is invalid")
        else:  # ollama mode
            # Ollama format: embedding
            embedding = data.get("embedding")
            if embedding is None:
                logger.error(
                    "Embedding API response missing 'embedding' field in Ollama mode"
                )
                raise EmbeddingError("Embedding API response is invalid")

        return embedding

    async def _embed_with_prefix(self, text: str, prefix: str) -> List[float]:
        """
        Shared embedding logic with prefix application.

        This is a private helper method that implements the common logic for
        both embed_single (query embeddings) and embed_passage (document embeddings).
        It validates input, applies the provided prefix, checks cache, calls the
        embedding API, extracts the embedding, and stores it in cache.

        Args:
            text: The text to embed (plain text, without prefix applied).
            prefix: The prefix to prepend to the text (query or document prefix).

        Returns:
            List of float values representing the embedding vector.

        Raises:
            EmbeddingError: If the API request fails or returns non-200 status.
        """
        # Validate text input
        if text is None:
            raise EmbeddingError("Text cannot be None")
        if not text.strip():
            raise EmbeddingError("Text cannot be empty or whitespace only")

        # Apply prefix to text
        text_to_embed = prefix + text if prefix else text

        # Build cache key with model + url + prefix fingerprints
        model_fingerprint = hashlib.md5(self.embedding_model.encode("utf-8")).hexdigest()[:8]
        url_fingerprint = hashlib.md5(self.embeddings_url.encode("utf-8")).hexdigest()[:8]
        prefix_fingerprint = hashlib.md5((prefix or "").encode("utf-8")).hexdigest()[:8]
        cache_key = f"{model_fingerprint}_{url_fingerprint}_{prefix_fingerprint}_{hashlib.md5(text_to_embed.encode('utf-8')).hexdigest()}"

        # Check cache
        cached = self._embed_cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            response = await embeddings_cb(self._client.post)(
                self.embeddings_url, json=self._build_payload(text_to_embed)
            )

            if response.status_code != 200:
                logger.warning(
                    f"Embedding API returned status {response.status_code} for {self.provider_mode} mode: {response.text}"
                )
                raise EmbeddingError(
                    f"Embedding API returned status {response.status_code}"
                )

            try:
                data = response.json()
            except ValueError as e:
                logger.warning(
                    f"Invalid JSON response from embedding API for {self.provider_mode} mode: {e}, response: {response.text}"
                )
                raise EmbeddingError("Invalid response from embedding service")

            embedding = self._extract_embedding(data)

            # Store in cache
            self._embed_cache.set(cache_key, embedding)

            return embedding

        except httpx.TimeoutException:
            raise EmbeddingError("Embedding request timed out")
        except httpx.HTTPError:
            raise EmbeddingError("Embedding HTTP error occurred")

    async def embed_single(self, text: str) -> List[float]:
        """
        Generate embedding for a single text.

        Applies the query prefix (if configured) to the input text before embedding.
        The query prefix is used for retrieval queries and must remain constant for
        a given index to ensure consistent embedding space.

        Results are cached using an LRU cache to avoid redundant API calls for
        repeated text inputs.

        Args:
            text: The text to embed.

        Returns:
            List of float values representing the embedding vector.

        Raises:
            EmbeddingError: If the API request fails or returns non-200 status.
        """
        return await self._embed_with_prefix(text, self.embedding_query_prefix)

    async def embed_passage(self, text: str) -> List[float]:
        """
        Generate embedding for a passage/document.

        Applies the document prefix (if configured) to the input text before embedding.
        The document prefix is used for indexing documents and must remain constant for
        a given index to ensure consistent embedding space.

        Results are cached using an LRU cache to avoid redundant API calls.

        Args:
            text: The text to embed as a passage/document.

        Returns:
            List of float values representing the embedding vector.

        Raises:
            EmbeddingError: If the API request fails or returns non-200 status.
        """
        return await self._embed_with_prefix(text, self.embedding_doc_prefix)

    async def validate_embedding_dimension(self, expected_dim: int) -> bool:
        """
        Validate that the embedding dimension matches the expected value.

        Args:
            expected_dim: The expected dimension of the embedding vector.
                Must be a positive integer.

        Returns:
            True if the dimension matches.

        Raises:
            EmbeddingError: If expected_dim is invalid or if the dimension
                does not match the expected value.
        """
        # Validate expected_dim input
        if not isinstance(expected_dim, int) or expected_dim <= 0:
            raise EmbeddingError(
                f"expected_dim must be a positive integer, got {expected_dim}"
            )

        embedding = await self.embed_single("dimension_check")
        actual_dim = len(embedding)
        if actual_dim != expected_dim:
            raise EmbeddingError(
                f"Embedding dimension mismatch: expected {expected_dim}, got {actual_dim}"
            )
        return True

    async def embed_batch(
        self, texts: List[str], batch_size: int | None = None, fail_fast: bool = True
    ) -> List[List[float]] | tuple[List[Optional[List[float]]], List[int]]:
        """
        Generate embeddings for a batch of texts using true API batching.

        Sends multiple texts per API request for efficient GPU utilization.
        Processes batches concurrently using asyncio.gather, limited by
        embedding_concurrent_batches setting.

        Applies the document prefix (if configured) to each input text before embedding.
        The document prefix is used for document embeddings and must remain constant for
        a given index to ensure consistent embedding space.

        Args:
            texts: List of texts to embed.
            batch_size: Number of texts per API request (default: 512).
            fail_fast: If True (default), raise on any batch failure.
                       If False, return (embeddings, failed_batch_indices) with None
                       placeholders for failed batches.

        Returns:
            When fail_fast=True: List of embedding vectors.
            When fail_fast=False: Tuple of (embeddings, failed_batch_indices) where failed batch positions contain None.

        Raises:
            EmbeddingError: If any batch fails and fail_fast=True.
        """
        if not texts:
            return [] if fail_fast else ([], [])

        # Input validation guards
        prefix_len = len(self.embedding_doc_prefix) if self.embedding_doc_prefix else 0
        effective_max = self.MAX_TEXT_LENGTH - prefix_len

        for idx, text in enumerate(texts):
            if text is None:
                raise EmbeddingError(f"Text at index {idx} is None")
            if not text.strip():
                raise EmbeddingError(f"Text at index {idx} is empty or whitespace only")
            if len(text) > effective_max:
                raise EmbeddingError(
                    f"Text at index {idx} exceeds maximum length ({effective_max} characters after prefix accounting)"
                )

        # Use configured batch size if not specified
        if batch_size is None:
            batch_size = settings.embedding_batch_size

        # Clamp batch_size to valid range
        batch_size = max(1, min(batch_size, self.MAX_BATCH_SIZE))

        # Apply document prefix to all texts
        texts_to_embed = []
        for text in texts:
            if self.embedding_doc_prefix:
                texts_to_embed.append(self.embedding_doc_prefix + text)
            else:
                texts_to_embed.append(text)

        # Process batches concurrently using both per-call and process-wide caps.
        all_embeddings: List[List[float]] = []
        sem = asyncio.Semaphore(settings.embedding_concurrent_batches)

        async def _process_batch(batch_texts: List[str]) -> List[List[float]]:
            async with sem:
                async with self._get_global_batch_semaphore():
                    return await self._embed_batch_api(batch_texts)

        batch_tasks = []
        for i in range(0, len(texts_to_embed), batch_size):
            batch = texts_to_embed[i : i + batch_size]
            batch_tasks.append(_process_batch(batch))

        batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)

        if fail_fast:
            for result in batch_results:
                if isinstance(result, Exception):
                    raise EmbeddingError(f"Embedding batch failed: {result}")
                all_embeddings.extend(result)
            return all_embeddings
        else:
            failed_indices: List[int] = []
            all_embeddings_with_nones: List[Optional[List[float]]] = []
            for batch_idx, result in enumerate(batch_results):
                if isinstance(result, Exception):
                    failed_indices.append(batch_idx)
                    # Add None placeholders for each text in this failed batch
                    start = batch_idx * batch_size
                    end = min(start + batch_size, len(texts_to_embed))
                    for _ in range(start, end):
                        all_embeddings_with_nones.append(None)
                    logger.warning(f"Batch {batch_idx} failed (skipping): {result}")
                else:
                    all_embeddings_with_nones.extend(result)
            return all_embeddings_with_nones, failed_indices

    async def _embed_batch_api(self, texts: List[str]) -> List[List[float]]:
        """
        Send a batch of texts to the embedding API in a single request.

        Implements adaptive batching: automatically retries with smaller sub-batches
        when llama.cpp token overflow errors occur.

        Args:
            texts: List of texts to embed (already prefixed).

        Returns:
            List of embedding vectors in the same order as input texts.

        Raises:
            EmbeddingError: If the API request fails after all retries.
        """
        max_retries = settings.embedding_batch_max_retries
        min_sub_size = settings.embedding_batch_min_sub_size

        return await self._embed_batch_with_retry(
            self._client, texts, max_retries, min_sub_size
        )

    def _log_pool_stats(self, client: httpx.AsyncClient) -> None:
        """Log connection pool and cache statistics for monitoring."""
        try:
            pool = getattr(client, "_transport", None)
            if pool and hasattr(pool, "_pool"):
                pool_obj = pool._pool
                connections = getattr(pool_obj, "_num_connections", 0)
                keepalive = getattr(pool_obj, "_num_keepalive", 0)
                limits = getattr(pool_obj, "_limits", None)
                max_connections = (
                    getattr(limits, "max_connections", 20) if limits else 20
                )
                max_keepalive = (
                    getattr(limits, "max_keepalive_connections", 10) if limits else 10
                )
                cache_stats = self._embed_cache.get_stats()
                logger.info(
                    f"Embedding service pool: {connections}/{max_connections} connections, "
                    f"{keepalive}/{max_keepalive} keepalive, "
                    f"cache: {cache_stats['hits']} hits, {cache_stats['misses']} misses, "
                    f"{cache_stats['hit_rate']}% hit rate, {cache_stats['size']}/{cache_stats['maxsize']} entries"
                )
        except Exception:
            pass  # Silently ignore any errors accessing internal pool state

    def get_cache_stats(self) -> dict:
        """
        Get embedding cache statistics.

        Returns:
            Dictionary with cache statistics including hits, misses,
            hit rate percentage, current size, and max size.
        """
        return self._embed_cache.get_stats()

    async def _embed_batch_with_retry(
        self,
        client: httpx.AsyncClient,
        texts: List[str],
        max_retries: int,
        min_sub_size: int,
        retry_count: int = 0,
    ) -> List[List[float]]:
        """
        Internal method that handles the retry logic for adaptive batching.

        Args:
            client: HTTP client for making requests
            texts: List of texts to embed
            max_retries: Maximum number of retry attempts
            min_sub_size: Minimum sub-batch size before giving up

        Returns:
            List of embedding vectors

        Raises:
            EmbeddingError: If all retries fail
        """
        # Empty-input guard
        if not texts:
            return []

        try:
            # Build payload with array of inputs
            if self.provider_mode == "openai":
                payload = {"model": self.embedding_model, "input": texts}
            else:  # ollama mode
                payload = {"model": self.embedding_model, "input": texts}

            try:
                response = await embeddings_cb(client.post)(
                    self.embeddings_url, json=payload
                )
            except CircuitBreakerError as e:
                raise EmbeddingError(f"Embedding service circuit breaker is open: {e}")

            # Check for token overflow error in HTTP 500 responses
            if response.status_code == 500:
                error_text = response.text.lower()
                if self._is_token_overflow_error(error_text):
                    logger.warning(
                        f"Token overflow error for {self.provider_mode} mode: {response.text}"
                    )
                    # Handle overflow using the shared helper
                    return await self._handle_overflow_retry(
                        client, texts, max_retries, min_sub_size, retry_count
                    )

            if response.status_code != 200:
                logger.warning(
                    f"Embedding API returned status {response.status_code} for {self.provider_mode} mode: {response.text}"
                )
                raise EmbeddingError(
                    f"Embedding API returned status {response.status_code}"
                )

            data = response.json()

            # Extract embeddings from response
            if self.provider_mode == "openai":
                # OpenAI format: data[].embedding
                embeddings = [item["embedding"] for item in data["data"]]
            else:
                # Ollama format may vary - try common formats
                if "embeddings" in data:
                    embeddings = data["embeddings"]
                elif "embedding" in data:
                    # Single embedding returned - shouldn't happen with batch
                    embeddings = [data["embedding"]]
                else:
                    logger.error(
                        f"Unexpected response format for {self.provider_mode} mode: {data.keys()}"
                    )
                    raise EmbeddingError("Unexpected response from embedding service")

            # Validate embedding structure
            if not isinstance(embeddings, list):
                logger.error("Embedding API response 'embeddings' is not a list")
                raise EmbeddingError("Embedding API response is invalid")
            for i, emb in enumerate(embeddings):
                if not isinstance(emb, list):
                    logger.error(f"Embedding at index {i} is not a list")
                    raise EmbeddingError("Embedding API response is invalid")
                for j, val in enumerate(emb):
                    if not isinstance(val, (int, float)):
                        logger.error(f"Embedding value at [{i}][{j}] is not a number")
                        raise EmbeddingError("Embedding API response is invalid")

            # Validate embedding count matches input count
            if len(embeddings) != len(texts):
                logger.error(
                    f"Embedding count mismatch for {self.provider_mode} mode: expected {len(texts)}, got {len(embeddings)}"
                )
                raise EmbeddingError(
                    f"Embedding count mismatch: expected {len(texts)}, got {len(embeddings)}"
                )

            # Log connection pool metrics
            self._log_pool_stats(client)

            return embeddings

        except httpx.TimeoutException as e:
            logger.warning(
                f"Embedding batch request timed out for {self.provider_mode} mode: {e}"
            )
            # For multi-item batches: split and retry each half so the server
            # gets smaller workloads — same recovery strategy as token overflow.
            if len(texts) > 1 and retry_count < max_retries:
                logger.info(
                    f"Timeout with {len(texts)} items — splitting batch and retrying "
                    f"(attempt {retry_count + 1}/{max_retries})"
                )
                return await self._handle_overflow_retry(
                    client, texts, max_retries, min_sub_size, retry_count
                )
            # For single-item batches: simple backoff retry
            if retry_count < max_retries:
                backoff_delay = min(0.5 * (2**retry_count), 2.0)
                logger.info(
                    f"Retrying single-item timeout (attempt {retry_count + 1}/{max_retries}) after {backoff_delay}s"
                )
                await asyncio.sleep(backoff_delay)
                return await self._embed_batch_with_retry(
                    client, texts, max_retries, min_sub_size, retry_count + 1
                )
            raise EmbeddingError(
                f"Embedding request timed out after {max_retries} retries"
            )
        except httpx.HTTPError as e:
            # Check if this is a token overflow error
            error_msg = str(e)
            response_text = ""

            # Try to get response text from the exception if available
            try:
                resp = getattr(e, "response", None)
                if resp is not None:
                    response_text = resp.text.lower()
            except (AttributeError, ValueError):
                # Response object may not have text attribute
                pass

            if self._is_token_overflow_error(
                error_msg
            ) or self._is_token_overflow_error(response_text):
                logger.warning(
                    f"Token overflow error for {self.provider_mode} mode: {response_text}"
                )
                # Handle overflow using the shared helper
                return await self._handle_overflow_retry(
                    client, texts, max_retries, min_sub_size, retry_count
                )
            else:
                # Not a token overflow error, re-raise
                logger.error(
                    f"Embedding batch HTTP error for {self.provider_mode} mode: {e}"
                )
                raise EmbeddingError("Embedding batch HTTP error occurred")

    def _split_text_at_midpoint(self, text: str) -> tuple:
        """
        Split a single text into two parts at a boundary-aware midpoint.

        Prefers splitting at newline or space characters near the midpoint
        to produce more natural splits. Falls back to strict midpoint if
        boundary-aware split would result in empty sides.

        Args:
            text: The text to split

        Returns:
            Tuple of (left_text, right_text), both non-empty for splittable text
        """
        if len(text) <= 1:
            return (text, "")

        midpoint = len(text) // 2

        # Try to find a better boundary near the midpoint
        # Look for newline first, then space
        search_start = max(0, midpoint - 50)
        search_end = min(len(text), midpoint + 50)

        # Search for newline near midpoint (forward)
        for i in range(midpoint, search_end):
            if text[i] == "\n":
                left, right = text[: i + 1], text[i + 1 :]
                # Ensure both sides are non-empty for splittable text
                if left and right:
                    return (left, right)

        # Search for newline near midpoint (backward)
        for i in range(midpoint - 1, search_start - 1, -1):
            if text[i] == "\n":
                left, right = text[: i + 1], text[i + 1 :]
                if left and right:
                    return (left, right)

        # Search for space near midpoint (forward)
        for i in range(midpoint, search_end):
            if text[i] == " ":
                left, right = text[:i], text[i:]
                if left and right:
                    return (left, right)

        # Search for space near midpoint (backward)
        for i in range(midpoint - 1, search_start - 1, -1):
            if text[i] == " ":
                left, right = text[:i], text[i:]
                if left and right:
                    return (left, right)

        # Fall back to strict midpoint (guaranteed non-empty for len > 1)
        left, right = text[:midpoint], text[midpoint:]
        # Double-check: if either is empty, adjust to ensure both non-empty
        if not left:
            left = text[:1]
            right = text[1:]
        elif not right:
            right = text[-1:]
            left = text[:-1]

        return (left, right)

    def _mean_pool_embeddings(
        self, emb1: List[float], emb2: List[float]
    ) -> List[float]:
        """
        Mean-pool two embedding vectors into one.

        Args:
            emb1: First embedding vector
            emb2: Second embedding vector

        Returns:
            Mean-pooled embedding vector
        """
        if len(emb1) != len(emb2):
            raise EmbeddingError(
                f"Cannot mean-pool embeddings of different dimensions: {len(emb1)} vs {len(emb2)}"
            )

        return [(a + b) / 2.0 for a, b in zip(emb1, emb2)]

    async def _handle_overflow_retry(
        self,
        client: httpx.AsyncClient,
        texts: List[str],
        max_retries: int,
        min_sub_size: int,
        retry_count: int,
    ) -> List[List[float]]:
        """
        Helper method to handle overflow retry logic with bounded retries and minimum split size.

        For single-item overflow, attempts to split the text and mean-pool the results.
        For multi-item overflow, splits the batch and processes each half.

        Args:
            client: HTTP client for making requests
            texts: List of texts to embed
            max_retries: Maximum number of retry attempts
            min_sub_size: Minimum sub-batch size before giving up
            retry_count: Current retry attempt count

        Returns:
            List of embedding vectors

        Raises:
            EmbeddingError: If bounded retries exhausted or split size too small
        """
        # Check if we've exhausted retries
        if retry_count > max_retries:
            logger.error(
                f"Max retries ({max_retries}) exhausted for embedding batch in {self.provider_mode} mode"
            )
            raise EmbeddingError(
                f"Max retries ({max_retries}) exhausted for embedding batch"
            )

        # Handle single-item overflow with text splitting
        if len(texts) == 1:
            single_text = texts[0]

            # Check if text is too short to split - raise actionable error
            if len(single_text) < self.MIN_SPLIT_CHARS:
                logger.warning(
                    f"Single input ({len(single_text)} chars) is below minimum split threshold ({self.MIN_SPLIT_CHARS}) in {self.provider_mode} mode"
                )
                raise EmbeddingError(
                    f"Single input ({len(single_text)} chars) exceeds token limit and is too short to split. "
                    f"Ensure chunk_size_chars is below server batch size limit (minimum {self.MIN_SPLIT_CHARS} chars required for recovery)."
                )

            # Split text at boundary-aware midpoint and recurse
            left_text, right_text = self._split_text_at_midpoint(single_text)

            # Guard: if either side is empty after split, raise actionable error
            if not left_text or not right_text:
                logger.error(
                    f"Text split produced empty side (left={len(left_text)}, right={len(right_text)}) for text of length {len(single_text)}"
                )
                raise EmbeddingError(
                    "Cannot split text for embedding recovery: split produced empty segment. "
                    "Ensure chunk_size_chars is within server limits."
                )

            logger.info(
                f"Splitting single input ({len(single_text)} chars) into parts ({len(left_text)} + {len(right_text)} chars), retry {retry_count}"
            )
            logger.info("Embedding batch size adapted: attempt %d", retry_count + 1)

            # Small bounded async backoff
            backoff_delay = min(0.5 * (2**retry_count), 1.0)
            await asyncio.sleep(backoff_delay)

            # Recurse on each part with incremented retry count
            left_embeddings = await self._embed_batch_with_retry(
                client,
                [left_text],
                max_retries,
                min_sub_size,
                retry_count=retry_count + 1,
            )
            right_embeddings = await self._embed_batch_with_retry(
                client,
                [right_text],
                max_retries,
                min_sub_size,
                retry_count=retry_count + 1,
            )

            # Mean-pool the two embeddings into one
            logger.debug("Using mean-pooling for overflow recovery")
            pooled = self._mean_pool_embeddings(left_embeddings[0], right_embeddings[0])

            # Return single embedding to preserve one-embedding-per-input contract
            return [pooled]

        # Multi-item batch overflow - use existing split behavior
        # Check if we've reached minimum split size
        if len(texts) <= min_sub_size:
            logger.warning(
                f"Cannot split batch further in {self.provider_mode} mode: {len(texts)} items below minimum split size ({min_sub_size})"
            )
            raise EmbeddingError(
                f"Cannot split batch further: {len(texts)} items below minimum split size"
            )

        # Split at midpoint and recurse with backoff
        midpoint = len(texts) // 2
        left_texts = texts[:midpoint]
        right_texts = texts[midpoint:]

        # Small bounded async backoff (exponential, capped at 1s)
        backoff_delay = min(0.5 * (2**retry_count), 1.0)
        await asyncio.sleep(backoff_delay)

        # Process left and right sub-batches concurrently, then concatenate in order
        left_task = self._embed_batch_with_retry(
            client, left_texts, max_retries, min_sub_size, retry_count=retry_count + 1
        )
        right_task = self._embed_batch_with_retry(
            client, right_texts, max_retries, min_sub_size, retry_count=retry_count + 1
        )

        try:
            left_result, right_result = await asyncio.gather(left_task, right_task)
            return left_result + right_result  # left+right concatenation preserves order
        except Exception:
            # Fallback: sequential if gather fails
            logger.warning("Parallel overflow retry failed, falling back to sequential")
            left_embeddings = await self._embed_batch_with_retry(
                client, left_texts, max_retries, min_sub_size, retry_count=retry_count + 1
            )
            right_embeddings = await self._embed_batch_with_retry(
                client, right_texts, max_retries, min_sub_size, retry_count=retry_count + 1
            )
            return left_embeddings + right_embeddings

    def _is_token_overflow_error(self, error_msg: str) -> bool:
        """
        Detect if an error message indicates a token overflow from llama.cpp.

        Args:
            error_msg: The error message string

        Returns:
            True if this is a token overflow error, False otherwise
        """
        error_lower = error_msg.lower()

        # Check for common llama.cpp token overflow patterns
        # Pattern 1: "input (X tokens) is too large" - typical llama.cpp error
        if "input (" in error_lower and "tokens) is too large" in error_lower:
            return True

        # Pattern 2: "too large to process" with "current batch size" - OpenAI mode error
        if (
            "too large to process" in error_lower
            and "current batch size" in error_lower
        ):
            return True

        # Pattern 3: "token limit exceeded"
        if "token limit exceeded" in error_lower:
            return True

        # Pattern 4: "batch size too small"
        if "batch size too small" in error_lower:
            return True

        return False

    async def close(self) -> None:
        """Close the persistent HTTP client and release connection pool resources.

        Idempotent — safe to call multiple times or if __init__ failed before
        client creation.
        """
        client = getattr(self, "_client", None)
        if client is not None and not client.is_closed:
            await client.aclose()
