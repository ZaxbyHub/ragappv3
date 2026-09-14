"""Provider-native thinking controls and the reasoning channel (issue #554).

Frozen acceptance contract (the fix must make every sentinel-bearing
assertion here pass; at the pre-fix tree each fails for the missing-feature
reason printed in its message):

- AC1: ``create_thinking_client``'s outgoing request payload carries the
  Ollama /v1-documented ``reasoning_effort`` control (thinking ON, e.g.
  ``"high"``) on BOTH the streaming and the non-streaming request paths; a
  plain ``LLMClient`` sends no ``reasoning_effort`` key. (Amended from
  ``think: true`` — see the AC1 section comment for the doc citation.)
- AC2: the instant client's no-think control is selected by the configured
  ``instant_chat_model`` family through
  ``app.services.llm_client.select_no_think_chat_template_kwargs`` —
  Qwen-family names get ``{"enable_thinking": False}``, any unrecognized
  family gets NO control (fail open, log, never raise), and
  ``instant_enable_thinking=True`` still means "send no control" (#494
  semantics preserved). The settings hot-rebind re-runs the same family
  selection, so swapping models at runtime installs/removes the control.
- AC3: ``chat_completion_stream`` reads ``reasoning`` and
  ``reasoning_content`` deltas into a separate accumulator and yields them
  as typed ``ReasoningDelta`` objects; content yields remain plain ``str``
  and reasoning text never mixes into the content channel.
- AC5: ``last_metrics`` gains ``reasoning_tokens_estimate`` (chars//4 of the
  accumulated reasoning, mirroring ``_approx_tokens``; 0 when none) and
  ``reasoning_duration_ms`` (wall-clock span first-to-last reasoning delta;
  0.0 when none).
- AC9: factory docstrings name the backends/models the clients actually
  talk to by default (thinking: Ollama; instant: LM Studio), with the stale
  gpt-oss/DGX-Spark and Gemma-4 claims gone.
"""

import json
import os
import sys
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.llm_client as llm_client_mod
from app.config import settings
from app.services.llm_client import (
    LLMClient,
    create_instant_client,
    create_thinking_client,
)


@pytest.fixture(autouse=True)
def _patch_ssrf():
    with patch("app.services.llm_client.assert_url_safe"):
        yield


# ---------------------------------------------------------------------------
# Fake transport (pattern: backend/tests/test_llm_finish_reason.py:28-98)
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.text = json.dumps(payload)
        self.headers = {"content-type": "application/json"}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeStreamResponse:
    def __init__(self, sse_lines: Iterable[str]):
        self._lines = list(sse_lines)
        self.headers = {"content-type": "text/event-stream"}

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class FakeStreamCM:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeAsyncHTTP:
    """Exposes exactly the surface LLMClient uses, and records every payload."""

    def __init__(self, non_stream_payload=None, stream_lines=None):
        self._payload = non_stream_payload or {}
        self._stream_lines = stream_lines or []
        self.post_calls: List[Dict[str, Any]] = []
        self.stream_calls: List[Dict[str, Any]] = []

    async def post(self, url, json=None):
        self.post_calls.append({"url": url, "json": json})
        return FakeResponse(self._payload)

    def stream(self, method, url, json=None):
        self.stream_calls.append({"method": method, "url": url, "json": json})
        return FakeStreamCM(FakeStreamResponse(self._stream_lines))


def _client(fake: FakeAsyncHTTP, **kwargs) -> LLMClient:
    client = LLMClient(
        base_url="http://localhost:9999",
        model="thinking-controls-test-model",
        cb_name="test_thinking_controls",
        **kwargs,
    )
    client._client = fake
    return client


def _sse_lines_from_deltas(deltas: List[Dict[str, Any]]) -> List[str]:
    """Properly framed SSE (data line + blank line) from delta dicts."""
    lines: List[str] = []
    for delta in deltas:
        lines.append("data: " + json.dumps({"choices": [{"delta": delta}]}))
        lines.append("")
    lines.append("data: [DONE]")
    lines.append("")
    return lines


def _non_stream_payload(content: str = "ok") -> Dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
    }


async def _collect_stream(client: LLMClient) -> List[Any]:
    out: List[Any] = []
    async for chunk in client.chat_completion_stream(
        [{"role": "user", "content": "q"}], max_tokens=64
    ):
        out.append(chunk)
    return out


def _reasoning_delta_type():
    """Resolve ReasoningDelta lazily so the pre-fix tree fails with a clear
    runtime assertion instead of an import error."""
    return getattr(llm_client_mod, "ReasoningDelta", None)


# ---------------------------------------------------------------------------
# AC1 — provider-native reasoning control on the thinking client
# (AMENDED from `think: true` to `reasoning_effort: "high"` — CHECK_WRONG:
# Ollama's official OpenAI-compatibility doc lists `reasoning_effort`
# (high/medium/low/max/none) as the supported /v1/chat/completions request
# field and documents no `think` field on that endpoint; `think` is
# native-/api/chat-only per ollama/ollama#14820 + docs PR #14821.)
# ---------------------------------------------------------------------------


class TestThinkingClientThinkControl:
    @pytest.mark.asyncio
    async def test_stream_payload_carries_think_true(self):
        fake = FakeAsyncHTTP(
            stream_lines=_sse_lines_from_deltas([{"content": "ok"}])
        )
        client = create_thinking_client()
        client._client = fake
        chunks = await _collect_stream(client)
        assert "".join(c for c in chunks if isinstance(c, str)) == "ok"
        assert len(fake.stream_calls) == 1
        payload = fake.stream_calls[0]["json"]
        assert payload.get("reasoning_effort") == "high", (
            "AC1-C1: the thinking client's streaming payload must carry the "
            "Ollama /v1-documented reasoning_effort control (thinking ON, "
            f"e.g. 'high'), got payload {payload}"
        )

    @pytest.mark.asyncio
    async def test_non_stream_payload_carries_think_true(self):
        fake = FakeAsyncHTTP(non_stream_payload=_non_stream_payload())
        client = create_thinking_client()
        client._client = fake
        text = await client.chat_completion(
            [{"role": "user", "content": "q"}], max_tokens=64
        )
        assert text == "ok"
        assert len(fake.post_calls) == 1
        payload = fake.post_calls[0]["json"]
        assert payload.get("reasoning_effort") == "high", (
            "AC1-C1: the thinking client's non-streaming payload must carry "
            "the Ollama /v1-documented reasoning_effort control (thinking "
            f"ON, e.g. 'high'), got payload {payload}"
        )

    @pytest.mark.asyncio
    async def test_plain_client_omits_think_key(self):
        # Control: only the thinking factory opts into the native control; a
        # plain LLMClient must not send a `reasoning_effort` key at all.
        fake = FakeAsyncHTTP(
            stream_lines=_sse_lines_from_deltas([{"content": "ok"}])
        )
        client = _client(fake)
        await _collect_stream(client)
        payload = fake.stream_calls[0]["json"]
        assert "reasoning_effort" not in payload, (
            "AC1-C1: plain LLMClient must omit the reasoning_effort key when "
            f"no provider-native control is configured, got payload {payload}"
        )


# ---------------------------------------------------------------------------
# AC2 — per-family no-think control selection (instant client)
# ---------------------------------------------------------------------------


class TestInstantFamilySelection:
    @pytest.mark.asyncio
    async def test_qwen_family_selects_enable_thinking_false(self, monkeypatch):
        monkeypatch.setattr(settings, "instant_chat_model", "qwen/qwen3.5-122b")
        monkeypatch.setattr(settings, "instant_enable_thinking", False)
        client = create_instant_client()
        assert client.chat_template_kwargs == {"enable_thinking": False}, (
            "AC2-C2: a Qwen-family instant model must select the "
            f"chat_template_kwargs enable_thinking=False control, got "
            f"{client.chat_template_kwargs!r}"
        )
        fake = FakeAsyncHTTP(
            stream_lines=_sse_lines_from_deltas([{"content": "ok"}])
        )
        client._client = fake
        await _collect_stream(client)
        payload = fake.stream_calls[0]["json"]
        assert payload.get("chat_template_kwargs") == {"enable_thinking": False}, (
            "AC2-C2: the qwen-family instant client's outgoing payload must "
            f"carry chat_template_kwargs enable_thinking=False, got {payload}"
        )

    @pytest.mark.asyncio
    async def test_unknown_family_fails_open_with_no_control(self, monkeypatch):
        monkeypatch.setattr(settings, "instant_chat_model", "nvidia/nemotron-3-nano-4b")
        monkeypatch.setattr(settings, "instant_enable_thinking", False)
        # Must never raise for an unrecognized family (fail open).
        client = create_instant_client()
        assert not client.chat_template_kwargs, (
            "AC2-C2: an unrecognized model family must send NO no-think "
            "control (log + fail open), got "
            f"{client.chat_template_kwargs!r}"
        )
        fake = FakeAsyncHTTP(
            stream_lines=_sse_lines_from_deltas([{"content": "ok"}])
        )
        client._client = fake
        await _collect_stream(client)
        payload = fake.stream_calls[0]["json"]
        assert "chat_template_kwargs" not in payload, (
            "AC2-C2: the outgoing payload for an unknown family must omit "
            f"chat_template_kwargs entirely, got {payload}"
        )

    @pytest.mark.asyncio
    async def test_enable_thinking_true_sends_no_control_even_for_qwen(
        self, monkeypatch
    ):
        monkeypatch.setattr(settings, "instant_chat_model", "qwen/qwen3.5-122b")
        monkeypatch.setattr(settings, "instant_enable_thinking", True)
        client = create_instant_client()
        assert not client.chat_template_kwargs, (
            "AC2-C2: instant_enable_thinking=True must mean 'send no control' "
            "even for a Qwen-family model (#494 semantics), got "
            f"{client.chat_template_kwargs!r}"
        )

    def test_selector_function_family_rules(self):
        select = getattr(
            llm_client_mod, "select_no_think_chat_template_kwargs", None
        )
        assert select is not None, (
            "AC2-C2: app.services.llm_client must expose "
            "select_no_think_chat_template_kwargs(model) for per-family "
            "no-think control selection"
        )
        assert select("qwen/qwen3.5-122b") == {"enable_thinking": False}, (
            "AC2-C2: qwen-family model names must select the "
            "enable_thinking=False template kwarg"
        )
        assert select("Qwen3-Coder-30B") == {"enable_thinking": False}, (
            "AC2-C2: family matching must be case-insensitive on the model name"
        )
        assert select("nvidia/nemotron-3-nano-4b") is None, (
            "AC2-C2: an unknown family must select NO control (None)"
        )
        assert select("") is None, "AC2-C2: empty model name must fail open (None)"
        assert select(None) is None, "AC2-C2: missing model name must fail open (None)"


class TestHotRebindFamilyAware:
    def test_hot_rebind_reselects_control_on_model_swap(self, monkeypatch):
        from app.api.routes.settings import SettingsUpdate, _hot_rebind_llm_clients

        monkeypatch.setattr(settings, "instant_enable_thinking", False)
        monkeypatch.setattr(settings, "instant_chat_model", "nvidia/nemotron-3-nano-4b")
        client = create_instant_client()
        app = SimpleNamespace(
            state=SimpleNamespace(
                instant_llm_client=client,
                thinking_llm_client=None,
                background_processor=None,
                multimodal_client=None,
            )
        )

        # Swap to a Qwen-family model: the hot rebind must install the control.
        monkeypatch.setattr(settings, "instant_chat_model", "qwen/qwen3.5-122b")
        _hot_rebind_llm_clients(
            app, SettingsUpdate(instant_chat_model="qwen/qwen3.5-122b")
        )
        assert client.chat_template_kwargs == {"enable_thinking": False}, (
            "AC2-C2: the settings hot-rebind must install the family-selected "
            "no-think control when the instant model swaps to a Qwen-family "
            f"name, got {client.chat_template_kwargs!r}"
        )

        # Swap back to an unknown family: the hot rebind must remove it.
        monkeypatch.setattr(settings, "instant_chat_model", "nvidia/nemotron-3-nano-4b")
        _hot_rebind_llm_clients(
            app, SettingsUpdate(instant_chat_model="nvidia/nemotron-3-nano-4b")
        )
        assert not client.chat_template_kwargs, (
            "AC2-C2: the settings hot-rebind must REMOVE the no-think control "
            "when the instant model swaps to an unrecognized family (the "
            "pre-fix rebind hard-codes the Qwen kwarg for every model), got "
            f"{client.chat_template_kwargs!r}"
        )


# ---------------------------------------------------------------------------
# AC3 — typed reasoning channel, never mixed into content
# ---------------------------------------------------------------------------


class TestReasoningChannel:
    @pytest.mark.asyncio
    async def test_reasoning_deltas_yielded_as_typed_objects(self):
        ReasoningDelta = _reasoning_delta_type()
        assert ReasoningDelta is not None, (
            "AC3-C3: app.services.llm_client must define a ReasoningDelta "
            "type yielded by chat_completion_stream for reasoning deltas"
        )
        deltas = [
            {"reasoning_content": "Plan the answer."},
            {"reasoning": "Check the policy section."},
            {"content": "Final answer."},
        ]
        fake = FakeAsyncHTTP(stream_lines=_sse_lines_from_deltas(deltas))
        client = _client(fake)
        chunks = await _collect_stream(client)
        reasoning_texts = [
            c.text for c in chunks if isinstance(c, ReasoningDelta)
        ]
        assert reasoning_texts == ["Plan the answer.", "Check the policy section."], (
            "AC3-C3: both reasoning_content and reasoning deltas must be read "
            f"into the typed reasoning channel in order, got {chunks!r}"
        )
        content_join = "".join(c for c in chunks if isinstance(c, str))
        assert content_join == "Final answer.", (
            "AC3-C3: content yields must remain plain str carrying only the "
            f"answer text, got {content_join!r}"
        )

    @pytest.mark.asyncio
    async def test_all_yields_are_str_or_reasoning_delta(self):
        ReasoningDelta = _reasoning_delta_type()
        assert ReasoningDelta is not None, (
            "AC3-C3: ReasoningDelta type missing from app.services.llm_client"
        )
        deltas = [
            {"reasoning_content": "think "},
            {"content": "A"},
            {"reasoning": "more"},
            {"content": "B"},
        ]
        fake = FakeAsyncHTTP(stream_lines=_sse_lines_from_deltas(deltas))
        client = _client(fake)
        chunks = await _collect_stream(client)
        for chunk in chunks:
            assert isinstance(chunk, (str, ReasoningDelta)), (
                "AC3-C3: chat_completion_stream may yield only plain str "
                f"content or ReasoningDelta objects, got {type(chunk)}"
            )
        content_join = "".join(c for c in chunks if isinstance(c, str))
        assert content_join == "AB", (
            "AC3-C3: reasoning must never mix into the content channel, got "
            f"{content_join!r}"
        )
        reasoning_join = "".join(
            c.text for c in chunks if isinstance(c, ReasoningDelta)
        )
        assert reasoning_join == "think more", (
            f"AC3-C3: reasoning accumulation broken, got {reasoning_join!r}"
        )

    @pytest.mark.asyncio
    async def test_single_delta_with_content_and_reasoning_yields_both(self):
        ReasoningDelta = _reasoning_delta_type()
        assert ReasoningDelta is not None, (
            "AC3-C3: ReasoningDelta type missing from app.services.llm_client"
        )
        fake = FakeAsyncHTTP(
            stream_lines=_sse_lines_from_deltas(
                [{"content": "Hi", "reasoning_content": "pondering"}]
            )
        )
        client = _client(fake)
        chunks = await _collect_stream(client)
        assert "".join(c for c in chunks if isinstance(c, str)) == "Hi", (
            "AC3-C3: content half of a mixed delta must still reach content"
        )
        assert [
            c.text for c in chunks if isinstance(c, ReasoningDelta)
        ] == ["pondering"], (
            "AC3-C3: reasoning half of a mixed delta must reach the typed "
            "reasoning channel"
        )

    @pytest.mark.asyncio
    async def test_reasoning_only_stream_yields_no_str_content(self):
        ReasoningDelta = _reasoning_delta_type()
        assert ReasoningDelta is not None, (
            "AC3-C3: ReasoningDelta type missing from app.services.llm_client"
        )
        fake = FakeAsyncHTTP(
            stream_lines=_sse_lines_from_deltas(
                [{"reasoning_content": "only reasoning, no answer"}]
            )
        )
        client = _client(fake)
        chunks = await _collect_stream(client)
        str_chunks = [c for c in chunks if isinstance(c, str)]
        assert str_chunks == [], (
            "AC3-C3: a reasoning-only stream must not emit any content "
            f"yields, got {str_chunks!r}"
        )


# ---------------------------------------------------------------------------
# AC5 — reasoning metrics on last_metrics
# ---------------------------------------------------------------------------


class TestReasoningMetrics:
    @pytest.mark.asyncio
    async def test_reasoning_tokens_estimate_in_last_metrics(self):
        reasoning_text = "abcdefgh" * 5  # 40 chars -> 40 // 4 = 10 tokens
        fake = FakeAsyncHTTP(
            stream_lines=_sse_lines_from_deltas(
                [{"reasoning_content": reasoning_text}, {"content": "ok"}]
            )
        )
        client = _client(fake)
        await _collect_stream(client)
        assert client.last_metrics.get("status") == "ok"
        assert client.last_metrics.get("reasoning_tokens_estimate") == 10, (
            "AC5-C5: last_metrics must carry reasoning_tokens_estimate as the "
            "chars//4 estimate of the accumulated reasoning text, got "
            f"{client.last_metrics!r}"
        )

    @pytest.mark.asyncio
    async def test_reasoning_metrics_zero_when_no_reasoning(self):
        fake = FakeAsyncHTTP(
            stream_lines=_sse_lines_from_deltas([{"content": "ok"}])
        )
        client = _client(fake)
        await _collect_stream(client)
        assert client.last_metrics.get("reasoning_tokens_estimate") == 0, (
            "AC5-C5: a stream with no reasoning deltas must report "
            f"reasoning_tokens_estimate == 0, got {client.last_metrics!r}"
        )
        assert "reasoning_duration_ms" in client.last_metrics, (
            "AC5-C5: last_metrics must carry reasoning_duration_ms (0.0 when "
            f"no reasoning was seen), got {client.last_metrics!r}"
        )

    @pytest.mark.asyncio
    async def test_reasoning_duration_ms_present_and_nonnegative(self):
        fake = FakeAsyncHTTP(
            stream_lines=_sse_lines_from_deltas(
                [{"reasoning_content": "planning"}, {"content": "ok"}]
            )
        )
        client = _client(fake)
        await _collect_stream(client)
        duration = client.last_metrics.get("reasoning_duration_ms")
        assert isinstance(duration, (int, float)) and duration >= 0, (
            "AC5-C5: last_metrics must carry reasoning_duration_ms as a "
            f"non-negative number, got {duration!r}"
        )


# ---------------------------------------------------------------------------
# AC9 — factory docstrings name the actual default backends
# ---------------------------------------------------------------------------


class TestFactoryDocstrings:
    def test_thinking_client_docstring_names_actual_backend(self):
        doc = (create_thinking_client.__doc__ or "").lower()
        assert "ollama" in doc, (
            "AC9-C9: the thinking client docstring must name the Ollama "
            "backend it talks to by default (settings.ollama_chat_url)"
        )
        assert "gpt-oss-120b" not in doc and "dgx spark" not in doc, (
            "AC9-C9: the thinking client docstring must not name models it "
            "does not talk to by default (gpt-oss-120b / DGX Spark)"
        )

    def test_instant_client_docstring_names_actual_backend(self):
        doc = (create_instant_client.__doc__ or "").lower()
        assert "lm studio" in doc, (
            "AC9-C9: the instant client docstring must name the LM Studio "
            "backend it talks to by default (settings.instant_chat_url)"
        )
        assert "gemma 4 must use" not in doc, (
            "AC9-C9: the instant client docstring must not carry the stale "
            "'Gemma 4 must use no-thinking template mode' claim (default "
            "model is nvidia/nemotron-3-nano-4b)"
        )
