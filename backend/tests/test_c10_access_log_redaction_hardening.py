"""Review-driven hardening for issue #910 (C15): access-log redaction scope.

Plan-critic Round 1 findings F1/F2 (both live-probed at plan time):

- F1: user-content FILTER parameters (`tag`, `entity`) carry user-derived
  values (KMS tags, wiki-claim entities) and flowed verbatim into the
  access log alongside `q`. The redaction set is provenance-classified
  (USER_TEXT_PARAMS in middleware/logging.py), not FTS-only.
- F2: the sanitizer's name matching must see through percent-encoding —
  `%71=` decodes to `q=` at the route, so a raw-fragment comparison would
  redact nothing while the route still receives the free-text value.

Additive review-driven coverage, outside the frozen acceptance table (the
frozen checks C10-C12 pin the q-marker, operator fields, and the credential
scrub; these pin the widened set and the decoding invariant).
"""

import json
import logging
import os
import sys
from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.middleware.logging import LoggingMiddleware
from app.utils.request_context import JsonFormatter

ACCESS_LOGGER = "http"


class _ListHandler(logging.Handler):
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


def _build_client() -> TestClient:
    app = FastAPI()
    app.add_middleware(LoggingMiddleware)

    @app.get("/probe")
    async def _probe():
        return {"ok": True}

    return TestClient(app)


def _formatted_lines(records) -> list:
    formatter = JsonFormatter()
    return [formatter.format(record) for record in records]


def _access_lines(lines):
    return [line for line in lines if '"http_request"' in line]


def test_user_content_filter_params_redacted():
    """F1: tag= and entity= values must not reach the serialized access log."""
    client = _build_client()
    with capture_records(ACCESS_LOGGER, logging.INFO) as captured:
        response = client.get("/probe?tag=C15TAGPRIVATE&entity=C15ENTITYPRIVATE&mode=list")
    assert response.status_code == 200
    access = _access_lines(_formatted_lines(captured.records))
    assert access, "the middleware must still emit its http_request record"
    joined = "\n".join(access)
    assert "C15TAGPRIVATE" not in joined and "C15ENTITYPRIVATE" not in joined, (
        f"user-content filter values leaked into the access log: {joined}"
    )
    assert "mode=list" in joined, "operational params must keep flowing"


def test_encoded_free_text_names_redacted():
    """F2: percent-encoded spellings of user-text names cannot bypass."""
    client = _build_client()
    with capture_records(ACCESS_LOGGER, logging.INFO) as captured:
        response = client.get(
            "/probe?%71=C15ENCODEDQ&%71uery=C15ENCODEDQUERY&search=C15PLAIN"
        )
    assert response.status_code == 200
    access = _access_lines(_formatted_lines(captured.records))
    assert access, "the middleware must still emit its http_request record"
    joined = "\n".join(access)
    for marker in ("C15ENCODEDQ", "C15ENCODEDQUERY", "C15PLAIN"):
        assert marker not in joined, f"encoded-name bypass leaked {marker}: {joined}"


def test_repeated_free_text_params_each_redacted():
    """Repeated user-text params are each redacted (fragment-wise)."""
    client = _build_client()
    with capture_records(ACCESS_LOGGER, logging.INFO) as captured:
        response = client.get("/probe?q=FIRSTSECRET&q=SECONDSECRET&mode=x")
    assert response.status_code == 200
    access = _access_lines(_formatted_lines(captured.records))
    joined = "\n".join(access)
    assert "FIRSTSECRET" not in joined and "SECONDSECRET" not in joined, (
        f"repeated free-text params leaked: {joined}"
    )


def test_sanitize_query_unit_contract():
    """Unit pins for the provenance-classified, decode-aware sanitizer."""
    from app.middleware.logging import _sanitize_query

    # Frozen C12 pin stays byte-identical (credential name scrub, operational flow).
    assert _sanitize_query("api_key=X&mode=list") == "api_key=[redacted]&mode=list"
    # User-text params by name.
    assert _sanitize_query("q=S") == "q=[redacted]"
    assert _sanitize_query("search=S") == "search=[redacted]"
    assert _sanitize_query("tag=T") == "tag=[redacted]"
    assert _sanitize_query("entity=E") == "entity=[redacted]"
    # Encoded user-text names are decoded before classification.
    assert _sanitize_query("%71=S") == "%71=[redacted]"
    assert _sanitize_query("%71uery=S") == "%71uery=[redacted]"
    # Operational params keep name=value; encoded operational names too.
    assert _sanitize_query("page=2") == "page=2"
    assert _sanitize_query("%70age=2") == "%70age=2"
    # Repeated params are handled fragment-wise.
    assert _sanitize_query("q=A&q=B&page=3") == "q=[redacted]&q=[redacted]&page=3"
