"""
Model availability checker for Ollama and OpenAI-compatible endpoints.
"""
import asyncio
from typing import Any, Dict

import httpx

from app.config import settings
from app.services.circuit_breaker import CircuitBreakerError, model_checker_cb
from app.services.ssrf import assert_url_safe


class ModelCheckerError(Exception):
    """Exception raised for model checker errors."""
    pass


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


class ModelChecker:
    """Checks availability of embedding and chat models via Ollama or OpenAI-compatible APIs."""

    def __init__(self, timeout: float = 10.0):
        """
        Initialize the model checker.

        Args:
            timeout: Request timeout in seconds (default: 10.0)
        """
        self.timeout = timeout

    async def check_models(self) -> Dict[str, Any]:
        """
        Check availability of configured embedding and chat models.

        Automatically detects endpoint type (Ollama or OpenAI-compatible) and
        calls the appropriate API to verify configured models are available.

        Returns:
            Dictionary with 'embedding_model', 'chat_model', and
            'instant_chat_model' keys,
            each containing a dict with 'available' (bool) and 'error' (str or None).

        Example:
            {
                'embedding_model': {'available': True, 'error': None},
                'chat_model': {'available': False, 'error': 'Model not found'},
                'instant_chat_model': {'available': True, 'error': None}
            }
        """
        result = {
            'embedding_model': {'available': False, 'error': None},
            'chat_model': {'available': False, 'error': None},
            'instant_chat_model': {'available': False, 'error': None},
        }

        await asyncio.to_thread(assert_url_safe, settings.ollama_embedding_url)
        await asyncio.to_thread(assert_url_safe, settings.ollama_chat_url)
        await asyncio.to_thread(assert_url_safe, settings.instant_chat_url)

        # follow_redirects=False so a 30x from a model host cannot bypass the
        # SSRF guard by redirecting to a private/internal address.
        # SSRFSafeTransport re-validates the resolved IP at request time to close
        # the DNS-rebinding TOCTOU gap the startup-only guard leaves open.
        from app.services.ssrf_transport import SSRFSafeTransport

        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=False,
            transport=SSRFSafeTransport(),
        ) as client:
            # Check embedding model
            result['embedding_model'] = await self._check_model_availability(
                client,
                settings.ollama_embedding_url,
                settings.embedding_model
            )

            # Check chat model
            result['chat_model'] = await self._check_model_availability(
                client,
                settings.ollama_chat_url,
                settings.chat_model
            )

            # Check instant chat model
            result['instant_chat_model'] = await self._check_model_availability(
                client,
                settings.instant_chat_url,
                settings.instant_chat_model
            )

        return result

    def _detect_provider_type(self, base_url: str) -> str:
        """
        Detect whether the endpoint is Ollama, OpenAI-compatible, or native TEI.

        Detection rules (kept consistent with
        ``EmbeddingService._detect_provider_mode``):
        - explicit /api/tags path => Ollama
        - explicit /v1/models or /v1/embeddings path => OpenAI-compatible
        - explicit /embed path => native TEI
        - no explicit path + port 8080 => native TEI (TEI default)
        - no explicit path + port 1234/8000/5000/5001 => OpenAI-compatible
        - otherwise Ollama

        Args:
            base_url: The base URL of the endpoint

        Returns:
            'ollama', 'openai_compatible', or 'tei'
        """
        url_lower = base_url.lower().rstrip('/')

        # Check for explicit Ollama path
        if '/api/tags' in url_lower:
            return 'ollama'

        # Check for explicit OpenAI-compatible paths
        if '/v1/models' in url_lower or '/v1/embeddings' in url_lower:
            return 'openai_compatible'

        # Check for explicit native TEI path (POST /embed)
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
            # Also check if path is empty or just /
            if parsed.path in ('', '/') and not parsed.port:
                # No explicit path and no special port - check common port patterns
                if any(f':{p}' in parsed.netloc for p in OPENAI_COMPATIBLE_PORTS):
                    return 'openai_compatible'
        except (ValueError, AttributeError):
            # URL parsing failed, continue with default provider
            pass

        # Default to Ollama for backward compatibility
        return 'ollama'

    async def _check_model_availability(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        model_name: str
    ) -> Dict[str, Any]:
        """
        Check if a specific model is available at the given endpoint.
        Supports both Ollama and OpenAI-compatible endpoints.

        Args:
            client: httpx AsyncClient instance
            base_url: Base URL of the endpoint
            model_name: Name of the model to check

        Returns:
            Dictionary with 'available' (bool) and 'error' (str or None)
        """
        provider_type = self._detect_provider_type(base_url)

        if provider_type == 'tei':
            return await self._check_tei_model(client, base_url, model_name)
        elif provider_type == 'openai_compatible':
            return await self._check_openai_compatible_model(client, base_url, model_name)
        else:
            return await self._check_ollama_model(client, base_url, model_name)

    async def _check_ollama_model(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        model_name: str
    ) -> Dict[str, Any]:
        """
        Check model availability using Ollama's /api/tags endpoint.
        """
        url = f"{base_url.rstrip('/')}/api/tags"

        try:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()

            # Parse models from response
            models = data.get('models', [])
            if not isinstance(models, list):
                return {
                    'available': False,
                    'error': "Invalid response format: 'models' is not a list"
                }

            # Check if model name matches any available model
            # Model names in Ollama may include tags (e.g., "qwen2.5:32b")
            # We check for exact match or if model_name is a prefix
            available_model_names = [m.get('name', '') for m in models]

            for available_name in available_model_names:
                if available_name == model_name or available_name.startswith(f"{model_name}:"):
                    return {'available': True, 'error': None}

            return {
                'available': False,
                'error': f"Model '{model_name}' not found. Available models: {', '.join(available_model_names) or 'none'}"
            }

        except Exception as e:
            return _model_check_error(self.timeout, e)

    async def _check_tei_model(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        model_name: str
    ) -> Dict[str, Any]:
        """
        Check model availability using native TEI's /info endpoint.

        Native HuggingFace TEI serves a single model and exposes its identity at
        ``<root>/info`` as ``{"model_id": "...", ...}``. The /info route lives at
        the server root, not under the /embed route, so any trailing /embed is
        stripped before probing.
        """
        root = base_url.rstrip('/')
        if root.endswith('/embed'):
            root = root[: -len('/embed')]
        url = f"{root.rstrip('/')}/info"

        try:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()

            live_model_id = data.get('model_id', '') if isinstance(data, dict) else ''
            if not live_model_id:
                return {
                    'available': False,
                    'error': "TEI /info response missing 'model_id'"
                }

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

        except Exception as e:
            return _model_check_error(self.timeout, e)

    @model_checker_cb
    async def _check_openai_compatible_model(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        model_name: str
    ) -> Dict[str, Any]:
        """
        Check model availability using OpenAI-compatible /v1/models endpoint.
        """
        # Build the models URL - strip any existing v1 paths and add /v1/models
        url_lower = base_url.lower().rstrip('/')
        if '/v1/models' in url_lower:
            # URL already has the full path
            url = base_url
        elif '/v1/embeddings' in url_lower:
            # Replace embeddings with models
            url = base_url.rstrip('/').replace('/v1/embeddings', '/v1/models')
        else:
            # Append /v1/models to base URL
            url = f"{base_url.rstrip('/')}/v1/models"

        try:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()

            # Parse models from OpenAI-compatible response
            # Response format: {"data": [{"id": "model-name", ...}, ...]}
            models = data.get('data', [])
            if not isinstance(models, list):
                return {
                    'available': False,
                    'error': "Invalid response format: 'data' is not a list"
                }

            # Extract model IDs
            available_model_ids = [m.get('id', '') for m in models if isinstance(m, dict)]

            # Check for exact match or partial match
            for available_id in available_model_ids:
                if available_id == model_name or available_id.startswith(f"{model_name}:"):
                    return {'available': True, 'error': None}

            return {
                'available': False,
                'error': f"Model '{model_name}' not found. Available models: {', '.join(available_model_ids) or 'none'}"
            }

        except CircuitBreakerError as e:
            return {
                'available': False,
                'error': f"Circuit breaker open: {str(e)}"
            }
        except Exception as e:
            return _model_check_error(self.timeout, e)
