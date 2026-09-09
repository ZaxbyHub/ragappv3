"""
Dual-provider embedding client service supporting Ollama and OpenAI-compatible APIs.
"""

import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

import httpx

try:
    import redis
except ImportError:  # pragma: no cover
    redis = None  # type: ignore[assignment]

from app.config import settings
from app.services import embedding_cache
from app.services.circuit_breaker import CircuitBreakerError, embeddings_cb
from app.services.redis_io import redis_call
from app.services.ssrf import assert_url_safe
from app.utils.secrets import redact_url

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


@dataclass(frozen=True)
class _EmbeddingRequestConfig:
    """Frozen per-request embedding configuration (EMBED-002, issue #511).

    Captured once at the start of an embed call (single or batch) so a
    concurrent settings change (Settings UI write) cannot mix provider
    dialects within one in-flight request: payload building, the request
    URL, response parsing, cache-key fingerprints, and last_metrics all
    read this snapshot instead of live settings.
    """

    url: str
    mode: str  # "ollama" | "openai" | "tei"
    ollama_style: Optional[str]  # "modern" | "legacy" | None (non-ollama)
    model: str
    doc_prefix: str
    query_prefix: str


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
        # follow_redirects=False so a 30x from the embedding host cannot bypass
        # the SSRF guard by redirecting to a private/internal address.
        # SSRFSafeTransport re-validates the resolved IP at request time to close
        # the DNS-rebinding TOCTOU gap the startup-only guard leaves open.
        # httpx ignores the `limits=` kwarg on AsyncClient whenever a custom
        # `transport=` is supplied, so `limits` must be forwarded into the
        # wrapped transport explicitly or connection-pool sizing silently
        # reverts to httpx's defaults.
        from app.services.ssrf_transport import SSRFSafeTransport

        self._client = httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=False,
            transport=SSRFSafeTransport(
                transport=httpx.AsyncHTTPTransport(
                    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10)
                )
            ),
        )

        # LRU cache. Cache keys include the live model/url/prefix fingerprints,
        # so a settings change naturally invalidates cached entries.
        self._embed_cache = LRUCache(maxsize=1000)

        # Last embedding-API call metrics (analogous to LLMClient.last_metrics).
        # Populated on every provider-path call (success or failure); None until
        # the first call. Exposed for operators/diagnostics (E3-6).
        self.last_metrics: Optional[Dict[str, Any]] = None

        # Redis L2 cache — shared across processes/workers.
        # Graceful fallback: if Redis is unavailable, embeddings still work via L1 LRU + provider.
        self._redis_client = None
        self._redis_available = False
        self._redis_hits = 0
        self._redis_misses = 0
        if settings.redis_url and redis is not None:
            try:
                self._redis_client = redis.from_url(settings.redis_url)
                # Verify connectivity with a quiet ping
                self._redis_client.ping()
                self._redis_available = True
                logger.info(
                    "Embedding Redis L2 cache connected: %s",
                    redact_url(settings.redis_url),
                )
            except Exception as e:
                logger.warning("Embedding Redis L2 cache unavailable (will use LRU fallback): %s", e)
                self._redis_client = None
                self._redis_available = False

        # TTL for Redis cache entries (default 7 days)
        self._cache_ttl = getattr(settings, 'embedding_cache_ttl_seconds', 604800)

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

    @staticmethod
    def _ollama_endpoint_style(url: str) -> Optional[str]:
        """Classify an Ollama-mode endpoint's request dialect.

        Modern Ollama exposes ``POST /api/embed`` accepting
        ``{"model", "input"}`` bodies (scalar or list); legacy Ollama exposes
        ``POST /api/embeddings`` accepting only per-item
        ``{"model", "prompt"}`` bodies. Bare/default Ollama URLs resolve to
        the legacy dialect. Returns ``None`` for URLs that are not
        Ollama-mode endpoints (OpenAI/TEI paths).

        Args:
            url: The resolved embeddings endpoint URL.

        Returns:
            "modern", "legacy", or None for non-Ollama endpoints.
        """
        path = urlparse(url).path
        if "/api/embeddings" in path:
            return "legacy"
        if path.rstrip("/").endswith("/api/embed"):
            return "modern"
        if "/v1/embeddings" in path or path.rstrip("/").endswith("/embed"):
            # Explicit OpenAI / native TEI paths are not Ollama endpoints.
            return None
        # No explicit path (bare host) — the resolver appends the legacy
        # /api/embeddings route for those, so treat them as legacy dialect.
        return "legacy"

    def _request_config(self) -> _EmbeddingRequestConfig:
        """Capture the live embedding configuration as a frozen snapshot.

        Reads every request-relevant setting exactly once (URL, provider
        mode, Ollama dialect, model, prefixes) so the caller completes under
        the configuration that issued the request even when an admin flips
        settings mid-flight (EMBED-002, issue #511).
        """
        mode, url = self._resolved_url_and_mode()
        return _EmbeddingRequestConfig(
            url=url,
            mode=mode,
            ollama_style=self._ollama_endpoint_style(url) if mode == "ollama" else None,
            model=self.embedding_model,
            doc_prefix=self.embedding_doc_prefix,
            query_prefix=self.embedding_query_prefix,
        )

    def _detect_provider_mode(self, base_url: str) -> tuple:
        """
        Detect which embedding provider mode to use based on URL path.

        Detection strategy:
        - If URL path includes '/api/embeddings' -> legacy Ollama mode
        - If URL path ends with '/api/embed' -> modern Ollama mode
        - If URL path includes '/v1/embeddings' -> OpenAI mode
        - If URL path ends with '/embed' -> native TEI mode
        - If no explicit embeddings path:
          - Port 1234 -> OpenAI mode (LM Studio default)
          - Port 8080 -> native TEI mode (Text Embeddings Inference default)
          - Otherwise -> Ollama mode (legacy dialect)

        Native TEI servers (HuggingFace Text Embeddings Inference) always expose
        the route ``POST /embed`` with an ``{"inputs": ...}`` payload, but only
        some deployments additionally expose the OpenAI-compatible
        ``/v1/embeddings`` route. A bare ``host:8080`` URL is therefore resolved
        to the native ``/embed`` route so it works against any TEI build, while
        an explicit ``/v1/embeddings`` path still selects OpenAI mode.

        Both Ollama generations keep the same "ollama" provider mode; the
        modern-vs-legacy endpoint style is derived separately via
        :meth:`_ollama_endpoint_style` because their request/response dialects
        differ (EMBED-001, issue #511): modern ``/api/embed`` accepts
        ``{"model", "input"}`` bodies, legacy ``/api/embeddings`` only
        per-item ``{"model", "prompt"}`` bodies.

        Args:
            base_url: The configured embedding URL

        Returns:
            Tuple of (provider_mode, embeddings_url)
        """
        parsed = urlparse(base_url)
        path = parsed.path

        # Check for explicit paths
        if "/api/embeddings" in path:
            # Already has legacy Ollama path, use as-is
            return ("ollama", base_url)
        elif path.rstrip("/").endswith("/api/embed"):
            # Modern Ollama /api/embed route, use as-is
            return ("ollama", base_url)
        elif "/v1/embeddings" in path:
            # Already has OpenAI path, use as-is
            return ("openai", base_url)
        elif path.rstrip("/").endswith("/embed"):
            # Already has native TEI path, use as-is
            return ("tei", base_url)

        # No explicit path - determine by port
        port = parsed.port
        if port == 1234:
            # LM Studio default port - use OpenAI mode
            base_url = base_url.rstrip("/") + "/v1/embeddings"
            return ("openai", base_url)
        elif port == 8080:
            # TEI default port - use native TEI mode (POST /embed)
            base_url = base_url.rstrip("/") + "/embed"
            return ("tei", base_url)
        else:
            # Default to Ollama mode (legacy dialect)
            base_url = base_url.rstrip("/") + "/api/embeddings"
            return ("ollama", base_url)

    def _build_payload(self, text: str, config: Optional[_EmbeddingRequestConfig] = None) -> dict:
        """
        Build the API request payload based on provider mode.

        Args:
            text: The text to embed
            config: Frozen request configuration. When omitted, a snapshot is
                captured from the live settings (single-request callers).

        Returns:
            Dictionary payload for the API request
        """
        if config is None:
            config = self._request_config()
        if config.mode == "openai":
            return {"model": config.model, "input": text}
        elif config.mode == "tei":
            # Native TEI serves a single model, so no model field is sent.
            return {"inputs": text}
        elif config.ollama_style == "modern":
            # Modern /api/embed speaks the "input" dialect.
            return {"model": config.model, "input": text}
        else:  # legacy ollama mode
            return {"model": config.model, "prompt": text}

    def _extract_embedding(
        self, data, config: Optional[_EmbeddingRequestConfig] = None
    ) -> List[float]:
        """
        Extract embedding vector from API response based on provider mode.

        Args:
            data: Parsed JSON response from the API. A mapping for OpenAI/Ollama
                modes, or a list-of-lists for native TEI mode.
            config: Frozen request configuration. When omitted, a snapshot is
                captured from the live settings (single-request callers).

        Returns:
            List of float values representing the embedding vector

        Raises:
            EmbeddingError: If embedding cannot be extracted
        """
        if config is None:
            config = self._request_config()
        if config.mode == "openai":
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
        elif config.mode == "tei":
            # Native TEI returns a raw JSON array of embedding arrays, e.g.
            # [[...]]. Some TEI-compatible servers wrap it as
            # {"embeddings": [[...]]}; accept both shapes.
            rows = data.get("embeddings") if isinstance(data, dict) else data
            if not isinstance(rows, list) or len(rows) == 0:
                logger.error(
                    "Embedding API response in TEI mode is not a non-empty list "
                    "or {'embeddings': [...]} object, got %s",
                    type(data).__name__,
                )
                raise EmbeddingError("Embedding API response is invalid")
            embedding = rows[0]
            if not isinstance(embedding, list):
                logger.error(
                    "Embedding API response first element is not a list in TEI mode"
                )
                raise EmbeddingError("Embedding API response is invalid")
        elif config.ollama_style == "modern":
            # Modern /api/embed returns {"embeddings": [[...]]} for scalar
            # inputs; accept the legacy {"embedding": [...]} shape too.
            rows = data.get("embeddings")
            if isinstance(rows, list) and rows and isinstance(rows[0], list):
                embedding = rows[0]
            else:
                embedding = data.get("embedding")
                if embedding is None:
                    logger.error(
                        "Embedding API response missing 'embedding' field in Ollama mode"
                    )
                    raise EmbeddingError("Embedding API response is invalid")
        else:  # legacy ollama mode
            # Ollama format: embedding
            embedding = data.get("embedding")
            if embedding is None:
                logger.error(
                    "Embedding API response missing 'embedding' field in Ollama mode"
                )
                raise EmbeddingError("Embedding API response is invalid")

        return embedding

    async def _embed_with_prefix(
        self,
        text: str,
        prefix: str,
        config: Optional[_EmbeddingRequestConfig] = None,
    ) -> List[float]:
        """
        Shared embedding logic with prefix application.

        This is a private helper method that implements the common logic for
        both embed_single (query embeddings) and embed_passage (document embeddings).
        It validates input, applies the provided prefix, checks cache, calls the
        embedding API, extracts the embedding, and stores it in cache.

        The provider configuration is frozen into `config` at the start of the
        call (EMBED-002): nothing after the first settings read re-reads live
        settings, so the payload, request URL, response parsing, cache key, and
        metrics all describe the configuration that issued the request.

        Args:
            text: The text to embed (plain text, without prefix applied).
            prefix: The prefix to prepend to the text (query or document prefix).
            config: Frozen request configuration; captured from live settings
                when omitted.

        Returns:
            List of float values representing the embedding vector.

        Raises:
            EmbeddingError: If the API request fails or returns non-200 status.
        """
        # EMBED-002: freeze the provider configuration for this request.
        if config is None:
            config = self._request_config()

        # Validate text input
        if text is None:
            raise EmbeddingError("Text cannot be None")
        if not text.strip():
            raise EmbeddingError("Text cannot be empty or whitespace only")

        # Apply prefix to text
        text_to_embed = prefix + text if prefix else text

        # Build cache key with model + url + prefix fingerprints (from the
        # frozen snapshot so the key describes the issuing configuration).
        model_fingerprint = hashlib.md5(config.model.encode("utf-8")).hexdigest()[:8]
        url_fingerprint = hashlib.md5(config.url.encode("utf-8")).hexdigest()[:8]
        prefix_fingerprint = hashlib.md5((prefix or "").encode("utf-8")).hexdigest()[:8]
        cache_key = f"{model_fingerprint}_{url_fingerprint}_{prefix_fingerprint}_{hashlib.md5(text_to_embed.encode('utf-8')).hexdigest()}"

        # L1: check in-process LRU first (fastest path)
        cached = self._embed_cache.get(cache_key)
        if cached is not None:
            return cached

        # L2: check Redis shared cache. redis_call runs the sync client call in
        # a worker thread under a bounded timeout so a slow Redis degrades to
        # a cache miss instead of stalling the event loop (FULL-ENH-04).
        redis_key = f"emb:{cache_key}"
        if self._redis_client is not None:
            try:
                raw = await redis_call(self._redis_client.get, redis_key)
                if raw is not None:
                    embedding = json.loads(raw)
                    self._redis_hits += 1
                    # Backfill L1 so hot path stays fast
                    self._embed_cache.set(cache_key, embedding)
                    return embedding
            except Exception as e:
                logger.warning("Redis L2 cache get failed (will fall through to provider): %s", e)

        # Provider path (cache miss on L1 and L2)
        self._redis_misses += 1

        _embed_started = time.perf_counter()
        try:
            response = await embeddings_cb(self._client.post)(
                config.url, json=self._build_payload(text_to_embed, config)
            )

            if response.status_code != 200:
                logger.warning(
                    f"Embedding API returned status {response.status_code} for {config.mode} mode: {response.text}"
                )
                raise EmbeddingError(
                    f"Embedding API returned status {response.status_code}"
                )

            try:
                data = response.json()
            except ValueError as e:
                logger.warning(
                    f"Invalid JSON response from embedding API for {config.mode} mode: {e}, response: {response.text}"
                )
                raise EmbeddingError("Invalid response from embedding service")

            embedding = self._extract_embedding(data, config)

            self.last_metrics = {
                "provider_url": config.url,
                "mode": config.mode,
                "latency_ms": round((time.perf_counter() - _embed_started) * 1000, 2),
                "status": "ok",
            }

            # Store in both L1 (in-process LRU) and L2 (Redis shared cache)
            self._embed_cache.set(cache_key, embedding)

            # L2: write to Redis (fire-and-forget; Redis failure must not raise)
            if self._redis_client is not None:
                try:
                    redis_key = f"emb:{cache_key}"
                    await redis_call(
                        self._redis_client.setex,
                        redis_key,
                        self._cache_ttl,
                        json.dumps(embedding),
                    )
                except Exception as e:
                    logger.warning("Redis L2 cache set failed (entry still in L1): %s", e)

            return embedding

        except CircuitBreakerError as e:
            logger.warning(
                "Embedding request failed (mode=%s): circuit breaker open: %s",
                config.mode,
                e,
            )
            self.last_metrics = {
                "provider_url": config.url,
                "mode": config.mode,
                "latency_ms": round((time.perf_counter() - _embed_started) * 1000, 2),
                "status": "circuit_open",
            }
            raise EmbeddingError(f"Embedding service circuit breaker is open: {e}") from e
        except httpx.TimeoutException as e:
            logger.warning(
                "Embedding request timed out (mode=%s): %s", config.mode, e
            )
            self.last_metrics = {
                "provider_url": config.url,
                "mode": config.mode,
                "latency_ms": round((time.perf_counter() - _embed_started) * 1000, 2),
                "status": "timeout",
            }
            raise EmbeddingError("Embedding request timed out") from e
        except httpx.HTTPError as e:
            logger.warning(
                "Embedding HTTP error (mode=%s): %s", config.mode, e
            )
            self.last_metrics = {
                "provider_url": config.url,
                "mode": config.mode,
                "latency_ms": round((time.perf_counter() - _embed_started) * 1000, 2),
                "status": "http_error",
            }
            raise EmbeddingError("Embedding HTTP error occurred") from e

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
        Batches are bounded BOTH by item count (``batch_size``) and by a total
        character budget (``settings.embedding_batch_max_chars``): a batch is
        closed before adding a text that would push its total character cost
        past the budget, and an oversized single text ships alone as its own
        batch (issue #513 W23). Processes batches concurrently using
        asyncio.gather, limited by embedding_concurrent_batches setting.

        Applies the document prefix (if configured) to each input text before embedding.
        The document prefix is used for document embeddings and must remain constant for
        a given index to ensure consistent embedding space.

        Args:
            texts: List of texts to embed.
            batch_size: Maximum number of texts per API request (default: 512).
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

        # EMBED-002: freeze the provider configuration once for the whole
        # batch call so every per-batch request, payload, and parse runs under
        # the configuration that issued the call.
        config = self._request_config()

        # Input validation guards
        prefix_len = len(config.doc_prefix) if config.doc_prefix else 0
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
            if config.doc_prefix:
                texts_to_embed.append(config.doc_prefix + text)
            else:
                texts_to_embed.append(text)

        # Process batches concurrently using both per-call and process-wide caps.
        all_embeddings: List[List[float]] = []
        sem = asyncio.Semaphore(settings.embedding_concurrent_batches)

        async def _process_batch(batch_texts: List[str]) -> List[List[float]]:
            if config.mode == "ollama" and config.ollama_style == "legacy":
                # Legacy fan-out: the per-call semaphore bounds the fan-out as
                # one unit; each per-item request acquires the process-wide
                # batch admission semaphore inside _embed_legacy_batch. The
                # parent must NOT hold a global slot here — the items take
                # their own, and asyncio.Semaphore is not reentrant.
                async with sem:
                    return await self._embed_batch_api(batch_texts, config)
            async with sem:
                async with self._get_global_batch_semaphore():
                    return await self._embed_batch_api(batch_texts, config)

        # Token-cost-bounded batching (issue #513 W23 / RC-21e): in addition
        # to the per-batch count limit, a batch is closed BEFORE adding a
        # text whose character cost (len of text) would push the batch's
        # total past ``settings.embedding_batch_max_chars``. An oversized
        # single text (cost alone over the budget) ships alone as its own
        # batch. Text order and exactly-one-embedding-per-text are preserved
        # by construction: the batches partition ``texts_to_embed`` in order.
        max_batch_chars = settings.embedding_batch_max_chars
        batches: List[tuple] = []  # (start offset into texts_to_embed, batch texts)
        current_batch: List[str] = []
        current_chars = 0
        for offset, text in enumerate(texts_to_embed):
            text_cost = len(text)
            if current_batch and (
                len(current_batch) >= batch_size
                or current_chars + text_cost > max_batch_chars
            ):
                batches.append((offset - len(current_batch), current_batch))
                current_batch = []
                current_chars = 0
            current_batch.append(text)
            current_chars += text_cost
        if current_batch:
            batches.append((len(texts_to_embed) - len(current_batch), current_batch))

        batch_tasks = [_process_batch(batch) for _start, batch in batches]

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
                    # (placeholders track the ACTUAL char-bounded batch size —
                    # batches are no longer uniform batch_size slices).
                    for _ in batches[batch_idx][1]:
                        all_embeddings_with_nones.append(None)
                    logger.warning(f"Batch {batch_idx} failed (skipping): {result}")
                else:
                    all_embeddings_with_nones.extend(result)
            return all_embeddings_with_nones, failed_indices

    async def _embed_batch_api(
        self, texts: List[str], config: Optional[_EmbeddingRequestConfig] = None
    ) -> List[List[float]]:
        """
        Send a batch of texts to the embedding API in a single request.

        Implements adaptive batching: automatically retries with smaller sub-batches
        when llama.cpp token overflow errors occur.

        On the legacy Ollama dialect there is no native batch route: the texts
        are fanned out as one ``{"model", "prompt"}`` request per item (see
        :meth:`_embed_legacy_batch`), preserving input order and per-item
        retry semantics (EMBED-001, issue #511).

        Args:
            texts: List of texts to embed (already prefixed).
            config: Frozen request configuration; captured from live settings
                when omitted (single-request callers).

        Returns:
            List of embedding vectors in the same order as input texts.

        Raises:
            EmbeddingError: If the API request fails after all retries.
        """
        max_retries = settings.embedding_batch_max_retries
        min_sub_size = settings.embedding_batch_min_sub_size
        if config is None:
            config = self._request_config()

        if config.mode == "ollama" and config.ollama_style == "legacy":
            return await self._embed_legacy_batch(texts, config, max_retries, min_sub_size)

        return await self._embed_batch_with_retry(
            self._client, texts, max_retries, min_sub_size, config=config
        )

    async def _embed_legacy_batch(
        self,
        texts: List[str],
        config: _EmbeddingRequestConfig,
        max_retries: int,
        min_sub_size: int,
    ) -> List[List[float]]:
        """
        Fan a batch out over the legacy Ollama ``/api/embeddings`` route.

        The legacy endpoint accepts exactly one ``{"model", "prompt"}`` body
        per request, so one request is issued per text and gathered
        concurrently; results are concatenated in input order. Each item is a
        single-item ``_embed_batch_with_retry`` call, so it keeps the existing
        single-item timeout/backoff (and token-overflow split) retry
        semantics. The per-call semaphore bounds the fan-out as one unit and
        each item request acquires the process-wide batch admission semaphore
        on its own (it is an HTTP request).

        A failed item raises, which embed_batch's existing batch-failure
        handling turns into fail_fast=true errors or recorded batch indices —
        identical to today's batch semantics.

        Args:
            texts: List of texts to embed (already prefixed).
            config: Frozen request configuration for the whole fan-out.
            max_retries: Maximum retry attempts per item.
            min_sub_size: Minimum sub-batch size for overflow splits.

        Returns:
            List of embedding vectors in the same order as input texts.

        Raises:
            EmbeddingError: If any item fails after all retries.
        """
        if not texts:
            return []

        async def _embed_one(text: str) -> List[float]:
            async with self._get_global_batch_semaphore():
                single = await self._embed_batch_with_retry(
                    self._client, [text], max_retries, min_sub_size, config=config
                )
                return single[0]

        gathered = await asyncio.gather(*(_embed_one(text) for text in texts))
        return list(gathered)

    def _log_pool_stats(self, client: httpx.AsyncClient) -> None:
        """Log connection pool and cache statistics for monitoring."""
        try:
            pool = getattr(client, "_transport", None)
            # SSRFSafeTransport wraps the real httpx.AsyncHTTPTransport as
            # `_transport`, not `_pool` — unwrap it before looking for `_pool`.
            if pool is not None and not hasattr(pool, "_pool"):
                pool = getattr(pool, "_transport", None)
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
            Dictionary with cache statistics including L1 LRU hits/misses/size
            and L2 Redis availability/hits/misses.
        """
        l1_stats = self._embed_cache.get_stats()
        return {
            "l1": l1_stats,
            "l2": {
                "available": self._redis_available,
                "hits": self._redis_hits,
                "misses": self._redis_misses,
            },
        }

    async def _embed_batch_with_retry(
        self,
        client: httpx.AsyncClient,
        texts: List[str],
        max_retries: int,
        min_sub_size: int,
        retry_count: int = 0,
        config: Optional[_EmbeddingRequestConfig] = None,
    ) -> List[List[float]]:
        """
        Internal method that handles the retry logic for adaptive batching.

        Args:
            client: HTTP client for making requests
            texts: List of texts to embed
            max_retries: Maximum number of retry attempts
            min_sub_size: Minimum sub-batch size before giving up
            retry_count: Current retry attempt count
            config: Frozen request configuration; captured from live settings
                when omitted (single-request callers). Every retry reuses the
                same snapshot the original call was issued under (EMBED-002).

        Returns:
            List of embedding vectors

        Raises:
            EmbeddingError: If all retries fail
        """
        # Empty-input guard
        if not texts:
            return []

        if config is None:
            config = self._request_config()

        try:
            # Build payload with array of inputs
            if config.mode == "openai":
                payload = {"model": config.model, "input": texts}
            elif config.mode == "tei":
                # Native TEI accepts a list under "inputs" and returns a raw
                # array of embedding arrays in the same order.
                payload = {"inputs": texts}
            elif config.ollama_style == "modern":
                # Modern /api/embed accepts a list under "input" and returns
                # {"embeddings": [[...]]} in the same order.
                payload = {"model": config.model, "input": texts}
            else:
                # Legacy ollama mode: single-prompt bodies only. The per-item
                # fan-out in _embed_legacy_batch guarantees exactly one text
                # here (the overflow/timeout retry helpers preserve that
                # invariant when splitting single items).
                if len(texts) != 1:
                    raise EmbeddingError(
                        "Legacy Ollama /api/embeddings accepts one prompt per request"
                    )
                payload = {"model": config.model, "prompt": texts[0]}

            try:
                response = await embeddings_cb(client.post)(
                    config.url, json=payload
                )
            except CircuitBreakerError as e:
                raise EmbeddingError(f"Embedding service circuit breaker is open: {e}")

            # Check for token overflow error in HTTP 500 responses
            if response.status_code == 500:
                error_text = response.text.lower()
                if self._is_token_overflow_error(error_text):
                    logger.warning(
                        f"Token overflow error for {config.mode} mode: {response.text}"
                    )
                    # Handle overflow using the shared helper
                    return await self._handle_overflow_retry(
                        client, texts, max_retries, min_sub_size, retry_count, config
                    )

            if response.status_code != 200:
                logger.warning(
                    f"Embedding API returned status {response.status_code} for {config.mode} mode: {response.text}"
                )
                raise EmbeddingError(
                    f"Embedding API returned status {response.status_code}"
                )

            data = response.json()

            # Extract embeddings from response
            if config.mode == "openai":
                # OpenAI format: data[].embedding
                embeddings = [item["embedding"] for item in data["data"]]
            elif config.mode == "tei":
                # Native TEI returns a raw JSON array of embedding arrays. Some
                # TEI-compatible servers wrap it as {"embeddings": [[...]]};
                # accept both shapes.
                embeddings = (
                    data.get("embeddings") if isinstance(data, dict) else data
                )
                if not isinstance(embeddings, list):
                    logger.error(
                        f"Unexpected response format for {config.mode} mode: "
                        f"expected a list or {{'embeddings': [...]}}, got "
                        f"{type(data).__name__}"
                    )
                    raise EmbeddingError("Unexpected response from embedding service")
            else:
                # Ollama format may vary - try common formats
                if "embeddings" in data:
                    embeddings = data["embeddings"]
                elif "embedding" in data:
                    # Single embedding returned - shouldn't happen with batch
                    embeddings = [data["embedding"]]
                else:
                    logger.error(
                        f"Unexpected response format for {config.mode} mode: {data.keys()}"
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
                    f"Embedding count mismatch for {config.mode} mode: expected {len(texts)}, got {len(embeddings)}"
                )
                raise EmbeddingError(
                    f"Embedding count mismatch: expected {len(texts)}, got {len(embeddings)}"
                )

            # Log connection pool metrics
            self._log_pool_stats(client)

            return embeddings

        except httpx.TimeoutException as e:
            logger.warning(
                f"Embedding batch request timed out for {config.mode} mode: {e}"
            )
            # For multi-item batches: split and retry each half so the server
            # gets smaller workloads — same recovery strategy as token overflow.
            if len(texts) > 1 and retry_count < max_retries:
                logger.info(
                    f"Timeout with {len(texts)} items — splitting batch and retrying "
                    f"(attempt {retry_count + 1}/{max_retries})"
                )
                return await self._handle_overflow_retry(
                    client, texts, max_retries, min_sub_size, retry_count, config
                )
            # For single-item batches: simple backoff retry
            if retry_count < max_retries:
                backoff_delay = min(0.5 * (2**retry_count), 2.0)
                logger.info(
                    f"Retrying single-item timeout (attempt {retry_count + 1}/{max_retries}) after {backoff_delay}s"
                )
                await asyncio.sleep(backoff_delay)
                return await self._embed_batch_with_retry(
                    client, texts, max_retries, min_sub_size, retry_count + 1, config
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
                    f"Token overflow error for {config.mode} mode: {response_text}"
                )
                # Handle overflow using the shared helper
                return await self._handle_overflow_retry(
                    client, texts, max_retries, min_sub_size, retry_count, config
                )
            else:
                # Not a token overflow error, re-raise
                logger.error(
                    f"Embedding batch HTTP error for {config.mode} mode: {e}"
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
        config: Optional[_EmbeddingRequestConfig] = None,
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
            config: Frozen request configuration; captured from live settings
                when omitted. Retries keep the configuration the original call
                was issued under (EMBED-002).

        Returns:
            List of embedding vectors

        Raises:
            EmbeddingError: If bounded retries exhausted or split size too small
        """
        if config is None:
            config = self._request_config()

        # Check if we've exhausted retries
        if retry_count > max_retries:
            logger.error(
                f"Max retries ({max_retries}) exhausted for embedding batch in {config.mode} mode"
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
                    f"Single input ({len(single_text)} chars) is below minimum split threshold ({self.MIN_SPLIT_CHARS}) in {config.mode} mode"
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
                config=config,
            )
            right_embeddings = await self._embed_batch_with_retry(
                client,
                [right_text],
                max_retries,
                min_sub_size,
                retry_count=retry_count + 1,
                config=config,
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
                f"Cannot split batch further in {config.mode} mode: {len(texts)} items below minimum split size ({min_sub_size})"
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
            client, left_texts, max_retries, min_sub_size, retry_count=retry_count + 1, config=config
        )
        right_task = self._embed_batch_with_retry(
            client, right_texts, max_retries, min_sub_size, retry_count=retry_count + 1, config=config
        )

        try:
            left_result, right_result = await asyncio.gather(left_task, right_task)
            return left_result + right_result  # left+right concatenation preserves order
        except Exception:
            # Fallback: sequential if gather fails
            logger.warning("Parallel overflow retry failed, falling back to sequential")
            left_embeddings = await self._embed_batch_with_retry(
                client, left_texts, max_retries, min_sub_size, retry_count=retry_count + 1, config=config
            )
            right_embeddings = await self._embed_batch_with_retry(
                client, right_texts, max_retries, min_sub_size, retry_count=retry_count + 1, config=config
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


async def embed_batch_cached(
    service: "EmbeddingService",
    texts: List[str],
    *,
    normalize: Optional[Callable[[str], str]] = None,
) -> tuple[List[Optional[List[float]]], List[int]]:
    """Embed ``texts`` through ``service.embed_batch`` with persistent-cache reuse.

    Cache-through facade over :meth:`EmbeddingService.embed_batch` with the
    same return shape as ``embed_batch(..., fail_fast=False)``:
    ``(embeddings, failed_indices)`` where ``embeddings`` holds exactly one
    entry per input text (``None`` for texts whose embedding failed) in input
    order. ``failed_indices`` lists the indices into ``texts`` of the failed
    texts (stable per-text identity — unlike raw ``embed_batch`` batch
    indices, these do not depend on how the misses happened to be batched).

    Cache keys bind the IMMUTABLE embedding contract via
    :func:`app.services.embedding_cache.embedding_cache_key`:
    model id (``settings.embedding_model``), a revision discriminator (the
    configured serving endpoint — no dedicated model-revision setting exists,
    and the endpoint is the remaining identity component, mirroring the L1
    cache's model+url fingerprint), doc prefix
    (``settings.embedding_doc_prefix``), dim (``settings.embedding_dim``),
    and the normalized text (``normalize(text)`` when ``normalize`` is
    provided, else the text as-is). Changing any component changes the key,
    invalidating old entries by construction.

    Only cache misses reach the provider (``service.embed_batch`` is called
    once, with just the missed texts); new vectors are stored after success.
    The cache is strictly best-effort: any cache failure (key computation,
    lookup, store) degrades to "no cache for this call" and NEVER fails the
    embedding path.

    Args:
        service: The live embedding service used for cache misses.
        texts: Texts to embed (same per-text validity rules as embed_batch).
        normalize: Optional key normalizer (e.g. whitespace collapse) so
            texts that are byte-identical after normalization share one
            cached vector. The provider call always receives the ORIGINAL
            text, never the normalized form.

    Returns:
        Tuple of (per-text embeddings with None placeholders, failed text
        indices).
    """
    if not texts:
        return [], []

    results: List[Optional[List[float]]] = [None] * len(texts)

    # Immutable contract snapshot. Guarded so a cache-side failure (e.g. an
    # exotic text that cannot be encoded into a key) can never fail embedding.
    keys: List[str] = []
    hits: dict = {}
    try:
        model_id = str(getattr(settings, "embedding_model", "") or "")
        model_revision = str(getattr(settings, "ollama_embedding_url", "") or "")
        doc_prefix = str(getattr(settings, "embedding_doc_prefix", "") or "")
        dim = int(getattr(settings, "embedding_dim", 0) or 0)
        normalized = [normalize(t) if normalize is not None else t for t in texts]
        keys = [
            embedding_cache.embedding_cache_key(
                model_id, model_revision, doc_prefix, dim, text
            )
            for text in normalized
        ]
        hits = embedding_cache.lookup(keys)
    except Exception:
        logger.warning(
            "embedding cache lookup failed; embedding without cache", exc_info=True
        )
        hits = {}

    miss_positions: List[int] = []
    for pos in range(len(texts)):
        key = keys[pos] if pos < len(keys) else None
        vector = hits.get(key) if key is not None else None
        if vector is not None:
            results[pos] = vector
        else:
            miss_positions.append(pos)

    if miss_positions:
        miss_texts = [texts[pos] for pos in miss_positions]
        miss_embeddings, _failed = await service.embed_batch(
            miss_texts, fail_fast=False
        )
        new_items: list = []
        for offset, pos in enumerate(miss_positions):
            vector = (
                miss_embeddings[offset] if offset < len(miss_embeddings) else None
            )
            if vector is not None:
                results[pos] = vector
                if pos < len(keys):
                    new_items.append((keys[pos], vector))
        if new_items:
            try:
                embedding_cache.store(new_items)
            except Exception:
                logger.warning(
                    "embedding cache store failed (non-fatal)", exc_info=True
                )

    failed = [pos for pos, vector in enumerate(results) if vector is None]
    return results, failed
