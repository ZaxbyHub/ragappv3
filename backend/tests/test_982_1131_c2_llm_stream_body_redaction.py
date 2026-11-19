"""Acceptance check C2 (issue #982 Family A): streaming chat error body.

Sibling of C1 on the streaming path: ``chat_completion_stream``'s
``except httpx.HTTPStatusError`` arm reads the full response body
(``await e.response.aread()``) and composes it verbatim into the raised
``LLMError`` message (llm_client.py). The same 15+ default-level caller
sinks format ``str(exc)``.

- The raised ``LLMError`` message must carry only a bounded summary — the
  raw body marker must NOT appear in it. RED at base, GREEN after the fix.
- The full body must be observable at DEBUG at the construction site.
  RED at base, GREEN after the fix.
- No INFO+ record of ``app.services.llm_client`` may carry the body marker.
- The bounded summary must keep the HTTP status code.
"""

import logging
import os
import sys
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# Add parent directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub optional dependencies same as other test files
try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow  # noqa: F401
except ImportError:
    import types

    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

from app.services.llm_client import LLMClient, LLMError

LLM_LOGGER = "app.services.llm_client"
MARKER = "MARKER982C2BODY"


class _ListHandler(logging.Handler):
    """Collect every record at or above its level; no assertion semantics."""

    def __init__(self, level: int):
        super().__init__(level=level)
        self.records = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def capture_records(logger_name: str, level: int = logging.INFO):
    lg = logging.getLogger(logger_name)
    handler = _ListHandler(level)
    previous_level = lg.level
    lg.setLevel(level)
    lg.addHandler(handler)
    try:
        yield handler
    finally:
        lg.removeHandler(handler)
        lg.setLevel(previous_level)


class _StreamContext:
    """Stand-in for httpx's ``client.stream(...)`` async context manager."""

    def __init__(self, response: httpx.Response):
        self._response = response

    async def __aenter__(self) -> httpx.Response:
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        return None


async def test_stream_error_body_bounded_in_llmerror():
    """AC2 (C2): streaming provider error body must not ride the LLMError.

    The stubbed transport opens the SSE stream against a 400 response whose
    body echoes the user text (carrying MARKER982C2BODY);
    ``response.raise_for_status()`` inside the stream context drives the
    streaming construction arm. RED at base, GREEN after the fix.
    """
    with patch("app.services.llm_client.assert_url_safe"):
        client = LLMClient(base_url="http://llm.invalid", model="test-model")
    marker_body = (
        f"invalid request: provider echoes the user prompt segment "
        f"{MARKER} verbatim in the error body"
    )
    request = httpx.Request("POST", "http://llm.invalid/v1/chat/completions")
    response = httpx.Response(
        400,
        request=request,
        content=marker_body.encode("utf-8"),
        headers={"content-type": "text/plain"},
    )
    stub = MagicMock()
    stub.stream = MagicMock(return_value=_StreamContext(response))
    client._ensure_started = AsyncMock(return_value=stub)  # noqa: SLF001

    with capture_records(LLM_LOGGER, logging.DEBUG) as captured:
        with pytest.raises(LLMError) as excinfo:
            async for _chunk in client.chat_completion_stream(
                [{"role": "user", "content": "hi"}]
            ):
                pass

    message = str(excinfo.value)
    assert MARKER not in message, (
        "982C2: provider error body composed verbatim into the raised "
        "LLMError message at the streaming construction site "
        f"(llm_client.chat_completion_stream): {message!r}"
    )
    assert "400" in message, (
        f"982C2: the bounded summary must keep the HTTP status code; got "
        f"{message!r}"
    )
    leaks = [
        r.getMessage()
        for r in captured.records
        if r.levelno >= logging.INFO and MARKER in r.getMessage()
    ]
    assert leaks == [], (
        f"982C2: {len(leaks)} INFO+ record(s) of {LLM_LOGGER} carry the "
        f"provider error body verbatim: {leaks}"
    )
    debug_full = [
        r.getMessage()
        for r in captured.records
        if r.levelno == logging.DEBUG and MARKER in r.getMessage()
    ]
    assert debug_full, (
        "982C2: the full provider error body must be observable at DEBUG at "
        "the streaming construction site; no DEBUG record carries it"
    )
