"""
Model availability checker for Ollama, OpenAI-compatible, and native TEI endpoints.

Probe strategy: each configured URL is probed with an ordered chain of API
dialects (Ollama ``/api/tags``, OpenAI-compatible ``/v1/models``, native TEI
``/info``). The dialect suggested by URL/port heuristics is tried first; when
the endpoint answers with a "wrong dialect" signal (HTTP 404/405, non-JSON
body, or a JSON body without the dialect's expected shape) the next dialect is
tried. The first dialect that succeeds is cached per derived base URL so
subsequent checks are single-probe.

Transport-level failures (timeout, connection refused) do NOT fall through —
the other dialects talk to the same host and would fail identically — and a
successful listing that lacks the configured model is authoritative
"unavailable" (genuine misconfiguration surfaces instead of being masked).
"""
import asyncio
from typing import Any, Dict

import httpx

from app.config import settings
from app.services.circuit_breaker import CircuitBreakerError
from app.services.ssrf import assert_url_safe


class ModelCheckerError(Exception):
    """Exception raised for model checker errors."""
    pass


class _DialectMismatch(Exception):
    """Internal: endpoint answered but does not speak the probed dialect.

    Raised for HTTP 404/405, a non-JSON body, or JSON without the dialect's
    expected shape — signals that the fallback chain should try the next
    dialect rather than report the service down.
    """


def _model_check_error(timeout: float, exc: BaseException) -> Dict[str, Any]:
    """Map a model-probe exception to the standard unavailable-result dict.

    Shared by all three model-check methods so the error-handling tail stays
    identical (TimeoutException / HTTPStatusError / RequestError /
    ValueError|TypeError|RuntimeError).
    """
    if isinstance(exc, httpx.TimeoutException):
        return {'available': False, 'error': f"Request timed out after {timeout}s"}
    if isinstance(exc, httpx.HTTPStatusError):
        return {
            'available': False,
            'error': f"HTTP error {exc.response.status_code}: {exc.response.text}",
        }
    if isinstance(exc, httpx.RequestError):
        return {'available': False, 'error': f"Request failed: {str(exc)}"}
    if isinstance(exc, (ValueError, TypeError, RuntimeError)):
        return {'available': False, 'error': f"Unexpected error: {str(exc)}"}
    # Should not reach here for the documented exception set; re-raise to avoid
    # silently swallowing an unexpected error type.
    raise exc


# URL suffixes that identify a specific API route rather than a server root.
# Stripping them (repeatedly, so e.g. "/v1/embeddings" behind a proxy prefix
# also resolves) yields the base every dialect appends its own path to.
_KNOWN_ROUTE_SUFFIXES = ('/v1/embeddings', '/v1/models', '/api/tags', '/embed')


def _derive_base(url: str) -> str:
    """Strip known API-route suffixes to derive the server root URL.

    e.g. ``http://host:8080/v1/embeddings`` -> ``http://host:8080``
    """
    base = url.rstrip('/')
    changed = True
    while changed:
        changed = False
        for suffix in _KNOWN_ROUTE_SUFFIXES:
            if base.lower().endswith(suffix):
                base = base[: -len(suffix)].rstrip('/')
                changed = True
    return base


class ModelChecker:
    """Checks availability of configured models via Ollama, OpenAI-compatible,
    or native TEI endpoints, with automatic dialect fallback."""

    def __init__(self, timeout: float = 10.0, fallback_timeout: float = 3.0):
        """
        Initialize the model checker.

        Args:
            timeout: Request timeout in seconds for the primary (first-tried)
                dialect probe (default: 10.0)
            fallback_timeout: Timeout for subsequent fallback dialect probes
                (default: 3.0). A wrong-dialect 404 fails fast; the short
                timeout bounds worst-case deep-health latency when a host is
                slow but responsive.
        """
        self.timeout = timeout
        self.fallback_timeout = fallback_timeout
        # derived base URL -> dialect that last succeeded
        self._dialect_cache: Dict[str, str] = {}

    async def check_models(self) -> Dict[str, Any]:
        """
        Check availability of the configured embedding, chat, and instant-chat
        models. The three checks run concurrently.

        Returns:
            Dictionary with 'embedding_model', 'chat_model', and
            'instant_chat_model' keys, each containing a dict with
            'available' (bool) and 'error' (str or None).
        """
        urls = (
            settings.ollama_embedding_url,
            settings.ollama_chat_url,
            settings.instant_chat_url,
        )
        await asyncio.gather(*(asyncio.to_thread(assert_url_safe, u) for u in urls))

        # follow_redirects=False so a 30x from a model host cannot bypass the
        # SSRF guard by redirecting to a private/internal address.
        # SSRFSafeTransport re-validates the resolved IP at request time to close
        # the DNS-rebinding TOCTOU gap the startup-only guard leaves open.
        # Derived probe URLs only ever shorten the path on an already-validated
        # host, so they remain inside the SSRF-safe envelope.
        from app.services.ssrf_transport import SSRFSafeTransport

        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=False,
            transport=SSRFSafeTransport(),
        ) as client:
            embedding_result, chat_result, instant_result = await asyncio.gather(
                self._check_model_availability(
                    client, settings.ollama_embedding_url, settings.embedding_model
                ),
                self._check_model_availability(
                    client, settings.ollama_chat_url, settings.chat_model
                ),
                self._check_model_availability(
                    client, settings.instant_chat_url, settings.instant_chat_model
                ),
            )

        return {
            'embedding_model': embedding_result,
            'chat_model': chat_result,
            'instant_chat_model': instant_result,
        }

    def _detect_provider_type(self, base_url: str) -> str:
        """
        Heuristically guess the endpoint dialect. Only orders the fallback
        chain — a wrong guess self-corrects on the first probe.

        Rules (kept consistent with ``EmbeddingService._detect_provider_mode``):
        - explicit /api/tags path => Ollama
        - explicit /v1/models or /v1/embeddings path => OpenAI-compatible
        - explicit /embed path => native TEI
        - no explicit path + port 8080 => native TEI (TEI default)
        - no explicit path + port 1234/8000/5000/5001 => OpenAI-compatible
        - otherwise Ollama
        """
        url_lower = base_url.lower().rstrip('/')

        if '/api/tags' in url_lower:
            return 'ollama'

        if '/v1/models' in url_lower or '/v1/embeddings' in url_lower:
            return 'openai_compatible'

        if url_lower.endswith('/embed'):
            return 'tei'

        # Port-based detection when no explicit path disambiguates the endpoint.
        # Native TEI defaults to 8080; other common OpenAI-compatible servers
        # (vLLM 8000, LM Studio 1234, etc.) use the ports below.
        OPENAI_COMPATIBLE_PORTS = {8000, 1234, 5000, 5001}
        try:
            from urllib.parse import urlparse
            parsed = urlparse(base_url)
            if parsed.port == 8080 or parsed.netloc.endswith(':8080'):
                return 'tei'
            if parsed.port in OPENAI_COMPATIBLE_PORTS or any(
                parsed.netloc.endswith(f':{p}') for p in OPENAI_COMPATIBLE_PORTS
            ):
                return 'openai_compatible'
        except (ValueError, AttributeError):
            # URL parsing failed, continue with default provider
            pass

        # Default to Ollama for backward compatibility
        return 'ollama'

    def _dialect_order(self, base_url: str, base_key: str):
        """Ordered dialect list: cached winner, heuristic guess, then the rest."""
        order = []
        for dialect in (
            self._dialect_cache.get(base_key),
            self._detect_provider_type(base_url),
            'openai_compatible',
            'tei',
            'ollama',
        ):
            if dialect and dialect not in order:
                order.append(dialect)
        return order

    async def _check_model_availability(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        model_name: str,
    ) -> Dict[str, Any]:
        """
        Check if a model is available at the given endpoint, trying each API
        dialect in turn until one answers.

        Returns:
            Dictionary with 'available' (bool) and 'error' (str or None)
        """
        base_key = _derive_base(base_url)
        order = self._dialect_order(base_url, base_key)
        mismatch_notes = []

        for i, dialect in enumerate(order):
            if dialect == 'tei':
                checker = self._check_tei_model
            elif dialect == 'openai_compatible':
                checker = self._check_openai_compatible_model
            else:
                checker = self._check_ollama_model
            timeout = self.timeout if i == 0 else self.fallback_timeout

            try:
                result = await checker(client, base_url, model_name, timeout)
            except _DialectMismatch as e:
                mismatch_notes.append(f"{dialect}: {e}")
                continue
            except CircuitBreakerError as e:
                # Defensive: no breaker wraps the dialect probes today, but a
                # tripped breaker must never wedge the whole chain — treat it
                # as "this dialect unusable, try the next".
                mismatch_notes.append(f"{dialect}: circuit breaker open ({e})")
                continue
            except Exception as e:
                return _model_check_error(timeout, e)

            if result.get('available'):
                self._dialect_cache[base_key] = dialect
                return result

            # Authoritative result: a working listing that lacks the model, a
            # transport failure, or a non-404 HTTP error. Other dialects target
            # the same host and cannot change this verdict.
            return result

        return {
            'available': False,
            'error': 'No probe dialect matched the endpoint ('
                     + '; '.join(mismatch_notes)
                     + ')',
        }

    async def _check_ollama_model(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        model_name: str,
        timeout: float,
    ) -> Dict[str, Any]:
        """Check model availability using Ollama's /api/tags endpoint."""
        url = f"{_derive_base(base_url)}/api/tags"

        try:
            response = await client.get(url, timeout=timeout)
            response.raise_for_status()
            try:
                data = response.json()
            except ValueError as e:
                raise _DialectMismatch(f"/api/tags returned a non-JSON body ({e})") from e
            if not isinstance(data, dict):
                raise _DialectMismatch("/api/tags returned unexpected JSON shape")
            models = data.get('models', [])
            if not isinstance(models, list):
                raise _DialectMismatch("/api/tags response missing 'models' list")

            # Model names in Ollama may include tags (e.g., "qwen2.5:32b");
            # check for exact match or model_name as a prefix.
            available_model_names = [m.get('name', '') for m in models]

            for available_name in available_model_names:
                if available_name == model_name or available_name.startswith(f"{model_name}:"):
                    return {'available': True, 'error': None}

            return {
                'available': False,
                'error': f"Model '{model_name}' not found. Available models: {', '.join(available_model_names) or 'none'}"
            }

        except _DialectMismatch:
            raise
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (404, 405):
                raise _DialectMismatch(f"/api/tags -> HTTP {e.response.status_code}") from e
            return _model_check_error(timeout, e)
        except Exception as e:
            return _model_check_error(timeout, e)

    async def _check_tei_model(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        model_name: str,
        timeout: float,
    ) -> Dict[str, Any]:
        """
        Check model availability using native TEI's /info endpoint.

        Native HuggingFace TEI serves a single model and exposes its identity
        at ``<root>/info`` as ``{"model_id": "..."", ...}``. The /info route
        lives at the server root, so any known API-route suffix (/embed,
        /v1/embeddings, ...) is stripped before probing.
        """
        url = f"{_derive_base(base_url)}/info"

        try:
            response = await client.get(url, timeout=timeout)
            response.raise_for_status()
            try:
                data = response.json()
            except ValueError as e:
                raise _DialectMismatch(f"/info returned a non-JSON body ({e})") from e
            if not isinstance(data, dict):
                raise _DialectMismatch("/info returned unexpected JSON shape (expected dict with 'model_id')")

            live_model_id = data.get('model_id', '')
            if not live_model_id:
                # A 200 without model_id is not native TEI — let the chain
                # try the next dialect rather than reporting a false outage.
                raise _DialectMismatch("tei /info response missing 'model_id' (expected dict with model_id)")

            # TEI serves one model; compare leniently by the last path segment
            # (e.g. "microsoft/harrier-oss-v1-0.6b" vs "harrier-oss-v1-0.6b").
            if (
                live_model_id == model_name
                or live_model_id.split('/')[-1] == model_name.split('/')[-1]
            ):
                return {'available': True, 'error': None}

            return {
                'available': False,
                'error': (
                    f"Model '{model_name}' not found. "
                    f"Live TEI model: '{live_model_id}'"
                )
            }

        except _DialectMismatch:
            raise
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (404, 405):
                raise _DialectMismatch(f"/info -> HTTP {e.response.status_code}") from e
            return _model_check_error(timeout, e)
        except Exception as e:
            return _model_check_error(timeout, e)

    async def _check_openai_compatible_model(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        model_name: str,
        timeout: float,
    ) -> Dict[str, Any]:
        """
        Check model availability using the OpenAI-compatible /v1/models listing.
        """
        url = f"{_derive_base(base_url)}/v1/models"

        try:
            response = await client.get(url, timeout=timeout)
            response.raise_for_status()
            try:
                data = response.json()
            except ValueError as e:
                raise _DialectMismatch(f"/v1/models returned a non-JSON body ({e})") from e
            if not isinstance(data, dict):
                raise _DialectMismatch("/v1/models returned unexpected JSON shape")
            models = data.get('data', [])
            if not isinstance(models, list):
                raise _DialectMismatch("/v1/models response missing 'data' list")

            available_model_ids = [m.get('id', '') for m in models if isinstance(m, dict)]

            for available_id in available_model_ids:
                if available_id == model_name or available_id.startswith(f"{model_name}:"):
                    return {'available': True, 'error': None}

            return {
                'available': False,
                'error': f"Model '{model_name}' not found. Available models: {', '.join(available_model_ids) or 'none'}"
            }

        except _DialectMismatch:
            raise
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (404, 405):
                raise _DialectMismatch(f"/v1/models -> HTTP {e.response.status_code}") from e
            return _model_check_error(timeout, e)
        except Exception as e:
            return _model_check_error(timeout, e)
