"""Carry-in coverage for issue #717 fixes that lie OUTSIDE the frozen checks.

Plan-critic round-2 non-blocking requirements (plan 05, Phase 4): the new
behaviors that no frozen check pins — (a) non-outage 4xx probe results do
NOT charge the model-checker breaker while transport failures still do,
with check_endpoint's classification unchanged; (b) the STREAM path admits
exactly one HALF_OPEN trial (the frozen C7 pins only AsyncCircuitBreaker.
call); (c) the SSE done-payload site redacts provider_url for non-admins
(the frozen C6 pins only the non-stream ChatResponse path).

RED at base (master), GREEN after the fix.
"""

import asyncio
import contextlib
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

import app.services.circuit_breaker as circuit_breaker_module
from app.services.circuit_breaker import CircuitBreakerError, is_outage_status
from app.services.llm_client import LLMClient, LLMError
from app.services.model_checker import ModelChecker, _ProbeFailureResult


async def _raise_connect_error() -> None:
    raise httpx.ConnectError("forced failure to open the test breaker")


class TestNonOutageStatusNoCharge(unittest.IsolatedAsyncioTestCase):
    """AC-carry-in: auth 4xx probe failures do not charge the breaker."""

    def setUp(self):
        circuit_breaker_module.model_checker_cb.reset()
        self.addCleanup(circuit_breaker_module.model_checker_cb.reset)

    async def test_401_probe_does_not_charge_breaker(self):
        checker = ModelChecker(timeout=1.0, fallback_timeout=1.0)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "bad key"})

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        self.addCleanup(client.aclose)

        host = "http://host-auth-carryin:8080"
        breaker = circuit_breaker_module.model_checker_cb.breaker_for(host)
        with patch.object(checker, "_dialect_order", return_value=["openai_compatible"]):
            # Fail well past fail_max: a charging implementation opens the
            # breaker; a no-charge one keeps it CLOSED.
            for _ in range(breaker.fail_max + 3):
                result = await checker._check_model_availability(
                    client, host, "m", headers={"Authorization": "Bearer k"}
                )
        self.assertFalse(result.get("available"))
        self.assertEqual(breaker.current_state.value, "closed")
        self.assertEqual(breaker.fail_counter, 0)

    async def test_transport_failure_still_charges_breaker(self):
        checker = ModelChecker(timeout=1.0, fallback_timeout=1.0)

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        self.addCleanup(client.aclose)

        host = "http://host-down-carryin:8080"
        breaker = circuit_breaker_module.model_checker_cb.breaker_for(host)
        with patch.object(checker, "_dialect_order", return_value=["openai_compatible"]):
            for _ in range(breaker.fail_max):
                await checker._check_model_availability(client, host, "m")
        # C12's semantics on the per-host breaker: same-host consecutive
        # transport failures open it.
        self.assertEqual(breaker.current_state.value, "open")

    def test_failure_result_shape_keeps_classification(self):
        """check_endpoint classifies _ProbeFailureResult as unreachable; the
        pass-through value keeps that exact shape (plus the new status_code
        key, which the wizard wrapper ignores)."""
        exc = httpx.HTTPStatusError(
            "Unauthorized",
            request=httpx.Request("GET", "http://h/v1/models"),
            response=httpx.Response(401, json={}),
        )
        from app.services.model_checker import _model_check_error

        result = _model_check_error(1.0, exc)
        self.assertIsInstance(result, _ProbeFailureResult)
        self.assertEqual(result["status_code"], 401)
        self.assertFalse(is_outage_status(401))
        self.assertTrue(is_outage_status(503))


class TestStreamSingleTrial(unittest.IsolatedAsyncioTestCase):
    """AC-carry-in: the STREAM pre-check admits one HALF_OPEN trial (7b).

    At base this scenario does not fail cleanly — the second stream would
    park on the release event forever (base admits every HALF_OPEN caller),
    i.e. a hang; the assertion below is meaningful only against the fixed
    admission seam, which is why this is a carry-in pin, not a frozen check.
    """

    def setUp(self):
        # Fake hostnames cannot pass the real SSRF DNS gate; the frozen
        # C7/C8 suites neutralize the constructor guard the same way.
        patcher = patch("app.services.llm_client.assert_url_safe", MagicMock())
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_stream_half_open_admits_single_trial(self):
        from app.services.circuit_breaker import AsyncCircuitBreaker

        client = LLMClient(base_url="http://stream-trial-carryin:9", model="m")
        # Small deterministic breaker: one failure opens, 0.05s reset.
        client._circuit_breaker = AsyncCircuitBreaker(
            fail_max=1, reset_timeout=0.05, name="test-stream-trial"
        )
        breaker = client._circuit_breaker
        with contextlib.suppress(httpx.ConnectError):
            await breaker.call(_raise_connect_error)
        self.assertEqual(breaker.current_state.value, "open")
        await asyncio.sleep(0.05 + 0.05)

        admitted = asyncio.Event()
        release = asyncio.Event()

        class _ParkingStreamCM:
            async def __aenter__(self):
                admitted.set()
                await release.wait()
                raise httpx.ConnectError("parked stream released")

            async def __aexit__(self, *exc):
                return False

        class _ParkingClient:
            def stream(self, *a, **kw):
                return _ParkingStreamCM()

        async def _fake_ensure_started(self):
            return _ParkingClient()

        with patch.object(
            LLMClient, "_ensure_started", _fake_ensure_started
        ):
            async def first_stream():
                gen = client.chat_completion_stream(
                    [{"role": "user", "content": "hi"}]
                )
                try:
                    await gen.__anext__()
                finally:
                    await gen.aclose()

            t1 = asyncio.create_task(first_stream())
            await admitted.wait()
            # While the first stream holds the HALF_OPEN trial (parked inside
            # the transport CM), a second stream's admission must be rejected
            # with LLMError (mapped from CircuitBreakerError) — issue #717
            # fix 7b: the stream path no longer admits every concurrent
            # caller in HALF_OPEN.
            async def second_stream():
                gen = client.chat_completion_stream(
                    [{"role": "user", "content": "hi"}]
                )
                try:
                    await gen.__anext__()
                finally:
                    await gen.aclose()

            with self.assertRaises(LLMError):
                await second_stream()
            release.set()
            # The parked transport CM raises ConnectError on release, which
            # the stream maps to LLMError - expected teardown noise.
            with contextlib.suppress(LLMError):
                await t1


class TestSseDonePayloadRedaction(unittest.TestCase):
    """AC-carry-in: the SSE done payload drops provider_url for non-admins."""

    def test_redact_helper_drops_provider_url_for_non_admin(self):
        from app.api.routes.chat import _redact_llm_metrics

        metrics = {
            "provider_url": "http://provider.internal:8080/v1",
            "status": "ok",
            "latency_ms": 5.0,
        }
        redacted = _redact_llm_metrics(metrics, redact=True)
        self.assertNotIn("provider_url", redacted)
        self.assertEqual(redacted["status"], "ok")
        # The caller's dict is not mutated (admin copies stay complete).
        self.assertIn("provider_url", metrics)

    def test_redact_helper_keeps_admin_metrics(self):
        from app.api.routes.chat import _redact_llm_metrics

        metrics = {"provider_url": "http://p:1/v1", "status": "ok"}
        kept = _redact_llm_metrics(metrics, redact=False)
        self.assertEqual(kept, metrics)
        self.assertIsNone(_redact_llm_metrics(None, redact=True))


class _PermissiveAdmission:
    """Permissive admission stub (A06 harness pattern)."""

    async def queue_depth(self, cls):
        return 0

    @contextlib.asynccontextmanager
    async def admit(self, admission_class, **kwargs):
        yield


def _leaking_metrics_engine():
    """Fake RAG engine whose done chunk carries llm_metrics with provider_url."""

    class _Engine:
        llm_client = None

        def query(self, *args, **kwargs):
            async def _gen():
                yield {"type": "content", "content": "answer"}
                yield {
                    "type": "done",
                    "sources": [],
                    "memories_used": [],
                    "llm_metrics": {
                        "provider_url": "http://provider.internal:8080/v1",
                        "status": "ok",
                        "latency_ms": 5.0,
                    },
                }

            return _gen()

    return _Engine()


class TestSseDonePayloadWiring(unittest.IsolatedAsyncioTestCase):
    """Reviewer round-1 correction: pin the WIRING, not just the helper.

    Drives stream_chat_response route-function-direct with a done chunk
    carrying llm_metrics.provider_url and asserts the emitted SSE done frame
    redacts it for non-admin callers (is_admin=False) and keeps it for
    admins. Reverting the chat.py redact call turns these RED.
    """

    async def _done_metrics(self, is_admin):
        from app.api.routes import chat as chat_module

        with patch.object(
            chat_module,
            "get_admission_controller",
            lambda: _PermissiveAdmission(),
        ):
            response = chat_module.stream_chat_response(
                "q", [], _leaking_metrics_engine(), is_admin=is_admin
            )
            done = None
            async for chunk in response.body_iterator:
                text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
                if text.startswith("data:") and '"done"' in text:
                    done = text
            assert done is not None, "harness: no done frame observed"
            payload = json.loads(done.split("data:", 1)[1])
            return payload.get("llm_metrics")

    async def test_non_admin_done_frame_has_no_provider_url(self):
        metrics = await self._done_metrics(is_admin=False)
        self.assertIsNotNone(metrics)
        self.assertNotIn("provider_url", metrics)
        self.assertEqual(metrics.get("status"), "ok")

    async def test_admin_done_frame_keeps_provider_url(self):
        metrics = await self._done_metrics(is_admin=True)
        self.assertIsNotNone(metrics)
        self.assertEqual(
            metrics.get("provider_url"), "http://provider.internal:8080/v1"
        )


class TestStreamCircuitOpenSsePayload(unittest.IsolatedAsyncioTestCase):
    """AC-carry-in: circuit-open stream raise surfaces non-ok metrics that
    flow into the done payload (C8's sibling surface on the stream path)."""

    def setUp(self):
        patcher = patch("app.services.llm_client.assert_url_safe", MagicMock())
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_circuit_open_stream_records_circuit_open_status(self):
        from app.services.circuit_breaker import AsyncCircuitBreaker

        client = LLMClient(base_url="http://stream-open-carryin:9", model="m")
        client._circuit_breaker = AsyncCircuitBreaker(
            fail_max=1, reset_timeout=60.0, name="test-stream-open"
        )
        breaker = client._circuit_breaker
        with contextlib.suppress(httpx.ConnectError):
            await breaker.call(_raise_connect_error)
        self.assertEqual(breaker.current_state.value, "open")
        client.last_metrics = {
            "provider_url": client.base_url,
            "model": "m",
            "status": "ok",
        }
        gen = client.chat_completion_stream([{"role": "user", "content": "hi"}])
        with self.assertRaises(LLMError):
            await gen.__anext__()
        await gen.aclose()
        self.assertNotEqual(client.last_metrics.get("status"), "ok")
        self.assertEqual(client.last_metrics.get("status"), "circuit_open")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
