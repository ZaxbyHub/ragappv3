"""Middleware that blocks writes during maintenance."""

import logging
from typing import Callable, Optional

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.security.recovery_routes import is_recovery_exempt
from app.services.maintenance import MaintenanceService

# Module logger name is load-bearing: the issue #549 acceptance check for the
# fail-open path binds on a logger whose name contains "maintenance".
logger = logging.getLogger(__name__)

# POST paths exempt from the maintenance block for OPERATIONAL (non-recovery)
# reasons, matched at their mounted runtime spelling. The admin toggle must
# stay reachable so a window can be ended over HTTP, and token
# refresh/logout must stay reachable so sessions can outlive a window longer
# than the access-token lifetime (issue #549 C01). The RECOVERY exemptions
# (login, change-password) moved to the shared predicate —
# app.security.recovery_routes.is_recovery_exempt (issue #1058) — consumed
# above, so this set no longer carries them and the two gates cannot
# disagree. Registration is deliberately NOT exempt: a maintenance window is
# a data freeze for account creation too.
_OPS_EXEMPT_POST_PATHS = frozenset(
    {
        "/api/admin/maintenance",
        "/api/auth/refresh",
        "/api/auth/logout",
    }
)

# Only these methods can be blocked; GET/HEAD/OPTIONS never consult the flag
# at all, so DB-free routes stay pool-free on the request path (issue #549 C02).
_BLOCKED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class MaintenanceMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: FastAPI,
        service: Optional[MaintenanceService] = None,
        service_getter: Optional[Callable[[], Optional[MaintenanceService]]] = None
    ) -> None:
        super().__init__(app)
        self._service = service
        self._service_getter = service_getter

    def _get_service(self) -> Optional[MaintenanceService]:
        """Get the maintenance service, either directly or via getter."""
        if self._service is not None:
            return self._service
        if self._service_getter is not None:
            return self._service_getter()
        return None

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        method = request.method.upper()
        path = (request.scope.get("path") or "").rstrip("/") or "/"
        # Recovery exemptions route through the SAME shared method-aware
        # canonicalizing predicate the must_change_password gate consumes
        # (app/security/recovery_routes.py, issue #1058): whatever the forced
        # password-change exit needs stays open here by construction — the
        # two gates can no longer disagree. The ops-only POST set below keeps
        # the admin toggle and token lifecycle reachable.
        if is_recovery_exempt(method, path):
            return await call_next(request)
        if method == "POST" and path in _OPS_EXEMPT_POST_PATHS:
            return await call_next(request)
        if method not in _BLOCKED_METHODS:
            return await call_next(request)

        service = self._get_service()
        # If service is not available yet, allow the request (fail open)
        if service is None:
            return await call_next(request)

        try:
            flag = await service.get_flag_async()
        except Exception:
            # Fail open with a WARNING rather than propagating to the client
            # (issue #549 C02): an infrastructure failure of the flag read
            # must not turn every mutating route into a 500.
            logger.warning(
                "maintenance flag read failed; failing open for %s %s",
                method,
                path,
                exc_info=True,
            )
            return await call_next(request)

        if flag.enabled:
            return Response(
                content='{"error": "maintenance", "retry_after": 300}',
                status_code=503,
                media_type="application/json",
                headers={"Retry-After": "300"},
            )
        return await call_next(request)
