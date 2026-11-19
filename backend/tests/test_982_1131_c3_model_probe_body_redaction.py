"""Acceptance check C3 (issue #982 Family A): model-probe error channel.

``backend/app/services/model_checker.py`` ``_model_check_error`` composes
``exc.response.text`` verbatim into the standard unavailable-result dict
(``'error': f"HTTP error {status}: {exc.response.text}"``). That dict is
the probe failure result: it flows to admin/monitor-gated HTTP responses
(health routes, settings) AND into ``_ProbeFailure`` messages that callers
log at WARNING — a provider body echoing the request payload therefore
reaches both the default log and HTTP surfaces.

- The result channel must carry only a bounded summary (status +
  body_len) — the raw body marker must NOT appear in ``result["error"]``.
  RED at base, GREEN after the fix.
- The full body must be observable at DEBUG at the construction site.
  RED at base, GREEN after the fix.
- No INFO+ record of ``app.services.model_checker`` may carry the marker.
- The bounded summary must keep the HTTP status code.
"""

import logging
import os
import sys
from contextlib import contextmanager

import httpx

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

from app.services.model_checker import _model_check_error

CHECKER_LOGGER = "app.services.model_checker"
MARKER = "MARKER982C3BODY"


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


def _status_error_with_body(body: str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://llm.invalid/v1/models")
    response = httpx.Response(
        401,
        request=request,
        content=body.encode("utf-8"),
        headers={"content-type": "text/plain"},
    )
    return httpx.HTTPStatusError("unauthorized", request=request, response=response)


def test_probe_error_channel_bounded():
    """AC3 (C3): the probe result channel must not carry the raw body.

    The probe's HTTP status error carries a body that echoes the user text
    (MARKER982C3BODY). RED at base (the dict formats ``response.text``
    verbatim), GREEN after the fix.
    """
    exc = _status_error_with_body(
        f"unauthorized: provider echoes the request segment {MARKER} "
        f"verbatim in the error body"
    )
    with capture_records(CHECKER_LOGGER, logging.DEBUG) as captured:
        result = _model_check_error(5.0, exc)

    error_text = result["error"]
    assert MARKER not in error_text, (
        "982C3: provider error body composed verbatim into the model-probe "
        f"result channel (model_checker._model_check_error): {error_text!r}"
    )
    assert "401" in error_text, (
        f"982C3: the bounded summary must keep the HTTP status code; got "
        f"{error_text!r}"
    )
    leaks = [
        r.getMessage()
        for r in captured.records
        if r.levelno >= logging.INFO and MARKER in r.getMessage()
    ]
    assert leaks == [], (
        f"982C3: {len(leaks)} INFO+ record(s) of {CHECKER_LOGGER} carry the "
        f"provider error body verbatim: {leaks}"
    )
    debug_full = [
        r.getMessage()
        for r in captured.records
        if r.levelno == logging.DEBUG and MARKER in r.getMessage()
    ]
    assert debug_full, (
        "982C3: the full provider error body must be observable at DEBUG at "
        "the probe-error construction site; no DEBUG record carries it"
    )
