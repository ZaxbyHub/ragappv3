"""Provider-contract regression tests for issue #571 (E08).

Covers the five adoption obligations:
- AC1: the 30s ping loop is gone and factories carry native keep-alive
  (Ollama ``keep_alive`` / LM Studio ``ttl``).
- AC2: provider ``usage`` counts land in ``LLMClient.last_metrics`` (both
  call paths) with the char-estimate fallback preserved.
- AC3: the planner and curator stages pass a json_schema ``response_format``.
- AC4: every factory carries an explicit context-window parameter.
- AC5: the bare-host Ollama embedding fallback resolves to ``/api/embed``.

All offline via ``httpx.MockTransport`` (mirrors test_issue494_llm_client.py).
"""

import ast
import asyncio
import inspect
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

import httpx

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import settings as live_settings
from app.services.agentic_planner import AgenticPlanner
from app.services.chunk_enrichment import ChunkEnrichmentService
from app.services.embeddings import EmbeddingService
from app.services.llm_client import (
    LLMClient,
    create_editorial_client,
    create_instant_client,
    create_thinking_client,
)

HOST = "http://llm-test-host:11434"


def _settings_mock(**extra) -> MagicMock:
    mock = MagicMock()
    mock.ollama_chat_url = HOST
    mock.editorial_chat_url = HOST
    mock.instant_chat_url = "http://llm-test-host:1234"
    mock.chat_model = "thinking-model"
    mock.instant_chat_model = "qwen/qwen3.5-9b"
    mock.instant_enable_thinking = True  # no template control on this path
    mock.llm_max_connections = 20
    mock.llm_max_keepalive_connections = 10
    mock.ollama_keep_alive = "-1"
    mock.ollama_num_ctx = 4096
    mock.lm_studio_ttl = 86400
    mock.lm_studio_context_length = 4096
    mock.instant_max_tokens = 4096
    mock.thinking_max_tokens = 32768
    for key, value in extra.items():
        setattr(mock, key, value)
    return mock


def _factory_patched(**extra):
    """Patch settings + SSRF guard for factory construction."""
    return (
        patch("app.services.llm_client.settings", _settings_mock(**extra)),
        patch("app.services.llm_client.assert_url_safe"),
    )


def _payload_capture_client(payloads: list) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "ok"}, "finish_reason": "stop"}
                ]
            },
            request=request,
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class RecordingClient:
    """Minimal async client double recording chat_completion kwargs."""

    def __init__(self, canned: str):
        self.calls = []
        self._canned = canned

    async def chat_completion(self, *args, **kwargs):
        self.calls.append(kwargs)
        return self._canned


# ------------------------------------------------------------------
# AC1 — keep-alive ping loop removed, native residency carried
# ------------------------------------------------------------------


class TestPingLoopRemoved(unittest.TestCase):
    def test_lifespan_has_no_keepalive_task_symbol(self):
        with open(
            os.path.join(os.path.dirname(__file__), "..", "app", "lifespan.py"),
            encoding="utf-8",
        ) as fh:
            tree = ast.parse(fh.read())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(node.name)
            if isinstance(node, ast.Name):
                names.add(node.id)
        self.assertNotIn("_llm_keepalive_task", names)

    def test_thinking_factory_carries_ollama_keep_alive(self):
        p1, p2 = _factory_patched()
        with p1, p2:
            client = create_thinking_client()
        self.assertEqual(client.keep_alive, "-1")
        self.assertEqual(client.num_ctx, 4096)

    def test_editorial_factory_carries_ollama_keep_alive(self):
        p1, p2 = _factory_patched()
        with p1, p2:
            client = create_editorial_client()
        self.assertEqual(client.keep_alive, "-1")
        self.assertEqual(client.num_ctx, 4096)

    def test_instant_factory_carries_lm_studio_ttl(self):
        p1, p2 = _factory_patched()
        with p1, p2:
            client = create_instant_client()
        self.assertEqual(client.ttl, 86400)
        self.assertEqual(client.context_length, 4096)

    def test_mocked_settings_without_contract_fields_stay_clean(self):
        """Partially mocked settings (no lm_studio_* attrs) send no ttl."""
        mock = _settings_mock()
        for attr in ("lm_studio_ttl", "lm_studio_context_length"):
            delattr(mock, attr)
        with patch("app.services.llm_client.settings", mock), patch(
            "app.services.llm_client.assert_url_safe"
        ):
            client = create_instant_client()
        self.assertIsNone(client.ttl)
        self.assertIsNone(client.context_length)


class TestInstantPayloadCarriesTtl(unittest.IsolatedAsyncioTestCase):
    async def test_non_stream_payload_carries_ttl(self):
        payloads: list = []
        client = _payload_capture_client(payloads)
        self.addAsyncCleanup(client.aclose)
        p1, p2 = _factory_patched()
        with p1, p2:
            llm = create_instant_client()
        llm._client = client
        await llm.chat_completion([{"role": "user", "content": "hi"}])
        self.assertEqual(payloads[0].get("ttl"), 86400)

    async def test_stream_payload_carries_ttl_and_include_usage(self):
        bodies: list = []
        raw = (
            'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            "data: [DONE]\n\n"
        ).encode()

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=raw,
                request=request,
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        p1, p2 = _factory_patched()
        with p1, p2:
            llm = create_instant_client()
        llm._client = client
        async for _ in llm.chat_completion_stream([{"role": "user", "content": "hi"}]):
            pass
        # The wire body must carry the LM Studio idle TTL and the usage
        # request exactly like the non-stream path (PRR-001: the SSE fake
        # previously discarded the request body, so neither was asserted).
        self.assertEqual(bodies[0].get("ttl"), 86400)
        self.assertEqual(
            bodies[0].get("stream_options"), {"include_usage": True}
        )


# ------------------------------------------------------------------
# AC2 — provider usage counts feed last_metrics
# ------------------------------------------------------------------


class _UsageBase(unittest.IsolatedAsyncioTestCase):
    def _llm(self, handler) -> LLMClient:
        with patch("app.services.llm_client.assert_url_safe"):
            llm = LLMClient(base_url=HOST, model="m")
        llm._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(llm._client.aclose)
        return llm


class TestUsageCountsNonStream(_UsageBase):
    async def test_provider_usage_recorded(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": "hello"}, "finish_reason": "stop"}
                    ],
                    "usage": {
                        "prompt_tokens": 123,
                        "completion_tokens": 7,
                        "total_tokens": 130,
                    },
                },
                request=request,
            )

        llm = self._llm(handler)
        await llm.chat_completion([{"role": "user", "content": "hi"}])
        self.assertEqual(llm.last_metrics["prompt_tokens"], 123)
        self.assertEqual(llm.last_metrics["completion_tokens"], 7)
        # estimates remain for compatibility
        self.assertIn("prompt_tokens_estimate", llm.last_metrics)
        self.assertIn("completion_tokens_estimate", llm.last_metrics)

    async def test_missing_usage_falls_back_to_estimates_only(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": "hello"}, "finish_reason": "stop"}
                    ]
                },
                request=request,
            )

        llm = self._llm(handler)
        await llm.chat_completion([{"role": "user", "content": "hi"}])
        self.assertNotIn("prompt_tokens", llm.last_metrics)
        self.assertNotIn("completion_tokens", llm.last_metrics)
        self.assertGreater(llm.last_metrics["prompt_tokens_estimate"], 0)


class TestUsageCountsStream(_UsageBase):
    SSE = (
        'data: {"choices":[{"delta":{"content":"He"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
        '"usage":{"prompt_tokens":123,"completion_tokens":7}}\n\n'
        "data: [DONE]\n\n"
    ).encode()

    def _sse_llm(self, payloads: list) -> LLMClient:
        def handler(request: httpx.Request) -> httpx.Response:
            payloads.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=self.SSE,
                request=request,
            )

        return self._llm(handler)

    async def test_include_usage_requested_and_usage_recorded(self):
        payloads: list = []
        llm = self._sse_llm(payloads)
        chunks = []
        async for chunk in llm.chat_completion_stream([{"role": "user", "content": "hi"}]):
            chunks.append(chunk)
        self.assertEqual("".join(c for c in chunks if isinstance(c, str)), "Hello")
        self.assertEqual(payloads[0]["stream_options"], {"include_usage": True})
        self.assertEqual(llm.last_metrics["prompt_tokens"], 123)
        self.assertEqual(llm.last_metrics["completion_tokens"], 7)

    async def test_no_usage_chunk_keeps_estimates_only(self):
        raw = (
            'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            "data: [DONE]\n\n"
        ).encode()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=raw,
                request=request,
            )

        llm = self._llm(handler)
        async for _ in llm.chat_completion_stream([{"role": "user", "content": "hi"}]):
            pass
        self.assertNotIn("prompt_tokens", llm.last_metrics)
        self.assertNotIn("completion_tokens", llm.last_metrics)

    async def test_sse_fallback_keeps_provider_usage(self):
        """Issue #571: a non-SSE response delegates to chat_completion; the
        stream summary must not overwrite the provider-exact usage it just
        recorded with estimate-only metrics."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": "hello"}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 55, "completion_tokens": 6},
                },
                request=request,
            )

        llm = self._llm(handler)
        chunks = []
        async for chunk in llm.chat_completion_stream([{"role": "user", "content": "hi"}]):
            chunks.append(chunk)
        self.assertEqual("".join(c for c in chunks if isinstance(c, str)), "hello")
        self.assertEqual(llm.last_metrics.get("prompt_tokens"), 55)
        self.assertEqual(llm.last_metrics.get("completion_tokens"), 6)
        self.assertTrue(llm.last_metrics.get("stream"))


# ------------------------------------------------------------------
# AC3 — structured output at production call sites
# ------------------------------------------------------------------


class TestResponseFormatReachesPayload(_UsageBase):
    """Guardrail (issue #571 defect class): the client must FORWARD the
    response_format kwarg into the request payload — plumbing that exists
    but is never exercised is how the pre-#571 dead parameter survived."""

    async def test_response_format_forwarded_into_payload(self):
        payloads: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            payloads.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": "ok"}, "finish_reason": "stop"}
                    ]
                },
                request=request,
            )

        llm = self._llm(handler)
        rf = {"type": "json_schema", "json_schema": {"name": "x", "schema": {}}}
        await llm.chat_completion(
            [{"role": "user", "content": "hi"}], response_format=rf
        )
        self.assertEqual(payloads[0].get("response_format"), rf)

    async def test_response_format_omitted_when_none(self):
        payloads: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            payloads.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": "ok"}, "finish_reason": "stop"}
                    ]
                },
                request=request,
            )

        llm = self._llm(handler)
        await llm.chat_completion([{"role": "user", "content": "hi"}])
        self.assertNotIn("response_format", payloads[0])


class TestPlannerResponseFormat(unittest.IsolatedAsyncioTestCase):
    async def test_planner_decision_passes_json_schema(self):
        llm = RecordingClient('{"action": "synthesize", "sub_query": ""}')
        planner = AgenticPlanner(tool_registry=None, llm_client=llm)
        await planner._decide_next_action("what is rag?", [])
        self.assertEqual(len(llm.calls), 1)
        rf = llm.calls[0].get("response_format")
        self.assertIsNotNone(rf)
        schema = rf["json_schema"]["schema"]
        self.assertEqual(
            schema["properties"]["action"]["enum"],
            ["retrieve_more", "synthesize"],
        )


class TestCuratorResponseFormat(unittest.IsolatedAsyncioTestCase):
    async def test_enrichment_passes_json_schema(self):
        llm = RecordingClient(
            json.dumps(
                {
                    "summary": "s",
                    "questions": ["q1"],
                    "entities": ["e1"],
                    "aliases": ["a1"],
                }
            )
        )
        service = ChunkEnrichmentService(
            llm_client=llm,
            enrichment_fields=["summary", "questions", "entities", "aliases"],
        )
        await service._generate_enrichment("c1", "text", "Doc", "Sec")
        self.assertEqual(len(llm.calls), 1)
        rf = llm.calls[0].get("response_format")
        self.assertIsNotNone(rf)
        schema = rf["json_schema"]["schema"]
        self.assertEqual(schema["properties"]["summary"], {"type": "string"})
        self.assertEqual(
            schema["properties"]["questions"],
            {"type": "array", "items": {"type": "string"}},
        )
        self.assertEqual(
            schema["required"], ["summary", "questions", "entities", "aliases"]
        )


# ------------------------------------------------------------------
# AC4 — explicit context window per factory
# ------------------------------------------------------------------


class TestExplicitContextWindows(unittest.TestCase):
    def test_factories_carry_context_parameters(self):
        p1, p2 = _factory_patched()
        with p1, p2:
            thinking = create_thinking_client()
            editorial = create_editorial_client()
            instant = create_instant_client()
        self.assertEqual(thinking.num_ctx, 4096)
        self.assertEqual(editorial.num_ctx, 4096)
        self.assertEqual(instant.context_length, 4096)


# ------------------------------------------------------------------
# prime_residency — the mechanism that replaced the ping loop
# ------------------------------------------------------------------


class TestPrimeResidency(unittest.IsolatedAsyncioTestCase):
    def _contract_llm(self, handler, **kwargs) -> LLMClient:
        with patch("app.services.llm_client.assert_url_safe"):
            llm = LLMClient(base_url=HOST, model="m", **kwargs)
        llm._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(llm._client.aclose)
        return llm

    async def test_ollama_preload_sends_keep_alive_and_num_ctx(self):
        requests: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append((request.url.path, json.loads(request.content.decode())))
            return httpx.Response(200, json={"done": True}, request=request)

        llm = self._contract_llm(handler, keep_alive="30m", num_ctx=8192)
        ok = await llm.prime_residency()
        self.assertTrue(ok)
        path, body = requests[0]
        self.assertEqual(path, "/api/generate")
        self.assertEqual(body["keep_alive"], "30m")
        self.assertEqual(body["options"], {"num_ctx": 8192})
        self.assertNotIn("prompt", body)

    async def test_lm_studio_load_v1_then_v0_fallback(self):
        requests: list = []
        state = {"calls": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append((request.url.path, json.loads(request.content.decode())))
            state["calls"] += 1
            if state["calls"] == 1:
                # v1 endpoint missing on a pre-0.4.0 server
                return httpx.Response(404, json={"error": "not found"}, request=request)
            return httpx.Response(
                200, json={"status": "loaded", "type": "llm"}, request=request
            )

        llm = self._contract_llm(handler, context_length=4096)
        ok = await llm.prime_residency()
        self.assertTrue(ok)
        self.assertEqual(requests[0][0], "/api/v1/models/load")
        self.assertEqual(requests[1][0], "/api/v0/models/load")
        self.assertEqual(
            requests[1][1], {"model": "m", "context_length": 4096}
        )

    async def test_failure_is_swallowed_and_reported(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        llm = self._contract_llm(handler, keep_alive="-1")
        self.assertIs(await llm.prime_residency(), False)

    async def test_noop_when_no_contracts_set(self):
        with patch("app.services.llm_client.assert_url_safe"):
            llm = LLMClient(base_url=HOST, model="m")
        self.assertIs(await llm.prime_residency(), False)


# ------------------------------------------------------------------
# AC5 — bare-host Ollama embed fallback (modern dialect)
# ------------------------------------------------------------------


class TestProviderContractValidators(unittest.TestCase):
    """PRR-006: the new settings validators must reject invalid values."""

    def _settings(self, **overrides):
        from app.config import Settings

        base = dict(
            users_enabled=False,
            admin_secret_token="test-admin-key-0123456789abcdef",
            jwt_secret_key="test-jwt-key-0123456789abcdef0123456789abcdef",
        )
        base.update(overrides)
        return Settings(**base)

    def test_zero_or_negative_contract_ints_rejected(self):
        from pydantic import ValidationError

        for field in (
            "ollama_num_ctx",
            "lm_studio_ttl",
            "lm_studio_context_length",
        ):
            with self.assertRaises(ValidationError):
                self._settings(**{field: 0})
            with self.assertRaises(ValidationError):
                self._settings(**{field: -5})

    def test_valid_contract_ints_accepted(self):
        settings = self._settings(
            ollama_num_ctx=8192, lm_studio_ttl=3600, lm_studio_context_length=2048
        )
        self.assertEqual(settings.ollama_num_ctx, 8192)
        self.assertEqual(settings.lm_studio_ttl, 3600)
        self.assertEqual(settings.lm_studio_context_length, 2048)

    def test_empty_keep_alive_rejected(self):
        from pydantic import ValidationError

        for value in ("", "   "):
            with self.assertRaises(ValidationError):
                self._settings(ollama_keep_alive=value)

    def test_keep_alive_expressions_accepted(self):
        for value in ("-1", "0", "3600", "30m", "1h"):
            settings = self._settings(ollama_keep_alive=value)
            self.assertEqual(settings.ollama_keep_alive, value)


class TestBareHostEmbedDialect(unittest.TestCase):
    def test_bare_host_resolves_modern(self):
        with patch.object(live_settings, "ollama_embedding_url", HOST), patch(
            "app.services.embeddings.assert_url_safe"
        ):
            service = EmbeddingService()
            mode, url = service._detect_provider_mode("http://localhost:11434")
            self.assertEqual(mode, "ollama")
            self.assertTrue(url.endswith("/api/embed"))
            self.assertEqual(
                service._ollama_endpoint_style("http://localhost:11434"), "modern"
            )

    def test_explicit_paths_unchanged(self):
        with patch.object(live_settings, "ollama_embedding_url", HOST), patch(
            "app.services.embeddings.assert_url_safe"
        ):
            service = EmbeddingService()
            mode, url = service._detect_provider_mode(
                "http://localhost:11434/api/embeddings"
            )
            self.assertEqual((mode, url), ("ollama", "http://localhost:11434/api/embeddings"))
            mode, _ = service._detect_provider_mode("http://localhost:8080/v1/embeddings")
            self.assertEqual(mode, "openai")
            mode, _ = service._detect_provider_mode("http://localhost:8080/embed")
            self.assertEqual(mode, "tei")


if __name__ == "__main__":
    unittest.main()
