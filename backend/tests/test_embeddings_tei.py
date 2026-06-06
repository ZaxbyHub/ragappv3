"""
Tests for native TEI (HuggingFace Text Embeddings Inference) provider mode.

Native TEI servers always expose ``POST /embed`` with an ``{"inputs": ...}``
payload and return a raw JSON array of embedding arrays. Only some TEI builds
additionally expose the OpenAI-compatible ``/v1/embeddings`` route, so a bare
``host:8080`` URL must resolve to the native ``/embed`` route rather than the
OpenAI path (which would 404 against a native-only deployment).
"""
import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Stub missing optional dependencies (mirrors sibling embedding test modules)
try:
    import lancedb  # noqa: F401
except ImportError:
    import types
    sys.modules['lancedb'] = types.ModuleType('lancedb')

try:
    import pyarrow  # noqa: F401
except ImportError:
    import types
    sys.modules['pyarrow'] = types.ModuleType('pyarrow')

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.circuit_breaker import CircuitBreakerError
from app.services.embeddings import EmbeddingError, EmbeddingService


@pytest.fixture
def mock_tei_settings():
    """Patch embedding settings with a native-TEI-style configuration.

    The SSRF guard in ``EmbeddingService.__init__`` resolves the host and
    rejects loopback addresses unless ``ALLOW_LOCAL_SERVICES=1``; we use a
    ``localhost`` URL (which always resolves) with that opt-in so detection
    can be exercised deterministically and offline.
    """
    prev = os.environ.get("ALLOW_LOCAL_SERVICES")
    os.environ["ALLOW_LOCAL_SERVICES"] = "1"
    try:
        with patch("app.services.embeddings.settings") as mock:
            mock.ollama_embedding_url = "http://localhost:8080"
            mock.embedding_model = "BAAI/bge-m3"
            mock.embedding_doc_prefix = ""
            mock.embedding_query_prefix = ""
            mock.embedding_batch_size = 512
            mock.embedding_batch_max_retries = 3
            mock.embedding_batch_min_sub_size = 1
            mock.embedding_concurrent_batches = 4
            mock.chunk_size_chars = 1200
            mock.chunk_overlap_chars = 120
            yield mock
    finally:
        if prev is None:
            os.environ.pop("ALLOW_LOCAL_SERVICES", None)
        else:
            os.environ["ALLOW_LOCAL_SERVICES"] = prev


def _mock_response(status_code, json_value):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_value
    resp.text = str(json_value)
    return resp


class TestTeiProviderDetection:
    """URL -> (provider_mode, embeddings_url) resolution for native TEI."""

    def test_bare_8080_resolves_to_native_embed(self, mock_tei_settings):
        mock_tei_settings.ollama_embedding_url = "http://localhost:8080"
        service = EmbeddingService()
        assert service.provider_mode == "tei"
        assert service.embeddings_url == "http://localhost:8080/embed"

    def test_bare_8080_trailing_slash_resolves_to_native_embed(self, mock_tei_settings):
        mock_tei_settings.ollama_embedding_url = "http://localhost:8080/"
        service = EmbeddingService()
        assert service.provider_mode == "tei"
        assert service.embeddings_url == "http://localhost:8080/embed"

    def test_explicit_embed_path_is_tei(self, mock_tei_settings):
        # An explicit /embed path on a non-default port must still select TEI.
        mock_tei_settings.ollama_embedding_url = "http://localhost:9000/embed"
        service = EmbeddingService()
        assert service.provider_mode == "tei"
        assert service.embeddings_url == "http://localhost:9000/embed"

    def test_explicit_v1_embeddings_on_8080_stays_openai(self, mock_tei_settings):
        # Regression guard: an explicit OpenAI path must NOT be hijacked by TEI
        # detection, even on the TEI default port.
        mock_tei_settings.ollama_embedding_url = "http://localhost:8080/v1/embeddings"
        service = EmbeddingService()
        assert service.provider_mode == "openai"
        assert service.embeddings_url == "http://localhost:8080/v1/embeddings"

    def test_port_1234_stays_openai(self, mock_tei_settings):
        mock_tei_settings.ollama_embedding_url = "http://localhost:1234"
        service = EmbeddingService()
        assert service.provider_mode == "openai"
        assert service.embeddings_url == "http://localhost:1234/v1/embeddings"

    def test_default_port_stays_ollama(self, mock_tei_settings):
        mock_tei_settings.ollama_embedding_url = "http://localhost:11434"
        service = EmbeddingService()
        assert service.provider_mode == "ollama"
        assert service.embeddings_url == "http://localhost:11434/api/embeddings"


class TestTeiPayloadShape:
    """Native TEI payloads use {"inputs": ...} with no model field."""

    def test_single_payload_uses_inputs(self, mock_tei_settings):
        service = EmbeddingService()
        assert service._build_payload("hello") == {"inputs": "hello"}


@pytest.mark.asyncio
class TestTeiRoundTrip:
    """End-to-end embed calls against a mocked native-TEI HTTP response."""

    async def test_embed_single_parses_array_response(self, mock_tei_settings):
        service = EmbeddingService()
        service._client = MagicMock()
        service._client.post = AsyncMock(
            return_value=_mock_response(200, [[0.1, 0.2, 0.3]])
        )

        vec = await service.embed_single("query text")

        assert vec == [0.1, 0.2, 0.3]
        args, kwargs = service._client.post.call_args
        assert args[0] == "http://localhost:8080/embed"
        assert kwargs["json"] == {"inputs": "query text"}

    async def test_embed_batch_parses_array_response(self, mock_tei_settings):
        service = EmbeddingService()
        service._client = MagicMock()
        service._client.post = AsyncMock(
            return_value=_mock_response(200, [[0.1, 0.2], [0.3, 0.4]])
        )

        out = await service.embed_batch(["a", "b"], batch_size=10)

        assert out == [[0.1, 0.2], [0.3, 0.4]]
        args, kwargs = service._client.post.call_args
        assert args[0] == "http://localhost:8080/embed"
        assert kwargs["json"] == {"inputs": ["a", "b"]}

    async def test_embed_single_rejects_empty_array(self, mock_tei_settings):
        service = EmbeddingService()
        service._client = MagicMock()
        service._client.post = AsyncMock(return_value=_mock_response(200, []))

        with pytest.raises(EmbeddingError):
            await service.embed_single("x")

    async def test_embed_single_rejects_non_array_response(self, mock_tei_settings):
        service = EmbeddingService()
        service._client = MagicMock()
        service._client.post = AsyncMock(
            return_value=_mock_response(200, {"data": [{"embedding": [0.1]}]})
        )

        with pytest.raises(EmbeddingError):
            await service.embed_single("x")

    async def test_embed_passage_applies_doc_prefix_in_tei_mode(self, mock_tei_settings):
        # LC5-07: the document prefix must be prepended to the TEI inputs payload.
        mock_tei_settings.embedding_doc_prefix = "PASSAGE: "
        service = EmbeddingService()
        service._client = MagicMock()
        service._client.post = AsyncMock(
            return_value=_mock_response(200, [[0.5, 0.6]])
        )

        vec = await service.embed_passage("hello")

        assert vec == [0.5, 0.6]
        assert service._client.post.call_args.kwargs["json"] == {
            "inputs": "PASSAGE: hello"
        }

    async def test_embed_batch_rejects_non_list_response(self, mock_tei_settings):
        # LC5-03: TEI batch guard — a non-list (e.g. error object) must raise.
        service = EmbeddingService()
        service._client = MagicMock()
        service._client.post = AsyncMock(
            return_value=_mock_response(200, {"error": "boom"})
        )

        with pytest.raises(EmbeddingError):
            await service.embed_batch(["a", "b"], batch_size=10)

    async def test_embed_batch_non_200_raises(self, mock_tei_settings):
        # LC5-01: a non-200 from a TEI batch call surfaces as EmbeddingError.
        service = EmbeddingService()
        service._client = MagicMock()
        service._client.post = AsyncMock(return_value=_mock_response(503, []))

        with pytest.raises(EmbeddingError):
            await service.embed_batch(["a"], batch_size=10)

    async def test_embed_single_timeout_wrapped(self, mock_tei_settings):
        # LC5-02: httpx.TimeoutException -> EmbeddingError (passthrough breaker).
        service = EmbeddingService()
        service._client = MagicMock()
        service._client.post = AsyncMock(side_effect=httpx.TimeoutException("t"))

        with patch("app.services.embeddings.embeddings_cb", lambda fn: fn):
            with pytest.raises(EmbeddingError):
                await service.embed_single("x")

    async def test_embed_single_http_error_wrapped(self, mock_tei_settings):
        # LC5-02: httpx.HTTPError -> EmbeddingError.
        service = EmbeddingService()
        service._client = MagicMock()
        service._client.post = AsyncMock(side_effect=httpx.HTTPError("boom"))

        with patch("app.services.embeddings.embeddings_cb", lambda fn: fn):
            with pytest.raises(EmbeddingError):
                await service.embed_single("x")

    async def test_embed_single_circuit_breaker_open_wrapped(self, mock_tei_settings):
        # F-C10 regression: an open circuit breaker must surface as EmbeddingError
        # from the single-embed path (parity with the batch path), not as a raw
        # CircuitBreakerError.
        service = EmbeddingService()
        service._client = MagicMock()
        service._client.post = AsyncMock(return_value=_mock_response(200, [[0.1]]))

        def open_breaker(_fn):
            async def _raise(*args, **kwargs):
                raise CircuitBreakerError("circuit open")
            return _raise

        with patch("app.services.embeddings.embeddings_cb", open_breaker):
            with pytest.raises(EmbeddingError) as exc:
                await service.embed_single("x")
        assert "circuit breaker" in str(exc.value).lower()


class TestTeiHttpsDetection:
    """LC5-08: HTTPS native-TEI URLs resolve to the tei mode and /embed route."""

    def test_https_bare_8080_resolves_to_native_embed(self, mock_tei_settings):
        mock_tei_settings.ollama_embedding_url = "https://localhost:8080"
        service = EmbeddingService()
        assert service.provider_mode == "tei"
        assert service.embeddings_url == "https://localhost:8080/embed"
