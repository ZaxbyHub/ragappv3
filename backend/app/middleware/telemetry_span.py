"""Per-request OpenTelemetry server spans (issue #518, Workstream E3).

Registered by ``app.services.telemetry.register_span_middleware`` ONLY when
the optional OTel extra is installed and telemetry is enabled — on a default
(air-gapped) install this module is never imported and the request path is
unchanged. Emits one SERVER span per request with ``http.*`` attributes and
no user content (method, path, status only).
"""

import logging

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)


class TelemetrySpanMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        from app.services.telemetry import get_tracer

        tracer = get_tracer()
        path = request.scope.get("path") or "/"
        # PR #595 review F-001b: pass a REAL SpanKind. `kind=None` crashed the
        # OTLP encoder (`_SPAN_KIND_MAP[sdk_span.kind]` has no None key) and
        # silently dropped the entire exported span batch. This middleware
        # only runs when tracing is live, so importing SpanKind here is safe.
        try:
            from opentelemetry.trace import SpanKind

            span_kind = SpanKind.SERVER
        except ImportError:  # pragma: no cover — tracer live implies extra
            span_kind = None
        # Route templates can contain user-chosen path parameters; the raw
        # path is what the access log already records — keep spans to the
        # same no-content policy as every other telemetry surface.
        with tracer.start_as_current_span(
            f"{request.method} {path.split('?')[0]}",
            kind=span_kind,
        ) as span:
            try:
                response = await call_next(request)
            except Exception:
                span.set_attribute("http.response.status_code", 500)
                raise
            span.set_attribute("http.request.method", request.method)
            span.set_attribute("url.path", path.split("?")[0])
            span.set_attribute("http.response.status_code", response.status_code)
            return response
