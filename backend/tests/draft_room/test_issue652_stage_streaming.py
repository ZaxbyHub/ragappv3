"""Issue #652: Draft Room stage model calls stream against the provider.

``_default_complete`` previously awaited a single non-streaming
``chat_completion``, so the whole generation had to land inside one httpx
read-timeout window — fatal against the deployed always-reasoning vLLM whose
large-prompt stage calls exceed 300 s (five terminal compose failures, jobs
24/27/29/30/31). These tests pin the streaming contract with a stdlib stub
OpenAI-compatible server:

- every request carries ``stream: true`` and the stage result is the
  concatenation of the streamed content deltas (all three logical modes);
- reasoning deltas ride the separate channel and never enter the result;
- ``response_format`` (issue #571 schema contract) is forwarded verbatim on
  the streamed request;
- a stream that outlives the configured client read timeout still completes
  (per-chunk timeout reset);
- the ``PipelineDeps.complete`` protocol seam is unchanged, so pre-#571
  strict fakes (no ``response_format`` parameter) keep working.

Chunk framing note: each SSE event is written as one complete chunked-transfer
chunk (``<len-hex>\\r\\n<data>\\r\\n``). Naive ``len(data)+1`` arithmetic
desynchronises httpx's chunked decoder (RemoteProtocolError "malformed chunked
footer").
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.config import settings
from app.services.draft_pipeline import (
    PipelineDeps,
    _accepts_response_format,
    _default_complete,
    _utcnow,
)

CONTENT_PARTS = ["Compo", "sed ", "stage ", "result", "."]
EXPECTED_CONTENT = "".join(CONTENT_PARTS)
SCHEMA_JSON = '{"ok": true, "sections": ["a"]}'
DRAFT_SCHEMA = {
    "type": "json_schema",
    "json_schema": {"name": "draft_probe", "schema": {"type": "object"}},
}
CHUNK_GAP = 0.35
REASONING_TOKENS = ("think ", "think ", "think ")

received_payloads = []


class _StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence request logging
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        received_payloads.append(body)
        if not body.get("stream"):
            payload = json.dumps(
                {"error": {"message": "non-streaming unsupported"}}
            ).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def write_event(event: bytes):
            self.wfile.write(f"{len(event):x}\r\n".encode() + event + b"\r\n")
            self.wfile.flush()

        def sse(obj):
            write_event(b"data: " + json.dumps(obj).encode() + b"\n\n")

        want_schema = bool(body.get("response_format"))
        for token in REASONING_TOKENS:
            sse(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"reasoning_content": token},
                            "finish_reason": None,
                        }
                    ]
                }
            )
            time.sleep(CHUNK_GAP)
        for part in SCHEMA_JSON if want_schema else CONTENT_PARTS:
            sse(
                {
                    "choices": [
                        {"index": 0, "delta": {"content": part}, "finish_reason": None}
                    ]
                }
            )
            time.sleep(CHUNK_GAP)
        sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        sse({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 20}})
        write_event(b"data: [DONE]\n\n")
        self.wfile.write(b"0\r\n\r\n")


@pytest.fixture()
def stub_server(monkeypatch):
    """Start the stub on an ephemeral port and point all three modes at it."""
    monkeypatch.setenv("ALLOW_LOCAL_SERVICES", "1")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    saved = (
        settings.ollama_chat_url,
        settings.chat_model,
        settings.editorial_chat_url,
        settings.instant_chat_url,
        settings.instant_chat_model,
    )
    settings.ollama_chat_url = base
    settings.chat_model = "stub-model"
    settings.editorial_chat_url = ""  # falls back to the thinking pair
    settings.instant_chat_url = base
    settings.instant_chat_model = "stub-model"
    received_payloads.clear()
    try:
        yield base
    finally:
        (
            settings.ollama_chat_url,
            settings.chat_model,
            settings.editorial_chat_url,
            settings.instant_chat_url,
            settings.instant_chat_model,
        ) = saved
        server.shutdown()


async def test_stage_calls_stream_and_accumulate_all_modes(stub_server):
    for mode in ("thinking", "editorial", "instant"):
        result = await _default_complete(
            "probe prompt", logical_mode=mode, temperature=0.2, sensitive=False
        )
        assert result == EXPECTED_CONTENT, mode
    assert received_payloads, "stub saw no requests"
    assert all(p.get("stream") is True for p in received_payloads)


async def test_reasoning_deltas_never_enter_stage_result(stub_server):
    result = await _default_complete(
        "probe prompt", logical_mode="thinking", temperature=0.2, sensitive=False
    )
    assert result == EXPECTED_CONTENT
    assert "think" not in result


async def test_response_format_forwarded_on_streaming_request(stub_server):
    result = await _default_complete(
        "probe prompt",
        logical_mode="thinking",
        temperature=0.2,
        sensitive=False,
        response_format=DRAFT_SCHEMA,
    )
    assert result == SCHEMA_JSON
    assert any(p.get("response_format") == DRAFT_SCHEMA for p in received_payloads)


async def test_stream_outliving_client_read_timeout_completes(stub_server, monkeypatch):
    # Per-chunk reset: each mode's total stream (~2.8 s with 0.35 s gaps)
    # outlives its configured read timeout, yet completes because httpx
    # applies the timeout per read, not per request.
    budgets = {
        "thinking": ("thinking_request_timeout_seconds", 2.0),
        "editorial": ("editorial_request_timeout_seconds", 2.5),
        "instant": ("instant_request_timeout_seconds", 1.5),
    }
    for mode, (field, value) in budgets.items():
        monkeypatch.setattr(settings, field, value, raising=False)
    for mode, (field, budget) in budgets.items():
        started = time.perf_counter()
        result = await _default_complete(
            "probe prompt", logical_mode=mode, temperature=0.2, sensitive=False
        )
        elapsed = time.perf_counter() - started
        assert result == EXPECTED_CONTENT, mode
        assert elapsed > budget, (mode, elapsed, budget)


def test_pipeline_deps_protocol_seam_unchanged():
    # Pre-#571 strict fakes (no response_format parameter) must keep the old
    # call shape; the production complete still advertises the extension.
    async def strict_fake(prompt, *, logical_mode, temperature, sensitive):
        return "ok"

    assert _accepts_response_format(strict_fake) is False
    assert _accepts_response_format(_default_complete) is True

    async def fake_retrieve(*_args, **_kwargs):
        return []

    deps = PipelineDeps(
        retrieve_sources=fake_retrieve, complete=strict_fake, now=_utcnow
    )
    assert deps.complete is strict_fake
