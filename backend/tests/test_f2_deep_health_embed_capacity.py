"""Issue #1009 acceptance checks — deep-health probe embedding capacity (frozen).

The deep-health probe (``EmbeddingService.embed_probe``) sends a single
scalar ``"ping"``: it proves the endpoint answers, not that it can embed
ingest-shaped traffic (a batch of chunk-sized inputs). Both checks are
arm's-length and frozen — they must pass unmodified once the probe sends a
configured batch (``embedding_probe_batch_count`` x
``embedding_probe_batch_chars`` synthetic chunk-sized inputs):

* C4 (DISCRIMINATING): when the provider answers a dialect-correct 200 for
  a scalar input but rejects the batch payload (424), the health check must
  report ``ok=False`` with an error. At base the probe sends the scalar,
  gets the 200 and reports healthy — the assertion fails for that reason.
* C5 (DISCRIMINATING): the probe request payload must be an ingest-shaped
  LIST of inputs (>= the configured count, each >= the configured char
  size). At base the payload's input value is the scalar string ``"ping"``
  so the list assertion fails.

Harness mirrors tests/test_issue494_llm_health.py (TEI-mode settings from
tests/test_embeddings_tei.py, httpx.MockTransport swapped in as the
service's persistent client, a fresh circuit breaker, assert_url_safe
patched) — single-level HTTP-layer doubles only.
"""

import contextlib
import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from app.services.circuit_breaker import AsyncCircuitBreaker
from app.services.embeddings import EmbeddingService
from app.services.llm_health import LLMHealthChecker

# Native TEI mode: bare :8080 URL resolves to POST /embed with an
# {"inputs": ...} payload and a raw JSON array-of-arrays response.
TEI_BASE_URL = "http://localhost:8080"

# Probe-batch configuration the fix consumes (Settings fields at fix time;
# harmless extra attributes on the settings namespace at base).
PROBE_BATCH_COUNT = 4
PROBE_BATCH_CHARS = 32


def _tei_settings_namespace() -> SimpleNamespace:
    """The TEI-mode settings object patched over app.services.embeddings."""
    return SimpleNamespace(
        ollama_embedding_url=TEI_BASE_URL,
        embedding_model="BAAI/bge-m3",
        embedding_doc_prefix="",
        embedding_query_prefix="",
        redis_url=None,
        embedding_cache_ttl_seconds=3600,
        embedding_batch_size=512,
        embedding_batch_max_chars=131072,
        embedding_probe_batch_count=PROBE_BATCH_COUNT,
        embedding_probe_batch_chars=PROBE_BATCH_CHARS,
    )


@contextlib.asynccontextmanager
async def _tei_embed_service(handler):
    """Yield an EmbeddingService whose HTTP client is a MockTransport.

    Patches follow the issue-#494 harness: settings namespace over the
    embeddings module, assert_url_safe bypassed, a fresh breaker.
    """
    with (
        patch(
            "app.services.embeddings.settings", _tei_settings_namespace()
        ) as mock_settings,
        patch("app.services.embeddings.assert_url_safe"),
        patch(
            "app.services.embeddings.embeddings_cb",
            AsyncCircuitBreaker(fail_max=50, reset_timeout=30, name="f2-probe"),
        ),
    ):
        service = EmbeddingService()
        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        service._client = mock_client
        try:
            yield service, mock_settings
        finally:
            await mock_client.aclose()


class TestEmbedProbeCapacityBlindSpot:
    """C4/C5: the deep-health probe must exercise ingest-shaped capacity."""

    async def test_deep_health_false_when_ping_passes_and_batch_fails(self):
        """C4: a provider that only accepts scalars is NOT healthy."""
        requests_seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_seen.append(request)
            body = json.loads(request.content)
            value = body.get("inputs", body.get("input", body.get("prompt")))
            if isinstance(value, list):
                # Batch capacity exceeded: dialect-correct client, server
                # refuses the ingest-shaped payload.
                return httpx.Response(
                    424, text="batch payload rejected", request=request
                )
            # A scalar ping always succeeds — the blind spot at base.
            return httpx.Response(200, json=[[0.5, 0.6]], request=request)

        async with _tei_embed_service(handler) as (service, _settings):
            checker = LLMHealthChecker(timeout=2.0, embedding_service=service)
            result = await checker.check_embeddings()

        assert requests_seen, "harness: the probe must have issued a request"
        assert result["ok"] is False, (
            "deep health must report ok=False when the provider accepts the "
            "scalar ping but rejects the ingest-shaped batch payload; got "
            f"{result!r} — the probe only exercised single-item capacity"
        )
        assert result["error"], (
            f"an unhealthy probe result must carry an error detail: {result!r}"
        )

    async def test_probe_sends_ingest_shaped_batch_payload(self):
        """C5: the probe payload must be a list of chunk-sized inputs."""

        payloads = []

        def handler(request: httpx.Request) -> httpx.Response:
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json=[[0.5, 0.6]], request=request)

        async with _tei_embed_service(handler) as (service, settings_ns):
            # The fix's Settings fields; at base they do not exist in code
            # and the in-test fallback default keeps the shape small.
            batch_count = int(
                getattr(settings_ns, "embedding_probe_batch_count", 4)
            )
            batch_chars = int(
                getattr(settings_ns, "embedding_probe_batch_chars", 32)
            )
            await service.embed_probe(2.0)

        assert payloads, "harness: the probe must have issued a request"
        payload = payloads[0]
        value = payload.get("inputs", payload.get("input", payload.get("prompt")))
        assert isinstance(value, list), (
            "the deep-health probe must send an ingest-shaped batch payload "
            f"(a list of inputs), got {value!r} in payload {payload!r}"
        )
        assert len(value) >= batch_count, (
            f"the probe batch must carry at least {batch_count} inputs, got "
            f"{len(value)}"
        )
        assert all(
            isinstance(text, str) and len(text) >= batch_chars for text in value
        ), (
            "every probe input must be chunk-sized (>= "
            f"{batch_chars} chars), got lengths "
            f"{[len(t) if isinstance(t, str) else type(t).__name__ for t in value]}"
        )
