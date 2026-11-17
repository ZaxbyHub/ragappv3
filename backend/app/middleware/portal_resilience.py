"""Resilience middleware for deployments behind a body-replacing portal.

Two independent concerns, both driven by a reverse proxy that sits in front
of this app and (a) rejects PATCH requests and (b) replaces upstream error
bodies with its own HTML page:

1. ``MethodOverrideMiddleware`` — lets a client express a PATCH as
   ``POST`` + ``X-HTTP-Method-Override: PATCH``. The rewrite happens before
   routing and ONLY when a PATCH route is registered for the request path,
   so a POST-only path is never silently reinterpreted; the matched route's
   CSRF/auth dependencies run exactly as they would for a native PATCH.

2. ``ErrorCodeHeaderMiddleware`` — mirrors the machine-readable error code
   from every non-2xx JSON body (``{"detail": ..., "code": ...}``) into the
   ``X-Meridian-Error-Code`` response header, so a client can still classify
   a failure whose body the proxy replaced. Responses without a code (and
   all 2xx/3xx responses) pass through untouched; only >=400 bodies are
   buffered, so streaming success responses are never delayed.

Both middlewares are pure ASGI: no response wrapping, no per-request
object allocation beyond the header lookup.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from typing import Iterable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

METHOD_OVERRIDE_HEADER = "x-http-method-override"
ERROR_CODE_HEADER = "x-meridian-error-code"
# ASGI scope headers are (bytes, bytes) — compare against encoded names.
_METHOD_OVERRIDE_HEADER_RAW = METHOD_OVERRIDE_HEADER.encode("latin-1")
_ERROR_CODE_HEADER_RAW = ERROR_CODE_HEADER.encode("latin-1")
_AUTHORIZATION_HEADER_RAW = b"authorization"


def _route_path(scope: Scope) -> str:
    """The path as the router sees it (root_path stripped)."""
    path = scope.get("path") or ""
    root_path = scope.get("root_path") or ""
    if root_path and path.startswith(root_path):
        path = path[len(root_path):] or "/"
    return path


def _iter_routes(app: object):
    """Yield every APIRoute reachable from ``app``, descending into the
    included sub-routers FastAPI wraps (``_IncludedRouter``), which a flat
    ``app.routes`` walk never reaches.
    """
    from fastapi.routing import APIRoute

    seen = set()
    stack = [app]
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        for route in getattr(node, "routes", None) or ():
            if isinstance(route, APIRoute):
                yield route
                continue
            # FastAPI's _IncludedRouter (>=0.11x) exposes the child router
            # as ``original_router`` (and ``.router`` on some builds); older
            # builds nest ``.routes`` directly.
            child = (getattr(route, "original_router", None)
                     or getattr(route, "router", None)
                     or route)
            stack.append(child)


def _has_patch_route(app: object, path: str) -> bool:
    """True when the app registers a PATCH route matching ``path``.

    Only ``APIRoute`` instances are considered: mounts (static assets) and
    the SPA catch-all are GET-only and must never accept the override.

    The request path carries the deployment prefix (``/api``, and
    ``/meridian/api`` behind the portal) while routes are registered
    without it, so both the full path and the prefix-stripped variants are
    tried.
    """
    candidates = [path]
    for prefix in ("/api", "/meridian/api"):
        if path.startswith(prefix + "/") or path == prefix:
            stripped = path[len(prefix):] or "/"
            candidates.append(stripped)
    for route in _iter_routes(app):
        methods = getattr(route, "methods", None) or ()
        if "PATCH" not in methods:
            continue
        regex = getattr(route, "path_regex", None)
        if regex is None:
            continue
        if any(regex.match(c) for c in candidates):
            return True
    return False


class MethodOverrideMiddleware:
    """Honor ``X-HTTP-Method-Override: PATCH`` on POST for PATCH routes only."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        headers: Iterable[tuple[bytes, bytes]] = scope.get("headers") or ()
        override = b""
        for name, value in headers:
            if name == _METHOD_OVERRIDE_HEADER_RAW:
                override = value
                break
        if override.decode("latin-1").strip().upper() != "PATCH":
            await self.app(scope, receive, send)
            return
        path = _route_path(scope)
        app = scope.get("app")
        if not _has_patch_route(app, path):
            # No PATCH handler for this path: the override is not accepted
            # and the request stays a POST (404/405 as usual).
            logger.warning(
                "method override rejected: no PATCH route for path=%s", path
            )
            await self.app(scope, receive, send)
            return
        scope["method"] = "PATCH"
        await self.app(scope, receive, send)


def _extract_code(body: bytes) -> str | None:
    """The ``code`` field of a JSON error body, or None."""
    if not body:
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    code = payload.get("code")
    if isinstance(code, str) and code:
        return code
    return None


# ── error-detail lookup ───────────────────────────────────────────────────
# A fronting reverse proxy may replace both the body and the headers of
# every non-2xx response, so the client cannot classify a failure from
# what it receives. The frontend therefore stamps every
# mutating request with an X-Meridian-Request-Id; the error middleware
# records the outcome (code + detail + status) against that id for a short
# TTL, and GET /api/errors/lookup returns it. Scoping: the record carries a
# SHA-256 of the caller's bearer token (never the token itself), so only
# the client that made the failing request can read its own record.
#
# Storage is in-process: the deployment runs a single uvicorn worker
# (Dockerfile CMD `--workers 1`). A multi-worker deployment would need a
# shared store (Redis); the TTL bounds the blast radius either way.

REQUEST_ID_HEADER = "x-meridian-request-id"
_REQUEST_ID_HEADER_RAW = REQUEST_ID_HEADER.encode("latin-1")
_ERROR_RECORD_TTL_SECONDS = 300
# Hard cap on stored records. Each entry is bounded (detail truncated to
# _ERROR_DETAIL_MAX, id length bounded), so the store stays under ~0.5 MB;
# 512 exceeds any plausible number of distinct failed mutating requests
# within one 300 s TTL window for a single worker.
_ERROR_RECORD_MAX = 512
# Request-id bounds and charset, identical to the lookup endpoint's
# Query(min_length=8, max_length=128); rejected at record time too so an
# attacker cannot store under ids the endpoint can never serve.
_ERROR_RID_MIN = 8
_ERROR_RID_MAX = 128
_ERROR_RID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]+$")
# Stored detail is diagnostic text, not data; 512 chars is plenty for a
# code + one-line reason and keeps entries a fixed maximum size.
_ERROR_DETAIL_MAX = 512

# Keyed by (request_id, token_hash): two callers may reuse the same
# request id (client-generated) without overwriting each other's record.
_error_records: dict[tuple[str, str], tuple[float, dict]] = {}
_error_records_lock = threading.Lock()


def _caller_token_hash(scope: Scope) -> str | None:
    for name, value in scope.get("headers") or ():
        if name == _AUTHORIZATION_HEADER_RAW:
            raw = value.decode("latin-1")
            token = raw[7:].strip() if raw.lower().startswith("bearer ") else raw.strip()
            if token:
                return hashlib.sha256(token.encode("utf-8")).hexdigest()
            return None
    return None


def record_error(
    request_id: str | None,
    token_hash: str | None,
    code: str | None,
    detail: object,
    status: int,
    path: str,
) -> None:
    """Record a non-2xx outcome against the caller's request id (short TTL).

    Bounded and defensive by construction: ids outside the lookup endpoint's
    length/charset bounds are refused (so garbage cannot be stored under keys
    the endpoint can never serve), the detail is truncated, entries are keyed
    by (request_id, token_hash) so callers cannot overwrite each other, and
    the store never exceeds _ERROR_RECORD_MAX entries: expiry is swept lazily
    (only once the store is full), and whatever remains is evicted
    oldest-first by insertion order.
    """
    if not request_id or not token_hash:
        return
    if not (_ERROR_RID_MIN <= len(request_id) <= _ERROR_RID_MAX):
        return
    if not _ERROR_RID_PATTERN.match(request_id):
        return
    detail_s = detail if isinstance(detail, str) else str(detail)
    if len(detail_s) > _ERROR_DETAIL_MAX:
        detail_s = detail_s[:_ERROR_DETAIL_MAX]
    now = time.monotonic()
    key = (request_id, token_hash)
    with _error_records_lock:
        if len(_error_records) >= _ERROR_RECORD_MAX:
            # Lazy bounded sweep: only when full, purge expired entries...
            for k in [
                k for k, (exp, _) in _error_records.items() if exp <= now
            ]:
                _error_records.pop(k, None)
            # ...then evict oldest-first until the cap holds.
            while len(_error_records) >= _ERROR_RECORD_MAX:
                _error_records.pop(next(iter(_error_records)))
        _error_records[key] = (
            now + _ERROR_RECORD_TTL_SECONDS,
            {
                "code": code,
                "detail": detail_s,
                "status": status,
                "path": path,
                "token_hash": token_hash,
            },
        )


def lookup_error(request_id: str, token_hash: str | None) -> dict | None:
    """The recorded error for ``request_id`` if it belongs to this caller."""
    now = time.monotonic()
    with _error_records_lock:
        entry = _error_records.get((request_id, token_hash)) if token_hash else None
        if entry is None:
            return None
        expires_at, record = entry
        if expires_at <= now:
            _error_records.pop((request_id, token_hash), None)
            return None
        if not token_hash or record.get("token_hash") != token_hash:
            return None
        return {k: v for k, v in record.items() if k != "token_hash"}


class ErrorCodeHeaderMiddleware:
    """Mirror the error body's ``code`` into ``X-Meridian-Error-Code``.

    Only responses with status >= 400 are buffered (error bodies are small
    JSON documents); 2xx/3xx stream through untouched.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Capture the caller's request id + token hash up front so a
        # non-2xx outcome can be recorded for the lookup endpoint.
        request_id: str | None = None
        for name, value in scope.get("headers") or ():
            if name == _REQUEST_ID_HEADER_RAW:
                request_id = value.decode("latin-1")
                break
        token_hash = _caller_token_hash(scope)
        request_path = scope.get("path") or ""

        start_message: Message | None = None
        body_chunks: list[bytes] = []
        passthrough = False

        async def send_wrapper(message: Message) -> None:
            nonlocal start_message, passthrough
            if message["type"] == "http.response.start":
                if message.get("status", 200) < 400:
                    passthrough = True
                    await send(message)
                    return
                start_message = message
                return
            if start_message is not None and message["type"] == "http.response.body":
                body_chunks.append(message.get("body", b"") or b"")
                if not message.get("more_body", False):
                    await self._finish(
                        start_message,
                        b"".join(body_chunks),
                        send,
                        request_id=request_id,
                        token_hash=token_hash,
                        path=request_path,
                    )
                return
            await send(message)

        await self.app(scope, receive, send_wrapper)

    async def _finish(
        self,
        start_message: Message,
        body: bytes,
        send: Send,
        *,
        request_id: str | None = None,
        token_hash: str | None = None,
        path: str = "",
    ) -> None:
        code = _extract_code(body)
        detail: object = None
        if body:
            try:
                payload = json.loads(body.decode("utf-8"))
                if isinstance(payload, dict):
                    detail = payload.get("detail")
            except (UnicodeDecodeError, json.JSONDecodeError):
                detail = None
        if code:
            headers = list(start_message.get("headers") or [])
            if not any(name == _ERROR_CODE_HEADER_RAW for name, _ in headers):
                headers.append((_ERROR_CODE_HEADER_RAW, code.encode("latin-1")))
                start_message["headers"] = headers
        # record the outcome so the client can recover the detail even
        # though the portal will replace this body and these headers.
        record_error(
            request_id,
            token_hash,
            code,
            detail,
            int(start_message.get("status", 500)),
            path,
        )
        await send(start_message)
        await send({"type": "http.response.body", "body": body})
