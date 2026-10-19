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
from typing import Any, Dict, Optional

import httpx

from app.config import settings
from app.services.circuit_breaker import (
    BreakerNeutralError,
    CircuitBreakerError,
    is_outage_status,
    model_checker_cb,
)
from app.services.ssrf import assert_url_safe
from app.services.telemetry import correlation_headers as _correlation_headers


class ModelCheckerError(Exception):
    """Exception raised for model checker errors."""
    pass


class _DialectMismatch(Exception):
    """Internal: endpoint answered but does not speak the probed dialect.

    Raised for HTTP 404/405, a non-JSON body, or JSON without the dialect's
    expected shape — signals that the fallback chain should try the next
    dialect rather than report the service down.
    """


class _ProbeFailureResult(dict):
    """An unavailable probe result caused by a transport/host failure.

    Produced only by :func:`_model_check_error` (timeouts, connection
    errors, non-404/405 HTTP errors; HTTP errors additionally carry a
    ``status_code`` key so the breaker wrapper can classify them).
    Outage-class failures (5xx/429/timeouts/connection errors) charge the
    probe circuit breaker (FU-004, issue #494); non-outage HTTP 4xx
    results (a 401 from a wrong key, for example) are NEUTRAL — they
    would fail identically on every retry, so they neither charge the
    breaker nor erase its accumulated failure evidence (they pass through
    as :class:`BreakerNeutralError`); listing-based "model not found"
    results are authoritative answers from a healthy endpoint and must
    not open the breaker either. Serializes/compares exactly like a plain
    dict.
    """


class _ProbeFailure(Exception):
    """Internal: re-surfaces a swallowed probe failure for the breaker.

    ``AsyncCircuitBreaker`` records a failure only when the wrapped
    operation raises; the ``_check_*`` helpers swallow transport errors
    into ``_ProbeFailureResult`` dicts, so the breaker-wrapped outer call
    re-raises them as this exception type.
    """

    def __init__(self, result: Dict[str, Any]):
        super().__init__(result.get("error") or "model probe failed")
        self.result = result


def _model_check_error(timeout: float, exc: BaseException) -> Dict[str, Any]:
    """Map a model-probe exception to the standard unavailable-result dict.

    Shared by all three model-check methods so the error-handling tail stays
    identical (TimeoutException / HTTPStatusError / RequestError /
    ValueError|TypeError|RuntimeError). Results are ``_ProbeFailureResult``
    dicts so the probe circuit breaker can distinguish transport failures
    from listing-based "model not found" answers.
    """
    if isinstance(exc, httpx.TimeoutException):
        return _ProbeFailureResult({'available': False, 'error': f"Request timed out after {timeout}s"})
    if isinstance(exc, httpx.HTTPStatusError):
        return _ProbeFailureResult({
            'available': False,
            'error': f"HTTP error {exc.response.status_code}: {exc.response.text}",
            # Structured status so the breaker wrapper can classify without
            # parsing the message (issue #717).
            'status_code': exc.response.status_code,
        })
    if isinstance(exc, httpx.RequestError):
        return _ProbeFailureResult({'available': False, 'error': f"Request failed: {str(exc)}"})
    if isinstance(exc, (ValueError, TypeError, RuntimeError)):
        return _ProbeFailureResult({'available': False, 'error': f"Unexpected error: {str(exc)}"})
    # Should not reach here for the documented exception set; re-raise to avoid
    # silently swallowing an unexpected error type.
    raise exc


# URL suffixes that identify a specific API route rather than a server root.
# Stripping them (repeatedly, so e.g. "/v1/embeddings" behind a proxy prefix
# also resolves) yields the base every dialect appends its own path to.
# Longest-match-first matters within the loop's first-match semantics: the
# loop breaks the strip cycle per iteration at the FIRST suffix that matches,
# so '/api/embed' must precede '/embed' (else "…/api/embed" over-strips to
# "…/api"), and '/api/embeddings'/'/api/embed' both precede '/embed'.
_KNOWN_ROUTE_SUFFIXES = (
    '/v1/embeddings',
    '/v1/models',
    '/api/tags',
    '/api/embeddings',
    '/api/embed',
    '/embed',
)


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
        # Empty chat URLs are the designed unconfigured state (issue #570):
        # they short-circuit to 'not_configured' inside
        # _check_model_availability, so exclude them from the SSRF gate —
        # assert_url_safe rejects empty strings and would crash the deep
        # health probe on a fresh install.
        urls = tuple(
            u
            for u in (
                settings.ollama_embedding_url,
                settings.ollama_chat_url,
                settings.instant_chat_url,
            )
            if u
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
            # Keyed remote providers reject unauthenticated /v1/models probes
            # with 401; send the configured key per endpoint so deep health
            # reflects the endpoint, not a missing header (issue #717 — the
            # setup-wizard sibling probe already did this). The instant
            # endpoint falls back to the chat key, mirroring the runtime
            # client's instant-key-with-thinking-fallback (issue #622).
            chat_headers = (
                {"Authorization": f"Bearer {settings.chat_api_key}"}
                if settings.chat_api_key
                else None
            )
            effective_instant_key = (
                settings.instant_api_key or settings.chat_api_key
            )
            instant_headers = (
                {"Authorization": f"Bearer {effective_instant_key}"}
                if effective_instant_key
                else None
            )
            embedding_result, chat_result, instant_result = await asyncio.gather(
                self._check_model_availability(
                    client, settings.ollama_embedding_url, settings.embedding_model
                ),
                self._check_model_availability(
                    client,
                    settings.ollama_chat_url,
                    settings.chat_model,
                    headers=chat_headers,
                ),
                self._check_model_availability(
                    client,
                    settings.instant_chat_url,
                    settings.instant_chat_model,
                    headers=instant_headers,
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

    async def check_endpoint(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        model_name: str,
    ) -> Dict[str, Any]:
        """Probe one operator-supplied endpoint for the setup wizard (issue #622).

        Maps the availability result onto the wizard's three-state contract —
        ``ok`` / ``unreachable`` (transport or host failure) / ``model_mismatch``
        (endpoint answered, model not served). Empty url/model never reach
        here (the route 422s them first), so ``not_configured`` cannot surface
        for a just-filled endpoint (AC3).

        The caller supplies the client: a probe of NOT-yet-saved operator
        values may carry an Authorization header (keyed remote providers)
        that the persisted-settings checks must not assume.
        """
        result = await self._check_model_availability(client, base_url, model_name)
        if result.get("available"):
            return {"status": "ok", "detail": "the model is available"}
        if isinstance(result, _ProbeFailureResult):
            return {
                "status": "unreachable",
                "detail": result.get("error") or "endpoint unreachable",
            }
        return {
            "status": "model_mismatch",
            "detail": result.get("error") or "the model is not served",
        }

    async def _check_model_availability(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        model_name: str,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        Check if a model is available at the given endpoint, trying each API
        dialect in turn until one answers.

        Args:
            client: Shared SSRF-safe HTTP client.
            base_url: Configured endpoint base URL.
            model_name: Model name to look for.
            headers: Optional auth headers for the endpoint's probes (the
                configured provider API key, issue #717) merged with any
                telemetry correlation headers.

        Returns:
            Dictionary with 'available' (bool) and 'error' (str or None);
            an empty URL/model pair short-circuits to status 'not_configured'
            (issue #570 — distinct from unreachable, and never probed).
        """
        if not base_url or not model_name:
            return {
                "available": False,
                "status": "not_configured",
                "error": "endpoint not configured (set it in Settings -> Models)",
            }
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

            async def _breaker_probe() -> Any:
                """Run one dialect probe through the model-checker breaker.

                FU-004 (issue #494): consecutive provider failures open the
                circuit so probe storms during outages fail fast
                (restore-not-delete decision; matches the reranking_cb
                precedent). The breaker wraps this OUTER per-dialect call
                because the inner ``_check_*`` helpers swallow
                HTTPStatusError/ConnectError into ``_model_check_error``
                RESULT dicts — an inner wrap would never record a failure.
                Swallowed failures are re-surfaced here as exceptions (the
                breaker records failures only on exceptions), while
                wrong-dialect answers pass through as values so the chain's
                normal fallback never charges the breaker. Non-outage HTTP
                4xx failures (auth/config errors such as a 401 from a wrong
                key) also pass through as values — they would fail
                identically on every retry and must NOT trip the breaker
                (OPS-002 policy, issue #717).
                """
                try:
                    result = await checker(client, base_url, model_name, timeout, headers)
                except _DialectMismatch as mismatch:
                    return mismatch
                if isinstance(result, _ProbeFailureResult):
                    status_code = result.get('status_code')
                    if (
                        isinstance(status_code, int)
                        and status_code >= 400
                        and not is_outage_status(status_code)
                    ):
                        # Non-outage 4xx (auth/config error): NEUTRAL — the
                        # old `return result` made the breaker record a
                        # SUCCESS, erasing accumulated outage evidence for
                        # the host (issue #717 review).
                        neutral = BreakerNeutralError(
                            _ProbeFailure(result), result=result
                        )
                        raise neutral
                    raise _ProbeFailure(result)
                return result

            try:
                # Per-host breaker (issue #717): failures on this endpoint's
                # host charge this host's breaker only; other hosts — and
                # the setup wizard's operator-typed URL — are isolated.
                outcome = await model_checker_cb.breaker_for(base_url).call(
                    _breaker_probe
                )
            except _ProbeFailure as e:
                result = e.result
            except BreakerNeutralError as e:
                # Non-outage 4xx: authoritative failure answer from a
                # reachable-but-misconfigured endpoint; recorded on neither
                # side of the breaker (issue #717 review).
                result = e.result
            except CircuitBreakerError as e:
                # A tripped breaker means the host has failed consecutively;
                # treat it as "this dialect unusable, try the next" so an
                # open breaker never wedges the whole chain.
                mismatch_notes.append(f"{dialect}: circuit breaker open ({e})")
                continue
            except Exception as e:
                return _model_check_error(timeout, e)
            else:
                if isinstance(outcome, _DialectMismatch):
                    mismatch_notes.append(f"{dialect}: {outcome}")
                    continue
                result = outcome

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
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Check model availability using Ollama's /api/tags endpoint."""
        url = f"{_derive_base(base_url)}/api/tags"

        try:
            _corr = _correlation_headers()
            probe_headers = {**(_corr or {}), **(headers or {})} or None
            if probe_headers:
                response = await client.get(
                    url, timeout=timeout, headers=probe_headers
                )
            else:
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
        headers: Optional[Dict[str, str]] = None,
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
            _corr = _correlation_headers()
            probe_headers = {**(_corr or {}), **(headers or {})} or None
            if probe_headers:
                response = await client.get(url, timeout=timeout, headers=probe_headers)
            else:
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
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        Check model availability using the OpenAI-compatible /v1/models listing.
        """
        url = f"{_derive_base(base_url)}/v1/models"

        try:
            _corr = _correlation_headers()
            probe_headers = {**(_corr or {}), **(headers or {})} or None
            if probe_headers:
                response = await client.get(url, timeout=timeout, headers=probe_headers)
            else:
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
