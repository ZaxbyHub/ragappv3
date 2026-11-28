"""
FastAPI application with lifespan context manager.
"""

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from slowapi.middleware import SlowAPIMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import FileResponse

from app.api.routes.activity import router as activity_router
from app.api.routes.admin import router as admin_router
from app.api.routes.auth import router as auth_router
from app.api.routes.canvas import router as canvas_router
from app.api.routes.chat import router as chat_router
from app.api.routes.documents import (
    router as documents_router,
)
from app.api.routes.documents import (
    validation_exception_handler,
)
from app.api.routes.draft_room import DraftRoomHTTPError, draft_room_exception_handler
from app.api.routes.draft_room import router as draft_room_router
from app.api.routes.email import router as email_router
from app.api.routes.errors import router as errors_router
from app.api.routes.eval import router as eval_router
from app.api.routes.folders import router as folders_router
from app.api.routes.groups import router as groups_router
from app.api.routes.health import router as health_router
from app.api.routes.kms import router as kms_router
from app.api.routes.memories import router as memories_router
from app.api.routes.onboarding import router as onboarding_router
from app.api.routes.organizations import router as organizations_router
from app.api.routes.prompts import router as prompts_router
from app.api.routes.quality_reports import router as quality_reports_router
from app.api.routes.review_queue import router as review_queue_router
from app.api.routes.search import router as search_router
from app.api.routes.service_accounts import router as service_accounts_router
from app.api.routes.settings import router as settings_router
from app.api.routes.tags import router as tags_router
from app.api.routes.users import router as users_router
from app.api.routes.vault_members import (
    group_access_router as vault_group_access_router,
)
from app.api.routes.vault_members import (
    router as vault_members_router,
)
from app.api.routes.vaults import router as vaults_router
from app.api.routes.wiki import router as wiki_router
from app.config import settings
from app.lifespan import lifespan
from app.limiter import limiter
from app.middleware.logging import LoggingMiddleware
from app.middleware.maintenance import MaintenanceMiddleware
from app.middleware.portal_resilience import (
    ErrorCodeHeaderMiddleware,
    MethodOverrideMiddleware,
)
from app.services.mcp_server import register_mcp_routes
from app.utils.paths import is_unstripped_prefix, normalize_root_path

logger = logging.getLogger(__name__)

app = FastAPI(
    title="KnowledgeVault API",
    version="0.1.0",
    description="Self-hosted RAG Knowledge Base API",
    root_path=normalize_root_path(settings.app_root_path),
    lifespan=lifespan,
)
logger.info(
    "Subpath config: APP_ROOT_PATH=%r  root_path=%r",
    settings.app_root_path,
    app.root_path,
)

# Security check: the well-known bootstrap admin token (#799 AC8, matrix-
# aware). Reachability matrix per Settings.reject_insecure_defaults: in
# single-admin mode (USERS_ENABLED=False) an empty token and the published
# literal both REFUSE TO START, so nothing can be warned about at runtime
# there. Under USERS_ENABLED=True the published literal is deliberately
# permitted as the required bootstrap/first-admin credential (config.py
# decision record, issue #1001 Item A) — that is the one reachable corner,
# and the warning below targets exactly it.
if settings.users_enabled and settings.admin_secret_token == "admin-secret-token":
    logger.warning(
        "SECURITY WARNING: admin_secret_token is still the published "
        "bootstrap credential. It is a public value; rotate it after "
        'first-admin setup with: python -c "import secrets; print(secrets.token_urlsafe(48))"'
    )

# Middleware is applied in reverse order of add_middleware calls:
# last added = outermost (first to see request, last to modify response).
# CORSMiddleware MUST be outermost OF THE ALWAYS-REGISTERED CORE so CORS
# headers are always present on responses that reach the core (two
# conditional layers may register outside it when enabled: the host-header
# validator and the telemetry span middleware — see below).
# LoggingMiddleware must sit OUTSIDE MaintenanceMiddleware and
# SlowAPIMiddleware (#731): both can answer a request without calling
# through (maintenance 503, slowapi's application-level-limit 429), and
# every response the middleware it wraps produces must carry X-Request-ID
# and produce an http_request access-log line. (Responses answered OUTSIDE
# LoggingMiddleware still do not: CORS preflight OPTIONS and invalid-Host
# 400s. Unhandled 500s DO get an http_request access-log line since #1179
# AC4 — LoggingMiddleware's exception arm logs status_code 500 before
# re-raising — but they carry no X-Request-ID response header, since the
# exception path produces no response object here; see docs/admin-guide.md.)
# Always-registered core,
# outermost first: CORS → Logging → SlowAPI → Maintenance.
# ── Portal-resilience layer (innermost of the core) ────────────────────
# Added FIRST so these two sit closest to the app (last added = outermost,
# per the ordering note above): the method override must run before routing,
# and the error-code middleware must see the exception-handler responses.
# They sit INSIDE Maintenance/SlowAPI/Logging, so maintenance 503s and
# rate-limit 429s keep their existing shape and are unaffected.
app.add_middleware(ErrorCodeHeaderMiddleware)
app.add_middleware(MethodOverrideMiddleware)

# Note: MaintenanceMiddleware is initialized with a lazy getter since
# maintenance_service is only available after lifespan startup
app.state._maintenance_service_getter = lambda: getattr(
    app.state, "maintenance_service", None
)
app.add_middleware(
    MaintenanceMiddleware, service_getter=app.state._maintenance_service_getter
)
# Set up rate limiting
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)
# LoggingMiddleware wraps both short-circuiting middleware (see above), so
# the access log and request-id header cover 503/429 responses too (#731).
app.add_middleware(LoggingMiddleware)
# CORSMiddleware outermost — ensures CORS headers on all responses
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.backend_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Security: warn if CORS origins contain wildcard when credentials are enabled
if "*" in settings.backend_cors_origins:
    # (#799 AC8, truthful wording): with allow_credentials=True Starlette
    # REFLECTS the request Origin on wildcard configurations, so any origin
    # can read credential-bearing responses — the exposure is real, but it
    # is reflection, not browser-side rejection (the old claim was false).
    logger.warning(
        "SECURITY WARNING: CORS origins contain wildcard ('*') with "
        "allow_credentials=True. Starlette reflects the request Origin on "
        "this configuration, so any origin can read credential-bearing "
        "responses. Set BACKEND_CORS_ORIGINS to specific origins "
        "(e.g., http://localhost:5173)."
    )

# Host-header validation (defense-in-depth for reverse-proxy deployments).
# Only activated when ALLOWED_HOSTS is configured — disabled by default so
# local dev and root deployments accessed by bare IP keep working.
# Health check endpoints are exempted so Docker/K8s/lb probes (which send
# Host: localhost) continue to work without needing the probe hostname in
# ALLOWED_HOSTS.
_HEALTH_EXEMPT_PATHS = {"/health", "/api/health", "/api/healthz"}

class _TrustedHostWithHealthExempt(TrustedHostMiddleware):
    """TrustedHostMiddleware that exempts health-check paths.

    Pure-ASGI override (#798 AC7 / #1179 AC3): Starlette's
    TrustedHostMiddleware implements only ``__call__`` (it never invokes a
    ``dispatch`` hook), so the previous ``dispatch()`` override was dead
    code — the exemption never executed. The ``scope["path"]`` check below
    mirrors the supported deployment shape (the reverse proxy strips the
    mount prefix, so the backend sees the bare routed path; root_path may
    be non-empty).
    """

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            # Exact-path match only (PR #1198 feedback F-02): a trailing-slash
            # form (/health/, /api/healthz/) is NOT exempt and stays under host
            # validation — under the previous rstrip normalization Starlette's
            # redirect_slashes built a 307 Location from the unvalidated,
            # attacker-chosen Host header on exactly those forms. Health probes
            # hit the canonical paths (docker-compose probes /health with no
            # slash), so no probe flow changes.
            path = scope.get("path") or "/"
            if path in _HEALTH_EXEMPT_PATHS:
                await self.app(scope, receive, send)
                return
        await super().__call__(scope, receive, send)

if settings.allowed_hosts:
    _hosts = list(settings.allowed_hosts)
    app.add_middleware(_TrustedHostWithHealthExempt, allowed_hosts=_hosts)
    logger.info(
        "TrustedHostMiddleware enabled with allowed_hosts=%r (health endpoints exempted)",
        settings.allowed_hosts,
    )

app.include_router(health_router, prefix="/api")

# T3: error-detail lookup for clients behind a body-replacing proxy.
app.include_router(errors_router, prefix="/api")

# E3 telemetry (issue #518): GET /metrics — Prometheus exposition of the
# measured turn/stage/queue/provider metrics (multiprocess-aggregated).
from app.services.telemetry import (  # noqa: E402
    register_metrics_route,
    register_span_middleware,
)

register_metrics_route(app)
# E3 closure (issue #518 amendment): FastAPI server spans — registers the
# span middleware only when the optional OTel extra + telemetry are live.
register_span_middleware(app)
app.include_router(activity_router, prefix="/api")
app.include_router(draft_room_router, prefix="/api")
app.include_router(chat_router, prefix="/api")
app.include_router(canvas_router, prefix="/api")
app.include_router(search_router, prefix="/api")
app.include_router(memories_router, prefix="/api")
app.include_router(documents_router, prefix="/api")
app.include_router(settings_router, prefix="/api")
app.include_router(vaults_router, prefix="/api")
app.include_router(admin_router, prefix="/api")
app.include_router(email_router, prefix="/api")
app.include_router(eval_router, prefix="/api")
app.include_router(auth_router, prefix="/api")
app.include_router(users_router, prefix="/api")
app.include_router(vault_members_router, prefix="/api")
app.include_router(vault_group_access_router, prefix="/api")
app.include_router(organizations_router, prefix="/api")
app.include_router(groups_router, prefix="/api")
app.include_router(wiki_router, prefix="/api")
app.include_router(kms_router, prefix="/api")
app.include_router(tags_router, prefix="/api")
app.include_router(folders_router, prefix="/api")
app.include_router(service_accounts_router, prefix="/api")
register_mcp_routes(app)
app.include_router(prompts_router, prefix="/api")
app.include_router(quality_reports_router, prefix="/api")
app.include_router(review_queue_router, prefix="/api")
app.include_router(onboarding_router, prefix="/api")

# Register exception handler for validation errors (empty filename)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
# Register Draft Room's {detail, code, context} error contract (SPEC section 8.3).
app.add_exception_handler(DraftRoomHTTPError, draft_room_exception_handler)


@app.exception_handler(HTTPException)
async def _logging_http_exception_handler(request: Request, exc: HTTPException):
    """Log every 403 with its denial reason, then defer to the default.

    A 403 access log line carries only the status; the *reason* (authz
    denial vs CSRF failure vs an API-key rejection) lived nowhere. This
    wrapper logs category + detail + method + path + request id before
    delegating to FastAPI's default handler, whose response (including the
    ``x-csrf-error`` header the frontend retry keys on) is unchanged.
    """
    if exc.status_code == 403:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        exc_headers = exc.headers or {}
        is_csrf = (
            exc_headers.get("x-csrf-error") == "true"
            or "csrf" in detail.lower()
        )
        logger.warning(
            "403 denial: category=%s detail=%r method=%s path=%s request_id=%s",
            "csrf" if is_csrf else "authz",
            detail,
            request.method,
            request.url.path,
            request.headers.get("x-request-id"),
        )
    return await http_exception_handler(request, exc)


@app.get("/health")
async def health_check():
    """Simple health check endpoint for Docker/tooling."""
    return {"status": "ok"}


# Serve frontend static files
from pathlib import Path  # noqa: E402

static_dir = Path("/app/static")
logger.info(
    f"Checking for static files at: {static_dir} (exists: {static_dir.exists()})"
)
if static_dir.exists():
    try:
        # Mount assets only (without html=True to avoid catch-all behavior)
        app.mount(
            "/assets",
            StaticFiles(directory=str(static_dir / "assets"), html=False),
            name="assets",
        )
        logger.info(f"Static files mounted successfully from {static_dir}")

        # ── SPA CATCH-ALL ──────────────────────────────────────
        # Everything below this line is caught by the SPA fallback.
        # All API routers MUST be registered ABOVE this point.
        # ───────────────────────────────────────────────────────
        @app.get("/{full_path:path}")
        async def serve_spa(full_path: str):
            normalized_path = full_path.lower()
            if (
                normalized_path == "api"
                or normalized_path == "assets"
                or normalized_path.startswith("api/")
                or normalized_path.startswith("assets/")
            ):
                raise HTTPException(status_code=404, detail="Not found")
            if is_unstripped_prefix(full_path.lower(), settings.app_root_path.lower()):
                raise HTTPException(
                    status_code=404,
                    detail=(
                        "Proxy misconfiguration: request path starts with "
                        "APP_ROOT_PATH. Your reverse proxy must strip the "
                        "prefix before forwarding. See deployment docs."
                    ),
                )
            return FileResponse(static_dir / "index.html")

    except Exception as e:
        logger.error(f"Failed to mount static files: {e}")
else:
    logger.warning(
        f"Static directory {static_dir} does not exist - frontend will not be served"
    )
    # List what's in /app to help debug
    try:
        app_contents = list(Path("/app").iterdir()) if Path("/app").exists() else []
        logger.info(f"Contents of /app: {[p.name for p in app_contents]}")
    except Exception as e:
        logger.error(f"Could not list /app contents: {e}")
