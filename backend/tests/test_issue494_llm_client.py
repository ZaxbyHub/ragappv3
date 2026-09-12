"""Issue #494 acceptance checks — AC10/LLM-001, AC11/LLM-003, AC31/FU-005,
AC38/ENH-015, and the P-LLM-STREAM pin.

* AC10 (DISCRIMINATING): the SSE parser in ``chat_completion_stream``
  (~llm_client.py:408-410) only accepts lines starting with ``"data: "`` —
  with a space. The SSE spec (W3C ``data:`` field) allows any number of
  spaces after the colon, and requires consecutive ``data:`` lines of one
  event to be joined with ``\\n``. A spec-compliant server emitting
  ``data:{...}`` (no space) yields an EMPTY answer at base -> RED.
* AC11 (DISCRIMINATING): ``SynthesisTool.execute`` (~agentic_tools.py:
  355-362) swallows LLM synthesis failures and returns success=True with the
  ECHOED input text. A failed synthesis must report success=False with an
  error, and ``AgenticPlanner.plan_and_execute`` must not present the echoed
  input alone as the answer -> RED at base.
* AC31a (PRESERVING): by default ``create_instant_client`` injects
  ``chat_template_kwargs={'enable_thinking': False}`` into the request
  payload — pin the current behavior (green at base, must stay green).
* AC31b (NEW-SURFACE): a documented bool setting ``instant_enable_thinking``
  (default False = current behavior) must exist; when True the instant
  client's payload must NOT carry ``enable_thinking: False``. The setting
  does not exist at base, so the ``hasattr`` sentinel assertion fails
  cleanly -> RED at base is expected for this class of check.
* AC38 (DISCRIMINATING): ``Settings(thinking_max_tokens=1234)`` must flow
  into the thinking client's ``chat_completion`` payload
  (``payload['max_tokens'] == 1234``). At base the default 32768 is always
  sent -> RED (real gap, reported as such).
* P-LLM-STREAM (PRESERVING): the streaming path already records an HTTP
  failure on the per-client circuit breaker (~llm_client.py:605-607) — a 503
  stream must increment the breaker failure count. Green at base.

All offline via ``httpx.MockTransport``.
"""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

import httpx

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import Settings
from app.config import settings as live_settings
from app.services.agentic_planner import AgenticPlanner
from app.services.agentic_tools import (
    AgenticTool,
    SynthesisTool,
    ToolRegistry,
    ToolResult,
)
from app.services.llm_client import (
    LLMClient,
    LLMError,
    create_instant_client,
    create_thinking_client,
)


def _llm_settings_mock() -> MagicMock:
    settings = MagicMock()
    settings.ollama_chat_url = "http://llm-test-host:11434"
    settings.chat_model = "thinking-model"
    settings.instant_chat_url = "http://llm-test-host:1234"
    settings.instant_chat_model = "instant-model"
    settings.llm_max_connections = 20
    settings.llm_max_keepalive_connections = 10
    return settings


def _payload_capture_client(payloads: list) -> httpx.AsyncClient:
    """Mock httpx client that records request payloads and answers 200."""

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


def _sse_client(raw: bytes) -> httpx.AsyncClient:
    """Mock httpx client serving one raw SSE body."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=raw,
            request=request,
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _consume_stream(llm: LLMClient, http_client: httpx.AsyncClient) -> str:
    llm._client = http_client
    parts = []
    async for chunk in llm.chat_completion_stream(
        [{"role": "user", "content": "hi"}]
    ):
        parts.append(chunk)
    return "".join(parts)


# ------------------------------------------------------------------
# AC10 / LLM-001
# ------------------------------------------------------------------

_SSE_SPACED = (
    'data: {"choices":[{"delta":{"content":"Hel"}}]}\n'
    "\n"
    'data: {"choices":[{"delta":{"content":"lo"}}]}\n'
    "\n"
    "data: [DONE]\n\n"
)
_SSE_NO_SPACE = (
    'data:{"choices":[{"delta":{"content":"Hel"}}]}\n'
    "\n"
    'data:{"choices":[{"delta":{"content":"lo"}}]}\n'
    "\n"
    "data:[DONE]\n\n"
)
_SSE_CRLF = (
    'data: {"choices":[{"delta":{"content":"Hel"}}]}\r\n'
    "\r\n"
    'data: {"choices":[{"delta":{"content":"lo"}}]}\r\n'
    "\r\n"
    "data: [DONE]\r\n\r\n"
)
# One event whose JSON is split across two data lines (per SSE spec the
# data field is the two lines joined with \n).
_SSE_MULTILINE = (
    'data: {"choices":[{"delta":{"content":\n'
    'data: "Hel"}}]}\n'
    "\n"
    'data: {"choices":[{"delta":{"content":"lo"}}]}\n'
    "\n"
    "data: [DONE]\n\n"
)


class TestAC10SSEParserSpecCompliance(unittest.IsolatedAsyncioTestCase):
    """`data:` without a space (and multiline data fields) must parse the same."""

    async def test_no_space_crlf_and_multiline_data_fields_assemble_identically(
        self,
    ):
        async def collect(raw: bytes) -> str:
            with patch(
                "app.services.llm_client.settings", _llm_settings_mock()
            ), patch("app.services.llm_client.assert_url_safe"):
                llm = LLMClient()
            client = _sse_client(raw)
            self.addAsyncCleanup(client.aclose)
            return await _consume_stream(llm, client)

        spaced = await collect(_SSE_SPACED.encode("utf-8"))
        no_space = await collect(_SSE_NO_SPACE.encode("utf-8"))
        crlf = await collect(_SSE_CRLF.encode("utf-8"))
        multiline = await collect(_SSE_MULTILINE.encode("utf-8"))

        # Sanity (green at base): the current parser handles 'data: ' + LF.
        self.assertEqual(spaced, "Hello")

        # DISCRIMINATING: identical deltas delivered without a space after
        # 'data:', with CRLF line endings, or split across two data lines of
        # one event must assemble to the SAME non-empty content. At base the
        # no-space stream yields empty content -> RED.
        print("AC10 CHECK: FAIL", flush=True)
        self.assertTrue(no_space, "'data:' without a space must not be ignored")
        self.assertEqual(
            no_space, spaced, "no-space and spaced SSE deltas must assemble identically"
        )
        self.assertEqual(
            crlf, spaced, "CRLF-delimited SSE must assemble identically"
        )
        self.assertEqual(
            multiline,
            spaced,
            "a data field split across two consecutive data: lines must be "
            "concatenated per the SSE spec",
        )


# ------------------------------------------------------------------
# AC11 / LLM-003
# ------------------------------------------------------------------


class FailingLLM:
    """LLM client whose every call fails with the production error type."""

    async def chat_completion(self, messages, **kwargs) -> str:
        raise LLMError("synthesis backend down")


class FakeRetrievalTool(AgenticTool):
    """Retrieval tool stub returning a fixed, non-echo output."""

    def __init__(self):
        self.calls = 0

    @property
    def name(self) -> str:
        return "retrieval"

    @property
    def description(self) -> str:
        return "fake retrieval tool for AC11"

    async def execute(self, **kwargs) -> ToolResult:
        self.calls += 1
        return ToolResult(
            output="Retrieved 2 sources for the query (evidence summary)",
            sources=[{"snippet": "evidence one", "score": 0.9}],
            success=True,
        )


class TestAC11SynthesisFailureNotSuccess(unittest.IsolatedAsyncioTestCase):
    """A failed synthesis must surface as failure, not success=True echo."""

    async def test_synthesis_failure_reports_failure_not_echo(self):
        query = "What is the deployment plan?"
        tool = SynthesisTool(llm_client=FailingLLM())

        result = await tool.execute(text=query, sources=[{"snippet": "doc text"}])

        # DISCRIMINATING: at base the except branch returns
        # ToolResult(output=text, success=True) -> both asserts RED.
        print("AC11 CHECK: FAIL", flush=True)
        self.assertFalse(
            result.success,
            f"LLM synthesis failure must set success=False, got success=True "
            f"with output {result.output!r}",
        )
        self.assertTrue(
            result.error,
            "LLM synthesis failure must populate ToolResult.error",
        )

        # Through the planner: the final answer must not be the echoed input
        # text alone — it must fall back to the retrieval tool's output (or
        # otherwise surface the failure).
        retrieval = FakeRetrievalTool()
        retrieval_output = "Retrieved 2 sources for the query (evidence summary)"
        registry = ToolRegistry()
        registry.register(retrieval)
        registry.register(SynthesisTool(llm_client=FailingLLM()))
        planner = AgenticPlanner(registry, llm_client=FailingLLM())
        agentic_result = await planner.plan_and_execute(query)

        self.assertNotEqual(
            agentic_result.output,
            query,
            "plan_and_execute must not present the echoed input text as the "
            "final answer when synthesis failed",
        )
        self.assertEqual(
            agentic_result.output,
            retrieval_output,
            f"failed synthesis must fall back to the first tool's output, got "
            f"{agentic_result.output!r}",
        )


# ------------------------------------------------------------------
# AC31a / FU-005 (PRESERVING)
# ------------------------------------------------------------------


class TestAC31aInstantThinkingDefaultPreserved(unittest.IsolatedAsyncioTestCase):
    """Default instant client must still send enable_thinking=False."""

    async def test_default_instant_client_payload_disables_thinking(self):
        payloads: list = []
        client = _payload_capture_client(payloads)
        self.addAsyncCleanup(client.aclose)

        with patch(
            "app.services.llm_client.settings", _llm_settings_mock()
        ), patch("app.services.llm_client.assert_url_safe"):
            llm = create_instant_client()
        llm._client = client

        await llm.chat_completion([{"role": "user", "content": "hi"}])

        self.assertEqual(len(payloads), 1)
        payload = payloads[0]
        template_kwargs = payload.get("chat_template_kwargs")
        self.assertIsInstance(template_kwargs, dict)
        self.assertIs(
            template_kwargs.get("enable_thinking"),
            False,
            f"default instant client must inject enable_thinking=False, got "
            f"payload {payload}",
        )
        print("PRESERVING GREEN: default instant payload carries enable_thinking=False")


# ------------------------------------------------------------------
# AC31b / FU-005 (NEW-SURFACE)
# ------------------------------------------------------------------


class TestAC31bInstantEnableThinkingSetting(unittest.IsolatedAsyncioTestCase):
    """Settings.instant_enable_thinking=True must lift the no-thinking template."""

    async def test_setting_exists_and_removes_enable_thinking_false(self):
        # The repo exposes settings via the app.config.settings singleton
        # (there is no separate get_settings() helper at base).
        print("AC31b CHECK: FAIL", flush=True)
        self.assertTrue(
            hasattr(live_settings, "instant_enable_thinking"),
            "documented bool setting 'instant_enable_thinking' (default False) "
            "must exist on Settings",
        )

        previous = getattr(live_settings, "instant_enable_thinking")
        setattr(live_settings, "instant_enable_thinking", True)
        try:
            payloads: list = []
            client = _payload_capture_client(payloads)
            self.addAsyncCleanup(client.aclose)

            with patch("app.services.llm_client.assert_url_safe"):
                llm = create_instant_client()
            llm._client = client
            await llm.chat_completion([{"role": "user", "content": "hi"}])

            self.assertEqual(len(payloads), 1)
            template_kwargs = payloads[0].get("chat_template_kwargs", {})
            self.assertIsNot(
                template_kwargs.get("enable_thinking"),
                False,
                f"instant_enable_thinking=True must not send "
                f"chat_template_kwargs.enable_thinking=False, got payload "
                f"{payloads[0]}",
            )
        finally:
            setattr(live_settings, "instant_enable_thinking", previous)


# ------------------------------------------------------------------
# AC38 / ENH-015
# ------------------------------------------------------------------


class TestAC38ThinkingMaxTokensEndToEnd(unittest.IsolatedAsyncioTestCase):
    """Settings.thinking_max_tokens must reach the thinking client's payload."""

    async def test_thinking_max_tokens_flows_to_chat_completion_payload(self):
        payloads: list = []
        client = _payload_capture_client(payloads)
        self.addAsyncCleanup(client.aclose)

        settings_with_cap = Settings(thinking_max_tokens=1234)
        with patch(
            "app.services.llm_client.settings", settings_with_cap
        ), patch("app.services.llm_client.assert_url_safe"):
            llm = create_thinking_client()
        llm._client = client

        await llm.chat_completion([{"role": "user", "content": "hi"}])

        self.assertEqual(len(payloads), 1)
        # DISCRIMINATING: at base chat_completion ignores
        # settings.thinking_max_tokens and always sends the hardcoded default
        # (32768) -> RED (real gap; see report).
        print("AC38 CHECK: FAIL", flush=True)
        self.assertEqual(
            payloads[0].get("max_tokens"),
            1234,
            f"create_thinking_client must carry "
            f"Settings.thinking_max_tokens=1234 into the chat payload, got "
            f"{payloads[0].get('max_tokens')}",
        )


# ------------------------------------------------------------------
# P-LLM-STREAM (PRESERVING)
# ------------------------------------------------------------------


class TestPLLMStreamRecordsBreakerFailure(unittest.IsolatedAsyncioTestCase):
    """A 503 SSE stream must record a failure on the client's breaker."""

    async def test_stream_503_increments_breaker_failure_count(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="Service Unavailable", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)

        with patch(
            "app.services.llm_client.settings", _llm_settings_mock()
        ), patch("app.services.llm_client.assert_url_safe"):
            llm = LLMClient()
        llm._client = client

        failures_before = llm._circuit_breaker.fail_counter
        with self.assertRaises(LLMError):
            async for _ in llm.chat_completion_stream(
                [{"role": "user", "content": "hi"}]
            ):
                pass

        self.assertEqual(
            llm._circuit_breaker.fail_counter,
            failures_before + 1,
            "streaming HTTP 503 must record_failure on the per-client breaker",
        )
        print(
            "PRESERVING GREEN: streaming 503 incremented breaker failure count "
            f"({failures_before} -> {failures_before + 1})"
        )
