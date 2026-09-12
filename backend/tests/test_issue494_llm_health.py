"""Issue #494 acceptance checks — AC3 / OPS-004 and AC2 / OPS-003 (DISCRIMINATING).

Root causes (verified at base a543361), both in
``LLMHealthChecker.check_embeddings`` (llm_health.py ~64-75):

* AC3 (timeout ignored): the probe mutates ``service.timeout`` but the
  persistent httpx client was built ONCE with 60s in
  ``EmbeddingService.__init__`` and ignores that attribute, so a provider
  that never responds hangs the probe until the 60s client timeout instead
  of the health checker's short timeout.
* AC2 (cache bypass): the probe calls ``embed_single("ping")``, which hits
  the L1 LRU cache; a warm "ping" entry from a healthy past makes the
  health check report ok=True forever even after the provider goes down.

Both nodes use single-level mocks of the httpx layer (``httpx.MockTransport``
or a real ``httpx.AsyncHTTPTransport`` against a local loopback server) —
never ``embed_single`` itself. Fully offline and deterministic; AC3 bounds
wall-clock with ``time.monotonic`` and uses an ``asyncio.sleep``-based
server, never ``time.sleep``.
"""

import asyncio
import contextlib
import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

import httpx

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import embeddings as embeddings_module
from app.services.circuit_breaker import AsyncCircuitBreaker
from app.services.embeddings import EmbeddingService
from app.services.llm_health import LLMHealthChecker

MOCK_HOST = "http://embed-test-host:11434"


def _mock_embedding_settings(url: str = MOCK_HOST) -> MagicMock:
    settings = MagicMock()
    settings.ollama_embedding_url = url
    settings.embedding_model = "nomic-embed-text"
    settings.embedding_doc_prefix = ""
    settings.embedding_query_prefix = ""
    settings.redis_url = None
    settings.embedding_cache_ttl_seconds = 3600
    return settings


class _PassthroughTransport:
    """Stand-in for SSRFSafeTransport: pure delegation, no SSRF blocking.

    Lets tests point the real httpx.AsyncClient (real httpcore stack, real
    timeout enforcement) at a local loopback server without the SSRF guard
    rejecting the loopback address.
    """

    def __init__(self, transport=None, **_kwargs):
        self._transport = transport

    async def handle_async_request(self, request):
        return await self._transport.handle_async_request(request)

    async def aclose(self):
        await self._transport.aclose()


class TestAC3HealthProbeHonorsTimeout(unittest.IsolatedAsyncioTestCase):
    """LLMHealthChecker(timeout=2.0) probe must fail within ~2s on a dead-slow provider."""

    async def test_probe_returns_unhealthy_within_checker_timeout(self):
        # A local loopback server that accepts the request and only responds
        # after 5.5s — far beyond the 2.0s health-check timeout, far below the
        # 60s persistent-client timeout. No external network involved.
        server_delay = 5.5

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            try:
                await reader.read(65536)
                await asyncio.sleep(server_delay)
                body = b'{"embedding": [0.1, 0.2]}'
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    b"Content-Length: "
                    + str(len(body)).encode()
                    + b"\r\nConnection: keep-alive\r\n\r\n"
                    + body
                )
                await writer.drain()
                while await reader.read(65536):
                    pass
            except (ConnectionError, asyncio.CancelledError):
                pass
            finally:
                try:
                    writer.close()
                except Exception:
                    pass

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        async def _close_server():
            server.close()
            try:
                await asyncio.wait_for(server.wait_closed(), timeout=2)
            except Exception:
                pass

        self.addAsyncCleanup(_close_server)

        # Patches stay active for the WHOLE test (ExitStack closed via
        # addCleanup) so the probe itself runs against the loopback URL, not
        # the live default settings.
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(
            patch(
                "app.services.embeddings.settings",
                _mock_embedding_settings(f"http://127.0.0.1:{port}"),
            )
        )
        stack.enter_context(patch("app.services.embeddings.assert_url_safe"))
        stack.enter_context(
            patch("app.services.ssrf_transport.SSRFSafeTransport", _PassthroughTransport)
        )
        stack.enter_context(
            patch(
                "app.services.embeddings.embeddings_cb",
                AsyncCircuitBreaker(fail_max=5, reset_timeout=30, name="emb-ac3"),
            )
        )
        service = EmbeddingService()
        self.addAsyncCleanup(service._client.aclose)

        original_service_timeout = service.timeout
        original_client_timeout = service._client.timeout

        checker = LLMHealthChecker(timeout=2.0, embedding_service=service)
        started = time.monotonic()
        result = await checker.check_embeddings()
        elapsed = time.monotonic() - started

        # PRESERVING: the probe must not permanently mutate service.timeout
        # (restored) nor leave the running client with a mutated timeout.
        self.assertEqual(service.timeout, original_service_timeout)
        self.assertEqual(
            service._client.timeout,
            original_client_timeout,
            "probe must not permanently mutate the persistent client timeout",
        )
        print("PRESERVING GREEN: service.timeout restored, client timeout untouched")

        # DISCRIMINATING: a never-responding-in-time provider must be
        # reported unhealthy within the checker timeout (~2s), not hang until
        # the 60s persistent-client timeout. At base the mutated
        # service.timeout is ignored by the already-built client, the probe
        # waits out the server's 5.5s delay, returns a healthy embedding and
        # ok=True -> RED.
        print("AC3 CHECK: FAIL", flush=True)
        self.assertFalse(
            result["ok"],
            f"a provider slower than the health timeout must report ok=False, "
            f"got {result} after {elapsed:.2f}s",
        )
        self.assertLess(
            elapsed,
            4.0,
            f"check_embeddings(timeout=2.0) must return within ~2s, took "
            f"{elapsed:.2f}s (persistent client ignored the probe timeout)",
        )


class TestAC2HealthProbeBypassesCache(unittest.IsolatedAsyncioTestCase):
    """A warm 'ping' cache entry must not mask a provider that just went down."""

    def setUp(self):
        self._settings_patcher = patch(
            "app.services.embeddings.settings", _mock_embedding_settings()
        )
        self._settings_patcher.start()
        self._ssrf_patcher = patch("app.services.embeddings.assert_url_safe")
        self._ssrf_patcher.start()
        self._breaker_patcher = patch(
            "app.services.embeddings.embeddings_cb",
            AsyncCircuitBreaker(fail_max=50, reset_timeout=30, name="emb-ac2"),
        )
        self._breaker_patcher.start()

    def tearDown(self):
        self._breaker_patcher.stop()
        self._ssrf_patcher.stop()
        self._settings_patcher.stop()

    async def test_probe_bypasses_warm_ping_cache_and_detects_outage(self):
        mode = {"state": "healthy"}
        http_calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            http_calls["n"] += 1
            if mode["state"] == "healthy":
                return httpx.Response(
                    200, json={"embedding": [0.5, 0.6]}, request=request
                )
            return httpx.Response(503, text="Service Unavailable", request=request)

        service = EmbeddingService()
        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(mock_client.aclose)
        service._client = mock_client

        # Warm the L1 cache for the exact probe text.
        embedding = await service.embed_single("ping")
        self.assertEqual(embedding, [0.5, 0.6])
        calls_after_warm = http_calls["n"]
        self.assertGreaterEqual(calls_after_warm, 1)

        # Provider goes down AFTER the cache was warmed.
        mode["state"] = "down"

        checker = LLMHealthChecker(timeout=2.0, embedding_service=service)
        result = await checker.check_embeddings()

        # DISCRIMINATING: the probe must bypass the warm cache and observe
        # the live 503. At base embed_single("ping") returns the cached
        # vector, no HTTP is issued, and the check reports ok=True -> RED.
        print("AC2 CHECK: FAIL", flush=True)
        self.assertFalse(
            result["ok"],
            f"health probe must not report ok from a stale cache entry while "
            f"the provider is down, got {result}",
        )
        self.assertGreater(
            http_calls["n"],
            calls_after_warm,
            "health probe must issue a live HTTP request, not serve the warm "
            "'ping' cache entry",
        )
