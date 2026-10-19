"""Acceptance checks for issue #910 (C15): raw query values in the access log.

The HTTP logging middleware attaches the request's raw query string to every
access-log record via ``extra={"query": ...}`` at INFO; the sanitizer only
redacts parameter NAMES matching ``SCRUB_FIELDS``, so free-text search terms
(``?q=<user search>``) land verbatim in the default application log.
``JsonFormatter`` serializes every extra into the emitted JSON line, so the
checks below format each captured record through the PRODUCTION formatter —
asserting on ``record.getMessage()`` alone would miss the extras channel
entirely (the #904 review lesson).

- AC1 (C10): a ``q=``/``query=`` value carrying a distinct marker must never
  appear in an INFO-or-higher record of the middleware logger ("http").
  RED at base, GREEN after the fix.
- AC2 (C11): request_id, method, path, status_code, client_ip and duration_ms
  keep flowing. GREEN at base, GREEN after the fix.
- AC3 (C12): the ``SCRUB_FIELDS`` credential-name scrubbing still applies
  (``?api_key=...`` stays redacted). GREEN at base, GREEN after the fix.
- AC4 (C13): docs/admin-guide.md states that access-log query VALUES are
  redacted. RED at base (the doc says nothing about it), GREEN after the fix.
- AC5 (C18): the shipped repo-root Dockerfile launches uvicorn with
  ``--no-access-log`` so the second, unsanitized uvicorn access-log channel
  cannot re-emit raw URLs. RED at base, GREEN after the fix.
"""

import json
import logging
import re
import sys
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.middleware.logging import LoggingMiddleware
from app.utils.request_context import JsonFormatter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The middleware logs on the fixed name "http" (NOT its module path) — an
# assertLogs/capture must target that exact logger.
ACCESS_LOGGER = "http"
QUERY_MARKER = "C10QMARKER"
CREDENTIAL_MARKER = "C12SECRET"


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


def _build_client() -> TestClient:
    """Minimal app carrying the REAL LoggingMiddleware (registered the same
    way app/main.py registers it) plus one probe route."""
    app = FastAPI()
    app.add_middleware(LoggingMiddleware)

    @app.get("/probe")
    async def _probe():
        return {"ok": True}

    return TestClient(app)


def _formatted_lines(records) -> list:
    """Render each record through the production JsonFormatter — the exact
    serialization the log stream receives (message + every extra field)."""
    formatter = JsonFormatter()
    return [formatter.format(record) for record in records]


def _access_records(records):
    return [r for r in records if r.getMessage() == "http_request"]


def test_query_param_not_emitted_in_access_log():
    """AC1: the q= value never reaches an INFO+ access-log record.

    RED at base (the record's ``query`` extra — serialized by JsonFormatter —
    carries ``q=C10QMARKER`` verbatim), GREEN after the fix.
    """
    client = _build_client()
    with capture_records(ACCESS_LOGGER, logging.INFO) as captured:
        response = client.get(f"/probe?q={QUERY_MARKER}&mode=list")
    assert response.status_code == 200
    assert _access_records(captured.records), (
        "the middleware must still emit its http_request access record — an "
        "absence check over zero records would be vacuous"
    )
    leaks = [
        line for line in _formatted_lines(captured.records) if QUERY_MARKER in line
    ]
    assert leaks == [], (
        f"{len(leaks)} INFO+ access-log line(s) carry the raw query value "
        f"(violates the #287/#710/#910 contract): {leaks}"
    )


def test_access_log_keeps_operator_fields():
    """AC2: request_id/method/path/status_code/client_ip/duration_ms survive.

    GREEN at base, GREEN after the fix — redacting the query value must not
    disturb the other fields or the JSONL shape.
    """
    client = _build_client()
    with capture_records(ACCESS_LOGGER, logging.INFO) as captured:
        response = client.get("/probe?mode=list")
    assert response.status_code == 200

    access = _access_records(captured.records)
    assert access, "the middleware must emit its http_request access record"
    record = access[-1]
    for field in (
        "request_id",
        "method",
        "path",
        "status_code",
        "client_ip",
        "duration_ms",
    ):
        assert hasattr(record, field), (
            f"access-log record lost the operator field {field!r} "
            f"(record attrs: {sorted(vars(record))})"
        )
    assert record.method == "GET"
    assert record.path == "/probe"
    assert record.status_code == 200
    assert isinstance(record.duration_ms, (int, float))

    payload = json.loads(JsonFormatter().format(record))
    for field in (
        "request_id",
        "method",
        "path",
        "status_code",
        "client_ip",
        "duration_ms",
    ):
        assert field in payload, (
            f"access-log JSON line lost the operator field {field!r}: {payload}"
        )


def test_scrub_fields_credential_redaction_still_applies():
    """AC3: SCRUB_FIELDS credential-name redaction keeps working.

    GREEN at base, GREEN after the fix — the free-text value redaction must
    be additive, never a replacement for the credential scrubber. Accepted
    post-fix shapes: the ``query`` extra gone entirely, or present with the
    credential parameter redacted by name.
    """
    client = _build_client()
    with capture_records(ACCESS_LOGGER, logging.INFO) as captured:
        response = client.get(f"/probe?api_key={CREDENTIAL_MARKER}&mode=list")
    assert response.status_code == 200
    access = _access_records(captured.records)
    assert access, "the middleware must emit its http_request access record"

    leaks = [
        line for line in _formatted_lines(captured.records) if CREDENTIAL_MARKER in line
    ]
    assert leaks == [], (
        f"credential parameter value leaked into the access log: {leaks}"
    )

    query = getattr(access[-1], "query", None)
    if query is not None:
        assert "api_key=[redacted]" in query, (
            f"a present query extra must keep the SCRUB_FIELDS name-based "
            f"redaction (api_key=[redacted]); got: {query!r}"
        )
        assert "mode=list" in query, (
            f"non-credential parameters must keep flowing; got: {query!r}"
        )

    # While the sanitizer exists, its credential-name behavior is pinned
    # exactly (a refactor must not weaken it even if the middleware later
    # drops the query extra altogether).
    from app.middleware import logging as middleware_logging

    sanitize = getattr(middleware_logging, "_sanitize_query", None)
    if sanitize is not None:
        assert sanitize(f"api_key={CREDENTIAL_MARKER}&mode=list") == (
            "api_key=[redacted]&mode=list"
        ), "SCRUB_FIELDS name-based redaction changed"


def test_admin_guide_documents_query_value_redaction():
    """AC4: docs/admin-guide.md states query VALUES are redacted.

    RED at base (the admin guide never describes access-log query-value
    redaction), GREEN after the fix adds the statement.
    """
    doc = Path(__file__).resolve().parents[2] / "docs" / "admin-guide.md"
    assert doc.exists(), f"admin guide expected at {doc}"
    text = doc.read_text(encoding="utf-8")
    statement_lines = [
        line
        for line in text.splitlines()
        if re.search(r"\bquer(y|ies)\b", line, re.IGNORECASE)
        and re.search(r"\bredact", line, re.IGNORECASE)
    ]
    assert statement_lines, (
        "docs/admin-guide.md must state that access-log query VALUES are "
        "redacted (one sentence in the access/operations area), so the "
        "documented log shape cannot drift from the shipped behavior (#910)"
    )


def test_dockerfile_disables_uvicorn_access_log():
    """AC5: the shipped uvicorn launch passes --no-access-log.

    The middleware record is the app-level access log; the uvicorn access
    log is a second, unsanitized emission of the same raw URL, so the
    shipped CMD must disable it. RED at base, GREEN after the fix.
    """
    dockerfile = Path(__file__).resolve().parents[2] / "Dockerfile"
    assert dockerfile.exists(), f"repo-root Dockerfile expected at {dockerfile}"
    content = dockerfile.read_text(encoding="utf-8")
    cmd_lines = [
        line
        for line in content.splitlines()
        if line.strip().upper().startswith("CMD")
    ]
    assert cmd_lines, "the repo-root Dockerfile must define a CMD instruction"
    assert "--no-access-log" in "\n".join(cmd_lines), (
        "the Dockerfile CMD must launch uvicorn with --no-access-log so the "
        "second, unsanitized uvicorn access-log channel cannot re-emit raw "
        f"URLs; CMD found: {cmd_lines}"
    )
