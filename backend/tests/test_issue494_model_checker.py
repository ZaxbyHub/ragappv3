"""Issue #494 acceptance checks — AC5 / MODEL-001 and AC30 / FU-004.

Both DISCRIMINATING, verified at base a543361:

* AC5: ``model_checker._KNOWN_ROUTE_SUFFIXES`` (~line 67) lacks
  ``'/api/embeddings'`` (and ``'/api/embed'`` therefore over-strips to
  ``<host>/api``). An Ollama embedding URL configured as ``.../api/embeddings``
  derives the WRONG base, so the Ollama-dialect availability probe hits
  ``<host>/api/embeddings/api/tags`` instead of ``<host>/api/tags`` and the
  model is reported unavailable even though it is installed.

  Contract: for each of the embedding-URL forms bare-host, ``.../api/tags``,
  ``.../api/embeddings``, ``.../api/embed`` and ``.../v1/embeddings``,
  ``_check_model_availability`` must probe the SAME derived tags endpoint
  ``<server-root>/api/tags`` (exactly one Ollama-dialect tags probe per
  check), and with the model present all forms must report available=True.
  At base the ``.../api/embeddings`` form probes ``.../api/embeddings/api/tags``
  and the ``.../api/embed`` form probes ``.../api/api/tags`` -> RED.

* AC30: ``circuit_breaker.model_checker_cb`` (fail_max=3) is dead — no
  production wiring references it. The fix (decision recorded here): WIRE it
  at the ModelChecker probe boundary so consecutive probe failures open the
  breaker.

  Contract: with a transport that always refuses model probes, after 3
  consecutive ``_check_model_availability`` failures (3 HTTP calls) the 4th
  call must NOT issue an HTTP request (call count stays 3): it either raises
  ``CircuitBreakerError`` or returns ``available=False``. At base nothing
  wraps the probe, so the 4th call hits HTTP and the count grows to 4 -> RED.

Offline and deterministic: both checks drive ``httpx.MockTransport``.
"""

import os
import sys
import unittest
from unittest.mock import patch

import httpx

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import circuit_breaker as circuit_breaker_module
from app.services.circuit_breaker import CircuitBreakerError
from app.services.model_checker import ModelChecker

OLLAMA_ROOT = "http://ollama-test-host:11434"
EXPECTED_TAGS_URL = f"{OLLAMA_ROOT}/api/tags"
EMBEDDING_URL_FORMS = [
    f"{OLLAMA_ROOT}",                      # bare host
    f"{OLLAMA_ROOT}/api/tags",             # explicit tags route
    f"{OLLAMA_ROOT}/api/embeddings",       # legacy Ollama embeddings route
    f"{OLLAMA_ROOT}/api/embed",            # modern Ollama embeddings route
    f"{OLLAMA_ROOT}/v1/embeddings",        # OpenAI-compatible embeddings route
]


def _tags_transport(recorded):
    """Mock transport: only the CORRECT derived tags URL answers 200."""

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(str(request.url))
        if str(request.url) == EXPECTED_TAGS_URL:
            return httpx.Response(
                200, json={"models": [{"name": "test-model"}]}, request=request
            )
        return httpx.Response(404, text="not found", request=request)

    return httpx.MockTransport(handler)


class TestAC5RouteSuffixDerivation(unittest.IsolatedAsyncioTestCase):
    """Every embedding-URL form must probe <server-root>/api/tags."""

    async def test_all_embedding_url_forms_probe_derived_tags_endpoint(self):
        print("AC5 CHECK: FAIL", flush=True)
        problems: list = []
        for form in EMBEDDING_URL_FORMS:
            recorded: list = []
            client = httpx.AsyncClient(transport=_tags_transport(recorded))
            try:
                checker = ModelChecker()
                result = await checker._check_model_availability(
                    client, form, "test-model"
                )
            finally:
                await client.aclose()

            tags_probes = [u for u in recorded if u.endswith("/api/tags")]
            if len(tags_probes) != 1:
                problems.append(
                    f"{form}: expected exactly one /api/tags probe, got "
                    f"{tags_probes} (all probes: {recorded})"
                )
                continue
            if tags_probes[0] != EXPECTED_TAGS_URL:
                problems.append(
                    f"{form}: Ollama-dialect probe must derive the server root "
                    f"{EXPECTED_TAGS_URL}, got {tags_probes[0]}"
                )
                continue
            if not result.get("available"):
                problems.append(
                    f"{form}: model is installed but reported unavailable: "
                    f"{result}"
                )
        self.assertEqual(
            problems,
            [],
            "embedding-URL forms must all derive the same tags endpoint: "
            + "; ".join(problems),
        )


class TestAC30ModelCheckerBreakerWiring(unittest.IsolatedAsyncioTestCase):
    """model_checker_cb must gate model probes after fail_max failures (FU-004)."""

    async def test_fourth_probe_blocked_by_open_breaker_without_http(self):
        # Reset the shared production breaker so the check is deterministic.
        circuit_breaker_module.model_checker_cb.reset()
        self.addCleanup(circuit_breaker_module.model_checker_cb.reset)
        fail_max = circuit_breaker_module.model_checker_cb.fail_max
        self.assertGreaterEqual(fail_max, 1)

        http_calls = {"n": 0}

        def refusing_transport(request: httpx.Request) -> httpx.Response:
            http_calls["n"] += 1
            raise httpx.ConnectError("connection refused", request=request)

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(refusing_transport)
        )
        self.addAsyncCleanup(client.aclose)
        checker = ModelChecker(timeout=1.0, fallback_timeout=1.0)

        # fail_max consecutive probe failures (one transport-level GET each —
        # transport errors are authoritative and do not fall through).
        for i in range(fail_max):
            result = await checker._check_model_availability(
                client, OLLAMA_ROOT, "test-model"
            )
            self.assertFalse(result["available"])
            self.assertEqual(http_calls["n"], i + 1)

        calls_before_fourth = http_calls["n"]
        raised_breaker_error = False
        fourth_result = None
        try:
            fourth_result = await checker._check_model_availability(
                client, OLLAMA_ROOT, "test-model"
            )
        except CircuitBreakerError:
            raised_breaker_error = True

        # DISCRIMINATING: with the breaker wired, the 4th probe is rejected
        # without HTTP. At base no breaker wraps the probe, the call count
        # grows to fail_max + 1 -> RED.
        print("AC30 CHECK: FAIL", flush=True)
        self.assertEqual(
            http_calls["n"],
            calls_before_fourth,
            f"after {fail_max} consecutive failures the next model probe must "
            f"be rejected by the open circuit breaker WITHOUT an HTTP request "
            f"(issued {http_calls['n'] - calls_before_fourth} extra)",
        )
        self.assertTrue(
            raised_breaker_error
            or (fourth_result is not None and fourth_result.get("available") is False),
            "the breaker-rejected probe must surface CircuitBreakerError or an "
            "unavailable result",
        )
