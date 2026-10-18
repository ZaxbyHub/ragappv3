"""Issue #717 acceptance checks — AC3 / AC4 (model checker).

Both checks are DISCRIMINATING. RED at base (master), GREEN after the fix.

* AC3 (per-host breaker isolation): ``circuit_breaker.model_checker_cb`` is
  ONE module-level breaker shared by every host the model checker probes.
  ``fail_max`` consecutive probe failures against host A open the shared
  circuit, so host B's next probe is rejected by ``CircuitBreakerError`` on
  every dialect and a healthy host B — whose ``/v1/models`` lists the model —
  is reported ``available == False``. One dead backend therefore masks the
  health of every other backend.

* AC4 (chat probe Authorization): ``ModelChecker.check_models`` never sends
  ``settings.chat_api_key``, unlike the sibling setup-wizard probe
  (``app/api/routes/settings.py`` ``probe_model_endpoint``, ~line 2075)
  which DOES build its client with ``Authorization: Bearer <key>``. A keyed
  remote chat provider therefore rejects the deep-health probe as
  unauthenticated.

Offline and deterministic: both checks drive ``httpx.MockTransport`` (the
AC3 check reuses the ``_check_model_availability`` + MockTransport pattern
of ``tests/test_issue494_model_checker.py``).
"""

import contextlib
import os
import sys
import unittest
from unittest.mock import patch

import httpx

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import settings
from app.services import circuit_breaker as circuit_breaker_module
from app.services.model_checker import ModelChecker

HOST_A = "http://host-a-test:11434"
HOST_B = "http://host-b-test:1234/v1/models"
CHAT_HOST = "http://chat-test-host:11434"


class TestPerHostBreakerIsolation(unittest.IsolatedAsyncioTestCase):
    """AC3 (issue #717): probe-breaker failures must be isolated per host."""

    async def test_failing_host_does_not_block_other_host(self):
        """AC3: WHEN model probes against host A fail ``fail_max``
        consecutive times THE SYSTEM SHALL still send a probe for host B,
        whose ``/v1/models`` lists the model, and report ``available == True``
        for it.

        RED at base (master), GREEN after the fix.
        """
        # Reset the shared production breaker so the check is deterministic
        # (same setup/cleanup discipline as TestAC30ModelCheckerBreakerWiring).
        # If the fix later keys breakers per host through a registry, this
        # reset stays: it clears whatever module-level breaker exists today.
        circuit_breaker_module.model_checker_cb.reset()
        self.addCleanup(circuit_breaker_module.model_checker_cb.reset)
        fail_max = circuit_breaker_module.model_checker_cb.fail_max
        self.assertGreaterEqual(fail_max, 1)

        host_a_calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "host-a-test":
                host_a_calls["n"] += 1
                raise httpx.ConnectError(
                    "connection refused (host A down)", request=request
                )
            # Host B: OpenAI-compatible /v1/models listing that serves the
            # model — a healthy endpoint.
            return httpx.Response(
                200, json={"data": [{"id": "model-b"}]}, request=request
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        checker = ModelChecker(timeout=1.0, fallback_timeout=1.0)

        # Open the breaker via host-A failures: fail_max consecutive probes,
        # each a single transport-level GET (transport errors are
        # authoritative and do not fall through to other dialects).
        for _ in range(fail_max):
            result_a = await checker._check_model_availability(
                client, HOST_A, "model-a"
            )
            self.assertFalse(result_a["available"])

        # Host B is a different, healthy host serving the model: its probe
        # must not be short-circuited by host A's failures.
        result_b = await checker._check_model_availability(client, HOST_B, "model-b")
        self.assertTrue(
            result_b.get("available"),
            "host B's /v1/models lists the model, so its probe must report "
            f"available=True even after host A failed {fail_max} consecutive "
            f"times (host B was blocked by host A's failures: {result_b})",
        )


class TestCheckModelsAuthHeader(unittest.IsolatedAsyncioTestCase):
    """AC4 (issue #717): check_models must authenticate the chat probe."""

    async def test_check_models_sends_configured_api_key(self):
        """AC4: WHEN ``ModelChecker.check_models`` probes the chat endpoint
        while ``settings.chat_api_key`` is non-empty THE SYSTEM SHALL send an
        ``Authorization: Bearer <key>`` header on that endpoint's probe
        requests.

        RED at base (master), GREEN after the fix.
        """
        circuit_breaker_module.model_checker_cb.reset()
        self.addCleanup(circuit_breaker_module.model_checker_cb.reset)

        chat_host_requests: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            chat_host_requests.append(request)
            return httpx.Response(
                200, json={"models": [{"name": "chat-model-x"}]}, request=request
            )

        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        # Only the chat endpoint is configured, so every recorded request is
        # a chat-endpoint probe (embedding/instant short-circuit to
        # not_configured without touching HTTP).
        stack.enter_context(patch.object(settings, "ollama_embedding_url", ""))
        stack.enter_context(patch.object(settings, "ollama_chat_url", CHAT_HOST))
        stack.enter_context(patch.object(settings, "instant_chat_url", ""))
        stack.enter_context(patch.object(settings, "chat_model", "chat-model-x"))
        stack.enter_context(patch.object(settings, "chat_api_key", "k-test"))
        # CHAT_HOST is a fake hostname: the real SSRF gate would try to
        # resolve it via DNS. Patched to a no-op for the same reason the
        # wizard-probe tests stand in for the transport wholesale.
        stack.enter_context(patch("app.services.model_checker.assert_url_safe"))
        # check_models imports SSRFSafeTransport from app.services.ssrf_transport
        # INSIDE the method, so the patch target is the source module (the
        # repo precedent in tests/test_issue494_llm_health.py). check_models
        # calls SSRFSafeTransport() with no arguments.
        stack.enter_context(
            patch(
                "app.services.ssrf_transport.SSRFSafeTransport",
                lambda *args, **kwargs: httpx.MockTransport(handler),
            )
        )

        checker = ModelChecker(timeout=1.0, fallback_timeout=1.0)
        result = await checker.check_models()

        # Sanity: the mock chat host does serve the configured model, so a
        # failing auth assertion cannot be explained away as a dead endpoint.
        self.assertTrue(
            result["chat_model"].get("available"),
            f"the mock chat host serves chat-model-x; probe said: "
            f"{result['chat_model']}",
        )
        self.assertTrue(
            chat_host_requests,
            "the chat-endpoint probe must issue at least one HTTP request",
        )
        self.assertTrue(
            all(
                req.headers.get("authorization") == "Bearer k-test"
                for req in chat_host_requests
            ),
            "every chat-endpoint probe request must carry "
            "Authorization: Bearer k-test when settings.chat_api_key is set "
            "(got headers: "
            f"{[req.headers.get('authorization') for req in chat_host_requests]})",
        )
