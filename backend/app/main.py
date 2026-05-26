"""
FastAPI application with lifespan context manager.
"""

import logging

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from slowapi.middleware import SlowAPIMiddleware
from starlette.responses import FileResponse

from app.api.routes.admin import router as admin_router
from app.api.routes.auth import router as auth_router
from app.api.routes.chat import router as chat_router
from app.api.routes.documents import (
    router as documents_router,
)
from app.api.routes.documents import (
    validation_exception_handler,
)
from app.api.routes.email import router as email_router
from app.api.routes.eval import router as eval_router
from app.api.routes.folders import router as folders_router
from app.api.routes.groups import router as groups_router
from app.api.routes.health import router as health_router
from app.api.routes.kms import router as kms_router
from app.api.routes.memories import router as memories_router
from app.api.routes.organizations import router as organizations_router
from app.api.routes.search import router as search_router
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
from app.utils.paths import normalize_root_path

logger = logging.getLogger(__name__)

app = FastAPI(
    title="KnowledgeVault API",
    version="0.1.0",
    description="Self-hosted RAG Knowledge Base API",
    root_path=normalize_root_path(settings.app_root_path),
    lifespan=lifespan,
)

# Security check: warn if admin_secret_token is not set or using default value
if settings.admin_secret_token in ("", "admin-secret-token"):
    logger.critical(
        "SECURITY WARNING: admin_secret_token is not set or is using the default value. "
        "All API routes are effectively unauthenticated."
    )

# Security check: warn if JWT secret key is using the default value
if (
    settings.users_enabled
    and settings.jwt_secret_key == "change-me-to-a-random-64-char-string"
):
    logger.warning(
        "SECURITY WARNING: jwt_secret_key is using the default value. "
        'Generate a secure key with: python -c "import secrets; print(secrets.token_urlsafe(48))"'
    )

# Middleware is applied in reverse order of add_middleware calls:
# last added = outermost (first to see request, last to modify response).
# CORSMiddleware MUST be outermost so CORS headers are always present,
# even on error responses from inner middleware.
app.add_middleware(LoggingMiddleware)
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
    logger.warning(
        "SECURITY WARNING: CORS origins contain wildcard ('*') with allow_credentials=True. "
        "This configuration is insecure and will be rejected by browsers. "
        "Set BACKEND_CORS_ORIGINS to specific origins (e.g., http://localhost:5173)."
    )

app.include_router(health_router, prefix="/api")
app.include_router(chat_router, prefix="/api")
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

# Register exception handler for validation errors (empty filename)
app.add_exception_handler(RequestValidationError, validation_exception_handler)


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

        # Catch-all route for SPA client-side routing
        # Serves index.html for any unmatched frontend routes (not API routes)
        @app.get("/{full_path:path}")
        async def serve_spa(full_path: str):
            # Return 404 for API and assets paths to avoid shadowing (case-insensitive)
            normalized_path = full_path.lower()
            if (
                normalized_path == "api"
                or normalized_path == "assets"
                or normalized_path.startswith("api/")
                or normalized_path.startswith("assets/")
            ):
                raise HTTPException(status_code=404, detail="Not found")
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
