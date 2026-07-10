"""
OpenAI-compatible LLM chat client using httpx.
"""

import json
import logging
import re
import time
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx

from app.config import settings
from app.services.circuit_breaker import (
    CircuitBreakerError,
    CircuitBreakerState,
    create_llm_circuit_breaker,
)
from app.services.ssrf import assert_url_safe
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


class LLMError(Exception):
    """Exception raised for LLM client errors."""

    pass


class LLMClient:
    """OpenAI-compatible LLM chat client."""

    def __init__(
        self,
        timeout: float = 300.0,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        cb_name: str = "llm",
    ):
        """
        Initialize the LLM client.

        Args:
            timeout: Request timeout in seconds (default: 300.0 for model loading)
            base_url: Override for the chat endpoint. Defaults to settings.ollama_chat_url.
            model: Override for the model name. Defaults to settings.chat_model.
            cb_name: Circuit breaker name (for logging / metrics distinction).
        """
        self.base_url = (base_url or settings.ollama_chat_url).rstrip("/")
        self.model = model or settings.chat_model
        self.timeout = timeout
        assert_url_safe(base_url or settings.ollama_chat_url)
        self._circuit_breaker = create_llm_circuit_breaker(name=cb_name)
        self._client: Optional[httpx.AsyncClient] = None
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
    ) -> None:
        """Hot-update base_url and/or model in place.

        Per-request URLs are computed from ``self.base_url`` at call time, so
        in-place updates take effect immediately without recreating the
        httpx pool or invalidating any stored references held by callers
        (LLMHealthChecker, background_processor, keepalive tasks, RAGEngine).

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
        if changed:
            self._circuit_breaker.reset()

    async def start(self):
        """Start the HTTP client. Must be called before using the client."""
        # Configure limits for connection pooling with keep-alive
        limits = httpx.Limits(
            max_keepalive_connections=settings.llm_max_keepalive_connections,
            max_connections=settings.llm_max_connections,
            keepalive_expiry=300.0,  # Keep connections alive for 5 minutes
        )
        # Add keep-alive headers to prevent LM Studio from unloading
        headers = {"Connection": "keep-alive", "Keep-Alive": "timeout=300, max=1000"}
        # SSRFSafeTransport re-validates the resolved IP at request time to
        # close the DNS-rebinding TOCTOU gap the startup-only guard leaves
        # open, while preserving TLS SNI/cert validation.
        from app.services.ssrf_transport import SSRFSafeTransport

        self._client = httpx.AsyncClient(
            timeout=self.timeout,
            limits=limits,
            headers=headers,
            transport=SSRFSafeTransport(),
        )

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()

    def _ensure_started(self) -> httpx.AsyncClient:
        """Ensure the client has been started and return it."""
        if self._client is None:
            raise RuntimeError(
                "LLMClient not started. Call start() before using the client."
            )
        return self._client

    def _log_pool_stats(self) -> None:
        """Log connection pool statistics for monitoring."""
        try:
            client = self._client
            if client is None:
                return
            pool = getattr(client, "_transport", None)
            if pool and hasattr(pool, "_pool"):
                pool_obj = pool._pool
                connections = getattr(pool_obj, "_num_connections", 0)
                keepalive = getattr(pool_obj, "_num_keepalive", 0)
                limits = getattr(pool_obj, "_limits", None)
                max_connections = (
                    getattr(limits, "max_connections", settings.llm_max_connections) if limits else settings.llm_max_connections
                )
                max_keepalive = (
                    getattr(limits, "max_keepalive_connections", settings.llm_max_keepalive_connections)
                    if limits else settings.llm_max_keepalive_connections
                )
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
        max_tokens: int = 32768,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Send a chat completion request and return the full response.

        Args:
            messages: List of message dicts with 'role' and 'content' keys
            temperature: Sampling temperature (default: 0.7)
            max_tokens: Maximum tokens to generate (default: 32768)

        Returns:
            The generated content string

        Raises:
            LLMError: If the request fails or response is invalid
            RuntimeError: If the client has not been started
        """
        client = self._ensure_started()
        url = f"{self.base_url}/v1/chat/completions"

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format is not None:
            payload["response_format"] = response_format

        started_at = time.perf_counter()
        prompt_tokens = self._prompt_token_estimate(messages)
        try:
            response = await self._circuit_breaker(client.post)(url, json=payload)
            response.raise_for_status()
            data = response.json()

            if "choices" not in data or not data["choices"]:
                raise LLMError("Invalid response: no choices in response")

            message = data["choices"][0].get("message", {})
            content = message.get("content", "")

            # Log connection pool metrics
            self._log_pool_stats()

            content = self._strip_thinking_content(content)
            self.last_metrics = {
                "provider_url": self.base_url,
                "model": self.model,
                "latency_ms": round((time.perf_counter() - started_at) * 1000, 2),
                "prompt_tokens_estimate": prompt_tokens,
                "completion_tokens_estimate": self._approx_tokens(content),
                "status": "ok",
            }
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
        max_tokens: int = 32768,
    ) -> AsyncGenerator[str, None]:
        """
        Send a streaming chat completion request and yield content chunks.

        Args:
            messages: List of message dicts with 'role' and 'content' keys
            temperature: Sampling temperature (default: 0.7)
            max_tokens: Maximum tokens to generate (default: 32768)

        Yields:
            Content chunks as they arrive from the SSE stream

        Raises:
            LLMError: If the request fails
            RuntimeError: If the client has not been started
        """
        client = self._ensure_started()
        url = f"{self.base_url}/v1/chat/completions"

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        started_at = time.perf_counter()
        prompt_tokens = self._prompt_token_estimate(messages)
        completion_chars = 0

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

        stream_succeeded = False
        try:
            async with client.stream("POST", url, json=payload) as response:
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
                    if content:
                        yield content
                    stream_succeeded = True
                    return

                try:
                    async for line in response.aiter_lines():
                        line = line.strip()

                        # Skip empty lines and SSE keep-alive comments
                        if not line or line.startswith(":"):
                            continue

                        # SSE format: data: {...}
                        if line.startswith("data: "):
                            data_str = line[6:]  # Remove "data: " prefix

                            # Check for stream end marker
                            if data_str == "[DONE]":
                                break

                            try:
                                data = json.loads(data_str)
                            except json.JSONDecodeError:
                                continue

                            # Extract content delta from choices
                            choices = data.get("choices", [])
                            if not choices:
                                continue

                            delta = choices[0].get("delta", {})
                            # ``reasoning_content`` is the OpenAI-compatible
                            # field used by some models (gpt-oss-120b,
                            # nvidia_nemotron) to emit chain-of-thought.
                            # We never expose it to users — drop it entirely
                            # and only stream ``content`` deltas.
                            content = delta.get("content") or ""

                            if not content:
                                # Pure reasoning chunk (or empty) — skip.
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
            if stream_succeeded:
                async with self._circuit_breaker._lock:
                    self._circuit_breaker.record_success()
                self.last_metrics = {
                    "provider_url": self.base_url,
                    "model": self.model,
                    "latency_ms": round((time.perf_counter() - started_at) * 1000, 2),
                    "prompt_tokens_estimate": prompt_tokens,
                    "completion_tokens_estimate": max(1, completion_chars // 4) if completion_chars else 0,
                    "status": "ok",
                    "stream": True,
                }
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


def create_thinking_client(timeout: float = 300.0) -> "LLMClient":
    """Create an LLMClient configured for the Thinking backend (gpt-oss-120b / DGX Spark)."""
    return LLMClient(
        timeout=timeout,
        base_url=settings.ollama_chat_url,
        model=settings.chat_model,
        cb_name="llm_thinking",
    )


def create_instant_client(timeout: float = 120.0) -> "LLMClient":
    """Create an LLMClient configured for the Instant backend (LM Studio / Nemotron 4B)."""
    return LLMClient(
        timeout=timeout,
        base_url=settings.instant_chat_url,
        model=settings.instant_chat_model,
        cb_name="llm_instant",
    )
