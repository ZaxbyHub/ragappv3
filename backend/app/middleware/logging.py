"""Structured request logging middleware."""

import logging
import time
import uuid

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.utils.request_context import request_id_var

SCRUB_FIELDS = {
    "message",
    "content",
    "user_input",
    "token",
    "email",
    "username",
    "file_path",
    "ip",
    "session_id",
    "api_key",
    "authorization",
    "secret",
}

logger = logging.getLogger("http")


def _scrub_value(value: str) -> str:
    """Redact a value if it contains any sensitive keyword.

    Used by :func:`_sanitize_query` for URL query-param *names* (e.g.
    ``?api_key=...``). Not used for free-text log message bodies.
    """
    if not value:
        return value
    return "[redacted]" if any(field in value.lower() for field in SCRUB_FIELDS) else value


class SensitiveFieldFilter(logging.Filter):
    """Redact sensitive record *attributes* by name.

    Scrubs the VALUE of any log-record attribute whose NAME is in
    :data:`SCRUB_FIELDS` (e.g. ``record.user_input``, ``record.api_key``),
    replacing it with ``[redacted]``. This is attribute-name-based, NOT
    free-text message matching, so a log line whose message merely contains
    the word "user_input" is left untouched while an ``extra={"user_input":
    ...}`` record is scrubbed. This honors the documented field-level
    redaction contract without over-redacting legitimate log messages.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        for field in SCRUB_FIELDS:
            if hasattr(record, field):
                try:
                    setattr(record, field, "[redacted]")
                except Exception:  # pragma: no cover — defensive
                    pass
        return True


def _sanitize_query(query: str) -> str:
    parts = [f"{param.split('=')[0]}=[redacted]" if any(field in param for field in SCRUB_FIELDS) else param for param in query.split("&") if param]
    return "&".join(parts)


class LoggingMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: FastAPI):
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        # Propagate request_id to service-layer loggers via contextvar
        token = request_id_var.set(request_id)
        start = time.time()
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        duration = time.time() - start
        sanitized_query = _sanitize_query(request.url.query)
        logger.info(
            "http_request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "query": sanitized_query,
                "status_code": response.status_code,
                "client_ip": request.client.host if request.client else None,
                "duration_ms": round(duration * 1000, 2),
            },
        )
        response.headers["X-Request-ID"] = request_id
        return response
