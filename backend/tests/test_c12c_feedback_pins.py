"""Feedback-round pins for issue #717 (swarm-pr-review run pr973-20261018).

Every test here is RED at the review head (4d18f65b) and GREEN after the
feedback fixes. Covers: fallback_from + provider_url redaction (helper,
SSE wiring, and ROUTE-level mapping incl. admin-keep), non-SSE body shape
validation (non-dict JSON -> LLMError), non-SSE finish_reason/token
estimate, 401-breaker neutrality (no credit), trial release then
re-admission, the chat_model arm of the not_configured gate, and the
url_blocked telemetry outcome.
"""

import asyncio
import contextlib
import json
import unittest
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

import app.services.circuit_breaker as circuit_breaker_module
from app.services.circuit_breaker import AsyncCircuitBreaker
from app.services.llm_client import LLMClient, LLMError

PROVIDER_URL = "http://provider.internal:8080/v1"
FALLBACK_URL = "http://fallback.internal:9090/v1"


class _PermissiveAdmission:
    """Permissive admission stub (A06 harness pattern)."""

    async def queue_depth(self, cls):
        return 0

    @contextlib.asynccontextmanager
    async def admit(self, admission_class, **kwargs):
        yield


def _leaking_metrics_engine():
    """Fake RAG engine whose done chunk carries both provider URLs."""

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
                        "provider_url": PROVIDER_URL,
                        "fallback_from": FALLBACK_URL,
                        "status": "ok",
                        "latency_ms": 5.0,
                    },
                }

            return _gen()

    return _Engine()


async def _collect_done(llm_metrics_key_testable: bool = True) -> Dict[str, Any]:
    from app.api.routes import chat as chat_module

    with patch.object(
        chat_module,
        "get_admission_controller",
        lambda: _PermissiveAdmission(),
    ):
        response = chat_module.stream_chat_response(
            "q", [], _leaking_metrics_engine(), is_admin=False
        )
        done = None
        async for chunk in response.body_iterator:
            text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
            if text.startswith("data:") and '"done"' in text:
                done = text
        assert done is not None, "harness: no done frame observed"
        return json.loads(done.split("data:", 1)[1])


class TestFallbackFromRedaction(unittest.IsolatedAsyncioTestCase):
    """PRR-001: fallback_from is a configured provider URL and must be
    redacted for non-admins exactly like provider_url (issue approach #6
    named both keys)."""

    def test_helper_drops_both_provider_urls(self):
        from app.api.routes.chat import _redact_llm_metrics

        metrics = {
            "provider_url": PROVIDER_URL,
            "fallback_from": FALLBACK_URL,
            "status": "ok",
        }
        redacted = _redact_llm_metrics(metrics, redact=True)
        self.assertNotIn("provider_url", redacted)
        self.assertNotIn("fallback_from", redacted)
        self.assertEqual(redacted["status"], "ok")
        # Non-mutating
        self.assertIn("fallback_from", metrics)

    async def test_sse_done_frame_drops_fallback_from(self):
        payload = await _collect_done()
        metrics = payload.get("llm_metrics") or {}
        self.assertNotIn("provider_url", metrics)
        self.assertNotIn("fallback_from", metrics)

    async def test_sse_admin_frame_keeps_both(self):
        from app.api.routes import chat as chat_module

        with patch.object(
            chat_module,
            "get_admission_controller",
            lambda: _PermissiveAdmission(),
        ):
            response = chat_module.stream_chat_response(
                "q", [], _leaking_metrics_engine(), is_admin=True
            )
            done = None
            async for chunk in response.body_iterator:
                text = (
                    chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
                )
                if text.startswith("data:") and '"done"' in text:
                    done = text
            payload = json.loads(done.split("data:", 1)[1])
        metrics = payload.get("llm_metrics") or {}
        self.assertEqual(metrics.get("provider_url"), PROVIDER_URL)
        self.assertEqual(metrics.get("fallback_from"), FALLBACK_URL)


class TestNonSseBodyShape(unittest.IsolatedAsyncioTestCase):
    """PRR-009/010: the non-SSE first-body path must map non-object JSON to
    LLMError and must record finish_reason plus a completion estimate."""

    def setUp(self):
        patcher = patch(
            "app.services.llm_client.assert_url_safe", MagicMock()
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _make_client(body: bytes, content_type: str) -> LLMClient:
        client = LLMClient(base_url="http://c12c-nonsse:9", model="m")
        handler = MagicMock(return_value=body)

        class _Response:
            headers = {"content-type": content_type}
            status_code = 200

            def raise_for_status(self):
                return None

            async def aread(self):
                return body

        class _StreamCM:
            async def __aenter__(self):
                return _Response()

            async def __aexit__(self, *exc):
                return False

        class _Transport:
            def stream(self, *a, **kw):
                return _StreamCM()

        transport = _Transport()
        client._client = transport
        client._client.aclose = AsyncMock()
        return client, handler

    async def test_non_object_body_maps_to_llm_error(self):
        client, _ = self._make_client(b"[1, 2, 3]", "application/json")
        gen = client.chat_completion_stream([{"role": "user", "content": "hi"}])
        with self.assertRaises(LLMError):
            await gen.__anext__()
        await gen.aclose()
        self.assertEqual(
            client.last_metrics.get("status"), "invalid_response"
        )

    async def test_non_sse_records_finish_reason_and_estimate(self):
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {"content": "abcdefgh answer"},
                        "finish_reason": "length",
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 6},
            }
        ).encode("utf-8")
        client, _ = self._make_client(body, "application/json")
        gen = client.chat_completion_stream([{"role": "user", "content": "hi"}])
        chunks = []
        with contextlib.suppress(StopAsyncIteration):
            while True:
                chunks.append(await gen.__anext__())
        await gen.aclose()
        self.assertIn("abcdefgh answer", "".join(chunks))
        self.assertEqual(client.last_metrics.get("finish_reason"), "length")
        self.assertGreater(
            client.last_metrics.get("completion_tokens_estimate"), 0
        )
        self.assertEqual(client.last_metrics.get("prompt_tokens"), 11)


class TestNonOutageNeutralNotCredit(unittest.IsolatedAsyncioTestCase):
    """PRR-004: a 401 must not CREDIT the breaker (the old pass-through
    recorded a success, erasing accumulated outage evidence)."""

    def setUp(self):
        circuit_breaker_module.model_checker_cb.reset()
        self.addCleanup(circuit_breaker_module.model_checker_cb.reset)

    async def test_401_after_transport_failure_keeps_fail_counter(self):
        checker = ModelCheckerStub()
        responses = [httpx.Response(500, json={}), httpx.Response(401, json={})]

        def handler(request: httpx.Request) -> httpx.Response:
            return responses.pop(0) if responses else httpx.Response(401, json={})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addCleanup(client.aclose)
        host = "http://host-neutral-c12c:8080"
        breaker = circuit_breaker_module.model_checker_cb.breaker_for(host)
        with patch.object(
            checker, "_dialect_order", return_value=["openai_compatible"]
        ):
            await checker._check_model_availability(client, host, "m")
            self.assertEqual(breaker.fail_counter, 1)
            await checker._check_model_availability(
                client, host, "m", headers={"Authorization": "Bearer k"}
            )
        # The 401 was neutral: the transport failure evidence SURVIVES.
        self.assertEqual(breaker.fail_counter, 1)
        self.assertEqual(breaker.current_state.value, "closed")


class ModelCheckerStub:
    """Minimal checker exposing _check_model_availability + dialect order."""

    def __init__(self):
        from app.services.model_checker import ModelChecker

        self._checker = ModelChecker(timeout=1.0, fallback_timeout=1.0)
        self._dialect_order = self._checker._dialect_order

    async def _check_model_availability(self, *args, **kwargs):
        return await self._checker._check_model_availability(*args, **kwargs)


class TestTrialReleaseThenReadmit(unittest.IsolatedAsyncioTestCase):
    """PRR-026: closing a HALF_OPEN stream mid-flight (GeneratorExit) must
    release the trial slot so the NEXT stream is admitted."""

    def setUp(self):
        patcher = patch(
            "app.services.llm_client.assert_url_safe", MagicMock()
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_generator_exit_releases_trial_for_next_admission(self):
        client = LLMClient(base_url="http://trial-exit-c12c:9", model="m")
        client._circuit_breaker = AsyncCircuitBreaker(
            fail_max=1, reset_timeout=0.05, name="c12c-trial-exit"
        )
        breaker = client._circuit_breaker
        with contextlib.suppress(httpx.ConnectError):
            await breaker.call(_raise_connect_error)
        await asyncio.sleep(0.1)

        entered = asyncio.Event()

        class _ParkedCM:
            async def __aenter__(self):
                entered.set()
                await asyncio.Event().wait()  # parked until aclose

            async def __aexit__(self, *exc):
                return False

        class _ParkedClient:
            def stream(self, *a, **kw):
                return _ParkedCM()

        async def _fake_ensure_started(self):
            return _ParkedClient()

        with patch.object(LLMClient, "_ensure_started", _fake_ensure_started):
            first = client.chat_completion_stream(
                [{"role": "user", "content": "hi"}]
            )
            first_task = asyncio.create_task(first.__anext__())
            await asyncio.wait_for(entered.wait(), timeout=2.0)
            # Mid-flight cancellation throws CancelledError into the
            # generator at the parked await; the finally must still release
            # the trial slot.
            first_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                await asyncio.wait_for(first_task, timeout=2.0)
            # The slot MUST be free: the next admission succeeds (does not
            # raise LLMError "circuit breaker open"/"trial in flight").
            second = client.chat_completion_stream(
                [{"role": "user", "content": "hi"}]
            )
            second_entered = asyncio.Event()

            class _SecondCM:
                async def __aenter__(self):
                    second_entered.set()
                    raise httpx.ConnectError("second stream reached transport")

                async def __aexit__(self, *exc):
                    return False

            class _SecondClient:
                def stream(self, *a, **kw):
                    return _SecondCM()

            async def _fake_ensure_started2(self):
                return _SecondClient()

            with patch.object(
                LLMClient, "_ensure_started", _fake_ensure_started2
            ):
                second_task = asyncio.create_task(second.__anext__())
                with contextlib.suppress(LLMError, httpx.ConnectError):
                    await asyncio.wait_for(second_task, timeout=2.0)
            self.assertTrue(second_entered.is_set())
            with contextlib.suppress(
                asyncio.CancelledError, LLMError, GeneratorExit, StopAsyncIteration
            ):
                await asyncio.wait_for(first_task, timeout=0.1)


async def _raise_connect_error() -> None:
    raise httpx.ConnectError("forced failure to open the test breaker")


class TestNotConfiguredChatModelArm(unittest.IsolatedAsyncioTestCase):
    """OOB tests-falsifiability-003: the OR's second arm (empty chat_model)
    must independently produce not_configured."""

    async def test_empty_chat_model_alone_reports_not_configured(self):
        from app.config import settings
        from app.services.llm_health import LLMHealthChecker

        with patch.object(settings, "ollama_chat_url", "http://url-set:11434"), \
             patch.object(settings, "chat_model", ""):
            result = await LLMHealthChecker(timeout=5).check_chat()
        self.assertEqual(result.get("status"), "not_configured")


class TestUrlBlockedTelemetryOutcome(unittest.TestCase):
    """PRR-015: url_blocked (and invalid_response) are outages, not soft
    partials."""

    def test_url_blocked_maps_to_unavailable(self):
        from app.services.telemetry import _status_to_outcome

        self.assertEqual(_status_to_outcome("url_blocked"), "unavailable")
        self.assertEqual(_status_to_outcome("invalid_response"), "unavailable")
