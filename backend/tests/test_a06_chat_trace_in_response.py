"""Issue #688 defect 2 (A06) — RAG trace never reaches the SSE done event.

The engine attaches ``done_msg["trace"] = trace.to_dict()`` only when the
operator opts in via ``settings.rag_trace_in_response``, but the chat
stream route's SSE done-payload builder has no ``trace`` key and never
references the flag — so even with the flag ON, the per-query observability
trace is dropped at the API boundary and never reaches the client.

Route-function-direct harness (same shape as the issue-#558 C14 and a05
stream tests): call ``stream_chat_response`` directly with a stub engine
whose done chunk carries ``trace={'stages': ['retrieve']}`` and parse the
SSE ``data:`` frames.
"""

import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

CSRF_TEST_POLICY = "naive"


def _stub_optional_modules() -> None:
    """Stub missing optional heavy deps so importing app modules is cheap.

    Per-file stubs are load-bearing for CI (requirements-ci.txt omits
    unstructured; lancedb/pyarrow stubs are local fallbacks) — see
    docs/engineering/testing.md section 2.
    """
    for name in ("lancedb", "pyarrow"):
        if name in sys.modules:
            continue
        try:
            __import__(name)
        except ImportError:
            sys.modules[name] = types.ModuleType(name)
    try:
        from unstructured.partition.auto import partition  # noqa: F401

        return
    except Exception:
        pass
    unstructured = types.ModuleType("unstructured")
    unstructured.__path__ = []
    partition_pkg = types.ModuleType("unstructured.partition")
    partition_pkg.__path__ = []
    auto = types.ModuleType("unstructured.partition.auto")
    auto.partition = lambda *args, **kwargs: []
    chunking = types.ModuleType("unstructured.chunking")
    chunking.__path__ = []
    title = types.ModuleType("unstructured.chunking.title")
    title.chunk_by_title = lambda *args, **kwargs: []
    documents = types.ModuleType("unstructured.documents")
    documents.__path__ = []
    elements = types.ModuleType("unstructured.documents.elements")
    elements.Element = type("Element", (), {})
    unstructured.partition = partition_pkg
    partition_pkg.auto = auto
    chunking.title = title
    documents.elements = elements
    for name, mod in [
        ("unstructured", unstructured),
        ("unstructured.partition", partition_pkg),
        ("unstructured.partition.auto", auto),
        ("unstructured.chunking", chunking),
        ("unstructured.chunking.title", title),
        ("unstructured.documents", documents),
        ("unstructured.documents.elements", elements),
    ]:
        sys.modules[name] = mod


_stub_optional_modules()


class _TraceEngine:
    """Stub engine whose done chunk carries an opted-in trace payload."""

    async def query(self, *args, **kwargs):
        yield {"type": "content", "content": "answer"}
        yield {
            "type": "done",
            "sources": [],
            "memories_used": [],
            "trace": {"stages": ["retrieve"]},
        }


@pytest.mark.asyncio
async def test_trace_reaches_sse_done_event_when_flag_on(monkeypatch):
    """With settings.rag_trace_in_response=True and a done chunk carrying a
    trace, the trace must be forwarded on the SSE done event."""
    from app.api.routes import chat as chat_routes
    from app.config import settings

    monkeypatch.setattr(settings, "rag_trace_in_response", True)
    try:
        response = chat_routes.stream_chat_response("q", [], _TraceEngine())
        done = None
        async for frame in response.body_iterator:
            text = frame if isinstance(frame, str) else frame.decode(
                "utf-8", "replace"
            )
            for line in text.splitlines():
                if line.startswith("data: "):
                    payload = json.loads(line[len("data: "):])
                    if payload.get("type") == "done":
                        done = payload
        assert done is not None, "harness: no done event reached the client"
        assert done.get("trace") == {"stages": ["retrieve"]}
    finally:
        monkeypatch.undo()
