"""Structured request logging middleware."""

import logging
import time
import uuid
from urllib.parse import unquote

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.utils.request_context import is_safe_request_id, request_id_var

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
    "password",
    "access_token",
    "refresh_token",
    "csrf_token",
    "cookie",
    "bearer",
}

logger = logging.getLogger("http")

# Parameter NAMES whose values are user-derived text on live routes (#910,
# #287/#710 contract). Provenance census — every value is matched against
# user-generated content by its consuming route: q/query/search are
# full-text search operands (search.py, documents.py, kms.py, memories.py,
# wiki.py); tag matches user-created KMS tag values (kms_store json_each);
# entity matches wiki-claim subject/object text (wiki_store). Operational
# selectors (status, page_type, severity, sort_by, sort_order, types, mode,
# job_type, format, ids, pagination ints) are NOT user-derived and keep
# their values in the access log.
USER_TEXT_PARAMS = frozenset({"q", "query", "search", "tag", "entity"})


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
    """Redact credential-name params and free-text search values (#910).

    Each ``&``-fragment's parameter NAME is percent-decoded before both the
    :data:`SCRUB_FIELDS` (credential substring) and :data:`USER_TEXT_PARAMS`
    membership tests, so an encoded spelling (``%71=`` = ``q``) cannot slip a
    user-derived value past redaction while the route still decodes it. The
    emitted parameter name keeps its raw wire form. Operational parameters
    (pagination ints, enum selectors like ``status``/``mode``/``sort_by``,
    string-encoded id lists) keep ``name=value`` — only user-derived text is
    redacted, additively to the pre-existing credential scrubbing.
    """
    parts = []
    for param in query.split("&"):
        if not param:
            continue
        raw_name = param.split("=", 1)[0]
        name = unquote(raw_name)
        if any(field in param for field in SCRUB_FIELDS) or any(
            field in name for field in SCRUB_FIELDS
        ):
            parts.append(f"{raw_name}=[redacted]")
        elif name in USER_TEXT_PARAMS:
            parts.append(f"{raw_name}=[redacted]")
        else:
            parts.append(param)
    return "&".join(parts)


class LoggingMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: FastAPI):
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Trust boundary (swarm review F-004): a client may deliver an
        # X-Request-ID with obs-text bytes (latin-1-decoded, unvalidated) or
        # absurd length. Regenerate anything not safe to propagate outbound.
        raw_request_id = request.headers.get("X-Request-ID")
        request_id = (
            raw_request_id
            if is_safe_request_id(raw_request_id)
            else str(uuid.uuid4())
        )
        request.state.request_id = request_id
        # Propagate request_id to service-layer loggers via contextvar
        token = request_id_var.set(request_id)
        start = time.time()
        try:
            response = await call_next(request)
            # Emit the access record BEFORE the contextvar reset: the
            # lifespan-installed RequestIdFilter overwrites record.request_id
            # with the CURRENT contextvar value on every emitted record, so
            # logging after reset() blanked the field in production
            # (swarm-pr-review PRR-004: the record asserted request_id, the
            # shipped JSON never carried it).
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
        finally:
            request_id_var.reset(token)
