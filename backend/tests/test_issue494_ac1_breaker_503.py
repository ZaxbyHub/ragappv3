"""Issue #494 acceptance check — AC1 / OPS-002 (DISCRIMINATING).

Root cause (verified at base a543361): ``EmbeddingService._embed_with_prefix``
(embeddings.py ~592-602) and ``_embed_batch_with_retry`` (~1075-1098) wrap ONLY
the HTTP POST in the circuit breaker::

    response = await embeddings_cb(self._client.post)(config.url, json=...)

An HTTP 503 response RETURNS normally from that wrapped call, so the breaker
records a SUCCESS; the ``status_code != 200 -> EmbeddingError`` raise happens
outside the wrap. ``fail_max`` consecutive 503s therefore never open the
embeddings breaker and every embed call keeps issuing a real HTTP request.

This node pins two things:

* PRESERVING (green at base): a transport-level exception raised inside the
  wrapped POST still trips the breaker — ``fail_max`` consecutive
  ``httpx.ConnectError`` embeds leave the breaker OPEN.
* DISCRIMINATING (RED at base): ``fail_max`` consecutive HTTP 503 responses
  must (a) leave the breaker state OPEN and (b) make the NEXT embed call fail
  fast WITHOUT issuing an HTTP request (transport call count unchanged).

Everything is offline and deterministic: the service's httpx client is swapped
for an ``httpx.AsyncClient`` backed by ``httpx.MockTransport``.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

import httpx

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import embeddings as embeddings_module
from app.services.circuit_breaker import AsyncCircuitBreaker, CircuitBreakerState
from app.services.embeddings import EmbeddingError, EmbeddingService

EMBED_URL = "http://embed-test-host:11434"


def _mock_embedding_settings() -> MagicMock:
    settings = MagicMock()
    settings.ollama_embedding_url = EMBED_URL
    settings.embedding_model = "nomic-embed-text"
    settings.embedding_doc_prefix = ""
    settings.embedding_query_prefix = ""
    settings.redis_url = None
    settings.embedding_cache_ttl_seconds = 3600
    return settings


class TestAC1BreakerOpensOn503(unittest.IsolatedAsyncioTestCase):
    """fail_max consecutive 503s must open the embeddings breaker (OPS-002)."""

    def setUp(self):
        self._settings_patcher = patch(
            "app.services.embeddings.settings", _mock_embedding_settings()
        )
        self._settings_patcher.start()
        self._ssrf_patcher = patch("app.services.embeddings.assert_url_safe")
        self._ssrf_patcher.start()

    def tearDown(self):
        self._ssrf_patcher.stop()
        self._settings_patcher.stop()

    async def test_503_responses_open_breaker_and_next_call_short_circuits(self):
        prod_fail_max = embeddings_module.embeddings_cb.fail_max

        service = EmbeddingService()

        http_calls = {"n": 0}

        def transport_503(request: httpx.Request) -> httpx.Response:
            http_calls["n"] += 1
            return httpx.Response(503, text="Service Unavailable", request=request)

        def transport_refused(request: httpx.Request) -> httpx.Response:
            http_calls["n"] += 1
            raise httpx.ConnectError("connection refused", request=request)

        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(transport_503))
        self.addAsyncCleanup(mock_client.aclose)
        service._client = mock_client

        # ------------------------------------------------------------------
        # PRESERVING sub-assert: transport-exception trip still works.
        # The wrapped POST raising httpx.ConnectError is recorded as a
        # breaker failure; fail_max consecutive failures open the breaker.
        # ------------------------------------------------------------------
        breaker_transport = AsyncCircuitBreaker(
            fail_max=prod_fail_max, reset_timeout=30, name="embeddings-ac1-transport"
        )
        mock_client._transport = httpx.MockTransport(transport_refused)
        with patch("app.services.embeddings.embeddings_cb", breaker_transport):
            for i in range(prod_fail_max):
                with self.assertRaises(EmbeddingError):
                    await service.embed_single(f"transport probe {i}")
        self.assertEqual(
            breaker_transport.current_state,
            CircuitBreakerState.OPEN,
            "PRESERVING: transport exceptions must still trip the breaker",
        )
        print(
            f"PRESERVING GREEN: transport-exception trip opened breaker after "
            f"{prod_fail_max} failures",
            flush=True,
        )

        # ------------------------------------------------------------------
        # DISCRIMINATING: HTTP 503 responses must open the breaker too.
        # At base the wrapped POST returns the 503 Response normally, the
        # breaker records a SUCCESS, and the raise happens outside the wrap
        # -> breaker stays CLOSED -> this node is RED at base.
        # ------------------------------------------------------------------
        breaker_503 = AsyncCircuitBreaker(
            fail_max=prod_fail_max, reset_timeout=30, name="embeddings-ac1-503"
        )
        mock_client._transport = httpx.MockTransport(transport_503)
        with patch("app.services.embeddings.embeddings_cb", breaker_503):
            for i in range(prod_fail_max):
                with self.assertRaises(EmbeddingError) as ctx:
                    await service.embed_single(f"503 probe {i}")
                self.assertIn("503", str(ctx.exception))

            print("AC1 CHECK: FAIL", flush=True)
            self.assertEqual(
                breaker_503.current_state,
                CircuitBreakerState.OPEN,
                f"{prod_fail_max} consecutive HTTP 503 embed failures must leave "
                f"the embeddings circuit breaker OPEN (got "
                f"{breaker_503.current_state})",
            )

            # Breaker OPEN -> next embed call must short-circuit WITHOUT HTTP.
            calls_before = http_calls["n"]
            with self.assertRaises(EmbeddingError) as ctx:
                await service.embed_single("post-open probe")
            self.assertIn(
                "circuit breaker",
                str(ctx.exception).lower(),
                "short-circuited embed must surface the open breaker",
            )
            self.assertEqual(
                http_calls["n"],
                calls_before,
                "OPEN breaker must reject the next embed without issuing HTTP "
                f"(issued {http_calls['n'] - calls_before} extra request(s))",
            )
