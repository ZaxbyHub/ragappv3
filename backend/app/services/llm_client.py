"""
OpenAI-compatible LLM chat client using httpx.
"""

import json
import logging
import re
from typing import AsyncGenerator, Dict, List, Optional

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
        self._client = httpx.AsyncClient(
            timeout=self.timeout, limits=limits, headers=headers
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
    ) -> str:
        """
        Send a chat completion request and return the full response.

        Args:
            messages: List of message dicts with 'role' and 'content' keys
            temperature: Sampling temperature (default: 0.7)
            max_tokens: Maximum tokens to generate (default: 2048)

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

            return self._strip_thinking_content(content)
        except CircuitBreakerError as e:
            raise LLMError(
                f"LLM service is currently unavailable (circuit breaker open): {e}"
            ) from e
        except httpx.TimeoutException as e:
            raise LLMError(f"Request timed out after {self.timeout}s") from e
        except httpx.HTTPStatusError as e:
            raise LLMError(
                f"HTTP error {e.response.status_code}: {e.response.text}"
            ) from e
        except httpx.RequestError as e:
            raise LLMError(f"Request failed: {str(e)}") from e
        except json.JSONDecodeError as e:
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
            max_tokens: Maximum tokens to generate (default: 2048)

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
        _thinking_active = False
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
                                        yield pre_think
                                        _content_emitted = True
                                    _thinking_active = True
                                    _buffer = _buffer[think_open_match.end() :]
                                    # Handle inline close in the same buffer
                                    close_match = _THINK_CLOSE_RE.search(_buffer)
                                    if close_match:
                                        _thinking_active = False
                                        _buffer = _buffer[close_match.end() :]
                                elif _THINK_PARTIAL_OPEN_RE.match(_buffer):
                                    # Partial "<think" open tag split across chunk
                                    # boundaries — hold the buffer until the rest
                                    # of the tag (or divergent content) arrives.
                                    pass
                                elif not _content_emitted and _stripped.startswith("_lhs"):
                                    # Legacy Qwen _lhs/_rhs reasoning marker. Only
                                    # honored at the very start of the response: a
                                    # later "_lhs" is genuine answer text.
                                    logger.debug(
                                        "Filtering thinking content from model response (_lhs/_rhs pattern)"
                                    )
                                    pre_think, _, remainder = _buffer.partition("_lhs")
                                    if pre_think:
                                        yield pre_think
                                        _content_emitted = True
                                    _thinking_active = True
                                    _buffer = remainder
                                    if "_rhs" in _buffer:
                                        _, _, after_think = _buffer.partition("_rhs")
                                        _thinking_active = False
                                        _buffer = after_think
                                elif not _content_emitted and (
                                    _stripped.startswith("Thinking Process:")
                                    or "Thinking Process:".startswith(_stripped)
                                ):
                                    # qwen3.5-122b "Thinking Process:" prefix. Only
                                    # honored before any content is emitted; after
                                    # that the substring is real answer text.
                                    if "Thinking Process:" in _buffer:
                                        logger.debug(
                                            "Filtering thinking content from model response (Thinking Process pattern)"
                                        )
                                        pre_marker, _, _ = _buffer.partition("Thinking Process:")
                                        if pre_marker:
                                            yield pre_marker
                                            _content_emitted = True
                                        _thinking_active = True
                                        _buffer = ""
                                    # else: still accumulating a partial prefix —
                                    # hold buffer until the full marker arrives or
                                    # it diverges.
                                elif _buffer:
                                    # No opening pattern and no partial-open
                                    # prefix — safe to yield.
                                    yield _buffer
                                    _content_emitted = True
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
                            # mode and not holding a partial open marker. The
                            # "Thinking Process:" prefix is only held before any
                            # content has been emitted (afterwards it is real text).
                            if (
                                not _thinking_active
                                and _buffer
                                and not (
                                    not _content_emitted
                                    and "Thinking Process:".startswith(_buffer)
                                )
                                and not _THINK_PARTIAL_OPEN_RE.match(_buffer)
                            ):
                                yield _buffer
                                _content_emitted = True
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
