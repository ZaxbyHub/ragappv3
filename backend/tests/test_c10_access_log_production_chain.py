"""Regression pin for swarm-pr-review PRR-004 (production-chain request_id).

The C11 acceptance test formats records through a bare ``JsonFormatter`` with
no lifespan filter, so it could not see that ``RequestIdFilter`` overwrites
``record.request_id`` with the CURRENT contextvar value. Because the middleware
used to emit the access record AFTER ``request_id_var.reset(token)``, the
shipped JSON access line never carried ``request_id`` — while the admin guide
and C11 both claimed it did.

This module wires the REAL production chain (root StreamHandler +
JsonFormatter + RequestIdFilter + SensitiveFieldFilter, as installed by
``app/lifespan.py``) so the emitted access record is asserted the way operators
see it.
"""

import json
import logging
import os
import sys
from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.middleware.logging import (  # noqa: E402
    LoggingMiddleware,
    SensitiveFieldFilter,
)
from app.utils.request_context import JsonFormatter, RequestIdFilter  # noqa: E402

ACCESS_LOGGER = "http"
REQUEST_ID = "C10REQIDPROBE"


class _Capture(logging.Handler):
    """Emit through the production filter chain, capture the rendered text."""

    def __init__(self):
        super().__init__(level=logging.INFO)
        self.lines = []
        self.setFormatter(JsonFormatter())
        self.addFilter(RequestIdFilter())
        self.addFilter(SensitiveFieldFilter())

    def emit(self, record):
        self.lines.append(self.format(record))

    @property
    def access_lines(self):
        return [ln for ln in self.lines if '"http_request"' in ln]


def _client_with_production_chain():
    """A minimal app whose 'http' logger is handled exactly as lifespan wires it."""
    app = FastAPI()
    app.add_middleware(LoggingMiddleware)

    @app.get("/probe")
    async def _probe():
        return {"ok": True}

    handler = _Capture()
    lg = logging.getLogger(ACCESS_LOGGER)
    previous_level, previous_handlers = lg.level, list(lg.handlers)
    lg.setLevel(logging.INFO)
    lg.addHandler(handler)
    try:
        yield TestClient(app), handler
    finally:
        lg.removeHandler(handler)
        lg.setLevel(previous_level)
        lg.handlers = previous_handlers


@contextmanager
def production_chain_client():
    gen = _client_with_production_chain()
    client, handler = next(gen)
    try:
        yield client, handler
    finally:
        try:
            next(gen)
        except StopIteration:
            pass


def test_access_record_carries_request_id_through_production_chain():
    """The emitted access line must carry the request id the middleware passed.

    RED before the PRR-004 fix (the reset ran before the log call, so the
    filter blanked the field and JsonFormatter omitted it), GREEN after.
    """
    with production_chain_client() as (client, handler):
        response = client.get("/probe?mode=list", headers={"X-Request-ID": REQUEST_ID})

    assert response.status_code == 200
    assert handler.access_lines, f"no http_request record emitted; got {handler.lines}"
    payload = json.loads(handler.access_lines[-1])
    assert payload.get("request_id") == REQUEST_ID, (
        "the production filter chain must not blank the access record's "
        f"request_id (emitted payload: {payload})"
    )


def test_client_supplied_request_id_is_stamped_on_access_record():
    """A client-supplied safe id reaches the access record (correlation path)."""
    with production_chain_client() as (client, handler):
        response = client.get("/probe", headers={"X-Request-ID": REQUEST_ID})

    assert response.status_code == 200
    payload = json.loads(handler.access_lines[-1])
    assert payload["request_id"] == REQUEST_ID
    assert response.headers["X-Request-ID"] == REQUEST_ID


def test_access_record_still_omits_query_values_through_production_chain():
    """The redaction contract holds on the production chain, not just the stub."""
    marker = "C10PRODMARKER"
    with production_chain_client() as (client, handler):
        client.get(f"/probe?q={marker}")

    joined = "\n".join(handler.access_lines)
    assert handler.access_lines, "no http_request record emitted"
    assert marker not in joined, f"query value leaked on the production chain: {joined}"
    assert "[redacted]" in joined, f"expected a redacted query field: {joined}"
