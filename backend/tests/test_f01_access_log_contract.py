"""Acceptance checks for the http_request access-log truthfulness contract.

Issue-tracer v3 trace 730-731 (GitHub issue #730, ACs 1-7): frozen
red-checkpoint checks pinning the documented access-log contract for the
``http_request`` line emitted by ``app.middleware.logging.LoggingMiddleware``:

- AC1: the caller's address must not survive into the rendered line
  (docs/release.md documents ``ip`` as automatically redacted and shows
  ``client_ip`` already redacted in the worked example).
- AC2: a safe inbound ``X-Request-ID`` is carried as ``request_id`` on the
  rendered line.
- AC3: a 50-request smoke keeps every line's ``request_id`` paired with that
  response's ``X-Request-ID`` header (ENH-05 metric).
- AC4: query-string redaction matches parameter names case-insensitively.
- AC5: ``duration_ms`` never goes negative under a backwards wall-clock step.
- AC6: the ``app.utils.request_context`` docstring names only filters that
  module actually provides.
- AC7: every key documented in docs/release.md's structured-logging example
  is emitted (docs-vs-code parity, fix-agnostic in either direction).

The harness mirrors the production handler chain wired in
``app/lifespan.py``: a root-logger handler carrying ``JsonFormatter`` with
``RequestIdFilter`` then ``SensitiveFieldFilter`` attached, writing to an
``io.StringIO`` instead of stderr. No network access; no production code is
modified by this module.
"""

import importlib
import io
import json
import logging
import re
import time as real_time_module
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.middleware.logging import (
    LoggingMiddleware,
    SensitiveFieldFilter,
    _sanitize_query,
)
from app.utils.request_context import JsonFormatter, RequestIdFilter

REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE_DOC = REPO_ROOT / "docs" / "release.md"


@pytest.fixture()
def access_log_harness():
    """Minimal app + LoggingMiddleware + the production handler chain.

    Mirrors the lifespan wiring: one handler on the ROOT logger with
    ``JsonFormatter`` and the ``RequestIdFilter``/``SensitiveFieldFilter``
    pair attached in the same order the lifespan attaches them, so records
    emitted by the ``http`` logger reach the handler exactly as in
    production. Root level is raised to INFO (the lifespan applies
    ``settings.log_level`` the same way).
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RequestIdFilter())
    handler.addFilter(SensitiveFieldFilter())

    root = logging.getLogger()
    http_logger = logging.getLogger("http")
    saved_root_level = root.level
    saved_http_level = http_logger.level

    root.addHandler(handler)
    root.setLevel(logging.INFO)
    if http_logger.level == logging.NOTSET or http_logger.level > logging.INFO:
        http_logger.setLevel(logging.INFO)

    app = FastAPI()
    app.add_middleware(LoggingMiddleware)

    @app.get("/f01/ping")
    def ping():
        return {"ok": True}

    client = TestClient(app)
    try:
        yield SimpleNamespace(client=client, stream=stream)
    finally:
        root.removeHandler(handler)
        root.setLevel(saved_root_level)
        http_logger.setLevel(saved_http_level)


def _rendered_http_request_lines(stream) -> list:
    """Raw rendered JSON lines whose parsed ``message`` is ``http_request``."""
    lines = []
    for raw in stream.getvalue().splitlines():
        text = raw.strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and parsed.get("message") == "http_request":
            lines.append(text)
    return lines


def _http_request_payloads(stream) -> list:
    return [json.loads(line) for line in _rendered_http_request_lines(stream)]


def test_http_request_line_does_not_carry_client_address(access_log_harness):
    """Issue #730 AC1 (T1-21-K-07): the rendered http_request line must not
    carry the caller's address.

    The TestClient host is ``testclient``, so a leak is the literal substring
    ``testclient`` in the rendered line. A redaction marker or an omitted
    ``client_ip`` field both pass; only the leak fails.
    """
    access_log_harness.client.get("/f01/ping")
    lines = _rendered_http_request_lines(access_log_harness.stream)
    assert lines, "expected at least one rendered http_request line"
    leaked = [line for line in lines if "testclient" in line]
    assert not leaked, f"caller address leaked into http_request line(s): {leaked}"


def test_http_request_line_carries_inbound_request_id(access_log_harness):
    """Issue #730 AC2 (TQ-differential-a-04): a safe inbound X-Request-ID is
    carried as ``request_id`` on the rendered line (emit-before-reset)."""
    access_log_harness.client.get(
        "/f01/ping", headers={"X-Request-ID": "f01-rid-0001"}
    )
    payloads = _http_request_payloads(access_log_harness.stream)
    assert payloads, "expected at least one rendered http_request line"
    assert payloads[0].get("request_id") == "f01-rid-0001", payloads[0]


def test_every_http_request_line_matches_response_request_id(access_log_harness):
    """Issue #730 AC3 (TQ-differential-a-04, ENH-05): 50 requests without an
    inbound header; every http_request line's request_id equals that
    response's X-Request-ID header and is non-empty (mismatches == 0)."""
    response_ids = []
    for _ in range(50):
        resp = access_log_harness.client.get("/f01/ping")
        response_ids.append(resp.headers.get("x-request-id"))

    payloads = _http_request_payloads(access_log_harness.stream)
    assert len(payloads) == 50, f"expected 50 http_request lines, got {len(payloads)}"
    mismatches = [
        (payload.get("request_id"), rid)
        for payload, rid in zip(payloads, response_ids)
        if not rid or payload.get("request_id") != rid
    ]
    assert mismatches == [], f"unpaired request_id/response pairs: {mismatches[:5]}"


def test_query_redaction_is_case_insensitive():
    """Issue #730 AC4 (T1-21-KR-03): a mixed-case credential-like parameter
    name must be redacted in the ``query`` field; benign parameters must
    survive, so blanket redaction does not pass."""
    out = _sanitize_query("API_KEY=f01-marker-zzz&page=2")
    assert "f01-marker-zzz" not in out, f"sensitive value survived redaction: {out!r}"
    assert "page=2" in out, f"blanket redaction dropped a benign parameter: {out!r}"


class _SteppedWallClock:
    """``time``-module shim: ``time()`` returns 1000.0 then 990.0.

    Models a 10 s backwards NTP step between the two ``time.time()`` calls a
    request makes. ``monotonic``/``perf_counter`` delegate to the real module
    so a monotonic-clock implementation measures real elapsed time and stays
    non-negative.
    """

    def __init__(self, real_module):
        self._real = real_module
        self._time_calls = 0

    def time(self):
        self._time_calls += 1
        return 1000.0 if self._time_calls == 1 else 990.0

    def monotonic(self):
        return self._real.monotonic()

    def perf_counter(self):
        return self._real.perf_counter()


def test_duration_is_monotonic_under_clock_step(access_log_harness, monkeypatch):
    """Issue #730 AC5 (T1-21-S-12): duration_ms must stay non-negative when
    the wall clock steps backwards mid-request (at base: -10000.0)."""
    monkeypatch.setattr(
        "app.middleware.logging.time", _SteppedWallClock(real_time_module)
    )
    access_log_harness.client.get("/f01/ping")
    payloads = _http_request_payloads(access_log_harness.stream)
    assert payloads, "expected at least one rendered http_request line"
    assert payloads[0]["duration_ms"] >= 0, payloads[0]


def test_request_context_docstring_names_only_its_own_filters():
    """Issue #730 AC6 (S04-SK2-09): every :class: role in the
    app.utils.request_context module docstring must resolve to an attribute
    that module provides (a qualified reference resolves through its module,
    falling back to its last dotted segment against app.middleware.logging)."""
    import app.utils.request_context as request_context

    doc = request_context.__doc__ or ""
    names = re.findall(r":class:`~?([\w.]+)`", doc)
    assert names, "expected the docstring to name at least one :class: filter"

    def resolves(name: str) -> bool:
        if "." not in name:
            return hasattr(request_context, name)
        module_path, attr = name.rsplit(".", 1)
        try:
            return hasattr(importlib.import_module(module_path), attr)
        except ImportError:
            from app.middleware import logging as middleware_logging

            return hasattr(middleware_logging, attr)

    missing = [name for name in names if not resolves(name)]
    assert missing == [], (
        f"docstring names classes the module does not provide: {missing}"
    )


def _documented_http_request_keys() -> set:
    """Keys of the documented http_request example in docs/release.md.

    Primary source: the first ```json fenced block containing
    ``"http_request"`` (the worked example under the structured-logging
    section). Fallback: the first ```json block after the
    ``### Structured Logging Format`` heading. Kept generic so the check
    compares documented keys to emitted keys whichever side a fix corrects.
    """
    text = RELEASE_DOC.read_text(encoding="utf-8")
    for block in re.findall(r"```json\s*(.*?)```", text, re.DOTALL):
        if "http_request" not in block:
            continue
        return set(json.loads(block.strip()).keys())
    heading = re.search(
        r"###\s*Structured Logging Format.*?```json\s*(.*?)```", text, re.DOTALL
    )
    if heading:
        return set(json.loads(heading.group(1).strip()).keys())
    pytest.fail(
        f"{RELEASE_DOC} documents no http_request example JSON block; "
        "cannot compare the documented key set"
    )


def test_emitted_line_has_every_documented_key(access_log_harness):
    """Issue #730 AC7 (T1-21-K-07, TQ-differential-a-04, ENH-05): a rendered
    http_request line carries every key the docs/release.md example
    documents (documented_keys - emitted_keys == set())."""
    documented_keys = _documented_http_request_keys()
    assert documented_keys, "no documented keys parsed from docs/release.md"

    access_log_harness.client.get(
        "/f01/ping", headers={"X-Request-ID": "f01-docs-key-probe"}
    )
    payloads = _http_request_payloads(access_log_harness.stream)
    assert payloads, "expected at least one rendered http_request line"
    emitted_keys = set(payloads[0].keys())
    assert documented_keys - emitted_keys == set(), (
        f"documented but not emitted: {sorted(documented_keys - emitted_keys)}"
    )
