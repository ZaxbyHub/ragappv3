"""Acceptance check C1 (issue #982 Family A): non-stream chat error body.

The #287/#710/#730 logging contract ("no raw user-derived text at the
default log level") reaches the LLM chat surface: a provider HTTP error
body can echo the request payload back (the prompt contains the user's
message), and ``backend/app/services/llm_client.py`` composes
``e.response.text`` verbatim into the raised ``LLMError`` message at the
construction site (``chat_completion``, the ``except httpx.HTTPStatusError``
arm). Every default-level caller sink formats ``str(exc)`` (canvas,
contextual chunking, rag engine, chat routes, planners...), so the leak is
composed once here and echoed by 15+ sinks.

- The raised ``LLMError`` message must carry only a bounded summary (status
  + body_len) — the raw body marker must NOT appear in it. RED at base,
  GREEN after the fix.
- The full body must be observable at DEBUG at the construction site.
  RED at base (no DEBUG record exists), GREEN after the fix.
- No INFO+ record of ``app.services.llm_client`` may carry the body marker.
- The bounded summary must keep the HTTP status code (operational signal).

Marker-probe style per the #904/#903 review lessons: the marker rides the
user-derived input (the provider body echoing it), a manually attached
capture handler collects records (assertLogs would fail a passing fix by
design when zero records exist), and the absence assertion counts
marker-bearing INFO+ records.
"""

import logging
import os
import sys
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

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
MARKER = "MARKER982C1BODY"


class _ListHandler(logging.Handler):
    """Collect every record at or above its level; no assertion semantics."""

    def __init__(self, level: int):
        super().__init__(level=level)
        self.records = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def capture_records(logger_name: str, level: int = logging.INFO):
    """Capture records of ``logger_name`` at ``level``+ without failing when
    the logger stays silent (unlike ``assertLogs``)."""
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


def _error_response(status_code: int, body: str) -> httpx.Response:
    request = httpx.Request("POST", "http://llm.invalid/v1/chat/completions")
    return httpx.Response(
        status_code,
        request=request,
        content=body.encode("utf-8"),
        headers={"content-type": "text/plain"},
    )


async def test_nonstream_error_body_bounded_in_llmerror():
    """AC1 (C1): non-stream provider error body must not ride the LLMError.

    A 400 whose body echoes the user text (carrying MARKER982C1BODY) drives
    ``chat_completion`` into the ``except httpx.HTTPStatusError``
    construction arm. RED at base (the message formats ``response.text``
    verbatim), GREEN after the fix.
    """
    with patch("app.services.llm_client.assert_url_safe"):
        client = LLMClient(base_url="http://llm.invalid", model="test-model")
    marker_body = (
        f"invalid request: provider echoes the user prompt segment "
        f"{MARKER} verbatim in the error body"
    )
    stub = AsyncMock()
    stub.post = AsyncMock(return_value=_error_response(400, marker_body))
    client._ensure_started = AsyncMock(return_value=stub)  # noqa: SLF001

    with capture_records(LLM_LOGGER, logging.DEBUG) as captured:
        with pytest.raises(LLMError) as excinfo:
            await client.chat_completion([{"role": "user", "content": "hi"}])

    message = str(excinfo.value)
    assert MARKER not in message, (
        "982C1: provider error body composed verbatim into the raised "
        "LLMError message at the non-stream construction site "
        f"(llm_client.chat_completion): {message!r}"
    )
    assert "400" in message, (
        f"982C1: the bounded summary must keep the HTTP status code; got "
        f"{message!r}"
    )
    leaks = [
        r.getMessage()
        for r in captured.records
        if r.levelno >= logging.INFO and MARKER in r.getMessage()
    ]
    assert leaks == [], (
        f"982C1: {len(leaks)} INFO+ record(s) of {LLM_LOGGER} carry the "
        f"provider error body verbatim: {leaks}"
    )
    debug_full = [
        r.getMessage()
        for r in captured.records
        if r.levelno == logging.DEBUG and MARKER in r.getMessage()
    ]
    assert debug_full, (
        "982C1: the full provider error body must be observable at DEBUG at "
        "the construction site (bounded summary at default level, full body "
        "at DEBUG); no DEBUG record carries it"
    )
