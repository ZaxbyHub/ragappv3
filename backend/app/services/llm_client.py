"""
OpenAI-compatible LLM chat client using httpx.
"""

import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Dict, List, Optional, Union

import httpx

from app.config import settings
from app.services.circuit_breaker import (
    CircuitBreakerError,
    CircuitBreakerState,
    create_llm_circuit_breaker,
    is_outage_status,
)
from app.services.ssrf import assert_url_safe
from app.services.telemetry import (
    correlation_headers as _correlation_headers,
)
from app.services.telemetry import start_span
from app.utils.assistant_sanitizer import sanitize_assistant_content

logger = logging.getLogger(__name__)

_MAX_THINKING_BUFFER = 1024 * 1024  # 1MB max thinking buffer

# Matches a complete <think> open tag case-insensitively, with optional attributes.
# Examples: <think>, <THINK>, <think type="reasoning">, <Think foo="bar">
_THINK_OPEN_RE = re.compile(r"<think(?:\s[^>]*)?>", re.IGNORECASE)
# Matches any case variant of </think>
_THINK_CLOSE_RE = re.compile(r"</think>", re.IGNORECASE)
# Matches any partial opening sequence — used to decide whether to hold the buffer.
# Covers: <, <t, <th, <thi, <thin, <think (any case)
_THINK_PARTIAL_OPEN_RE = re.compile(r"^<[tT]?[hH]?[iI]?[nN]?[kK]?$")
# Full "Thinking Process:" literal used for the anchored startswith check
# that distinguishes a real qwen3.5-122b prefix marker from a benign
# mid-content occurrence (e.g. quoted from a RAG document).
_THINKING_PROCESS_MARKER = "Thinking Process:"

# Legacy hardcoded generation budget; still the signature default of
# chat_completion/chat_completion_stream (a pinned contract test asserts
# the default equals 32768).
_DEFAULT_MAX_TOKENS = 32768


class _UnsetMaxTokens(int):
    """Sentinel for "the caller did not pass ``max_tokens``.

    Compares equal to the legacy default (32768) so the pinned signature
    contract (``inspect.signature(...).parameters["max_tokens"].default
    == 32768``) keeps holding, while ``isinstance`` still distinguishes an
    explicitly-passed 32768 from the default at runtime — an explicit value
    is never overridden by the client's configured per-mode budget
    (ENH-015, issue #494).
    """

    pass


_UNSET_MAX_TOKENS = _UnsetMaxTokens(_DEFAULT_MAX_TOKENS)


@dataclass(frozen=True)
class ReasoningDelta:
    """One provider reasoning chunk, surfaced on its own typed channel.

    ``chat_completion_stream`` yields these alongside (never instead of) the
    plain-``str`` answer-content chunks (issue #554). Consumers that only
    want the answer discriminate with ``isinstance(chunk, str)``; reasoning
    never enters the content pipeline or the think-tag filter.
    """

    text: str


def select_no_think_chat_template_kwargs(
    model: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Select the family-appropriate no-think template control (issue #554).

    Only model families with a *verified* chat-template mechanism get a
    control: Qwen-family names (matched case-insensitively as a substring,
    so ``qwen/qwen3.5``, ``Qwen3-Coder-30B`` and org-prefixed variants all
    match) document the
    ``chat_template_kwargs={'enable_thinking': False}`` no-think control on
    their model cards (Qwen3.5+ chat templates). Any other family —
    including empty/missing names — gets ``None``: the client logs a
    warning and sends no control, failing open rather than hard-coding a
    Qwen-specific kwarg for a model that may not honor it (the pre-#554
    behavior shipped it for every model, e.g. nemotron).
    """
    if model and "qwen" in model.lower():
        return {"enable_thinking": False}
    logger.warning(
        "No verified no-think control for instant model %r; sending none "
        "(fail open — provider default governs)",
        model,
    )
    return None


def _httpcore_live_pool_counts(
    client: httpx.AsyncClient,
) -> "tuple[Optional[int], Optional[int]]":
    """Best-effort live connection counts from the httpcore pool.

    Returns ``(total_connections, keepalive_connections)`` where the
    keepalive count is the number of connections in httpcore's IDLE state.
    Returns ``(None, None)`` when the pool state cannot be introspected —
    never fabricated zeros (OBS-001, issue #494). A defensive copy of this
    helper lives in ``embeddings.py``; keep the two in sync.
    """
    transport = getattr(client, "_transport", None)
    # SSRFSafeTransport wraps the real httpx.AsyncHTTPTransport as
    # `_transport`, not `_pool` — unwrap it before looking for `_pool`.
    if transport is not None and not hasattr(transport, "_pool"):
        transport = getattr(transport, "_transport", None)
    pool_obj = getattr(transport, "_pool", None)
    live = getattr(pool_obj, "_connections", None)
    if live is None:
        return (None, None)
    keepalive = 0
    for conn in live:
        state = getattr(conn, "state", None)
        if str(getattr(state, "name", state)).upper() == "IDLE":
            keepalive += 1
    return (len(live), keepalive)


class LLMError(Exception):
    """Exception raised for LLM client errors."""

    pass


# Sentinel for LLMClient.reconfigure: "leave chat_template_kwargs unchanged".
# None itself is a meaningful value there (omit the kwarg from the payload).
_KEEP_TEMPLATE_KWARGS = object()


class LLMClient:
    """OpenAI-compatible LLM chat client."""

    def __init__(
        self,
        timeout: float = 300.0,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        cb_name: str = "llm",
        chat_template_kwargs: Optional[Dict[str, Any]] = None,
        max_tokens: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
        keep_alive: Optional[str] = None,
        num_ctx: Optional[int] = None,
        ttl: Optional[int] = None,
        context_length: Optional[int] = None,
    ):
        """
        Initialize the LLM client.

        Args:
            timeout: Request timeout in seconds (default 300.0 for model loading)
            base_url: Override for the chat endpoint. Defaults to settings.ollama_chat_url.
            model: Override for the model name. Defaults to settings.chat_model.
            cb_name: Circuit breaker name (for logging / metrics distinction).
            chat_template_kwargs: Per-client chat-template controls (e.g.
                ``{"enable_thinking": False}`` for Qwen-family Instant models;
                selected via :func:`select_no_think_chat_template_kwargs`).
            max_tokens: Default generation budget for chat_completion /
                chat_completion_stream when the caller does not pass an
                explicit ``max_tokens`` (ENH-015, issue #494). ``None`` keeps
                the legacy default of 32768.
            reasoning_effort: Provider-native reasoning control for
                OpenAI-compatible ``/v1/chat/completions`` endpoints (issue
                #554). Ollama documents ``reasoning_effort`` (high/medium/low/
                max/none) on that endpoint; ``None`` sends no control field
                and the provider default governs (``think`` is native
                ``/api/chat``-only and is intentionally not used here).
            keep_alive: Ollama-native residency control (issue #571) sent on
                the :meth:`prime_residency` preload call — ``"-1"`` keeps the
                model loaded, durations like ``"30m"`` or seconds also work.
                ``None`` disables priming for this client.
            num_ctx: Ollama-native context window (issue #571) pinned at model
                load via :meth:`prime_residency`'s ``options.num_ctx``; the
                OpenAI-compatible surface cannot set it per request.
                ``None`` sends no sizing.
            ttl: LM Studio idle TTL in seconds (issue #571) carried on every
                chat payload; the idle timer resets on each request.
                ``None`` sends no TTL field.
            context_length: LM Studio context length (issue #571) requested
                from the native model-load endpoint by
                :meth:`prime_residency`; the OpenAI-compatible surface cannot
                set it per request. ``None`` sends no load call.
        """
        self.base_url = (base_url or settings.ollama_chat_url).rstrip("/")
        self.model = model or settings.chat_model
        self.timeout = timeout
        self.max_tokens = max_tokens
        # Provider residency/context contracts (issue #571). Factory-fixed
        # like reasoning_effort: a hot reconfigure never silently flips the
        # residency posture mid-operation; a settings change applies on
        # restart when the factories run again.
        self.keep_alive = keep_alive
        self.num_ctx = num_ctx
        self.ttl = ttl
        self.context_length = context_length
        # Per-client template controls (e.g. Qwen-family Instant no-thinking).
        # Copy so callers cannot mutate the live request policy after creation.
        self.chat_template_kwargs = dict(chat_template_kwargs or {})
        # Per-client provider-native reasoning control (issue #554). Not part
        # of reconfigure(): it is factory-fixed so a hot rebind of URL/model
        # never silently flips the thinking posture mid-operation.
        self.reasoning_effort = reasoning_effort
        assert_url_safe(base_url or settings.ollama_chat_url)
        self._circuit_breaker = create_llm_circuit_breaker(name=cb_name)
        self._client: Optional[httpx.AsyncClient] = None
        # Configured pool limits; start() replaces these with the live
        # settings values. Kept in sync at construction so _log_pool_stats
        # can report configured caps even for directly injected clients
        # (OBS-001, issue #494).
        self._pool_limits: httpx.Limits = httpx.Limits(
            max_connections=settings.llm_max_connections,
            max_keepalive_connections=settings.llm_max_keepalive_connections,
        )
        self.last_metrics: Dict[str, Any] = {}

    def _approx_tokens(self, text: str) -> int:
        return max(1, len(text) // 4) if text else 0

    def _prompt_token_estimate(self, messages: List[Dict[str, str]]) -> int:
        return sum(
            self._approx_tokens(str(message.get("content", ""))) for message in messages
        )

    def reconfigure(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        chat_template_kwargs: object = _KEEP_TEMPLATE_KWARGS,
    ) -> None:
        """Hot-update base_url, model, default max_tokens and/or template kwargs in place.

        ``chat_template_kwargs`` uses a sentinel default because ``None`` is a
        MEANINGFUL value here (omit the kwarg from the payload — instant-mode
        thinking enabled); pass ``_KEEP_TEMPLATE_KWARGS`` to leave it unchanged.

        Per-request URLs are computed from ``self.base_url`` at call time, so
        in-place updates take effect immediately without recreating the
        httpx pool or invalidating any stored references held by callers
        (LLMHealthChecker, background_processor, residency priming tasks, RAGEngine).

        Resets the circuit breaker on any change so a previously opened
        breaker from a now-unreachable endpoint does not block requests to
        the newly configured target.
        """
        changed = False
        if base_url is not None:
            new_base_url = base_url.rstrip("/")
            if new_base_url != self.base_url:
                # Re-validate against SSRF on hot-update: the SettingsUpdate
                # validator only checks URL format, not IP resolution, so an
                # admin pointing this at an internal address (e.g. cloud
                # metadata) must still be rejected here before it takes effect.
                assert_url_safe(new_base_url)
                self.base_url = new_base_url
                changed = True
        if model is not None and model != self.model:
            self.model = model
            changed = True
        if max_tokens is not None and max_tokens != self.max_tokens:
            # Review F5 (PR #576): a saved instant/thinking_max_tokens change
            # must reach the running client, not wait for a restart.
            self.max_tokens = max_tokens
            changed = True
        if chat_template_kwargs is not _KEEP_TEMPLATE_KWARGS:
            normalized = chat_template_kwargs or None
            if normalized != self.chat_template_kwargs:
                self.chat_template_kwargs = normalized
                changed = True
        if changed:
            self._circuit_breaker.reset()

    async def prime_residency(self) -> bool:
        """One-shot native model-load call pinning residency/context (issue #571).

        Replaces the retired 30-second ``max_tokens=1`` ping loop: Ollama
        clients (``keep_alive``/``num_ctx`` set) preload the model via the
        native ``/api/generate`` surface — an empty prompt loads the model
        without generating, ``keep_alive`` pins residency, and
        ``options.num_ctx`` sizes the loaded instance's context window (the
        OpenAI-compatible surface cannot express either). LM Studio clients
        (``context_length`` set) request the context length from the native
        model-load endpoint, trying the current v1 API first and the legacy
        v0 path on 404 for pre-0.4.0 servers; residency for LM Studio comes
        from the per-payload ``ttl`` every chat request already carries.

        Best-effort by contract: any transport/HTTP failure is logged and
        reported as ``False`` — residency priming must never break startup
        or a chat turn. Returns ``True`` when the native call succeeded.
        """
        if self.keep_alive is None and self.num_ctx is None and self.context_length is None:
            return False
        client = await self._ensure_started()
        try:
            if self.context_length is not None:
                # LM Studio native load (v1, with a v0 fallback for older
                # servers). `ttl` is intentionally not part of the load
                # request: it rides every chat payload instead.
                body = {"model": self.model, "context_length": self.context_length}
                url = f"{self.base_url}/api/v1/models/load"
                response = await client.post(url, json=body)
                if response.status_code == 404:
                    response = await client.post(
                        f"{self.base_url}/api/v0/models/load", json=body
                    )
                response.raise_for_status()
                logger.info(
                    "Model residency primed via LM Studio load API (%s, ctx=%d)",
                    self.base_url,
                    self.context_length,
                )
                return True
            # Ollama native preload: empty-prompt /api/generate loads the
            # model without generating (documented preload pattern).
            body: Dict[str, Any] = {"model": self.model}
            if self.keep_alive is not None:
                body["keep_alive"] = self.keep_alive
            if self.num_ctx is not None:
                body["options"] = {"num_ctx": self.num_ctx}
            response = await client.post(f"{self.base_url}/api/generate", json=body)
            response.raise_for_status()
            logger.info(
                "Model residency primed via Ollama preload (%s, keep_alive=%s, num_ctx=%s)",
                self.base_url,
                self.keep_alive,
                self.num_ctx,
            )
            return True
        except (httpx.HTTPError, LLMError) as e:
            logger.warning(
                "Residency priming failed for %s (model %s): %s — the model "
                "will load on demand with provider defaults instead",
                self.base_url,
                self.model,
                type(e).__name__ if isinstance(e, LLMError) else str(e),
            )
            return False

    async def start(self):
        """Start the HTTP client. Must be called before using the client."""
        # Configure limits for connection pooling with keep-alive
        limits = httpx.Limits(
            max_keepalive_connections=settings.llm_max_keepalive_connections,
            max_connections=settings.llm_max_connections,
            keepalive_expiry=300.0,  # Keep connections alive for 5 minutes
        )
        # Stored so _log_pool_stats reports the CONFIGURED caps instead of
        # fabricating counts from urllib3-era internals httpx never had
        # (OBS-001, issue #494).
        self._pool_limits = limits
        # Add keep-alive headers to prevent LM Studio from unloading
        headers = {"Connection": "keep-alive", "Keep-Alive": "timeout=300, max=1000"}
        # SSRFSafeTransport re-validates the resolved IP at request time to
        # close the DNS-rebinding TOCTOU gap the startup-only guard leaves
        # open, while preserving TLS SNI/cert validation. httpx ignores the
        # `limits=` kwarg on AsyncClient whenever a custom `transport=` is
        # supplied, so `limits` must be forwarded into the wrapped transport
        # explicitly or connection-pool sizing silently reverts to httpx's
        # defaults.
        from app.services.ssrf_transport import SSRFSafeTransport

        self._client = httpx.AsyncClient(
            timeout=self.timeout,
            headers=headers,
            follow_redirects=False,
            transport=SSRFSafeTransport(
                transport=httpx.AsyncHTTPTransport(limits=limits)
            ),
        )

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()

    async def _ensure_started(self) -> httpx.AsyncClient:
        """Ensure the client has been started and return it.

        Lazily starts the client when a caller skipped explicit start()
        (production always starts clients via lifespan; lazy start makes
        direct-construction usage — as in diagnostics and tests — work
        instead of raising, matching EmbeddingService's constructor-built
        client).
        """
        if self._client is None:
            await self.start()
        return self._client

    def _log_pool_stats(self) -> None:
        """Log connection pool statistics for monitoring.

        Reports the CONFIGURED limits from the ``httpx.Limits`` stored at
        client creation plus live counts read from httpcore's pool state
        when introspectable. The urllib3-era ``_num_connections`` /
        ``_num_keepalive`` attributes do not exist under httpx's asyncio
        transport, so the previous implementation fabricated zeros from
        getattr defaults; when the live state cannot be read, the log says
        so instead of inventing counts (OBS-001, issue #494).
        """
        try:
            client = self._client
            if client is None:
                return
            max_connections = self._pool_limits.max_connections
            max_keepalive = self._pool_limits.max_keepalive_connections
            connections, keepalive = _httpcore_live_pool_counts(client)
            if connections is None:
                logger.info(
                    f"LLM client pool: live counts unavailable "
                    f"(configured {max_connections} max connections, "
                    f"{max_keepalive} max keepalive)"
                )
            else:
                logger.info(
                    f"LLM client pool: {connections}/{max_connections} connections, "
                    f"{keepalive}/{max_keepalive} keepalive"
                )
        except Exception as e:
            logger.debug("Could not log pool stats: %s", e)

    def _strip_thinking_content(self, content: str) -> str:
        """Delegate to the centralized assistant sanitizer.

        Kept as an instance method for backwards compatibility with tests
        that patch or call it directly. All actual logic lives in
        :func:`app.utils.assistant_sanitizer.sanitize_assistant_content`,
        which handles ``<think>...</think>``, ``_lhs/_rhs``,
        ``Thinking Process:...</think>``, and unterminated thinking tails.
        """
        return sanitize_assistant_content(content)

    async def chat_completion(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = _UNSET_MAX_TOKENS,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Send a chat completion request and return the full response.

        Args:
            messages: List of message dicts with 'role' and 'content' keys
            temperature: Sampling temperature (default: 0.7)
            max_tokens: Maximum tokens to generate (default: 32768, or the
                client's configured per-mode budget when one was supplied
                at construction — ENH-015, issue #494)

        Returns:
            The generated content string

        Raises:
            LLMError: If the request fails or response is invalid
            RuntimeError: If the client has not been started
        """
        if isinstance(max_tokens, _UnsetMaxTokens):
            # An explicitly passed value always wins over the configured
            # per-mode default; see _UnsetMaxTokens for why the sentinel is
            # needed (the signature default must stay comparable to 32768).
            max_tokens = (
                self.max_tokens if self.max_tokens is not None else _DEFAULT_MAX_TOKENS
            )
        client = await self._ensure_started()
        url = f"{self.base_url}/v1/chat/completions"

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if self.chat_template_kwargs:
            payload["chat_template_kwargs"] = self.chat_template_kwargs
        if self.reasoning_effort is not None:
            payload["reasoning_effort"] = self.reasoning_effort
        if self.ttl is not None:
            # LM Studio idle TTL (issue #571): seconds until an idle JIT-loaded
            # model is unloaded; the timer resets on every request. LM Studio
            # documents this per-payload field on its OpenAI-compatible
            # surface, which is what makes native residency possible without
            # the retired 30-second ping loop.
            payload["ttl"] = self.ttl
        if response_format is not None:
            payload["response_format"] = response_format

        started_at = time.perf_counter()
        prompt_tokens = self._prompt_token_estimate(messages)
        try:
            # OPS-002 (issue #494): run the POST and the HTTP status check
            # as ONE breaker-wrapped operation so HTTPStatusError trips the
            # breaker (previously raise_for_status() ran outside the wrap,
            # so every error status recorded a SUCCESS). JSON decoding and
            # response validation stay outside — a malformed body is not an
            # outage signal.
            async def _checked_post() -> httpx.Response:
                # E3 telemetry (issue #518): propagate traceparent/X-Request-ID
                # when a correlation identity is bound; the kwarg is omitted
                # when empty so minimal fakes (test doubles with strict
                # signatures) keep working.
                _corr = _correlation_headers()
                if _corr:
                    response = await client.post(
                        url, json=payload, headers=_corr
                    )
                else:
                    response = await client.post(url, json=payload)
                # Only OUTAGE statuses charge the breaker inside the wrap
                # (review RP-002, PR #576): ordinary 4xx are input/config
                # errors that would recur on every retry — raising them
                # here would open the shared breaker off a misconfigured
                # request, not a provider outage.
                if is_outage_status(response.status_code):
                    response.raise_for_status()
                return response

            # E3 closure (#518): gen_ai client span for the non-stream
            # chat call — no-op without the optional OTel extra.
            with start_span(
                "gen_ai chat",
                attributes={
                    "gen_ai.operation.name": "chat",
                    "gen_ai.request.model": str(payload.get("model", "")),
                },
            ):
                response = await self._circuit_breaker(_checked_post)()
            response.raise_for_status()
            data = response.json()

            if "choices" not in data or not data["choices"]:
                raise LLMError("Invalid response: no choices in response")

            message = data["choices"][0].get("message", {})
            content = message.get("content", "")
            # Why the generation stopped — "length" means the answer was cut
            # off by the token limit (issue #511 FULL-ENH-01). None when the
            # provider omits the field.
            finish_reason = data["choices"][0].get("finish_reason")

            # Log connection pool metrics
            self._log_pool_stats()

            content = self._strip_thinking_content(content)
            # Issue #571: prefer provider-exact token counts when the
            # provider reports them (OpenAI-compatible `usage`; Ollama maps
            # prompt_eval_count/eval_count onto these fields). The char-based
            # estimates stay for compatibility and remain the only values
            # when the provider omits `usage`.
            usage = data.get("usage") or {}
            provider_prompt_tokens = usage.get("prompt_tokens")
            provider_completion_tokens = usage.get("completion_tokens")
            self.last_metrics = {
                "provider_url": self.base_url,
                "model": self.model,
                "latency_ms": round((time.perf_counter() - started_at) * 1000, 2),
                "prompt_tokens_estimate": prompt_tokens,
                "completion_tokens_estimate": self._approx_tokens(content),
                "finish_reason": finish_reason,
                "status": "ok",
            }
            if provider_prompt_tokens is not None:
                self.last_metrics["prompt_tokens"] = provider_prompt_tokens
            if provider_completion_tokens is not None:
                self.last_metrics["completion_tokens"] = provider_completion_tokens
            return content
        except CircuitBreakerError as e:
            self.last_metrics = {"provider_url": self.base_url, "model": self.model, "status": "circuit_open"}
            raise LLMError(
                f"LLM service is currently unavailable (circuit breaker open): {e}"
            ) from e
        except httpx.TimeoutException as e:
            self.last_metrics = {"provider_url": self.base_url, "model": self.model, "status": "timeout"}
            raise LLMError(f"Request timed out after {self.timeout}s") from e
        except httpx.HTTPStatusError as e:
            self.last_metrics = {
                "provider_url": self.base_url,
                "model": self.model,
                "status": "http_error",
                "status_code": e.response.status_code,
            }
            raise LLMError(
                f"HTTP error {e.response.status_code}: {e.response.text}"
            ) from e
        except httpx.RequestError as e:
            self.last_metrics = {"provider_url": self.base_url, "model": self.model, "status": "request_error"}
            raise LLMError(f"Request failed: {str(e)}") from e
        except json.JSONDecodeError as e:
            self.last_metrics = {"provider_url": self.base_url, "model": self.model, "status": "invalid_json"}
            raise LLMError(f"Failed to parse response JSON: {str(e)}") from e

    async def chat_completion_stream(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = _UNSET_MAX_TOKENS,
    ) -> AsyncGenerator["Union[str, ReasoningDelta]", None]:
        """
        Send a streaming chat completion request and yield content chunks.

        Args:
            messages: List of message dicts with 'role' and 'content' keys
            temperature: Sampling temperature (default: 0.7)
            max_tokens: Maximum tokens to generate (default: 32768, or the
                client's configured per-mode budget when one was supplied
                at construction — ENH-015, issue #494)

        Yields:
            Plain ``str`` answer-content chunks as they arrive from the SSE
            stream, plus :class:`ReasoningDelta` objects on the distinct
            reasoning channel (issue #554). Consumers that only want the
            answer must discriminate with ``isinstance(chunk, str)``.

        Raises:
            LLMError: If the request fails
            RuntimeError: If the client has not been started
        """
        if isinstance(max_tokens, _UnsetMaxTokens):
            max_tokens = (
                self.max_tokens if self.max_tokens is not None else _DEFAULT_MAX_TOKENS
            )
        client = await self._ensure_started()
        url = f"{self.base_url}/v1/chat/completions"

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": temperature,
            "max_tokens": max_tokens,
            # Issue #571: both supported providers document
            # `stream_options.include_usage` on the OpenAI-compatible
            # surface — the final (choices-less) chunk then carries the
            # authoritative `usage` object, parsed below.
            "stream_options": {"include_usage": True},
        }
        if self.chat_template_kwargs:
            payload["chat_template_kwargs"] = self.chat_template_kwargs
        if self.reasoning_effort is not None:
            payload["reasoning_effort"] = self.reasoning_effort
        if self.ttl is not None:
            # LM Studio idle TTL (issue #571) — same semantics as the
            # non-streaming payload above.
            payload["ttl"] = self.ttl
        started_at = time.perf_counter()
        prompt_tokens = self._prompt_token_estimate(messages)
        completion_chars = 0
        # Issue #571: provider-exact usage from the final usage-only chunk,
        # captured before the empty-choices skip so the reporting chunk is
        # never dropped.
        _usage: Dict[str, Any] = {}

        # Check circuit breaker state before attempting stream connection.
        # Use the lock to avoid race conditions with concurrent requests.
        async with self._circuit_breaker._lock:
            if self._circuit_breaker.current_state == CircuitBreakerState.OPEN:
                self._circuit_breaker._check_timeout()
                if self._circuit_breaker.current_state == CircuitBreakerState.OPEN:
                    raise LLMError(
                        "LLM service is currently unavailable (circuit breaker open)"
                    )

        # State for filtering thinking content. Handles three open markers:
        #   <think>             — standard (gpt-oss-120b and most others)
        #   _lhs                — legacy Qwen tag style
        #   "Thinking Process:" — qwen3.5-122b prefix style
        # And two close markers: </think> and _rhs.
        # ``reasoning_content`` deltas are *never* streamed to users; they are
        # treated like any other thinking content and suppressed at the source.
        #
        # The ``_in_prefix_region`` flag is True until we yield any
        # user-visible content. It gates the two bare-substring legacy markers
        # (``_lhs`` and ``"Thinking Process:"``): these
        # markers are only valid as a *prefix* of the model response
        # (Qwen-style models always emit their reasoning up front, before
        # the actual answer). Once any answer text has been streamed, a
        # bare ``_lhs`` or ``"Thinking Process:"`` substring appearing in a
        # later delta is no longer a thinking-block open marker — it is
        # almost certainly legitimate content (e.g. an identifier like
        # ``expr_lhs`` or a section title quoted from a RAG document), and
        # treating it as an open would silently truncate the answer. See
        # issue #227.
        _thinking_active = False
        _in_prefix_region = True
        _buffer = ""
        # The bare legacy reasoning markers ("_lhs", "Thinking Process:") are only
        # ever emitted as a prefix to the whole response. Once any real content has
        # been streamed to the user, a later occurrence of those substrings is
        # genuine answer text (e.g. an identifier like ``expr_lhs``) and must NOT be
        # treated as a thinking marker — otherwise the rest of the answer is
        # silently swallowed. The XML ``<think>`` tag is unambiguous and stays
        # active anywhere.
        _content_emitted = False

        # Last non-null finish_reason observed on the SSE choice deltas
        # (issue #511 FULL-ENH-01). Providers send it on a final, often
        # delta-only chunk (e.g. {"delta": {}, "finish_reason": "length"}),
        # so it must be captured even when no content is streamed.
        _finish_reason: Optional[str] = None

        # Issue #554: provider reasoning deltas (``reasoning_content`` is
        # the OpenAI-compatible field used by e.g. gpt-oss/nemotron;
        # ``reasoning`` is Ollama's /v1 field) are accumulated here and
        # yielded on the distinct ``ReasoningDelta`` channel instead of
        # being silently dropped. They never enter the content pipeline
        # below, so the think-tag filter and the "reasoning never reaches
        # the content channel" invariant are unchanged.
        _reasoning_chars = 0
        _reasoning_started_at: Optional[float] = None
        _reasoning_last_at: Optional[float] = None

        stream_succeeded = False
        # E3 closure (#518): gen_ai client span for the streaming chat call.
        # Entered manually (not via ``with``) so the large stream-consumption
        # block keeps its existing indentation; exited in the finally below
        # so the span covers the whole stream, error or not. The CONTEXT
        # MANAGER is held in a local for the whole function: exiting the
        # span object instead of the CM, or letting the CM be GC'd early
        # (PR #595 review F-001c), ends the span at enter time — 0ms
        # duration, orphaned child spans, and SDK end-twice warnings.
        _genai_stream_cm = start_span(
            "gen_ai chat stream",
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.request.model": str(payload.get("model", "")),
            },
        )
        _genai_stream_cm.__enter__()
        try:
            # E3 telemetry (issue #518): propagate correlation headers when
            # bound; kwarg omitted when empty (strict test fakes compat).
            _corr = _correlation_headers()
            if _corr:
                stream_ctx = client.stream(
                    "POST", url, json=payload, headers=_corr
                )
            else:
                stream_ctx = client.stream("POST", url, json=payload)
            async with stream_ctx as response:
                response.raise_for_status()

                # Validate content-type for SSE
                content_type = response.headers.get("content-type", "")
                if "text/event-stream" not in content_type:
                    # Fall back to non-stream completion for providers that don't return SSE
                    content = await self.chat_completion(
                        messages=messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                    )
                    # Issue #571: keep the provider-exact usage the non-stream
                    # call just recorded — the stream summary below would
                    # otherwise overwrite last_metrics and drop those keys
                    # (the fallback response never rode the SSE usage chunk).
                    _fallback_metrics = self.last_metrics or {}
                    if _fallback_metrics.get("prompt_tokens") is not None:
                        _usage = {
                            "prompt_tokens": _fallback_metrics["prompt_tokens"],
                            "completion_tokens": _fallback_metrics.get("completion_tokens"),
                        }
                    if content:
                        yield content
                    stream_succeeded = True
                    return

                async def _sse_events() -> AsyncGenerator[str, None]:
                    """Yield one joined ``data`` payload per SSE event.

                    Implements SSE framing (LLM-001, issue #494): the field
                    name is everything before the first colon; at most ONE
                    optional leading space is stripped from the value
                    (``data: {...}`` and ``data:{...}`` are equivalent);
                    consecutive ``data:`` lines of one event accumulate and
                    dispatch — joined with ``\\n`` — on the blank line that
                    ends the event or at stream end; ``\\r`` is stripped so
                    CRLF transports parse identically; comment lines
                    (leading ``:``) and other field names (``event:``,
                    ``id:``, ``retry:``) are ignored.
                    """
                    data_lines: List[str] = []
                    async for raw_line in response.aiter_lines():
                        line = raw_line.rstrip("\r")
                        if not line:
                            # Blank line: end of the current SSE event.
                            if data_lines:
                                yield "\n".join(data_lines)
                                data_lines.clear()
                            continue
                        if line.startswith(":"):
                            # SSE comment / keep-alive — ignore.
                            continue
                        field, _sep, value = line.partition(":")
                        if value.startswith(" "):
                            value = value[1:]
                        if field == "data":
                            data_lines.append(value)
                    # Stream ended mid-event: dispatch any remaining data.
                    if data_lines:
                        yield "\n".join(data_lines)
                        data_lines.clear()

                try:
                    async for data_str in _sse_events():
                        # Check for stream end marker
                        if data_str == "[DONE]":
                            break

                        try:
                            data = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        # Extract content delta from choices
                        choices = data.get("choices", [])

                        # Issue #571: the usage-only final chunk (sent when
                        # stream_options.include_usage is requested) carries
                        # no choices — capture its usage before the skip
                        # below so provider-exact counts are never dropped.
                        chunk_usage = data.get("usage")
                        if isinstance(chunk_usage, dict):
                            _usage = chunk_usage

                        if not choices:
                            continue

                        delta = choices[0].get("delta", {})
                        # Issue #554: provider reasoning deltas ride their
                        # own typed channel. ``reasoning_content`` is the
                        # OpenAI-compatible field used by some models
                        # (gpt-oss-120b, nvidia_nemotron); ``reasoning`` is
                        # Ollama's /v1 field. Read the first non-empty one,
                        # yield it as a ``ReasoningDelta``, and keep it out
                        # of the content pipeline entirely — the answer
                        # channel still carries only ``content`` deltas and
                        # the think-tag filter is unchanged.
                        content = delta.get("content") or ""

                        # finish_reason lives on the choice object, not
                        # the delta — read it BEFORE the empty-content
                        # skip below, otherwise the delta-only final
                        # chunk that carries it is never inspected.
                        chunk_finish_reason = choices[0].get("finish_reason")
                        if chunk_finish_reason:
                            _finish_reason = chunk_finish_reason

                        reasoning_text = delta.get("reasoning_content")
                        if not isinstance(reasoning_text, str) or not reasoning_text:
                            reasoning_text = delta.get("reasoning")
                            if not isinstance(reasoning_text, str) or not reasoning_text:
                                reasoning_text = None
                        if reasoning_text is not None:
                            _now = time.perf_counter()
                            if _reasoning_started_at is None:
                                _reasoning_started_at = _now
                            _reasoning_last_at = _now
                            _reasoning_chars += len(reasoning_text)
                            yield ReasoningDelta(text=reasoning_text)

                        if not content:
                            # Pure reasoning chunk (or empty) — nothing to
                            # add to the content pipeline (any reasoning was
                            # yielded on its own channel above).
                            continue

                        _buffer += content

                        if len(_buffer) > _MAX_THINKING_BUFFER:
                            logger.error(
                                "Thinking content buffer exceeded %d bytes, possible malformed response",
                                _MAX_THINKING_BUFFER,
                            )
                            raise LLMError(
                                "Thinking content buffer overflow - model response may be malformed"
                            )

                        if not _thinking_active:
                            # Not currently in a thinking block.  Look for
                            # complete open markers first; if none found,
                            # check whether the buffer is a partial prefix
                            # of a known marker (hold it) or safe to yield.
                            think_open_match = _THINK_OPEN_RE.search(_buffer)
                            _stripped = _buffer.lstrip()
                            if think_open_match:
                                logger.debug(
                                    "Filtering thinking content from model response (<think> pattern)"
                                )
                                pre_think = _buffer[: think_open_match.start()]
                                if pre_think:
                                    completion_chars += len(pre_think)
                                    yield pre_think
                                    _content_emitted = True
                                _thinking_active = True
                                _in_prefix_region = False
                                _buffer = _buffer[think_open_match.end() :]
                                # Handle inline close in the same buffer
                                close_match = _THINK_CLOSE_RE.search(_buffer)
                                if close_match:
                                    _thinking_active = False
                                    _buffer = _buffer[close_match.end() :]
                            elif _in_prefix_region and _buffer.lstrip().startswith("_lhs"):
                                # Legacy Qwen-style marker. Only valid as a
                                # prefix of the model response: once we
                                # have streamed any non-thinking answer
                                # text, a bare ``_lhs`` substring (e.g.
                                # ``expr_lhs``, ``node_lhs``) is just
                                # legitimate content and must not be
                                # treated as a thinking-block open.
                                logger.debug(
                                    "Filtering thinking content from model response (_lhs/_rhs pattern)"
                                )
                                pre_think, _, remainder = _buffer.partition("_lhs")
                                if pre_think:
                                    yield pre_think
                                    _content_emitted = True
                                _thinking_active = True
                                _in_prefix_region = False
                                _buffer = remainder
                                if "_rhs" in _buffer:
                                    _, _, after_think = _buffer.partition("_rhs")
                                    _thinking_active = False
                                    _buffer = after_think
                            elif _in_prefix_region and (
                                _THINKING_PROCESS_MARKER.startswith(_buffer)
                                or _buffer.startswith(_THINKING_PROCESS_MARKER)
                                or _THINK_PARTIAL_OPEN_RE.match(_buffer)
                            ):
                                # Two related hold-buffer cases, both
                                # anchored to the start of the buffer:
                                #
                                # 1. qwen3.5-122b "Thinking Process:"
                                #    prefix — the marker must appear at
                                #    the start of the buffer (either as
                                #    a full marker or as a partial
                                #    prefix still streaming in). A bare
                                #    substring match later in the
                                #    buffer is treated as legitimate
                                #    content. See issue #227.
                                #
                                # 2. ``<think`` partial prefix — we
                                #    hold the buffer while the angle-
                                #    bracketed open tag streams in.
                                if _THINKING_PROCESS_MARKER in _buffer:
                                    logger.debug(
                                        "Filtering thinking content from model response (Thinking Process pattern)"
                                    )
                                    pre_marker, _, _ = _buffer.partition(
                                        _THINKING_PROCESS_MARKER
                                    )
                                    if pre_marker:
                                        completion_chars += len(pre_marker)
                                        yield pre_marker
                                        _content_emitted = True
                                    _thinking_active = True
                                    _in_prefix_region = False
                                    _buffer = ""
                                # else: still accumulating a partial
                                # open marker — hold buffer until full
                                # marker arrives or it diverges from
                                # any prefix.
                            elif _buffer:
                                # No opening pattern and no partial-open
                                # prefix — safe to yield.
                                completion_chars += len(_buffer)
                                yield _buffer
                                _in_prefix_region = False
                                _buffer = ""
                        else:
                            # Currently inside a thinking block — look for any
                            # of the known closing markers (case-insensitive).
                            close_match = _THINK_CLOSE_RE.search(_buffer)
                            if close_match:
                                _thinking_active = False
                                _buffer = _buffer[close_match.end() :]
                            elif "_rhs" in _buffer:
                                _, _, after_think = _buffer.partition("_rhs")
                                _thinking_active = False
                                _buffer = after_think
                            # Else: still inside thinking; drop accumulated
                            # thinking content periodically so the buffer
                            # cap protects against runaway thinking blocks.
                            if (
                                _thinking_active
                                and len(_buffer) > _MAX_THINKING_BUFFER // 2
                            ):
                                _buffer = _buffer[-256:]
                        # Yield any buffered content when not in thinking
                        # mode and not holding a partial open marker.
                        # The partial-prefix-holding checks (e.g. for
                        # ``<think`` or ``Thinking Process:``) only
                        # apply while we are still in the prefix region
                        # of the model response; once any non-thinking
                        # answer text has been streamed, partial
                        # substrings of those markers are just ordinary
                        # content and must be yielded. See issue #227.
                        if (
                            not _thinking_active
                            and _buffer
                            and not (
                                _in_prefix_region
                                and (
                                    "Thinking Process:".startswith(_buffer)
                                    or _THINK_PARTIAL_OPEN_RE.match(_buffer)
                                )
                            )
                        ):
                            completion_chars += len(_buffer)
                            yield _buffer
                            _in_prefix_region = False
                            _buffer = ""
                    stream_succeeded = True
                except GeneratorExit:
                    # Generator was closed by consumer - clean exit
                    stream_succeeded = True
                    raise
        except LLMError:
            # Don't record LLMError as transport failure — it may come from
            # the chat_completion() fallback path and represent an app-level error,
            # not a service-unavailability signal.
            raise
        except httpx.TimeoutException as e:
            async with self._circuit_breaker._lock:
                self._circuit_breaker.record_failure()
            raise LLMError(f"Streaming request timed out after {self.timeout}s") from e
        except httpx.HTTPStatusError as e:
            # Charge the breaker for OUTAGE statuses only (review RP-002,
            # PR #576): ordinary 4xx are input/config errors that would
            # recur on every retry, not provider outages.
            if is_outage_status(e.response.status_code):
                async with self._circuit_breaker._lock:
                    self._circuit_breaker.record_failure()
            # Read response content first to avoid ResponseNotRead error in streaming context
            try:
                response_text = (await e.response.aread()).decode(
                    "utf-8", errors="replace"
                )
            except Exception:
                response_text = "<unable to read response>"
            raise LLMError(
                f"HTTP error {e.response.status_code}: {response_text}"
            ) from e
        except httpx.RequestError as e:
            async with self._circuit_breaker._lock:
                self._circuit_breaker.record_failure()
            raise LLMError(f"Streaming request failed: {str(e)}") from e
        finally:
            _genai_stream_cm.__exit__(*sys.exc_info())
            if stream_succeeded:
                async with self._circuit_breaker._lock:
                    self._circuit_breaker.record_success()
                self.last_metrics = {
                    "provider_url": self.base_url,
                    "model": self.model,
                    "latency_ms": round((time.perf_counter() - started_at) * 1000, 2),
                    "prompt_tokens_estimate": prompt_tokens,
                    "completion_tokens_estimate": max(1, completion_chars // 4) if completion_chars else 0,
                    # Issue #554: reasoning accounting. The token count is a
                    # chars//4 estimate (the provider's delta stream carries
                    # no usage breakdown); the duration is the wall-clock
                    # span from the first to the last reasoning delta.
                    "reasoning_tokens_estimate": (
                        max(1, _reasoning_chars // 4) if _reasoning_chars else 0
                    ),
                    "reasoning_duration_ms": (
                        round((_reasoning_last_at - _reasoning_started_at) * 1000, 2)
                        if _reasoning_started_at is not None
                        and _reasoning_last_at is not None
                        else 0.0
                    ),
                    "finish_reason": _finish_reason,
                    "status": "ok",
                    "stream": True,
                }
                # Issue #571: provider-exact counts when the usage chunk
                # arrived; estimates remain the only values otherwise.
                if _usage.get("prompt_tokens") is not None:
                    self.last_metrics["prompt_tokens"] = _usage["prompt_tokens"]
                if _usage.get("completion_tokens") is not None:
                    self.last_metrics["completion_tokens"] = _usage["completion_tokens"]
            else:
                # Streaming failed — record a failure metric so the done
                # payload's llm_metrics does not report a stale/ok value for
                # a failed turn (mirrors the non-streaming per-error
                # branches above and in chat_completion). If the SSE-fallback
                # path already set a more specific status (timeout/http_error/
                # request_error/circuit_open via chat_completion), preserve it
                # rather than overwriting with the generic "error".
                _existing_status = (self.last_metrics or {}).get("status", "ok")
                if _existing_status == "ok":
                    self.last_metrics = {
                        "provider_url": self.base_url,
                        "model": self.model,
                        "latency_ms": round((time.perf_counter() - started_at) * 1000, 2),
                        "prompt_tokens_estimate": prompt_tokens,
                        "completion_tokens_estimate": max(1, completion_chars // 4) if completion_chars else 0,
                        "status": "error",
                        "stream": True,
                    }

        # Log connection pool metrics after streaming completes
        self._log_pool_stats()

    async def close(self):
        """Close the HTTP client (idempotent — safe to call multiple times)."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class ModelNotConfiguredError(RuntimeError):
    """A chat-model endpoint pair (URL + model) is not configured.

    The system ships no model defaults (issue #570): operators configure
    endpoints via env vars or Settings -> Models. Factories raise this
    instead of constructing a client that would send an empty model name.
    """


def create_thinking_client(timeout: float = 300.0) -> "LLMClient":
    """Create an LLMClient configured for the Thinking backend (Ollama).

    Talks to ``settings.ollama_chat_url`` with ``settings.chat_model`` —
    both operator-configured (no shipped default; configure them via
    ``OLLAMA_CHAT_URL``/``CHAT_MODEL`` or Settings -> Models). Carries the
    Ollama /v1-documented reasoning control ``reasoning_effort='high'``
    (issue #554): Ollama's OpenAI-compatibility doc lists
    ``reasoning_effort`` (high/medium/low/max/none) as the supported
    ``/v1/chat/completions`` request field and documents no ``think``
    field there (``think`` is native ``/api/chat``-only). Deployments on
    older Ollama versions that do not recognize the field ignore it, which
    degrades to the pre-#554 provider-default behavior.

    The client also carries ``settings.thinking_max_tokens`` as its default
    generation budget (ENH-015, issue #494): callers that do not pass an
    explicit ``max_tokens`` get the configured thinking budget instead of
    the legacy hardcoded 32768. An explicit per-call ``max_tokens`` still
    wins.

    Provider residency contracts (issue #571): the Ollama-native
    ``keep_alive`` (default ``"-1"``, the same indefinite residency the
    retired ping loop provided) and ``num_ctx`` (default 4096, Ollama's
    documented default) are applied by :meth:`LLMClient.prime_residency`
    at startup — the OpenAI-compatible surface cannot express either.
    """
    if not settings.ollama_chat_url or not settings.chat_model:
        raise ModelNotConfiguredError(
            "Thinking chat model is not configured. Set OLLAMA_CHAT_URL and "
            "CHAT_MODEL (e.g. an Ollama, LM Studio, or vLLM endpoint), or "
            "configure it in Settings -> Models."
        )
    return LLMClient(
        timeout=timeout,
        base_url=settings.ollama_chat_url,
        model=settings.chat_model,
        cb_name="llm_thinking",
        max_tokens=settings.thinking_max_tokens,
        reasoning_effort="high",
        keep_alive=settings.ollama_keep_alive,
        num_ctx=settings.ollama_num_ctx,
    )


def create_editorial_client(timeout: float = 300.0) -> "LLMClient":
    """Client for editorial desk stages (copy/standards/fact).

    Some reasoning-strong chat models loop in reasoning on desk-style
    structured-edit prompts; deployments can point these stages at a
    different endpoint (DRAFT_EDITORIAL_CHAT_URL/MODEL) while falling
    back to the thinking backend when unset.

    Carries the same Ollama-native residency contracts as
    :func:`create_thinking_client` (issue #571): the editorial backend is
    an Ollama-mode backend by default, and priming covers it when an
    explicit override is configured (otherwise its URL+model equal the
    thinking client's and the thinking prime pins the same server).
    """
    base_url = settings.editorial_chat_url or settings.ollama_chat_url
    model = settings.editorial_chat_model or settings.chat_model
    if not base_url or not model:
        raise ModelNotConfiguredError(
            "Editorial chat model is not configured. Set "
            "DRAFT_EDITORIAL_CHAT_URL/MODEL, or the thinking pair "
            "(OLLAMA_CHAT_URL + CHAT_MODEL) it falls back to, or configure "
            "them in Settings -> Models."
        )
    return LLMClient(
        timeout=timeout,
        base_url=base_url,
        model=model,
        cb_name="llm_editorial",
        keep_alive=settings.ollama_keep_alive,
        num_ctx=settings.ollama_num_ctx,
    )


def create_instant_client(timeout: float = 120.0) -> "LLMClient":
    """Create the Instant client (LM Studio, ``settings.instant_chat_url``).

    Both the URL and ``settings.instant_chat_model`` are
    operator-configured (no shipped default; configure via
    ``INSTANT_CHAT_URL``/``INSTANT_CHAT_MODEL`` or Settings -> Models).
    ``settings.instant_enable_thinking`` (FU-005, issue #494; default False)
    selects the posture: True sends no control at all and the provider/model
    chat-template default governs; False sends the family-appropriate
    no-think control chosen by
    :func:`select_no_think_chat_template_kwargs` for the configured
    ``instant_chat_model`` (issue #554) — Qwen-family names get
    ``chat_template_kwargs={'enable_thinking': False}``, unrecognized
    families (e.g. nemotron models, whose cards name no template
    mechanism) log a warning and send nothing, failing open. The client
    also carries ``settings.instant_max_tokens`` as its default generation
    budget (ENH-015, issue #494), mirroring ``create_thinking_client``.

    Provider residency contracts (issue #571): every chat payload carries
    LM Studio's per-request idle ``ttl`` (default 86400s — far more generous
    than the 60-minute JIT default and the retired ping loop's interval), and
    ``context_length`` (default 4096) is requested from LM Studio's native
    model-load endpoint by :meth:`LLMClient.prime_residency` at startup.
    Note LM Studio's JIT Auto-Evict unloads this model when a DIFFERENT
    model is requested on the same server.
    """
    if not settings.instant_chat_url or not settings.instant_chat_model:
        raise ModelNotConfiguredError(
            "Instant chat model is not configured. Set INSTANT_CHAT_URL and "
            "INSTANT_CHAT_MODEL (e.g. an LM Studio or llama-server endpoint), "
            "or configure it in Settings -> Models."
        )
    # Pydantic guarantees a real bool here; the identity check keeps
    # partially mocked settings objects (tests) on the default-False branch.
    chat_template_kwargs = (
        None
        if settings.instant_enable_thinking is True
        else select_no_think_chat_template_kwargs(settings.instant_chat_model)
    )
    instant_max_tokens = getattr(settings, "instant_max_tokens", None)
    lm_studio_ttl = getattr(settings, "lm_studio_ttl", None)
    lm_studio_ctx = getattr(settings, "lm_studio_context_length", None)
    return LLMClient(
        timeout=timeout,
        base_url=settings.instant_chat_url,
        model=settings.instant_chat_model,
        cb_name="llm_instant",
        max_tokens=instant_max_tokens if isinstance(instant_max_tokens, int) else None,
        chat_template_kwargs=chat_template_kwargs,
        ttl=lm_studio_ttl if isinstance(lm_studio_ttl, int) else None,
        context_length=(
            lm_studio_ctx if isinstance(lm_studio_ctx, int) else None
        ),
    )
