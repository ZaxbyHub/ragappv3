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
        # Route templates can contain user-chosen path parameters; the raw
        # path is what the access log already records — keep spans to the
        # same no-content policy as every other telemetry surface.
        with tracer.start_as_current_span(
            f"{request.method} {path.split('?')[0]}",
            kind=None,
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
