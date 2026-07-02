"""Middleware that blocks writes during maintenance."""

from typing import Callable, Optional

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.services.maintenance import MaintenanceService


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
        path = (request.scope.get("path") or "").rstrip("/") or "/"
        if request.method.upper() == "POST" and path == "/api/admin/maintenance":
            return await call_next(request)

        service = self._get_service()
        # If service is not available yet, allow the request (fail open)
        if service is None:
            return await call_next(request)

        flag = service.get_flag()
        if flag.enabled and request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
            return Response(
                content='{"error": "maintenance", "retry_after": 300}',
                status_code=503,
                media_type="application/json",
                headers={"Retry-After": "300"},
            )
        return await call_next(request)
