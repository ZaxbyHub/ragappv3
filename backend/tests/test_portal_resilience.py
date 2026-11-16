"""Portal-resilience middleware + 403 denial logging (branch: fix/portal-resilience).

Contracts:
- MethodOverrideMiddleware rewrites POST + X-HTTP-Method-Override: PATCH to
  PATCH before routing, but ONLY for paths with a registered PATCH route;
  POST-only paths are never reinterpreted, and the header is ignored
  without the POST method.
- ErrorCodeHeaderMiddleware mirrors the JSON error body's `code` into
  X-Meridian-Error-Code on non-2xx; non-JSON bodies and 2xx pass through.
- The 403 exception handler logs the denial reason (csrf vs authz).
"""

import logging

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.responses import Response

from app.middleware.portal_resilience import (
    ErrorCodeHeaderMiddleware,
    MethodOverrideMiddleware,
)


def _build_app(root_path: str = "") -> FastAPI:
    app = FastAPI(root_path=root_path or None)

    @app.patch("/api/thing")
    async def patch_thing():
        return {"method": "PATCH"}

    @app.post("/api/postonly")
    async def post_only():
        return {"method": "POST"}

    @app.get("/api/coded-error")
    async def coded_error():
        return JSONResponse(
            status_code=422,
            content={"detail": "bad input", "code": "invalid_input"},
        )

    @app.get("/api/html-error")
    async def html_error():
        return Response(content="<html>nope</html>", status_code=500)

    @app.get("/api/ok")
    async def ok():
        return {"ok": True}

    # Innermost first (see main.py ordering note): last added = outermost.
    app.add_middleware(ErrorCodeHeaderMiddleware)
    app.add_middleware(MethodOverrideMiddleware)
    return app


def test_override_rewrites_post_to_patch_when_patch_route_exists():
    client = TestClient(_build_app())
    resp = client.post(
        "/api/thing", headers={"X-HTTP-Method-Override": "PATCH"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"method": "PATCH"}


def test_override_is_case_insensitive_and_ignores_other_methods():
    client = TestClient(_build_app())
    resp = client.post(
        "/api/thing", headers={"X-HTTP-Method-Override": "patch"}
    )
    assert resp.status_code == 200
    assert resp.json()["method"] == "PATCH"
    # PUT with the override header is not rewritten (POST only).
    resp = client.put(
        "/api/thing", headers={"X-HTTP-Method-Override": "PATCH"}
    )
    assert resp.status_code == 405


def test_override_not_accepted_on_route_without_patch_handler():
    client = TestClient(_build_app())
    resp = client.post(
        "/api/postonly", headers={"X-HTTP-Method-Override": "PATCH"}
    )
    # No PATCH route registered: the request stays a POST and the POST
    # handler answers — the override is never silently reinterpreted.
    assert resp.status_code == 200
    assert resp.json() == {"method": "POST"}


def test_override_absent_leaves_method_alone():
    client = TestClient(_build_app())
    # POST to the PATCH-only path without the header: normal 405.
    resp = client.post("/api/thing")
    assert resp.status_code == 405


def test_override_resolves_paths_under_root_path():
    app = _build_app(root_path="/meridian")
    client = TestClient(app)
    resp = client.post(
        "/meridian/api/thing", headers={"X-HTTP-Method-Override": "PATCH"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"method": "PATCH"}


def test_error_code_header_mirrors_json_body_code():
    client = TestClient(_build_app())
    resp = client.get("/api/coded-error")
    assert resp.status_code == 422
    assert resp.headers["x-meridian-error-code"] == "invalid_input"


def test_error_code_header_absent_for_non_json_and_success():
    client = TestClient(_build_app())
    html = client.get("/api/html-error")
    assert html.status_code == 500
    assert "x-meridian-error-code" not in html.headers
    ok = client.get("/api/ok")
    assert ok.status_code == 200
    assert "x-meridian-error-code" not in ok.headers


def test_error_lookup_scoped_to_caller_token():
    from app.middleware.portal_resilience import lookup_error, record_error

    record_error("req-1", "hash-a", "invalid_input", "bad input", 422, "/api/x")
    got = lookup_error("req-1", "hash-a")
    assert got == {
        "code": "invalid_input",
        "detail": "bad input",
        "status": 422,
        "path": "/api/x",
    }
    # Another caller's token never reads the record.
    assert lookup_error("req-1", "hash-b") is None
    assert lookup_error("req-1", None) is None
    assert lookup_error("req-unknown", "hash-a") is None


def test_error_lookup_expires():
    import time as _time

    from app.middleware import portal_resilience as pr

    pr.record_error("req-exp", "hash-a", "c", "d", 500, "/api/x")
    with pr._error_records_lock:
        pr._error_records["req-exp"] = (
            _time.monotonic() - 1,
            pr._error_records["req-exp"][1],
        )
    assert pr.lookup_error("req-exp", "hash-a") is None


def test_error_lookup_endpoint_end_to_end():
    from app.api.routes.errors import router as errors_router
    from app.middleware.portal_resilience import ErrorCodeHeaderMiddleware

    app = FastAPI()

    @app.get("/api/coded-error")
    async def coded_error():
        return JSONResponse(
            status_code=422,
            content={"detail": "bad input", "code": "invalid_input"},
        )

    app.include_router(errors_router, prefix="/api")
    app.add_middleware(ErrorCodeHeaderMiddleware)
    client = TestClient(app)

    failed = client.get(
        "/api/coded-error",
        headers={
            "X-Meridian-Request-Id": "req-e2e-1234567890",
            "Authorization": "Bearer tok-123",
        },
    )
    assert failed.status_code == 422
    assert failed.headers["x-meridian-error-code"] == "invalid_input"

    # Same caller recovers the recorded detail even though the portal will
    # have replaced the body and headers.
    ok = client.get(
        "/api/errors/lookup",
        params={"request_id": "req-e2e-1234567890"},
        headers={"Authorization": "Bearer tok-123"},
    )
    assert ok.status_code == 200
    assert ok.json()["code"] == "invalid_input"
    assert ok.json()["detail"] == "bad input"

    # A different token gets 404 (no cross-caller leak), none gets 401.
    other = client.get(
        "/api/errors/lookup",
        params={"request_id": "req-e2e-1234567890"},
        headers={"Authorization": "Bearer someone-else"},
    )
    assert other.status_code == 404
    anon = client.get("/api/errors/lookup", params={"request_id": "req-e2e-1234567890"})
    assert anon.status_code == 401


def test_403_handler_logs_denial_reason(caplog):
    from app.main import _logging_http_exception_handler

    class _Req:
        method = "PUT"
        class url:
            path = "/api/kms/entries/243"
        headers = {"x-request-id": "req-1"}

    async def _run(exc):
        with caplog.at_level(logging.WARNING, logger="app.main"):
            await _logging_http_exception_handler(_Req(), exc)

    import anyio

    authz = HTTPException(status_code=403, detail="No write access to this vault")
    anyio.run(_run, authz)
    csrf = HTTPException(
        status_code=403,
        detail="CSRF token missing or mismatch",
        headers={"x-csrf-error": "true"},
    )
    anyio.run(_run, csrf)

    records = [r for r in caplog.records if "403 denial" in r.getMessage()]
    assert len(records) == 2, caplog.text
    assert "category=authz" in records[0].getMessage()
    assert "No write access to this vault" in records[0].getMessage()
    assert "category=csrf" in records[1].getMessage()
    assert "CSRF token missing or mismatch" in records[1].getMessage()
